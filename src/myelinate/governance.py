"""Governance: every change to learned skills is a changeset of typed delta ops that is scanned, risk-rated,
approved (by policy or a human), optionally canaried against the incumbent on live traffic, applied
atomically with an immutable version snapshot, and reversible by rollback.
"""

from __future__ import annotations

import hashlib
import random
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import experiments as xp
from . import safety
from .core import Conflict, Core, NotFound
from .db import BulletRow, ChangesetRow, ExperimentRow, SkillRow, SkillVersionRow, new_id, short_id, utcnow
from .skills import OpBatch, lint_bullet, normalise_ops, simulate
from .telemetry import metrics

log = structlog.get_logger()


def bullet_dict(b: BulletRow) -> dict[str, Any]:
    return {"id": b.id, "section": b.section, "content": b.content, "issue": b.issue, "keywords": b.keywords,
            "position": b.position}


class Governance:
    def __init__(self, core: Core) -> None:
        self.c = core

    # ---------------------------------------------------------------- library views

    async def library(self, ses: AsyncSession, tenant: str, agent_id: str) -> tuple[dict[str, SkillRow],
                                                                                   dict[str, dict[str, Any]]]:
        """Skills visible to an agent (its own + tenant-shared '*'), and their active bullets."""
        skills = (await ses.scalars(select(SkillRow).where(
            SkillRow.tenant_id == tenant, or_(SkillRow.agent_id == agent_id, SkillRow.agent_id == "*")))).all()
        by_name: dict[str, SkillRow] = {}
        for s in skills:  # agent-specific beats shared on name clash
            if s.name not in by_name or s.agent_id != "*":
                by_name[s.name] = s
        ids = {s.id: s.name for s in by_name.values()}
        rows = (await ses.scalars(select(BulletRow).where(BulletRow.tenant_id == tenant,
                                                          BulletRow.skill_id.in_(ids), BulletRow.status == "active"))
                ).all() if ids else []
        bullets = {b.id: {**bullet_dict(b), "skill": ids[b.skill_id], "helpful": b.helpful, "harmful": b.harmful,
                          "used": b.used} for b in rows}
        return by_name, bullets

    async def active_bullets_rows(self, ses: AsyncSession, skill_id: str) -> list[BulletRow]:
        return list((await ses.scalars(select(BulletRow).where(BulletRow.skill_id == skill_id,
                                                               BulletRow.status == "active"))).all())

    async def active_bullets(self, ses: AsyncSession, skill_id: str) -> list[dict[str, Any]]:
        rows = (await ses.scalars(select(BulletRow).where(BulletRow.skill_id == skill_id,
                                                          BulletRow.status == "active"))).all()
        return [bullet_dict(b) for b in rows]

    # ---------------------------------------------------------------- propose

    async def propose(self, tenant: str, actor: str, agent_id: str, raw_ops: list[dict[str, Any]], rationale: str,
                      source: str, episode_ids: list[str] | None = None, *, eval_score: float | None = None,
                      baseline_score: float | None = None, restrict_skill: str | None = None,
                      max_ops: int | None = None, auto: bool = True,
                      read_versions: dict[str, int] | None = None) -> ChangesetRow | None:
        """read_versions: skill_id -> version the proposer actually read. Pass it whenever ops were derived
        from an earlier read (LLM calls take time); a skill changed in between makes the changeset stale."""
        async with self.c.db.session() as ses:
            skills, bullets = await self.library(ses, tenant, agent_id)
            batch: OpBatch = normalise_ops(
                raw_ops, {n: {"id": s.id, "description": s.description} for n, s in skills.items()}, bullets,
                max_ops or self.c.s.max_ops_per_changeset, restrict_skill=restrict_skill)
            if batch.dropped:
                log.info("changeset.dropped_ops", dropped=batch.dropped[:10])
            if not batch.ops:
                return None
            for op in batch.ops:  # fix ids now so canaries, evaluation and application agree
                if op["type"] == "add" and "new_id" not in op:
                    op["new_id"] = short_id("b")
            text = "\n".join(str(op.get(k, "")) for op in batch.ops for k in ("content", "description", "issue"))
            findings = safety.scan(text, self.c.s.max_skill_chars)
            for op in batch.ops:
                if op.get("content"):
                    findings += lint_bullet(op["content"], self.c.s.max_bullet_words)
            row = ChangesetRow(
                id=new_id("cs"), tenant_id=tenant, agent_id=agent_id, ops=batch.ops, rationale=rationale,
                source=source, risk=batch.risk, eval_score=eval_score, baseline_score=baseline_score,
                safety_findings=sorted(set(findings)), episode_ids=episode_ids or [],
                base_versions={skills[n].id: (read_versions or {}).get(skills[n].id, skills[n].version)
                               for n in batch.skills_touched if n in skills})
            ses.add(row)
            self.c.audit(ses, tenant, actor, "changeset.proposed", row.id, risk=row.risk, source=source,
                         ops=len(batch.ops), findings=row.safety_findings)
            await ses.commit()
        metrics.changesets.labels(source=source, status="proposed").inc()
        if auto and self.auto_approvable(row):
            row.status = await self.review(tenant, "system:policy", row.id, approve=True,
                                           comment="auto-approved by policy")
        if row.status == "pending" and (auto or row.safety_findings):  # authored changes are approved by their author
            await self.notify(tenant, "changeset.proposed", row.id)
        return row

    async def notify(self, tenant: str, event: str, ref: str) -> None:
        """Queue a signed webhook delivery (durable and retried by the worker), if subscribed."""
        if self.c.s.webhook_url and event in self.c.s.webhook_events:
            async with self.c.db.session() as ses:
                await self.c.enqueue(ses, tenant, "webhook", {"event": event, "ref": ref})
                await ses.commit()

    async def proposal(self, tenant: str, cs_id: str) -> dict[str, Any]:
        """A changeset as a reviewable suggestion: per skill, the current and proposed SKILL.md and a unified diff,
        plus the evidence it came from. This is what to promote in an external content or review system."""
        import difflib

        from .db import EpisodeRow
        from .skills import to_skill_md

        async with self.c.db.session() as ses:
            cs = await ses.get(ChangesetRow, cs_id)
            if cs is None or cs.tenant_id != tenant:
                raise NotFound(cs_id)
            skills, _ = await self.library(ses, tenant, cs.agent_id)
            out = []
            for name in dict.fromkeys(op["skill"] for op in cs.ops):
                sk = skills.get(name)
                cur = await self.active_bullets(ses, sk.id) if sk else []
                desc, ver, tags = (sk.description, sk.version, sk.tags or []) if sk else ("", 0, [])
                new, new_desc = simulate(cur, desc, cs.ops, name)
                cur_md = to_skill_md(name, desc, ver, tags, cur) if sk else ""
                new_md = to_skill_md(name, new_desc, ver + 1, tags, new)
                diff = "".join(difflib.unified_diff(cur_md.splitlines(True), new_md.splitlines(True),
                                                    f"{name}/SKILL.md (v{ver})", f"{name}/SKILL.md (proposed)"))
                out.append({"skill": name, "skill_id": sk.id if sk else None, "current_version": ver,
                            "current_skill_md": cur_md, "proposed_skill_md": new_md, "diff": diff,
                            "ops": [o for o in cs.ops if o["skill"] == name]})
            eps = (await ses.scalars(select(EpisodeRow).where(EpisodeRow.id.in_(cs.episode_ids[:20])))).all() \
                if cs.episode_ids else []
            evidence = [{"episode_id": e.id, "task": (e.task or "")[:300], "outcome": e.outcome,
                         "verification": e.meta.get("verification"), "feedback_score": e.feedback_score,
                         "created_at": e.created_at.isoformat()} for e in eps]
        return {"changeset_id": cs.id, "agent_id": cs.agent_id, "status": cs.status, "risk": cs.risk,
                "source": cs.source, "rationale": cs.rationale, "eval_score": cs.eval_score,
                "baseline_score": cs.baseline_score, "safety_findings": cs.safety_findings,
                "created_at": cs.created_at.isoformat(), "skills": out, "evidence": evidence,
                "approve_url": f"/v1/changesets/{cs.id}/review"}

    def auto_approvable(self, cs: ChangesetRow) -> bool:
        if self.c.s.governance_mode == "propose_only":
            return False
        blocking = [f for f in cs.safety_findings if not f.startswith("lint:")]
        if blocking:
            return False
        if cs.eval_score is not None and cs.eval_score < self.c.s.auto_approve_min_eval_score:
            return False
        if cs.risk == "low":
            return self.c.s.auto_approve_low_risk
        if cs.risk == "medium":
            return self.c.s.auto_approve_medium_risk
        return False  # removals and routing changes always need a human

    # ---------------------------------------------------------------- review / apply

    async def review(self, tenant: str, actor: str, cs_id: str, approve: bool, comment: str | None,
                     skip_canary: bool = False) -> str:
        async with self.c.db.session() as ses:
            cs = await ses.get(ChangesetRow, cs_id)
            if cs is None or cs.tenant_id != tenant:
                raise NotFound(cs_id)
            if cs.status != "pending":
                raise Conflict(f"changeset is {cs.status}")
            cs.reviewer, cs.review_comment, cs.decided_at = actor, comment, utcnow()
            if not approve:
                cs.status, cs.rejection_reason = "rejected", f"reviewer: {comment or 'rejected'}"
                self.c.audit(ses, tenant, actor, "changeset.rejected", cs.id)
            else:
                await self._approve(ses, tenant, actor, cs, skip_canary)
            await ses.commit()
            status = cs.status
        metrics.changesets.labels(source=cs.source, status=status).inc()
        if actor != "system:policy":
            await self.notify(tenant, "changeset.reviewed", cs_id)
        return status

    async def _stale(self, ses: AsyncSession, cs: ChangesetRow) -> str | None:
        """A changeset only applies to the exact skill versions it was proposed against (no silent rebase)."""
        for sid, ver in cs.base_versions.items():
            sk = await ses.get(SkillRow, sid)
            if sk is None or sk.version != ver:
                return f"stale: skill {sid} moved from v{ver} to v{sk.version if sk else '?'}; re-propose"
        for op in cs.ops:
            if op["type"] == "create_skill" and await ses.scalar(select(SkillRow.id).where(
                    SkillRow.tenant_id == cs.tenant_id, SkillRow.agent_id == cs.agent_id, SkillRow.name == op["skill"])):
                return f"stale: skill {op['skill']} was created by another change; re-propose"
        return None

    async def _approve(self, ses: AsyncSession, tenant: str, actor: str, cs: ChangesetRow, skip_canary: bool) -> None:
        if reason := await self._stale(ses, cs):
            cs.status, cs.rejection_reason = "failed", reason
            self.c.audit(ses, tenant, actor, "changeset.failed", cs.id, reason=reason)
            return
        touched = set(cs.base_versions)  # existing skills this changeset modifies
        running = (await ses.scalars(select(ExperimentRow).where(ExperimentRow.skill_id.in_(touched),
                                                                 ExperimentRow.status == "running"))).all() if touched else []
        if running:
            cs.status = "approved"  # queued; re-evaluated when the running canary on that skill ends
            self.c.audit(ses, tenant, actor, "changeset.queued", cs.id, behind=[e.id for e in running])
            return
        if (self.c.s.canary_enabled and self.c.s.governance_mode == "auto" and not skip_canary
                and cs.risk != "low" and len(touched) == 1):
            sk = await ses.get(SkillRow, next(iter(touched)))
            if sk is not None and sk.uses >= self.c.s.canary_min_samples:
                await self._start_canary(ses, cs, sk)
                self.c.audit(ses, tenant, actor, "changeset.canary", cs.id, skill=sk.name)
                return
        await self._apply(ses, tenant, actor, cs)
        self.c.audit(ses, tenant, actor, f"changeset.{cs.status}", cs.id, versions=cs.applied_versions,
                     reason=cs.rejection_reason)

    async def _apply(self, ses: AsyncSession, tenant: str, actor: str, cs: ChangesetRow) -> None:
        if reason := await self._stale(ses, cs):
            cs.status, cs.rejection_reason = "failed", reason
            return
        skills, _ = await self.library(ses, tenant, cs.agent_id)
        touched: dict[str, SkillRow] = {}
        next_pos: dict[str, float] = {}  # running position counter per skill keeps authored order
        new_texts: list[tuple[Any, str]] = []
        for op in cs.ops:
            name = op["skill"]
            if op["type"] == "create_skill":
                sk = SkillRow(id=new_id("skl"), tenant_id=tenant, agent_id=cs.agent_id, name=name,
                              description=op["description"], version=0, created_by=cs.source)
                ses.add(sk)
                skills[name] = sk
                touched[name] = sk
                next_pos[sk.id] = 1.0
                new_texts.append((sk, f"{name.replace('-', ' ')}: {sk.description}"))
                continue
            sk = skills[name]
            touched[name] = sk
            if op["type"] == "set_description":
                sk.description = op["description"]
                new_texts.append((sk, f"{name.replace('-', ' ')}: {sk.description}"))
            elif op["type"] == "add":
                if sk.id not in next_pos:
                    next_pos[sk.id] = max((b["position"] for b in await self.active_bullets(ses, sk.id)),
                                          default=0.0) + 1
                pos, next_pos[sk.id] = next_pos[sk.id], next_pos[sk.id] + 1
                b = BulletRow(id=op["new_id"], tenant_id=tenant, skill_id=sk.id, section=op["section"],
                              content=op["content"], issue=op.get("issue", ""), keywords=op.get("keywords", []),
                              position=pos, sources=cs.episode_ids[:20])
                ses.add(b)
                new_texts.append((b, f"{b.content} {b.issue}"))
            else:
                b = await ses.get(BulletRow, op["bullet_id"])
                if b is None or b.tenant_id != tenant:
                    continue
                if op["type"] == "update":
                    b.content = op["content"]
                    b.issue = op.get("issue") or b.issue
                    b.keywords = op.get("keywords") or b.keywords
                    b.sources = list(dict.fromkeys([*b.sources, *cs.episode_ids]))[-20:]
                    new_texts.append((b, f"{b.content} {b.issue}"))
                elif op["type"] == "remove":
                    b.status = "removed"
                    if into := op.get("merged_into"):  # merged duplicate: keep its evidence (ACE sums counters)
                        await ses.execute(update(BulletRow).where(BulletRow.id == into).values(
                            helpful=BulletRow.helpful + b.helpful, harmful=BulletRow.harmful + b.harmful,
                            neutral=BulletRow.neutral + b.neutral, used=BulletRow.used + b.used))
        await ses.flush()
        embs = await self.c.embed([t for _, t in new_texts])
        if embs:
            for (obj, _), e in zip(new_texts, embs, strict=True):
                obj.embedding = e
        for sk in touched.values():
            await self._snapshot(ses, tenant, sk, cs.id, cs.source, cs.reviewer or "system")
            cs.applied_versions = {**cs.applied_versions, sk.id: sk.version}
        cs.status = "applied"

    async def _snapshot(self, ses: AsyncSession, tenant: str, sk: SkillRow, cs_id: str | None, source: str,
                        actor: str) -> None:
        await ses.flush()
        sk.version += 1
        sk.updated_at = utcnow()
        if sk.status in ("stale", "archived"):
            sk.status = "active"
        ses.add(SkillVersionRow(id=new_id("skv"), tenant_id=tenant, skill_id=sk.id, version=sk.version,
                                description=sk.description, bullets=await self.active_bullets(ses, sk.id),
                                changeset_id=cs_id, source=source, created_by=actor))

    # ---------------------------------------------------------------- canary experiments

    async def _start_canary(self, ses: AsyncSession, cs: ChangesetRow, sk: SkillRow) -> None:
        bullets, desc = simulate(await self.active_bullets(ses, sk.id), sk.description, cs.ops, sk.name)
        ses.add(ExperimentRow(id=new_id("exp"), tenant_id=cs.tenant_id, skill_id=sk.id, changeset_id=cs.id,
                              base_version=sk.version, candidate_bullets=bullets, candidate_description=desc))
        cs.status = "canary"

    async def running_experiments(self, ses: AsyncSession, skill_ids: list[str]) -> dict[str, ExperimentRow]:
        if not skill_ids:
            return {}
        rows = (await ses.scalars(select(ExperimentRow).where(ExperimentRow.skill_id.in_(skill_ids),
                                                              ExperimentRow.status == "running"))).all()
        return {r.skill_id: r for r in rows}

    def choose_arm(self, exp: ExperimentRow, sticky_key: str | None) -> str:
        """Thompson sampling with exploration floor; deterministic per session/user so a conversation
        doesn't flip between versions mid-way."""
        seed = int(hashlib.sha256(f"{exp.id}:{sticky_key}".encode()).hexdigest()[:12], 16) if sticky_key else None
        rng = random.Random(seed)
        cand = xp.choose_candidate(xp.Arm(exp.inc_alpha, exp.inc_beta), xp.Arm(exp.cand_alpha, exp.cand_beta), rng,
                                   floor=self.c.s.canary_floor)
        return "candidate" if cand else "incumbent"

    async def credit_experiment(self, ses: AsyncSession, exp_id: str, arm: str, delta_alpha: float,
                                delta_beta: float) -> None:
        a, b = ((ExperimentRow.cand_alpha, ExperimentRow.cand_beta) if arm == "candidate"
                else (ExperimentRow.inc_alpha, ExperimentRow.inc_beta))
        # Atomic increments: concurrent episodes must never lose each other's evidence.
        res = await ses.execute(update(ExperimentRow).where(ExperimentRow.id == exp_id,
                                                            ExperimentRow.status == "running")
                                .values({a.key: a + delta_alpha, b.key: b + delta_beta}))
        if not res.rowcount:  # type: ignore[attr-defined]
            return
        exp = await ses.scalar(select(ExperimentRow).where(ExperimentRow.id == exp_id).with_for_update()
                               .execution_options(populate_existing=True))
        if exp is None or exp.status != "running":
            return
        verdict, p = xp.decide(xp.Arm(exp.inc_alpha, exp.inc_beta), xp.Arm(exp.cand_alpha, exp.cand_beta),
                               self.c.s.canary_min_samples, self.c.s.canary_max_samples, self.c.s.canary_threshold)
        exp.p_better = p
        if verdict != xp.Verdict.continue_:
            await self._conclude(ses, exp, verdict, p)

    async def _conclude(self, ses: AsyncSession, exp: ExperimentRow, verdict: xp.Verdict, p: float,
                        reason: str | None = None) -> None:
        cs = await ses.get(ChangesetRow, exp.changeset_id)
        exp.status = "promoted" if verdict == xp.Verdict.promote else ("expired" if reason else "rejected")
        exp.decided_at = utcnow()
        metrics.experiments.labels(verdict=exp.status).inc()
        if cs is not None:
            if verdict == xp.Verdict.promote:
                await self._apply(ses, exp.tenant_id, "system:canary", cs)
                metrics.changesets.labels(source=cs.source, status=cs.status).inc()
                self.c.audit(ses, exp.tenant_id, "system:canary", "canary.promoted", cs.id, p_better=round(p, 3),
                             result=cs.status)
            else:
                cs.status = "rejected"
                cs.rejection_reason = reason or f"canary: P(candidate better)={p:.2f} after live traffic"
                metrics.changesets.labels(source=cs.source, status=cs.status).inc()
                self.c.audit(ses, exp.tenant_id, "system:canary", f"canary.{exp.status}", cs.id, p_better=round(p, 3))
        await self.release_queue(ses, exp.tenant_id, exp.skill_id)

    async def release_queue(self, ses: AsyncSession, tenant: str, skill_id: str) -> None:
        """Re-evaluate every changeset queued behind an experiment on this skill, in order. Each goes back
        through approval: stale ones fail, risky ones may start their own canary (queueing the rest)."""
        queued = (await ses.scalars(select(ChangesetRow).where(ChangesetRow.tenant_id == tenant,
                                                               ChangesetRow.status == "approved")
                                    .order_by(ChangesetRow.created_at))).all()
        for q in queued:
            if skill_id in q.base_versions:
                await self._approve(ses, tenant, q.reviewer or "system", q, skip_canary=False)
                metrics.changesets.labels(source=q.source, status=q.status).inc()

    async def expire_experiments(self, tenant: str) -> int:
        """Canaries that can't reach a verdict (too little traffic, skill archived) keep the incumbent."""
        cutoff = utcnow() - timedelta(days=self.c.s.canary_max_days)
        n = 0
        async with self.c.db.session() as ses:
            rows = (await ses.scalars(select(ExperimentRow).where(ExperimentRow.tenant_id == tenant,
                                                                  ExperimentRow.status == "running"))).all()
            for exp in rows:
                sk = await ses.get(SkillRow, exp.skill_id)
                old = await ses.scalar(select(ExperimentRow.id).where(ExperimentRow.id == exp.id,
                                                                      ExperimentRow.created_at < cutoff))
                if old or sk is None or sk.status in ("archived", "deprecated"):
                    await self._conclude(ses, exp, xp.Verdict.reject, exp.p_better or 0.5,
                                         reason="canary expired without a verdict; incumbent kept")
                    n += 1
            await ses.commit()
        return n

    # ---------------------------------------------------------------- rollback

    async def rollback(self, tenant: str, actor: str, skill_id: str, to_version: int) -> int:
        async with self.c.db.session() as ses:
            sk = await ses.get(SkillRow, skill_id)
            if sk is None or sk.tenant_id != tenant:
                raise NotFound(skill_id)
            snap = await ses.scalar(select(SkillVersionRow).where(SkillVersionRow.skill_id == skill_id,
                                                                  SkillVersionRow.version == to_version))
            if snap is None:
                raise NotFound(f"{skill_id}@v{to_version}")
            stopped = list((await self.running_experiments(ses, [skill_id])).values())
            for exp in stopped:
                exp.status, exp.decided_at = "rejected", utcnow()
                metrics.experiments.labels(verdict="rejected").inc()
                if cs := await ses.get(ChangesetRow, exp.changeset_id):
                    cs.status, cs.rejection_reason = "rejected", f"rolled back by {actor}"
                    metrics.changesets.labels(source=cs.source, status="rejected").inc()
            keep = {b["id"]: b for b in snap.bullets}
            for b in (await ses.scalars(select(BulletRow).where(BulletRow.skill_id == skill_id))).all():
                if b.id in keep:
                    b.status, b.content = "active", keep.pop(b.id)["content"]
                else:
                    b.status = "removed"
            for b in keep.values():  # bullets that no longer exist at all
                ses.add(BulletRow(id=b["id"], tenant_id=tenant, skill_id=skill_id, section=b["section"],
                                  content=b["content"], issue=b.get("issue", ""), keywords=b.get("keywords", []),
                                  position=b.get("position", 0.0)))
            sk.description = snap.description
            await self._snapshot(ses, tenant, sk, None, f"rollback:v{to_version}", actor)
            await self.release_queue(ses, tenant, skill_id)  # queued changes were against older versions -> fail
            self.c.audit(ses, tenant, actor, "skill.rollback", skill_id, to_version=to_version, new_version=sk.version)
            await ses.commit()
            return sk.version
