"""Library upkeep, run as a periodic per-agent `curate` job.

- Grow-and-refine dedup: near-duplicate rules inside a skill are judged by an LLM (merge / keep / remove);
  KEEP decisions are remembered so pairs aren't re-litigated; merges sum the evidence counters.
- Lifecycle: unused skills go active -> stale -> archived (never deleted; pinned skills are exempt).
- Evidence-based nomination: skills with poor posterior success or rules that keep being tagged harmful
  are queued for evolution. Counters only nominate; they never delete rules by themselves.
- Profile cards for users whose memories changed are rebuilt.
"""

from __future__ import annotations

import json
from datetime import timedelta
from itertools import combinations
from typing import Any

import structlog
from sqlalchemy import and_, or_, select, update

from . import prompts
from .core import Core
from .db import BulletRow, PairDecisionRow, ProfileRow, SkillRow, utcnow
from .governance import Governance
from .llm import LLMError
from .memory import MemoryService
from .retrieval import cosine, jaccard

log = structlog.get_logger()


class Maintainer:
    def __init__(self, core: Core, gov: Governance, mem: MemoryService) -> None:
        self.c, self.gov, self.mem = core, gov, mem

    async def run(self, tenant: str, payload: dict[str, Any]) -> dict[str, int]:
        agent = payload["agent_id"]
        stats = {"merged_pairs": 0, "stale": 0, "archived": 0, "evolve_queued": 0, "profiles": 0,
                 "experiments_expired": 0}
        stats["merged_pairs"] = await self.dedup(tenant, agent)
        stats["stale"], stats["archived"] = await self.lifecycle(tenant, agent)
        stats["evolve_queued"] = await self.nominate(tenant, agent)
        stats["profiles"] = await self.profiles(tenant, agent)
        stats["experiments_expired"] = await self.gov.expire_experiments(tenant)
        log.info("curation.done", tenant=tenant, agent=agent, **stats)
        return stats

    def _similar(self, a: BulletRow, b: BulletRow) -> bool:
        if a.embedding and b.embedding:
            return cosine(a.embedding, b.embedding) >= self.c.s.dedup_similarity
        return jaccard(a.content, b.content) >= self.c.s.dedup_jaccard

    async def dedup(self, tenant: str, agent: str) -> int:
        async with self.c.db.session() as ses:
            skills = (await ses.scalars(select(SkillRow).where(SkillRow.tenant_id == tenant, SkillRow.agent_id == agent,
                                                               SkillRow.status != "archived"))).all()
            kept = {(d.a, d.b) for d in (await ses.scalars(select(PairDecisionRow).where(
                PairDecisionRow.tenant_id == tenant))).all()}
            pairs: list[tuple[SkillRow, BulletRow, BulletRow]] = []
            for sk in skills:
                bl = (await ses.scalars(select(BulletRow).where(BulletRow.skill_id == sk.id,
                                                                BulletRow.status == "active"))).all()
                for a, b in combinations(sorted(bl, key=lambda x: x.id), 2):
                    if a.section == b.section and (a.id, b.id) not in kept and self._similar(a, b):
                        pairs.append((sk, a, b))
        if not pairs:
            return 0
        pairs = pairs[:20]
        listing = [{"a": a.id, "a_text": a.content, "a_helpful": a.helpful, "a_harmful": a.harmful,
                    "b": b.id, "b_text": b.content, "b_helpful": b.helpful, "b_harmful": b.harmful,
                    "skill": sk.name} for sk, a, b in pairs]
        try:
            out = await self.c.llm_json(tenant, "dedup", prompts.DEDUP_SYSTEM, json.dumps(listing, indent=1),
                                        prompts.DEDUP_SCHEMA)
        except LLMError as e:
            log.warning("dedup.failed", error=str(e))
            return 0
        skill_of = {(a.id, b.id): sk.name for sk, a, b in pairs}
        ops: list[dict[str, Any]] = []
        touched: set[str] = set()
        async with self.c.db.session() as ses:
            for d in out.get("decisions", []):
                key = (d.get("a"), d.get("b"))
                if key not in skill_of or touched & set(key):
                    continue
                name, act = skill_of[key], d.get("action")
                if act == "keep":
                    if key not in kept:
                        kept.add(key)
                        ses.add(PairDecisionRow(tenant_id=tenant, a=key[0], b=key[1], decision="keep"))
                elif act == "merge" and d.get("merged_content"):
                    ops += [{"type": "update", "skill": name, "bullet_id": key[0], "content": d["merged_content"]},
                            {"type": "remove", "skill": name, "bullet_id": key[1], "merged_into": key[0],
                             "reason": "merged duplicate"}]
                    touched |= set(key)
                elif act in ("remove_a", "remove_b"):
                    drop, keep = (key[0], key[1]) if act == "remove_a" else (key[1], key[0])
                    ops.append({"type": "remove", "skill": name, "bullet_id": drop, "merged_into": keep,
                                "reason": "redundant"})
                    touched |= set(key)
            await ses.commit()
        if ops:
            await self.gov.propose(tenant, "system:curation", agent, ops, "Merge near-duplicate rules", "curation",
                                   max_ops=len(ops), read_versions={sk.id: sk.version for sk, _, _ in pairs})
        return len(touched) // 2

    async def lifecycle(self, tenant: str, agent: str) -> tuple[int, int]:
        now = utcnow()
        stale_cut, arch_cut = now - timedelta(days=self.c.s.stale_after_days), now - timedelta(days=self.c.s.archive_after_days)
        last = SkillRow.last_used_at
        base = and_(SkillRow.tenant_id == tenant, SkillRow.agent_id == agent, SkillRow.pinned.is_(False))
        async with self.c.db.session() as ses:
            a = await ses.execute(update(SkillRow).where(
                base, SkillRow.status.in_(["active", "stale"]),
                or_(last < arch_cut, and_(last.is_(None), SkillRow.created_at < arch_cut))).values(status="archived"))
            s = await ses.execute(update(SkillRow).where(
                base, SkillRow.status == "active",
                or_(last < stale_cut, and_(last.is_(None), SkillRow.created_at < stale_cut))).values(status="stale"))
            if a.rowcount or s.rowcount:
                self.c.audit(ses, tenant, "system:curation", "skills.lifecycle", agent, stale=s.rowcount,
                             archived=a.rowcount)
            await ses.commit()
        return s.rowcount or 0, a.rowcount or 0

    async def nominate(self, tenant: str, agent: str) -> int:
        n = 0
        async with self.c.db.session() as ses:
            skills = (await ses.scalars(select(SkillRow).where(SkillRow.tenant_id == tenant, SkillRow.agent_id == agent,
                                                               SkillRow.status.in_(["active", "stale"])))).all()
            for sk in skills:
                fresh = sk.uses - sk.last_evolved_uses
                poor = sk.alpha / (sk.alpha + sk.beta) < self.c.s.evolve_max_success_rate
                bad_rule = await ses.scalar(select(BulletRow.id).where(
                    BulletRow.skill_id == sk.id, BulletRow.status == "active", BulletRow.harmful >= 3,
                    BulletRow.harmful >= BulletRow.helpful + 2).limit(1))
                if fresh >= self.c.s.evolve_min_uses and (poor or bad_rule):
                    await self.c.enqueue(ses, tenant, "evolve", {"skill_id": sk.id}, dedupe_key=f"evolve:{sk.id}")
                    n += 1
            await ses.commit()
        return n

    async def profiles(self, tenant: str, agent: str) -> int:
        async with self.c.db.session() as ses:
            dirty = (await ses.scalars(select(ProfileRow).where(ProfileRow.tenant_id == tenant,
                                                                ProfileRow.agent_id == agent,
                                                                ProfileRow.dirty.is_(True)).limit(50))).all()
        for p in dirty:
            try:
                await self.mem.refresh_profile(tenant, agent, p.user_id)
            except LLMError as e:
                log.warning("profile.failed", error=str(e))
        return len(dirty)
