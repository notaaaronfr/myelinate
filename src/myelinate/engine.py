"""Learning engine facade: the hot path (recall / record / feedback) plus read models.

Hot-path calls never invoke an LLM. Reflection, curation, evolution and maintenance run as durable
background jobs, so the host agent's latency is a couple of DB round-trips.
"""

from __future__ import annotations

import json
import random
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from . import experiments as xp
from . import prompts, safety
from .config import Settings
from .core import Conflict, Core, NotFound, clip
from .db import (
    AgentStateRow,
    BulletRow,
    ChangesetRow,
    Database,
    EpisodeRow,
    ExperimentRow,
    MemoryRow,
    ProfileRow,
    RecallRow,
    SkillRow,
    UsageRow,
    new_id,
    utcnow,
)
from .embeddings import Embedder
from .evolution import Evolver
from .governance import Governance, bullet_dict
from .learning import REWARD, Learner, signal
from .llm import LLM
from .maintenance import Maintainer
from .memory import MemoryService
from .models import (
    AskIn,
    AskOut,
    BulletRef,
    EpisodeIn,
    EpisodeOut,
    FeedbackIn,
    MemoryIn,
    MemoryRef,
    Outcome,
    RecallRequest,
    RecallResponse,
    SkillImport,
    SkillIn,
    SkillRef,
)
from .retrieval import Candidate
from .skills import CITE, ordered, parse_skill_md, render_bullets, slug, to_skill_md
from .telemetry import metrics

log = structlog.get_logger()


class LearningEngine:
    def __init__(self, db: Database, settings: Settings, llm: LLM, embedder: Embedder | None = None) -> None:
        self.c = Core(db, settings, llm, embedder)
        self.db, self.s = db, settings
        self.gov = Governance(self.c)
        self.mem = MemoryService(self.c)
        self.learner = Learner(self.c, self.gov, self.mem, self.credit)
        self.evolver = Evolver(self.c, self.gov)
        self.maint = Maintainer(self.c, self.gov, self.mem)
        self._rng = random.Random()

    async def run_job(self, tenant: str, kind: str, payload: dict[str, Any]) -> Any:
        match kind:
            case "reflect":
                return await self.learner.run(tenant, payload)
            case "evolve":
                return await self.evolver.run(tenant, payload)
            case "curate":
                return await self.maint.run(tenant, payload)
            case "profile":
                return await self.mem.refresh_profile(tenant, payload["agent_id"], payload["user_id"])
        raise ValueError(f"unknown job kind {kind}")

    # ================================================================ recall

    async def recall(self, tenant: str, req: RecallRequest, allow_holdout: bool = True) -> RecallResponse:
        s = self.s
        agent, user = req.scope.agent_id, req.scope.user_id
        max_skills = s.recall_max_skills if req.max_skills is None else req.max_skills
        max_mem = s.recall_max_memories if req.max_memories is None else req.max_memories
        qe = await self.c.embed([req.query])
        qv = qe[0] if qe else None
        sticky = req.session_id or user
        async with self.db.session() as ses:
            prev = None
            if req.session_id:
                prev = await ses.scalar(select(RecallRow).where(RecallRow.tenant_id == tenant,
                                                                RecallRow.session_id == req.session_id)
                                        .order_by(RecallRow.created_at.desc()).limit(1))
            holdout = allow_holdout and (prev.holdout if prev else (s.holdout_rate > 0 and self._rng.random() < s.holdout_rate))

            # --- skills
            skills, lib_bullets = await self.gov.library(ses, tenant, agent)
            live = [k for k in skills.values() if k.status in ("active", "stale")]
            rule_text: dict[str, list[str]] = {}
            for b in lib_bullets.values():  # rules' issues/keywords widen the vocabulary a skill is found by
                rule_text.setdefault(b["skill"], []).append(f"{b.get('issue', '')} {' '.join(b.get('keywords') or [])} "
                                                            f"{b['content'][:200]}")
            cands = [Candidate(k.id, f"{k.name.replace('-', ' ')} {k.name.replace('-', ' ')} {k.description} "
                                     f"{k.description} {' '.join(k.tags or [])} {' '.join(rule_text.get(k.name, []))}",
                               k.embedding,
                               prior=(0.6 + 0.8 * k.alpha / (k.alpha + k.beta)) * (0.8 if k.status == "stale" else 1))
                     for k in live]
            hits = self.c.retriever.search(req.query, cands, qv, max_skills + s.skill_index_size, s.recall_min_score)
            by_id = {k.id: k for k in live}
            loaded_hits = [] if holdout else hits[:max_skills]
            index_hits = [] if holdout else hits[max_skills:]
            exps = await self.gov.running_experiments(ses, [h.key for h in loaded_hits])
            prev_arms = {x["skill_id"]: x.get("arm") for x in (prev.served if prev else [])}

            skill_refs: list[SkillRef] = []
            # Holdout runs record what *would* have been served, so lift compares like with like.
            served: list[dict[str, Any]] = [{"skill_id": h.key, "version": by_id[h.key].version, "arm": "holdout",
                                             "exp": None} for h in hits[:max_skills]] if holdout else []
            injected: list[str] = []
            for h in sorted(loaded_hits, key=lambda h: by_id[h.key].name):  # stable order -> cache-friendly
                k = by_id[h.key]
                exp = exps.get(k.id)
                arm = None
                if exp is not None:
                    arm = prev_arms.get(k.id) or self.gov.choose_arm(exp, sticky)
                if arm == "candidate":
                    bullets, desc = exp.candidate_bullets, exp.candidate_description  # type: ignore[union-attr]
                else:
                    rows = (await ses.scalars(select(BulletRow).where(BulletRow.skill_id == k.id,
                                                                      BulletRow.status == "active"))).all()
                    rows = sorted(rows, key=lambda b: (b.harmful - b.helpful, b.position))[: s.max_bullets_per_skill_injected]
                    bullets, desc = [bullet_dict(b) for b in rows], k.description
                bullets = ordered(bullets)
                injected += [b["id"] for b in bullets]
                served.append({"skill_id": k.id, "version": k.version, "arm": arm, "exp": exp.id if exp else None})
                skill_refs.append(SkillRef(id=k.id, name=k.name, description=desc, version=k.version, score=h.score,
                                           arm=arm, bullets=[BulletRef(id=b["id"], section=b["section"],
                                                                       content=b["content"]) for b in bullets]))
            index = [SkillRef(id=by_id[h.key].id, name=by_id[h.key].name, description=by_id[h.key].description,
                              version=by_id[h.key].version, score=h.score) for h in index_hits]

            # --- profile + memories
            profile = None
            in_card: set[str] = set()
            if user and req.include_profile:
                p = await ses.get(ProfileRow, (tenant, agent, user))
                if p and p.card:
                    profile, in_card = p.card, set(p.source_memory_ids)
            mems = [m for m in await self.mem.visible(ses, tenant, agent, user, req.as_of) if m.id not in in_card]
            # The current user's preferences always apply (USER.md semantics) until folded into the profile card.
            prefs = [m for m in mems if user and m.user_id == user and m.kind == "preference"][: max(1, max_mem // 2)]
            rest = [m for m in mems if m not in prefs]
            mhits = self.c.retriever.search(req.query, self.mem.candidates(rest), qv, max_mem - len(prefs),
                                            s.recall_min_score)
            mrow = {m.id: m for m in mems}
            mem_refs = [MemoryRef(id=m.id, kind=m.kind, content=m.content, score=1.0, valid_from=m.valid_from)
                        for m in prefs]
            mem_refs += [MemoryRef(id=h.key, kind=mrow[h.key].kind, content=mrow[h.key].content, score=h.score,
                                   valid_from=mrow[h.key].valid_from) for h in mhits]

            rec = RecallRow(id=new_id("rec"), tenant_id=tenant, agent_id=agent, session_id=req.session_id,
                            user_id=user, query=clip(safety.redact(req.query) if s.redact_pii else req.query, 4000),
                            served=served, bullet_ids=injected,
                            memory_ids=[m.id for m in mem_refs], holdout=holdout)
            ses.add(rec)
            if mem_refs:
                await ses.execute(update(MemoryRow).where(MemoryRow.id.in_([m.id for m in mem_refs]))
                                  .values(uses=MemoryRow.uses + 1))
            await ses.commit()
        metrics.recalls.inc()
        metrics.recall_skills.observe(len(skill_refs))
        return RecallResponse(recall_id=rec.id, instructions=self._compose(skill_refs, index, mem_refs, profile),
                              skills=skill_refs, skill_index=index, memories=mem_refs, profile=profile,
                              holdout=holdout)

    def _compose(self, skills: list[SkillRef], index: list[SkillRef], mems: list[MemoryRef],
                 profile: str | None) -> str:
        out: list[str] = []
        if profile:
            out.append(f"## About the user\n{profile}")
        if skills:
            cite = (" When one of these rules shapes what you do, mention its id in brackets, e.g. [b-1a2b3c4d]."
                    if self.s.ask_for_citations else "")
            out.append("## Learned skills\nRules learned from previous runs. Follow them when they apply; if one is "
                       "wrong for this situation, don't follow it." + cite)
            for k in skills:
                bl = [b.model_dump() for b in k.bullets or []]
                out.append(f"### {k.name}\n{k.description}\n\n"
                           f"{render_bullets(bl, with_ids=self.s.ask_for_citations, presorted=True)}")
        if index:
            out.append("## Other learned skills\nLoad one with the `skill_view` tool if it applies:\n"
                       + "\n".join(f"- {k.name}: {k.description}" for k in index))
        if mems:
            out.append("## Memory\n" + "\n".join(f"- [{m.kind}] {m.content}" for m in mems))
        return "\n\n".join(out)

    # ================================================================ record & credit

    async def record(self, tenant: str, ep: EpisodeIn) -> EpisodeOut:
        s = self.s
        red = safety.redact if s.redact_pii else (lambda x: x)
        msgs = [{"role": m.role, "content": red(m.content)} for m in ep.messages]
        tools = [{"name": t.name, "arguments": red(str(t.arguments)) if t.arguments is not None else None,
                  "result": red(t.result) if t.result else None, "error": red(t.error) if t.error else None}
                 for t in ep.tool_calls]
        agent = ep.scope.agent_id
        task = ep.task or next((m["content"] for m in msgs if m["role"] == "user"), None)
        meta = dict(ep.metadata)
        if ep.verification:
            meta["verification"] = {"passed": ep.verification.passed, "source": ep.verification.source,
                                    "details": red(ep.verification.details or "")}
        if ep.idempotency_key and (dup := await self._by_key(tenant, ep.idempotency_key)):
            return EpisodeOut(episode_id=dup, reflection_scheduled=False)  # client retry: already recorded
        async with self.db.session() as ses:
            served: list[dict[str, Any]] = []
            injected: list[str] = []
            holdout = False
            if ep.recall_id:
                rec = await ses.get(RecallRow, ep.recall_id)
                if rec and rec.tenant_id == tenant:
                    served, injected, holdout = list(rec.served), list(rec.bullet_ids), rec.holdout
            if ep.skills_loaded and not holdout:  # skills the agent pulled itself (skill_view / OTel skill spans)
                skills, _ = await self.gov.library(ses, tenant, agent)
                have = {x["skill_id"] for x in served}
                served += [{"skill_id": skills[n].id, "version": skills[n].version, "arm": None, "exp": None,
                            "loaded": True} for n in dict.fromkeys(slug(x) for x in ep.skills_loaded)
                           if n in skills and skills[n].id not in have]
            said = " ".join(m["content"] for m in msgs if m["role"] == "assistant")
            cited = [b for b in dict.fromkeys(CITE.findall(said)) if b in injected]
            row = EpisodeRow(
                id=new_id("ep"), tenant_id=tenant, agent_id=agent, user_id=ep.scope.user_id,
                idempotency_key=ep.idempotency_key, session_id=ep.session_id, recall_id=ep.recall_id,
                task=red(task) if task else None, messages=msgs, tool_calls=tools, outcome=ep.outcome.value,
                error=red(ep.error) if ep.error else None, duration_ms=ep.duration_ms, framework=ep.framework,
                meta=meta, served=served, bullet_ids=injected, cited_ids=cited, holdout=holdout)
            ses.add(row)
            if injected:
                await ses.execute(update(BulletRow).where(BulletRow.id.in_(injected)).values(used=BulletRow.used + 1))
            used = [x["skill_id"] for x in served if x.get("arm") != "holdout"]
            if used:  # usage counts when served; success is credited separately when a reward exists
                await ses.execute(update(SkillRow).where(SkillRow.id.in_(used)).values(
                    uses=SkillRow.uses + 1, last_used_at=utcnow()))
                await ses.execute(update(SkillRow).where(SkillRow.id.in_(used), SkillRow.status == "stale")
                                  .values(status="active"))

            await self.c.ensure_agent_state(ses, tenant, agent)
            await ses.execute(update(AgentStateRow).where(AgentStateRow.tenant_id == tenant,
                                                          AgentStateRow.agent_id == agent).values(
                episodes_since_reflect=AgentStateRow.episodes_since_reflect + 1,
                episodes_since_curate=AgentStateRow.episodes_since_curate + 1))
            st = await ses.scalar(select(AgentStateRow).where(AgentStateRow.tenant_id == tenant,
                                                              AgentStateRow.agent_id == agent)
                                  .with_for_update().execution_options(populate_existing=True))
            assert st is not None
            failed = ep.outcome == Outcome.failure or (ep.verification is not None and not ep.verification.passed)
            should = ((s.reflect_on_failure and failed) or len(ep.tool_calls) >= s.reflect_min_tool_calls
                      or st.episodes_since_reflect >= s.reflect_every_n_episodes)
            if should:
                st.episodes_since_reflect = 0
                await self.c.enqueue(ses, tenant, "reflect", {"agent_id": agent},
                                     dedupe_key=f"reflect:{tenant}:{agent}", delay_s=s.reflect_debounce_seconds)
            if st.episodes_since_curate >= s.curate_every_n_episodes:
                st.episodes_since_curate = 0
                await self.c.enqueue(ses, tenant, "curate", {"agent_id": agent}, dedupe_key=f"curate:{tenant}:{agent}")
            try:
                await ses.commit()
            except IntegrityError:
                await ses.rollback()
                if ep.idempotency_key and (dup := await self._by_key(tenant, ep.idempotency_key)):
                    return EpisodeOut(episode_id=dup, reflection_scheduled=False)  # concurrent retry won
                raise

        await self.credit(tenant, row.id, source="record")
        metrics.episodes.labels(outcome=ep.outcome.value, framework=ep.framework or "unknown").inc()
        return EpisodeOut(episode_id=row.id, reflection_scheduled=should)

    async def merge_episode(self, tenant: str, episode_id: str, ep: EpisodeIn) -> str:
        """Fold a later partial view of the same run (e.g. a trace exported in pieces) into its episode."""
        red = safety.redact if self.s.redact_pii else (lambda x: x)
        async with self.db.session() as ses:
            row = await ses.scalar(select(EpisodeRow).where(EpisodeRow.id == episode_id).with_for_update())
            if row is None or row.tenant_id != tenant:
                raise NotFound(episode_id)
            seen = {json.dumps(t, sort_keys=True) for t in row.tool_calls}
            for t in ep.tool_calls:
                d = {"name": t.name, "arguments": red(str(t.arguments)) if t.arguments is not None else None,
                     "result": red(t.result) if t.result else None, "error": red(t.error) if t.error else None}
                if json.dumps(d, sort_keys=True) not in seen:
                    row.tool_calls = [*row.tool_calls, d]
            if not row.messages and ep.messages:
                row.messages = [{"role": m.role, "content": red(m.content)} for m in ep.messages]
                row.task = row.task or (red(ep.task) if ep.task else None)
            if row.outcome == Outcome.unknown.value and ep.outcome != Outcome.unknown:
                row.outcome = ep.outcome.value
            if ep.verification and "verification" not in row.meta:
                row.meta = {**row.meta, "verification": {"passed": ep.verification.passed,
                                                         "source": ep.verification.source,
                                                         "details": red(ep.verification.details or "")}}
            if ep.error and not row.error:
                row.error = red(ep.error)
            if ep.skills_loaded and not row.holdout:
                skills, _ = await self.gov.library(ses, tenant, row.agent_id)
                have = {x["skill_id"] for x in row.served}
                new = [skills[n] for n in dict.fromkeys(slug(x) for x in ep.skills_loaded)
                       if n in skills and skills[n].id not in have]
                if new:
                    row.served = [*row.served, *({"skill_id": k.id, "version": k.version, "arm": None, "exp": None,
                                                  "loaded": True} for k in new)]
                    await ses.execute(update(SkillRow).where(SkillRow.id.in_([k.id for k in new]))
                                      .values(uses=SkillRow.uses + 1, last_used_at=utcnow()))
                    prior = row.meta.get("credited")
                    if prior is not None:  # bring late-added skills up to the reward already credited to the rest
                        await ses.execute(update(SkillRow).where(SkillRow.id.in_([k.id for k in new])).values(
                            alpha=SkillRow.alpha + prior, beta=SkillRow.beta + (1 - prior),
                            successes=SkillRow.successes + prior))
            await ses.commit()
        await self.credit(tenant, episode_id, source="merge")  # re-credit with the (possibly) stronger signal
        return episode_id

    async def _by_key(self, tenant: str, key: str) -> str | None:
        async with self.db.session() as ses:
            return await ses.scalar(select(EpisodeRow.id).where(EpisodeRow.tenant_id == tenant,
                                                                EpisodeRow.idempotency_key == key))

    async def credit(self, tenant: str, episode_id: str, source: str,
                     judged: tuple[str | None, float | None] | None = None) -> None:
        """(Re)assign an episode's reward to everything that was served for it.

        The reward is recomputed from the locked row with the signal hierarchy (verification > feedback >
        reported outcome > judge), so a late judge can never override feedback. Updates are Beta pseudo-count
        deltas (new reward minus previously credited) applied as atomic SQL increments: concurrent episodes
        never lose updates, and re-crediting never double counts."""
        async with self.db.session() as ses:
            ep = await ses.scalar(select(EpisodeRow).where(EpisodeRow.id == episode_id).with_for_update())
            if ep is None or ep.tenant_id != tenant:
                return
            if judged and ep.judged_outcome is None:
                ep.judged_outcome, ep.judge_confidence = judged
            reward, _ = signal(ep, self.s.min_judge_confidence)
            old = ep.meta.get("credited")
            if reward is None or reward == old:
                await ses.commit()
                return
            da = reward - (old or 0.0)
            db = (1 - reward) - ((1 - old) if old is not None else 0.0)
            ep.meta = {**ep.meta, "credited": reward, "credit_source": source}
            live = [x for x in ep.served if x.get("arm") != "holdout"]
            for x in live:
                await ses.execute(update(SkillRow).where(SkillRow.id == x["skill_id"]).values(
                    alpha=SkillRow.alpha + da, beta=SkillRow.beta + db, successes=SkillRow.successes + da))
                if x.get("exp"):
                    await self.gov.credit_experiment(ses, x["exp"], x.get("arm") or "incumbent", da, db)
            if old is None:  # first reward: consider evolution for poorly performing skills
                for sk in (await ses.scalars(select(SkillRow).where(SkillRow.id.in_([x["skill_id"] for x in live]))
                                             .execution_options(populate_existing=True))).all() if live else []:
                    if (sk.uses - sk.last_evolved_uses >= self.s.evolve_min_uses
                            and sk.alpha / (sk.alpha + sk.beta) < self.s.evolve_max_success_rate):
                        await self.c.enqueue(ses, tenant, "evolve", {"skill_id": sk.id, "agent_id": ep.agent_id},
                                             dedupe_key=f"evolve:{sk.id}")
            # Lift compares like with like: only runs where some skill applied (holdout: would have applied).
            if ep.recall_id and ep.served:
                cols = ((AgentStateRow.holdout_alpha, AgentStateRow.holdout_beta) if ep.holdout
                        else (AgentStateRow.treated_alpha, AgentStateRow.treated_beta))
                await ses.execute(update(AgentStateRow).where(AgentStateRow.tenant_id == tenant,
                                                              AgentStateRow.agent_id == ep.agent_id)
                                  .values({cols[0].key: cols[0] + da, cols[1].key: cols[1] + db}))
            await ses.commit()

    async def feedback(self, tenant: str, actor: str, fb: FeedbackIn) -> None:
        red = safety.redact if self.s.redact_pii else (lambda x: x)
        async with self.db.session() as ses:
            ep = await ses.get(EpisodeRow, fb.episode_id)
            if ep is None or ep.tenant_id != tenant:
                raise NotFound(fb.episode_id)
            ep.feedback_score = fb.score
            ep.meta = {**ep.meta, "feedback_comment": red(fb.comment) if fb.comment else None,
                       "feedback_source": fb.source, "correction": red(fb.correction) if fb.correction else None}
            if fb.score < 0.5 or fb.comment or fb.correction:
                ep.reflected = False  # learn from it again with the new evidence
                ep.meta = {**ep.meta, "reflect_attempts": 0}
                await self.c.enqueue(ses, tenant, "reflect", {"agent_id": ep.agent_id},
                                     dedupe_key=f"reflect:{tenant}:{ep.agent_id}", delay_s=self.s.reflect_debounce_seconds)
            self.c.audit(ses, tenant, actor, "feedback", ep.id, score=fb.score)
            await ses.commit()
        await self.credit(tenant, fb.episode_id, source="feedback")

    # ================================================================ skills (authoring / import / read)

    async def author_skill(self, tenant: str, actor: str, s: SkillIn) -> ChangesetRow | None:
        bullets = [b.model_dump() for b in s.bullets] if s.bullets else parse_skill_md(s.body or "")[2]
        return await self._author(tenant, actor, s.agent_id, s.name, s.description, bullets, "api")

    async def import_skill(self, tenant: str, actor: str, imp: SkillImport) -> ChangesetRow | None:
        name, desc, bullets = parse_skill_md(imp.markdown)
        if not name or not desc:
            raise Conflict("SKILL.md needs frontmatter with name and description")
        return await self._author(tenant, actor, imp.agent_id, slug(name), desc, bullets, "import")

    async def _author(self, tenant: str, actor: str, agent: str, name: str, desc: str,
                      bullets: list[dict[str, Any]], source: str) -> ChangesetRow | None:
        """Human-authored skills replace the bullet set wholesale, but still as a governed, versioned changeset."""
        async with self.db.session() as ses:
            skills, cur = await self.gov.library(ses, tenant, agent)
        ops: list[dict[str, Any]] = []
        if name not in skills:
            ops.append({"type": "create_skill", "skill": name, "description": desc})
        elif skills[name].description != desc:
            ops.append({"type": "set_description", "skill": name, "description": desc})
        existing = {b["content"]: bid for bid, b in cur.items() if b["skill"] == name}
        for b in bullets:
            if b["content"] not in existing:
                ops.append({"type": "add", "skill": name, **b})
        wanted = {b["content"] for b in bullets}
        ops += [{"type": "remove", "skill": name, "bullet_id": bid, "reason": "replaced by author"}
                for content, bid in existing.items() if content not in wanted]
        cs = await self.gov.propose(tenant, actor, agent, ops, f"authored via {source}", source,
                                    max_ops=max(len(ops), 1), auto=False)
        if cs is not None and not [f for f in cs.safety_findings if not f.startswith("lint:")]:
            cs.status = await self.gov.review(tenant, actor, cs.id, approve=True, comment=f"authored via {source}",
                                              skip_canary=True)
        return cs

    async def get_skill(self, tenant: str, agent_id: str, ref: str) -> tuple[SkillRow, list[BulletRow]]:
        async with self.db.session() as ses:
            q = select(SkillRow).where(SkillRow.tenant_id == tenant)
            sk = await ses.scalar(q.where(SkillRow.id == ref)) or await ses.scalar(
                q.where(SkillRow.name == ref, SkillRow.agent_id.in_([agent_id, "*"])).order_by(SkillRow.agent_id.desc()))
            if sk is None:
                raise NotFound(ref)
            bl = (await ses.scalars(select(BulletRow).where(BulletRow.skill_id == sk.id,
                                                            BulletRow.status == "active"))).all()
            return sk, list(bl)

    async def skill_markdown(self, tenant: str, agent_id: str, ref: str) -> str:
        sk, bl = await self.get_skill(tenant, agent_id, ref)
        return to_skill_md(sk.name, sk.description, sk.version, sk.tags or [], [bullet_dict(b) for b in bl])

    async def add_memory(self, tenant: str, actor: str, m: MemoryIn) -> list[str]:
        return await self.mem.reconcile(tenant, m.scope.agent_id, m.scope.user_id,
                                        [{"kind": m.kind.value, "content": m.content, "user_id": m.scope.user_id}],
                                        [], actor)

    # ================================================================ ask (dialectic query)

    async def ask(self, tenant: str, req: AskIn) -> AskOut:
        r = await self.recall(tenant, RecallRequest(scope=req.scope, query=req.question, max_skills=3,
                                                    max_memories=12), allow_holdout=False)
        ev: list[str] = []
        if r.profile:
            ev.append(f"[profile] {r.profile}")
        ev += [f"[{m.id}] ({m.kind}) {m.content}" for m in r.memories]
        ev += [f"[{b.id}] ({k.name}) {b.content}" for k in r.skills for b in (k.bullets or [])]
        if not ev:
            return AskOut(answer="Nothing has been learned about that yet.", evidence=[])
        out = await self.c.llm_json(tenant, "ask", prompts.ASK_SYSTEM,
                                    f"<question>{req.question}</question>\n<evidence>\n" + "\n".join(ev) + "\n</evidence>",
                                    prompts.ASK_SCHEMA)
        return AskOut(answer=out.get("answer", ""), evidence=out.get("evidence_ids", []))

    # ================================================================ insights & export

    async def insights(self, tenant: str, agent: str) -> dict[str, Any]:
        async with self.db.session() as ses:
            st = await ses.get(AgentStateRow, (tenant, agent))
            skills = (await ses.scalars(select(SkillRow).where(SkillRow.tenant_id == tenant,
                                                               SkillRow.agent_id == agent))).all()
            exps = (await ses.scalars(select(ExperimentRow).where(ExperimentRow.tenant_id == tenant)
                                      .order_by(ExperimentRow.created_at.desc()).limit(20))).all()
            pending = await ses.scalar(select(func.count()).select_from(ChangesetRow).where(
                ChangesetRow.tenant_id == tenant, ChangesetRow.agent_id == agent, ChangesetRow.status == "pending"))
            bad = (await ses.scalars(select(BulletRow).where(BulletRow.tenant_id == tenant, BulletRow.status == "active",
                                                             BulletRow.harmful > BulletRow.helpful)
                                     .order_by(BulletRow.harmful.desc()).limit(10))).all()
            since = (utcnow() - timedelta(days=30)).date()
            usage = (await ses.scalars(select(UsageRow).where(UsageRow.tenant_id == tenant, UsageRow.day >= since))).all()
        lift = None
        if st is not None:
            ctrl, trt = xp.Arm(st.holdout_alpha, st.holdout_beta), xp.Arm(st.treated_alpha, st.treated_beta)
            mean, lo, hi = xp.lift_interval(ctrl, trt)
            lift = {"treated_success": round(trt.mean, 3), "treated_n": round(trt.n, 1),
                    "holdout_success": round(ctrl.mean, 3), "holdout_n": round(ctrl.n, 1),
                    "lift": round(mean, 3), "lift_90ci": [round(lo, 3), round(hi, 3)],
                    "significant": lo > 0 or hi < 0}
        return {
            "agent_id": agent,
            "learning_lift": lift,
            "skills": [{"id": k.id, "name": k.name, "version": k.version, "status": k.status, "uses": k.uses,
                        "success_rate": round(k.alpha / (k.alpha + k.beta), 3),
                        "success_90ci": [round(v, 3) for v in xp.credible_interval(xp.Arm(k.alpha, k.beta))]}
                       for k in sorted(skills, key=lambda k: -k.uses)],
            "experiments": [{"id": e.id, "skill_id": e.skill_id, "status": e.status,
                             "p_candidate_better": e.p_better, "incumbent_n": round(e.inc_alpha + e.inc_beta - 2, 1),
                             "candidate_n": round(e.cand_alpha + e.cand_beta - 2, 1)} for e in exps],
            "pending_changesets": pending,
            "problem_rules": [{"id": b.id, "content": b.content, "helpful": b.helpful, "harmful": b.harmful}
                              for b in bad],
            "tenant_learning_cost_30d": {  # usage is metered per tenant, not per agent
                "calls": sum(u.calls for u in usage),
                "input_tokens": sum(u.input_tokens for u in usage),
                "output_tokens": sum(u.output_tokens for u in usage),
                "by_purpose": {p: sum(u.input_tokens + u.output_tokens for u in usage if u.purpose == p)
                               for p in {u.purpose for u in usage}}},
        }

    async def export(self, tenant: str, agent: str, limit: int = 10000) -> AsyncIterator[str]:
        """JSONL of episodes with rewards: training data for RL / fine-tuning (e.g. Agent Lightning)."""
        async with self.db.session() as ses:
            res = await ses.stream_scalars(select(EpisodeRow).where(EpisodeRow.tenant_id == tenant,
                                                                    EpisodeRow.agent_id == agent)
                                           .order_by(EpisodeRow.created_at).limit(limit))
            async for e in res:
                reward, strength = signal(e, self.s.min_judge_confidence)
                yield json.dumps({"episode_id": e.id, "task": e.task, "messages": e.messages, "tool_calls": e.tool_calls,
                                  "reward": reward, "reward_strength": strength, "served": e.served,
                                  "holdout": e.holdout, "framework": e.framework,
                                  "created_at": e.created_at.isoformat()}) + "\n"

    async def purge_expired(self) -> dict[str, int]:
        """Retention: episodes, recall logs (they hold query text and user ids), finished jobs, old audit entries."""
        from sqlalchemy import delete

        from .db import AuditRow, JobRow

        cutoff = utcnow() - timedelta(days=self.s.episode_retention_days)
        audit_cutoff = utcnow() - timedelta(days=self.s.audit_retention_days)
        async with self.db.session() as ses:
            out = {
                "episodes": (await ses.execute(delete(EpisodeRow).where(EpisodeRow.created_at < cutoff))).rowcount,
                "recalls": (await ses.execute(delete(RecallRow).where(RecallRow.created_at < cutoff))).rowcount,
                "jobs": (await ses.execute(delete(JobRow).where(JobRow.created_at < cutoff,
                                                                 JobRow.status.in_(["done", "failed"])))).rowcount,
                "audit": (await ses.execute(delete(AuditRow).where(AuditRow.created_at < audit_cutoff))).rowcount,
            }
            await ses.commit()
        return {k: v or 0 for k, v in out.items()}


__all__ = ["Conflict", "LearningEngine", "NotFound", "REWARD"]
