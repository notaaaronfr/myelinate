"""Memory: extract -> reconcile (ADD / UPDATE / SUPERSEDE / NOOP) -> bi-temporal store, plus profile cards.

Nothing is hard-deleted by learning: a contradicted or refined memory gets `invalid_at` and
`superseded_by`, so audits and point-in-time recall (`as_of`) still see what the agent believed then.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import prompts, safety
from .core import BudgetExceeded, Core, clip
from .db import MemoryRow, ProfileRow, new_id, utcnow
from .llm import LLMError
from .retrieval import Candidate, similarity

log = structlog.get_logger()


def content_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()


def valid_at(as_of: datetime | None):  # type: ignore[no-untyped-def]
    if as_of is None:
        return MemoryRow.invalid_at.is_(None)
    return and_(MemoryRow.valid_from <= as_of, or_(MemoryRow.invalid_at.is_(None), MemoryRow.invalid_at > as_of))


class MemoryService:
    def __init__(self, core: Core) -> None:
        self.c = core

    async def visible(self, ses: AsyncSession, tenant: str, agent_id: str, user_id: str | None,
                      as_of: datetime | None = None, limit: int = 2000) -> list[MemoryRow]:
        q = select(MemoryRow).where(MemoryRow.tenant_id == tenant, MemoryRow.agent_id.in_([agent_id, "*"]),
                                    MemoryRow.status == "active", valid_at(as_of),
                                    or_(MemoryRow.user_id.is_(None), MemoryRow.user_id == user_id))
        return list((await ses.scalars(q.order_by(MemoryRow.created_at.desc()).limit(limit))).all())

    async def reconcile(self, tenant: str, agent_id: str, user_id: str | None, candidates: list[dict[str, Any]],
                        sources: list[str], actor: str) -> list[str]:
        """Merge candidate memories into the store. Returns ids of new memory rows."""
        cands = []
        for c in candidates:
            text = (c.get("content") or "").strip()
            if not text:
                continue
            if self.c.s.redact_pii:
                text = safety.redact(text)
            kind = c.get("kind") if c.get("kind") in ("fact", "preference", "lesson") else "fact"
            cands.append({"kind": kind, "content": clip(text, 1000),
                          "user_id": user_id if kind == "preference" else c.get("user_id", user_id)})
        if not cands:
            return []
        async with self.c.db.session() as ses:
            existing = await self.visible(ses, tenant, agent_id, user_id, limit=1000)
        hashes = {m.content_hash for m in existing}
        cands = [c for c in cands if content_hash(c["content"]) not in hashes]
        if not cands:
            return []

        embs = await self.c.embed([c["content"] for c in cands])
        neighbours: list[list[MemoryRow]] = []
        for i, c in enumerate(cands):
            e = embs[i] if embs else None
            scored = sorted(((m, similarity(c["content"], m.content, e, m.embedding)) for m in existing),
                            key=lambda x: x[1], reverse=True)
            neighbours.append([m for m, s in scored[:5] if s >= 0.2])

        decisions: list[dict[str, Any]]
        if not any(neighbours):
            decisions = [{"candidate": i, "action": "add", "target_id": "", "content": c["content"]}
                         for i, c in enumerate(cands)]
        else:
            payload = [{"candidate": i, "kind": c["kind"], "content": c["content"],
                        "similar_existing": [{"id": m.id, "kind": m.kind, "content": m.content} for m in nb]}
                       for i, (c, nb) in enumerate(zip(cands, neighbours, strict=True))]
            try:
                out = await self.c.llm_json(tenant, "memory", prompts.MEMORY_SYSTEM,
                                            json.dumps(payload, indent=1), prompts.MEMORY_SCHEMA)
                decisions = out.get("decisions", [])
            except (LLMError, BudgetExceeded) as e:
                # Never lose a memory: novel ones are added; ones that may duplicate or contradict an existing
                # memory are held for review (approve with POST /v1/memories/{id}/approve).
                log.warning("memory.reconcile_unavailable", error=str(e))
                decisions = [{"candidate": i, "action": "add", "target_id": "", "content": c["content"],
                              "hold": bool(neighbours[i])} for i, c in enumerate(cands)]

        by_id = {m.id: m for nb in neighbours for m in nb}
        created: list[str] = []
        texts: list[tuple[MemoryRow, str]] = []
        async with self.c.db.session() as ses:
            for d in decisions:
                i = d.get("candidate")
                if not isinstance(i, int) or not 0 <= i < len(cands) or d.get("action") == "noop":
                    continue
                c = cands[i]
                content = clip((d.get("content") or c["content"]).strip(), 1000)
                findings = safety.scan(content, 2000)
                status = ("active" if self.c.s.governance_mode == "auto" and self.c.s.auto_approve_memories
                          and not findings and not d.get("hold") else "pending")
                row = MemoryRow(id=new_id("mem"), tenant_id=tenant, agent_id=agent_id, user_id=c["user_id"],
                                kind=c["kind"], status=status, content=content, content_hash=content_hash(content),
                                sources=sources[:20])
                ses.add(row)
                texts.append((row, content))
                created.append(row.id)
                target = by_id.get(d.get("target_id") or "")
                if d.get("action") in ("update", "supersede") and target is not None and status == "active":
                    old = await ses.get(MemoryRow, target.id)
                    if old is not None and old.invalid_at is None:
                        old.invalid_at, old.superseded_by = utcnow(), row.id
                        row.sources = list(dict.fromkeys([*old.sources, *row.sources]))[-20:]
                self.c.audit(ses, tenant, actor, f"memory.{d.get('action')}", row.id, status=status,
                             target=d.get("target_id") or None, findings=findings)
                if status == "pending" and self.c.s.webhook_url and "memory.pending" in self.c.s.webhook_events:
                    await self.c.enqueue(ses, tenant, "webhook", {"event": "memory.pending", "ref": row.id})
                if c["user_id"]:
                    await self.mark_profile_dirty(ses, tenant, agent_id, c["user_id"])
            embs2 = await self.c.embed([t for _, t in texts])
            if embs2:
                for (r, _), e in zip(texts, embs2, strict=True):
                    r.embedding = e
            await ses.commit()
        return created

    async def mark_profile_dirty(self, ses: AsyncSession, tenant: str, agent_id: str, user_id: str) -> None:
        p = await ses.get(ProfileRow, (tenant, agent_id, user_id))
        if p is None:
            ses.add(ProfileRow(tenant_id=tenant, agent_id=agent_id, user_id=user_id, dirty=True))
        else:
            p.dirty = True

    async def refresh_profile(self, tenant: str, agent_id: str, user_id: str) -> str:
        async with self.c.db.session() as ses:
            # The card is about the person (preferences, role, style); environment facts stay as recallable memories.
            mems = [m for m in await self.visible(ses, tenant, agent_id, user_id, limit=300)
                    if m.user_id == user_id and m.kind == "preference"]
        if not mems:
            card, ids = "", []
        else:
            lines = "\n".join(f"- ({m.kind}, since {m.valid_from:%Y-%m-%d}) {m.content}" for m in mems)
            out = await self.c.llm_json(
                tenant, "profile", prompts.PROFILE_SYSTEM,
                f"Character limit: {self.c.s.profile_max_chars}\n\nMemories about the user:\n{lines}",
                prompts.PROFILE_SCHEMA)
            card, ids = clip(out.get("card", ""), self.c.s.profile_max_chars), [m.id for m in mems]
        async with self.c.db.session() as ses:
            p = await ses.get(ProfileRow, (tenant, agent_id, user_id))
            if p is None:
                p = ProfileRow(tenant_id=tenant, agent_id=agent_id, user_id=user_id)
                ses.add(p)
            p.card, p.source_memory_ids, p.dirty = card, ids, False
            await ses.commit()
        return card

    def candidates(self, mems: list[MemoryRow]) -> list[Candidate]:
        return [Candidate(m.id, m.content, m.embedding, prior=0.7 + 0.3 * m.confidence) for m in mems]
