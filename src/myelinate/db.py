"""Persistence schema. Works on SQLite (dev / embedded) and PostgreSQL (prod). Every row is tenant-scoped.

Knowledge model
- Skill: a named procedure (portable as SKILL.md). Its content is a set of *bullets*.
- Bullet: one atomic, individually-tracked rule ("When <trigger>, <action>") with helpful/harmful/neutral
  counters and provenance. All learning edits are typed deltas on bullets, never whole-document rewrites
  (whole rewrites cause context collapse; ACE, ICLR 2026).
- Memory: a bi-temporal fact. Contradictions close the old fact (invalid_at) instead of deleting it.
- Changeset: a governed batch of delta ops (the unit of review, versioning, canary and rollback).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def short_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON, datetime: DateTime(timezone=True), date: Date}


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    scopes: Mapped[list[Any]] = mapped_column(JSON)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SkillRow(Base):
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("tenant_id", "agent_id", "name"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)  # "*" = shared across the tenant's agents
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)  # when to use it; drives retrieval
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | stale | archived | deprecated
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)  # exempt from lifecycle archival
    created_by: Mapped[str] = mapped_column(String(256), default="agent")
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    embedding: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    successes: Mapped[float] = mapped_column(Float, default=0.0)
    alpha: Mapped[float] = mapped_column(Float, default=1.0)  # Beta posterior over outcome when used
    beta: Mapped[float] = mapped_column(Float, default=1.0)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_evolved_uses: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class BulletRow(Base):
    __tablename__ = "bullets"
    __table_args__ = (Index("ix_bullet_skill", "skill_id", "status"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # short, citable: b-1a2b3c4d
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    skill_id: Mapped[str] = mapped_column(String(64))
    section: Mapped[str] = mapped_column(String(32))  # steps | pitfalls | heuristics | verification | snippets
    content: Mapped[str] = mapped_column(Text)  # the only text injected
    issue: Mapped[str] = mapped_column(Text, default="")  # retrieval/diagnosis metadata, never injected
    keywords: Mapped[list[Any]] = mapped_column(JSON, default=list)
    position: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | removed
    helpful: Mapped[int] = mapped_column(Integer, default=0)
    harmful: Mapped[int] = mapped_column(Integer, default=0)
    neutral: Mapped[int] = mapped_column(Integer, default=0)
    used: Mapped[int] = mapped_column(Integer, default=0)
    sources: Mapped[list[Any]] = mapped_column(JSON, default=list)  # provenance: episode ids
    embedding: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class SkillVersionRow(Base):
    """Immutable snapshot of a skill's rendered content after each applied changeset."""

    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("skill_id", "version"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    skill_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    bullets: Mapped[list[Any]] = mapped_column(JSON)  # [{id, section, content, issue, keywords, position}]
    changeset_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class MemoryRow(Base):
    __tablename__ = "memories"
    __table_args__ = (Index("ix_mem_scope", "tenant_id", "agent_id", "user_id", "invalid_at"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    agent_id: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))  # fact | preference | lesson
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | pending (awaiting review)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    sources: Mapped[list[Any]] = mapped_column(JSON, default=list)
    embedding: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    valid_from: Mapped[datetime] = mapped_column(default=utcnow)
    invalid_at: Mapped[datetime | None] = mapped_column(nullable=True)  # closed by contradiction / update
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ProfileRow(Base):
    """Compact per-user card (Honcho-style) rebuilt from preference memories by the curation job."""

    __tablename__ = "profiles"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    card: Mapped[str] = mapped_column(Text, default="")
    source_memory_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    dirty: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class EpisodeRow(Base):
    __tablename__ = "episodes"
    __table_args__ = (Index("ix_ep_scope", "tenant_id", "agent_id", "created_at"),
                      UniqueConstraint("tenant_id", "idempotency_key"))
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(256), nullable=True)  # safe client retries
    tenant_id: Mapped[str] = mapped_column(String(128))
    agent_id: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    recall_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task: Mapped[str | None] = mapped_column(Text, nullable=True)
    messages: Mapped[list[Any]] = mapped_column(JSON)
    tool_calls: Mapped[list[Any]] = mapped_column(JSON)
    outcome: Mapped[str] = mapped_column(String(16))  # as reported
    judged_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)  # self-judge when unknown
    judge_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    framework: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    served: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{skill_id, version, arm}] from recall
    bullet_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)  # injected bullets
    cited_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)  # bullets the agent cited
    holdout: Mapped[bool] = mapped_column(Boolean, default=False)
    feedback_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    reflection: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reflected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class RecallRow(Base):
    __tablename__ = "recalls"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[str] = mapped_column(String(128))
    session_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    query: Mapped[str] = mapped_column(Text)
    served: Mapped[list[Any]] = mapped_column(JSON)
    bullet_ids: Mapped[list[Any]] = mapped_column(JSON)
    memory_ids: Mapped[list[Any]] = mapped_column(JSON)
    holdout: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ChangesetRow(Base):
    __tablename__ = "changesets"
    __table_args__ = (Index("ix_cs_status", "tenant_id", "status"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    agent_id: Mapped[str] = mapped_column(String(128))
    # pending -> (approved) -> canary -> applied | rejected | superseded | failed
    status: Mapped[str] = mapped_column(String(16), default="pending")
    ops: Mapped[list[Any]] = mapped_column(JSON)
    rationale: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32))  # reflection | evolution | curation | api | replay
    risk: Mapped[str] = mapped_column(String(16), default="low")  # low | medium | high
    eval_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    safety_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    base_versions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # skill_id -> version
    episode_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    reviewer: Mapped[str | None] = mapped_column(String(256), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_versions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Rejected-edit buffer (SkillOpt): why it was rejected, fed back into later curation/evolution prompts.
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ExperimentRow(Base):
    """Canary of a changeset against the incumbent skill version, decided by Bayesian posterior."""

    __tablename__ = "experiments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    skill_id: Mapped[str] = mapped_column(String(64), index=True)
    changeset_id: Mapped[str] = mapped_column(String(64))
    base_version: Mapped[int] = mapped_column(Integer)
    candidate_bullets: Mapped[list[Any]] = mapped_column(JSON)
    candidate_description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | promoted | rejected | expired
    inc_alpha: Mapped[float] = mapped_column(Float, default=1.0)
    inc_beta: Mapped[float] = mapped_column(Float, default=1.0)
    cand_alpha: Mapped[float] = mapped_column(Float, default=1.0)
    cand_beta: Mapped[float] = mapped_column(Float, default=1.0)
    p_better: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)


class PairDecisionRow(Base):
    """Remembered KEEP decisions for similar bullet pairs, so curation doesn't re-litigate them."""

    __tablename__ = "pair_decisions"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    a: Mapped[str] = mapped_column(String(32), primary_key=True)
    b: Mapped[str] = mapped_column(String(32), primary_key=True)
    decision: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AgentStateRow(Base):
    __tablename__ = "agent_state"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    episodes_since_reflect: Mapped[int] = mapped_column(Integer, default=0)
    episodes_since_curate: Mapped[int] = mapped_column(Integer, default=0)
    holdout_alpha: Mapped[float] = mapped_column(Float, default=1.0)
    holdout_beta: Mapped[float] = mapped_column(Float, default=1.0)
    treated_alpha: Mapped[float] = mapped_column(Float, default=1.0)
    treated_beta: Mapped[float] = mapped_column(Float, default=1.0)


class UsageRow(Base):
    """Learning-side LLM spend per tenant per day, for budgets and chargeback."""

    __tablename__ = "llm_usage"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    purpose: Mapped[str] = mapped_column(String(32), primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)


class JobRow(Base):
    """Durable job queue. Survives restarts; safe with multiple gateway replicas."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"), Index("ix_jobs_dedupe", "dedupe_key", "status"))
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    dedupe_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(default=utcnow)
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuditRow(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_scope", "tenant_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    actor: Mapped[str] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(64))
    resource: Mapped[str] = mapped_column(String(512))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Database:
    def __init__(self, url: str) -> None:
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if not url.startswith("sqlite"):
            kwargs |= {"pool_size": 10, "max_overflow": 20}
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        self.session: async_sessionmaker[AsyncSession] = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()
