"""Reflection pipeline (ACE Generator/Reflector/Curator, adapted to a gateway).

For each batch of unreflected episodes:
1. Establish an outcome signal: verification > explicit feedback > reported outcome > LLM self-judge.
2. Reflect per episode (in parallel, run on successes too): diagnosis, helpful/harmful tags on the
   injected bullets, memory candidates.
3. Apply bullet tags as counters (telemetry, ungoverned).
4. Reconcile memory candidates.
5. Curate: one call turns the *evidence-bearing* reflections into bounded delta ops -> changeset.
   Weak-signal episodes never change skills; ACE and Dynamic Cheatsheet both fall below baseline when
   learning from unreliable feedback.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from sqlalchemy import select, update

from . import prompts
from .core import Core, clip
from .db import BulletRow, ChangesetRow, EpisodeRow
from .governance import Governance
from .llm import LLMError
from .memory import MemoryService
from .retrieval import Candidate

log = structlog.get_logger()

REWARD = {"success": 1.0, "partial": 0.5, "failure": 0.0}


def render_episode(ep: EpisodeRow, budget: int = 20000) -> str:
    parts = [f"Task: {ep.task or '(not given)'}", f"Reported outcome: {ep.outcome}"]
    if ep.judged_outcome:
        parts.append(f"Judged outcome: {ep.judged_outcome} (confidence {ep.judge_confidence:.2f})")
    v = ep.meta.get("verification")
    if v:
        parts.append(f"Verification: {'PASSED' if v.get('passed') else 'FAILED'} ({v.get('source')}) "
                     f"{clip(v.get('details'), 2000)}")
    if ep.error:
        parts.append(f"Error: {clip(ep.error, 2000)}")
    if ep.feedback_score is not None:
        parts.append(f"User feedback: {ep.feedback_score:.2f} {ep.meta.get('feedback_comment') or ''}")
    if ep.meta.get("correction"):
        parts.append(f"User correction: {clip(ep.meta['correction'], 2000)}")
    for m in ep.messages:
        parts.append(f"[{m['role']}] {clip(m['content'], 3000)}")
    for t in ep.tool_calls:
        line = f"[tool:{t['name']}] args={clip(str(t.get('arguments')), 600)}"
        line += f" ERROR={clip(t['error'], 800)}" if t.get("error") else f" result={clip(t.get('result'), 800)}"
        parts.append(line)
    return clip("\n".join(parts), budget)


def signal(ep: EpisodeRow, min_conf: float) -> tuple[float | None, str]:
    """(reward, strength). strength: strong | judged | weak."""
    v = ep.meta.get("verification")
    if v is not None:
        return (1.0 if v.get("passed") else 0.0), "strong"
    if ep.feedback_score is not None:
        return ep.feedback_score, "strong"
    if ep.outcome in REWARD:
        return REWARD[ep.outcome], "strong"
    if ep.judged_outcome in REWARD and (ep.judge_confidence or 0) >= min_conf:
        return REWARD[ep.judged_outcome], "judged"
    return None, "weak"


class Learner:
    def __init__(self, core: Core, gov: Governance, mem: MemoryService, credit) -> None:  # type: ignore[no-untyped-def]
        self.c, self.gov, self.mem = core, gov, mem
        self.credit = credit  # engine callback: apply a late reward (from the judge) to skills/experiments

    async def judge_outcome(self, tenant: str, ep: EpisodeRow) -> None:
        try:
            out = await self.c.llm_json(tenant, "judge_outcome", prompts.OUTCOME_SYSTEM, render_episode(ep),
                                        prompts.OUTCOME_SCHEMA)
        except LLMError as e:
            log.warning("judge_outcome.failed", error=str(e))
            return
        ep.judged_outcome = out.get("outcome")
        ep.judge_confidence = max(0.0, min(1.0, float(out.get("confidence", 0))))

    async def reflect_one(self, tenant: str, ep: EpisodeRow, injected: dict[str, str]) -> dict[str, Any] | None:
        rules = "\n".join(f"[{bid}] {txt}" for bid, txt in injected.items()) or "(none)"
        cited = ", ".join(ep.cited_ids) or "(none)"
        prompt = (f"<rules_in_context>\n{rules}\n</rules_in_context>\n<rules_cited_by_agent>{cited}"
                  f"</rules_cited_by_agent>\n\n<run>\n{render_episode(ep)}\n</run>")
        try:
            return await self.c.llm_json(tenant, "reflect", prompts.REFLECT_SYSTEM, prompt, prompts.REFLECT_SCHEMA)
        except LLMError as e:
            log.warning("reflect.failed", episode=ep.id, error=str(e))
            return None

    async def run(self, tenant: str, payload: dict[str, Any]) -> ChangesetRow | None:
        agent = payload["agent_id"]
        async with self.c.db.session() as ses:
            eps = list((await ses.scalars(
                select(EpisodeRow).where(EpisodeRow.tenant_id == tenant, EpisodeRow.agent_id == agent,
                                         EpisodeRow.reflected.is_(False))
                .order_by(EpisodeRow.created_at).limit(20))).all())
            if not eps:
                return None
            bids = {b for e in eps for b in e.bullet_ids}
            texts = {b.id: b.content for b in (await ses.scalars(select(BulletRow).where(BulletRow.id.in_(bids)))).all()} if bids else {}

        # 1. outcome signal (self-judge only where nothing better exists)
        if self.c.s.self_judge_unknown_outcomes:
            todo = [e for e in eps if signal(e, 1.1)[1] == "weak" and e.judged_outcome is None]
            await asyncio.gather(*(self.judge_outcome(tenant, e) for e in todo))
            for e in todo:
                if e.judged_outcome is not None:  # credit() re-checks precedence on the locked row
                    await self.credit(tenant, e.id, source="judge", judged=(e.judged_outcome, e.judge_confidence))

        # 2. reflect (bounded parallelism)
        sem = asyncio.Semaphore(4)

        async def one(e: EpisodeRow) -> dict[str, Any] | None:
            async with sem:
                return await self.reflect_one(tenant, e, {b: texts[b] for b in e.bullet_ids if b in texts})

        refl = await asyncio.gather(*(one(e) for e in eps))

        # 3. tags -> counters (only for bullets that were actually injected into that run)
        async with self.c.db.session() as ses:
            for e, r in zip(eps, refl, strict=True):
                e2 = await ses.get(EpisodeRow, e.id)
                if e2 is None:
                    continue
                if r is None:  # LLM failure: retry on a later run, but don't loop forever
                    tries = int(e2.meta.get("reflect_attempts", 0)) + 1
                    e2.meta = {**e2.meta, "reflect_attempts": tries}
                    e2.reflected = tries >= 3
                    continue
                if e2.feedback_score != e.feedback_score:
                    continue  # feedback arrived mid-run: leave it unreflected so the new evidence is used
                e2.reflected = True
                e2.reflection = r
                for tag in (r or {}).get("bullet_tags", []):
                    if tag.get("id") not in e.bullet_ids:
                        continue
                    col = {"helpful": BulletRow.helpful, "harmful": BulletRow.harmful,
                           "neutral": BulletRow.neutral}.get(tag.get("tag"))
                    if col is not None:
                        await ses.execute(update(BulletRow).where(BulletRow.id == tag["id"])
                                          .values({col.key: col + 1}))
            await ses.commit()

        # 4. memories (per user scope)
        by_user: dict[str | None, list[dict[str, Any]]] = {}
        srcs: dict[str | None, list[str]] = {}
        for e, r in zip(eps, refl, strict=True):
            for m in (r or {}).get("memory_candidates", []):
                # Privacy: what was learned in one user's run stays with that user unless sharing is enabled.
                owner = None if (self.c.s.share_learned_facts and m.get("kind") != "preference") else e.user_id
                by_user.setdefault(e.user_id, []).append({**m, "user_id": owner})
                srcs.setdefault(e.user_id, []).append(e.id)
        for uid, cands in by_user.items():
            await self.mem.reconcile(tenant, agent, uid, cands, srcs[uid], "system:reflection")

        # 5. curate from evidence-bearing reflections only
        strong = [(e, r) for e, r in zip(eps, refl, strict=True)
                  if r and r.get("category") != "NO_LEARNING"
                  and signal(e, self.c.s.min_judge_confidence)[1] != "weak"]
        if not strong:
            log.info("reflection.no_curation", agent=agent, episodes=len(eps))
            return None
        return await self.curate(tenant, agent, strong)

    async def curate(self, tenant: str, agent: str, items: list[tuple[EpisodeRow, dict[str, Any]]],
                     source: str = "reflection") -> ChangesetRow | None:
        query = " ".join(f"{e.task or ''} {r.get('key_insight', '')}" for e, r in items)
        async with self.c.db.session() as ses:
            skills, bullets = await self.gov.library(ses, tenant, agent)
            live = [s for s in skills.values() if s.status in ("active", "stale")]
            qe = await self.c.embed([query])
            hits = self.c.retriever.search(query, [Candidate(s.name, f"{s.name.replace('-', ' ')} {s.description}",
                                                             s.embedding) for s in live],
                                           qe[0] if qe else None, k=5, min_relevance=0.05)
            rejected = (await ses.scalars(select(ChangesetRow).where(
                ChangesetRow.tenant_id == tenant, ChangesetRow.agent_id == agent, ChangesetRow.status == "rejected")
                .order_by(ChangesetRow.created_at.desc()).limit(8))).all()
        relevant = [h.key for h in hits]
        lib = []
        for name in relevant:
            s = skills[name]
            bl = [f"  [{bid}] ({b['section']}; helpful={b['helpful']} harmful={b['harmful']} used={b['used']}) "
                  f"{b['content']}" for bid, b in bullets.items() if b["skill"] == name]
            lib.append(f"<skill name=\"{name}\" uses=\"{s.uses}\">\n  description: {s.description}\n" + "\n".join(bl)
                       + "\n</skill>")
        others = [f"- {s.name}: {s.description}" for s in live if s.name not in relevant][:30]
        refl = [{"episode": e.id, "task": clip(e.task, 400), "outcome": e.judged_outcome or e.outcome,
                 **{k: r.get(k) for k in ("category", "error_identification", "root_cause", "correct_approach",
                                          "key_insight")}} for e, r in items]
        rej = [f"- {json.dumps(c.ops)[:400]} -> {c.rejection_reason}" for c in rejected]
        prompt = (f"<reflections>\n{json.dumps(refl, indent=1)}\n</reflections>\n\n"
                  f"<relevant_skills>\n{chr(10).join(lib) or '(none)'}\n</relevant_skills>\n\n"
                  f"<other_skills>\n{chr(10).join(others) or '(none)'}\n</other_skills>\n\n"
                  f"<previously_rejected_edits>\n{chr(10).join(rej) or '(none)'}\n</previously_rejected_edits>\n\n"
                  f"Return at most {self.c.s.max_ops_per_changeset} operations (create_skill not counted).")
        out = await self.c.llm_json(tenant, "curate", prompts.CURATE_SYSTEM, prompt, prompts.CURATE_SCHEMA)
        return await self.gov.propose(tenant, f"system:{source}", agent, out.get("operations", []),
                                      out.get("reasoning", ""), source, [e.id for e, _ in items],
                                      read_versions={s.id: s.version for s in live})
