"""Skill evolution: GEPA-style reflective search over bounded edits, gated offline, then canaried live.

Algorithm (per underperforming skill):
- Episodes that used the skill are split into D_feedback (failures first; drives mutations) and D_pareto
  (held out; scores candidates). Seed = current skill.
- Each iteration: pick a parent from the Pareto front (candidates that win on at least one held-out
  episode, sampled by how many they win), run a minibatch of 3 feedback episodes through the judge to
  get scores + textual feedback, ask the evolver for bounded ops, and accept the child only if it
  strictly beats the parent on the same minibatch (cheap pre-gate). Survivors are scored on D_pareto.
- The best candidate must beat the seed's held-out mean by `evolve_min_improvement` (ties lose). It is
  then proposed as a changeset with its net diff, which governance canaries on live traffic.
- Brevity guard: children that drop more than 30% of the rules are discarded (context collapse).
- Rejected attempts are fed back into later prompts (SkillOpt's rejected-edit buffer).
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select

from . import prompts
from .core import Core, clip
from .db import ChangesetRow, EpisodeRow, SkillRow
from .governance import Governance
from .learning import render_episode, signal
from .llm import LLMError
from .skills import diff_ops, normalise_ops, render_bullets, simulate
from .telemetry import metrics

log = structlog.get_logger()


@dataclass
class Cand:
    bullets: list[dict[str, Any]]
    description: str
    parent: int | None
    scores: dict[str, float] = field(default_factory=dict)  # episode id -> judge score
    feedback: dict[str, str] = field(default_factory=dict)

    def mean(self, ids: list[str]) -> float:
        xs = [self.scores[i] for i in ids if i in self.scores]
        return sum(xs) / len(xs) if xs else 0.0


def pareto_select(cands: list[Cand], ids: list[str], rng: random.Random) -> int:
    """GEPA Algorithm 2: per-instance winners, drop dominated, sample by number of instances won."""
    winners: dict[int, int] = {}
    for i in ids:
        best = max(c.scores.get(i, 0.0) for c in cands)
        for k, c in enumerate(cands):
            if c.scores.get(i, 0.0) >= best - 1e-9:
                winners[k] = winners.get(k, 0) + 1
    front = list(winners)

    def dominated(a: int) -> bool:
        return any(all(cands[b].scores.get(i, 0) >= cands[a].scores.get(i, 0) for i in ids)
                   and any(cands[b].scores.get(i, 0) > cands[a].scores.get(i, 0) for i in ids)
                   for b in front if b != a)

    front = [k for k in front if not dominated(k)] or front
    weights = [winners[k] for k in front]
    return rng.choices(front, weights=weights)[0]


class Evolver:
    def __init__(self, core: Core, gov: Governance) -> None:
        self.c, self.gov = core, gov

    async def _judge(self, tenant: str, cand: Cand, name: str, eps: list[EpisodeRow]) -> None:
        sem = asyncio.Semaphore(4)
        skill_text = f"{name}: {cand.description}\n\n{render_bullets(cand.bullets, with_ids=False)}"

        async def one(e: EpisodeRow) -> None:
            if e.id in cand.scores:
                return
            async with sem:
                try:
                    r = await self.c.llm_json(tenant, "judge_skill", prompts.JUDGE_SYSTEM,
                                              f"<skill>\n{skill_text}\n</skill>\n\n<recorded_task>\n"
                                              f"{render_episode(e, 10000)}\n</recorded_task>", prompts.JUDGE_SCHEMA)
                    cand.scores[e.id] = max(0.0, min(1.0, float(r["score"])))
                    cand.feedback[e.id] = r.get("feedback", "")
                except (LLMError, KeyError, ValueError) as exc:
                    log.warning("judge.failed", error=str(exc))
                    cand.scores[e.id] = 0.0  # GEPA: failures score 0, never raise

        await asyncio.gather(*(one(e) for e in eps))

    async def run(self, tenant: str, payload: dict[str, Any]) -> ChangesetRow | None:
        s = self.c.s
        async with self.c.db.session() as ses:
            sk = await ses.get(SkillRow, payload["skill_id"])
            if sk is None or sk.tenant_id != tenant or sk.status not in ("active", "stale"):
                return None
            seed_bullets = await self.gov.active_bullets(ses, sk.id)
            recent = (await ses.scalars(select(EpisodeRow).where(
                EpisodeRow.tenant_id == tenant, EpisodeRow.agent_id.in_([sk.agent_id, payload.get("agent_id", sk.agent_id)]),
                EpisodeRow.holdout.is_(False)).order_by(EpisodeRow.created_at.desc()).limit(400))).all()
            rejected = (await ses.scalars(select(ChangesetRow).where(
                ChangesetRow.tenant_id == tenant, ChangesetRow.status == "rejected",
                ChangesetRow.source == "evolution").order_by(ChangesetRow.created_at.desc()).limit(10))).all()
        eps = [e for e in recent if any(x.get("skill_id") == sk.id for x in e.served)]
        if len(eps) < 4:
            return None

        def badness(e: EpisodeRow) -> float:
            r, _ = signal(e, s.min_judge_confidence)
            return 1.0 - (r if r is not None else 0.5)

        rng = random.Random(f"{sk.id}:{sk.version}")
        rng.shuffle(eps)
        n_par = min(s.evolve_eval_sample, max(2, len(eps) // 2))
        pareto, feedback_set = eps[:n_par], sorted(eps[n_par:] or eps[:n_par], key=badness, reverse=True)
        par_ids = [e.id for e in pareto]
        rej_notes = [f"- {json.dumps(c.ops)[:300]} -> {c.rejection_reason}" for c in rejected
                     if any(o.get("skill") == sk.name for o in c.ops)]

        pool = [Cand(seed_bullets, sk.description, None)]
        await self._judge(tenant, pool[0], sk.name, pareto)
        for it in range(s.evolve_iterations):
            pi = pareto_select(pool, par_ids, rng)
            parent = pool[pi]
            mb = feedback_set[: s.evolve_minibatch] if it == 0 else rng.sample(feedback_set,
                                                                              min(s.evolve_minibatch, len(feedback_set)))
            await self._judge(tenant, parent, sk.name, mb)
            records = [{"task": clip(e.task, 500), "run": render_episode(e, 6000), "skill_score": parent.scores[e.id],
                        "feedback": parent.feedback.get(e.id, "")} for e in mb]
            prompt = (f"<skill name=\"{sk.name}\">\ndescription: {parent.description}\n"
                      f"{render_bullets(parent.bullets, with_ids=True)}\n</skill>\n\n"
                      f"<records>\n{json.dumps(records, indent=1)}\n</records>\n\n"
                      f"<previously_rejected_edits>\n{chr(10).join(rej_notes) or '(none)'}\n"
                      f"</previously_rejected_edits>\n\nReturn at most {s.max_ops_per_changeset} operations.")
            try:
                out = await self.c.llm_json(tenant, "evolve", prompts.EVOLVE_SYSTEM, prompt, prompts.EVOLVE_SCHEMA)
            except LLMError as e:
                log.warning("evolve.failed", error=str(e))
                break
            pb = {b["id"]: {**b, "skill": sk.name} for b in parent.bullets}
            batch = normalise_ops(out.get("operations", []), {sk.name: {"id": sk.id}}, pb, s.max_ops_per_changeset,
                                  restrict_skill=sk.name)
            if not batch.ops:
                continue
            bullets, desc = simulate(parent.bullets, parent.description, batch.ops, sk.name)
            if len(bullets) < 0.7 * len(parent.bullets):
                rej_notes.append(f"- dropped too many rules ({len(parent.bullets)} -> {len(bullets)})")
                continue
            child = Cand(bullets, desc, pi)
            await self._judge(tenant, child, sk.name, mb)
            mb_ids = [e.id for e in mb]
            if child.mean(mb_ids) <= parent.mean(mb_ids):  # strict: ties are rejected
                rej_notes.append(f"- {json.dumps(batch.ops)[:300]} -> no gain on minibatch "
                                 f"({parent.mean(mb_ids):.2f} -> {child.mean(mb_ids):.2f})")
                continue
            await self._judge(tenant, child, sk.name, pareto)
            pool.append(child)

        best = max(range(len(pool)), key=lambda k: pool[k].mean(par_ids))
        base, top = pool[0].mean(par_ids), pool[best].mean(par_ids)
        async with self.c.db.session() as ses:
            row = await ses.get(SkillRow, sk.id)
            if row is not None:
                row.last_evolved_uses = row.uses
                await ses.commit()
        if best == 0 or top - base < s.evolve_min_improvement:
            metrics.evolutions.labels(result="no_gain").inc()
            log.info("evolution.no_gain", skill=sk.name, baseline=round(base, 3), best=round(top, 3),
                     candidates=len(pool))
            return None
        metrics.evolutions.labels(result="improved").inc()
        ops = diff_ops(sk.name, seed_bullets, sk.description, pool[best].bullets, pool[best].description)
        return await self.gov.propose(
            tenant, "system:evolution", sk.agent_id, ops,
            f"GEPA search over {len(pool)} candidates; held-out judge score {base:.2f} -> {top:.2f} "
            f"on {len(par_ids)} episodes.", "evolution", [e.id for e in eps][:50], eval_score=top,
            baseline_score=base, restrict_skill=sk.name, max_ops=s.max_ops_per_changeset * 3,
            read_versions={sk.id: sk.version})
