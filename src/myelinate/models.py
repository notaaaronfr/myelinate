"""Framework-neutral wire contract shared by the gateway, SDKs and adapters."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

SECTIONS = ("steps", "pitfalls", "heuristics", "verification", "snippets", "notes")
Section = Literal["steps", "pitfalls", "heuristics", "verification", "snippets", "notes"]
SKILL_NAME = r"^[a-z0-9][a-z0-9-]{1,63}$"


AGENT_ID = r"^[A-Za-z0-9*][A-Za-z0-9._:@/-]{0,127}$"


class Scope(BaseModel):
    """tenant_id always comes from the authenticated principal, never the body."""

    agent_id: str = Field(default="default", pattern=AGENT_ID)
    user_id: str | None = Field(default=None, max_length=256)


class Message(BaseModel):
    role: str
    content: str


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] | str | None = None
    result: str | None = None
    error: str | None = None


class Outcome(StrEnum):
    success = "success"
    failure = "failure"
    partial = "partial"
    unknown = "unknown"


class Verification(BaseModel):
    """Ground-truth signal from tests, validators or business checks. The strongest learning signal there is."""

    passed: bool
    details: str | None = None
    source: str = "verifier"


# ---------------------------------------------------------------- recall


class RecallRequest(BaseModel):
    scope: Scope = Field(default_factory=Scope)
    query: str = Field(max_length=100_000)
    session_id: str | None = Field(default=None, max_length=256)
    max_skills: int | None = None
    max_memories: int | None = None
    as_of: datetime | None = None  # point-in-time view for audits / replays
    include_profile: bool = True


class BulletRef(BaseModel):
    id: str
    section: str
    content: str


class SkillRef(BaseModel):
    id: str
    name: str
    description: str
    version: int
    score: float
    arm: str | None = None  # "incumbent" | "candidate" when a canary is running
    bullets: list[BulletRef] | None = None  # only for skills loaded in full


class MemoryRef(BaseModel):
    id: str
    kind: str
    content: str
    score: float
    valid_from: datetime


class RecallResponse(BaseModel):
    recall_id: str
    instructions: str  # ready-to-inject context block
    skills: list[SkillRef]
    skill_index: list[SkillRef]  # progressive disclosure: names + descriptions only
    memories: list[MemoryRef]
    profile: str | None = None
    holdout: bool = False  # this run is in the control group (no learned knowledge injected)


# ---------------------------------------------------------------- record


class EpisodeIn(BaseModel):
    scope: Scope = Field(default_factory=Scope)
    idempotency_key: str | None = Field(default=None, max_length=256)  # set by SDKs; retries don't duplicate
    session_id: str | None = Field(default=None, max_length=256)
    recall_id: str | None = Field(default=None, max_length=64)
    task: str | None = None
    messages: list[Message] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    outcome: Outcome = Outcome.unknown
    verification: Verification | None = None
    error: str | None = None
    duration_ms: int | None = None
    framework: str | None = Field(default=None, max_length=64)
    skills_loaded: list[str] = Field(default_factory=list)  # names loaded via skill_view / gen_ai.skill.name
    metadata: dict[str, Any] = Field(default_factory=dict)


class EpisodeOut(BaseModel):
    episode_id: str
    reflection_scheduled: bool


class FeedbackIn(BaseModel):
    episode_id: str = Field(max_length=64)
    score: float = Field(ge=0.0, le=1.0)
    comment: str | None = None
    correction: str | None = None  # what the right answer/approach was; the richest feedback
    source: str = "user"


# ---------------------------------------------------------------- knowledge


class BulletIn(BaseModel):
    section: Section = "steps"
    content: str = Field(min_length=3, max_length=2000)
    issue: str = ""
    keywords: list[str] = Field(default_factory=list)


class Bullet(BaseModel):
    id: str
    section: str
    content: str
    issue: str
    keywords: list[str]
    helpful: int
    harmful: int
    neutral: int
    used: int
    status: str
    sources: list[str]


class Skill(BaseModel):
    id: str
    agent_id: str
    name: str
    description: str
    version: int
    status: str
    pinned: bool = False
    tags: list[str]
    uses: int
    success_rate: float | None
    bullets: list[Bullet]
    created_at: datetime
    updated_at: datetime


class SkillIn(BaseModel):
    agent_id: str = Field(default="default", pattern=AGENT_ID)
    name: str = Field(pattern=SKILL_NAME)
    description: str = Field(max_length=1024)
    bullets: list[BulletIn] | None = None
    body: str | None = None  # markdown alternative; list items become bullets, headings become sections
    tags: list[str] = Field(default_factory=list)


class SkillImport(BaseModel):
    """Import an existing SKILL.md (Anthropic / Hermes / agentskills.io format)."""

    agent_id: str = Field(default="default", pattern=AGENT_ID)
    markdown: str = Field(max_length=200_000)


class MemoryKind(StrEnum):
    fact = "fact"
    preference = "preference"
    lesson = "lesson"


class MemoryIn(BaseModel):
    scope: Scope = Field(default_factory=Scope)
    kind: MemoryKind = MemoryKind.fact
    content: str = Field(max_length=4000)


class Memory(BaseModel):
    id: str
    agent_id: str
    user_id: str | None
    kind: str
    content: str
    confidence: float
    valid_from: datetime
    invalid_at: datetime | None
    superseded_by: str | None
    sources: list[str]


class Changeset(BaseModel):
    id: str
    agent_id: str
    status: str
    ops: list[dict[str, Any]]
    rationale: str
    source: str
    risk: str
    eval_score: float | None
    baseline_score: float | None
    safety_findings: list[str]
    episode_ids: list[str]
    reviewer: str | None
    review_comment: str | None
    created_at: datetime


class ReviewIn(BaseModel):
    approve: bool
    comment: str | None = None
    skip_canary: bool = False


class AskIn(BaseModel):
    scope: Scope = Field(default_factory=Scope)
    question: str


class AskOut(BaseModel):
    answer: str
    evidence: list[str]
