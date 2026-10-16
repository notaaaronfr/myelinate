"""Myelinate gateway: REST + MCP + OTLP ingestion over one learning engine."""

import contextlib
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

import anthropic
import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text, update

from .. import __version__
from ..config import Settings, get_settings
from ..core import BudgetExceeded, Conflict, NotFound
from ..db import (
    ApiKey,
    AuditRow,
    ChangesetRow,
    Database,
    EpisodeRow,
    MemoryRow,
    ProfileRow,
    RecallRow,
    SkillRow,
    SkillVersionRow,
    UsageRow,
    new_id,
)
from ..embeddings import Embedder, HttpEmbedder
from ..engine import LearningEngine
from ..llm import LLM, AnthropicLLM, DisabledLLM, LLMError
from ..memory import valid_at
from ..models import (
    AGENT_ID,
    AskIn,
    AskOut,
    Bullet,
    Changeset,
    EpisodeIn,
    EpisodeOut,
    FeedbackIn,
    Memory,
    MemoryIn,
    RecallRequest,
    RecallResponse,
    ReviewIn,
    Skill,
    SkillImport,
    SkillIn,
)
from ..otel import decode_otlp, group_spans
from ..telemetry import configure_logging, configure_tracing, metrics
from ..worker import Worker
from .auth import ALL_SCOPES, Authenticator, AuthError, Principal, RateLimiter, generate_key, hash_key
from .mcp_server import build_mcp

log = structlog.get_logger()


class BodyLimit:
    """ASGI request-size guard that also covers chunked bodies (no Content-Length)."""

    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app, self.max = app, max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        too_big = JSONResponse({"detail": "request too large"}, status_code=413)
        if declared is not None and (not declared.isdigit() or int(declared) > self.max):
            return await too_big(scope, receive, send)
        seen = 0

        async def limited() -> Any:
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max:
                    raise HTTPException(413, "request too large")
            return msg

        await self.app(scope, limited, send)


class RollbackIn(BaseModel):
    version: int = Field(ge=1)


class PinIn(BaseModel):
    pinned: bool = True


class ReplayIn(BaseModel):
    limit: int = Field(default=200, le=5000)


class KeyIn(BaseModel):
    name: str
    scopes: list[str] = Field(default_factory=lambda: ["learn"])


def build_llm(s: Settings) -> LLM:
    if s.llm_provider == "anthropic":
        key = s.anthropic_api_key.get_secret_value() if s.anthropic_api_key else None
        return AnthropicLLM(s.llm_model, s.llm_effort, s.llm_max_tokens,
                            client=anthropic.AsyncAnthropic(api_key=key) if key else None)
    return DisabledLLM()


def build_embedder(s: Settings) -> Embedder | None:
    if not s.embeddings_url:
        return None
    key = s.embeddings_api_key.get_secret_value() if s.embeddings_api_key else None
    return HttpEmbedder(s.embeddings_url, s.embeddings_model, key)


def skill_out(sk: SkillRow, bullets: list[Any]) -> Skill:
    n = sk.alpha + sk.beta - 2
    return Skill(id=sk.id, agent_id=sk.agent_id, name=sk.name, description=sk.description, version=sk.version,
                 status=sk.status, pinned=sk.pinned, tags=sk.tags or [], uses=sk.uses,
                 success_rate=round(sk.alpha / (sk.alpha + sk.beta), 3) if n > 0 else None,
                 bullets=[Bullet.model_validate(b, from_attributes=True) for b in bullets],
                 created_at=sk.created_at, updated_at=sk.updated_at)


def create_app(settings: Settings | None = None, llm: LLM | None = None, embedder: Embedder | None = None) -> FastAPI:
    s = settings or get_settings()
    configure_logging(json_logs=s.env != "dev")
    db = Database(s.database_url, s.db_pool_size, s.db_max_overflow)
    engine = LearningEngine(db, s, llm or build_llm(s), embedder or build_embedder(s))
    authn = Authenticator(s, db)
    limiter = RateLimiter(s.rate_limit_per_minute)
    ip_limiter = RateLimiter(s.ip_rate_limit_per_minute)
    worker = Worker(engine)
    mcp = build_mcp(engine, authn, limiter)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=s.mcp_allowed_hosts,
                                                     allowed_origins=s.cors_origins))

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await db.create_all()
        if s.worker_enabled:
            worker.start()
        async with mcp.session_manager.run():
            yield
        await worker.stop()
        await db.close()

    app = FastAPI(title="Myelinate", version=__version__, lifespan=lifespan,
                  description="Plug-in self-improvement for any agent framework: skills, memory, reflection, "
                              "evolution, canary rollout and governance.")
    app.state.engine, app.state.worker, app.state.db = engine, worker, db
    if s.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_methods=["*"], allow_headers=["*"])
    app.router.routes.extend(mcp_app.routes)
    app.add_middleware(BodyLimit, max_bytes=s.max_request_bytes)

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get("x-request-id") or new_id("req")
        structlog.contextvars.bind_contextvars(request_id=rid)
        try:
            ip_limiter.check(f"ip:{request.client.host if request.client else 'unknown'}")  # before auth
        except AuthError as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status, headers={"Retry-After": "60"})
        if request.url.path.startswith("/mcp"):  # MCP requires credentials up front, not just per tool call
            try:
                limiter.check((await authn.authenticate(request.headers)).subject)
            except AuthError as e:
                headers = {"WWW-Authenticate": "Bearer"} if e.status == 401 else None
                return JSONResponse({"detail": e.detail}, status_code=e.status, headers=headers)
        t0 = time.perf_counter()
        try:
            resp: Response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id", "tenant")
        route = getattr(request.scope.get("route"), "path", "unmatched")
        metrics.http.labels(route=route, method=request.method, status=resp.status_code).observe(time.perf_counter() - t0)
        resp.headers["x-request-id"] = rid
        return resp

    for exc, code in ((NotFound, 404), (Conflict, 409)):
        app.add_exception_handler(exc, lambda _r, e, code=code: JSONResponse({"detail": str(e)}, status_code=code))

    @app.exception_handler(AuthError)
    async def _auth(_: Request, e: AuthError) -> JSONResponse:
        headers = {"Retry-After": "60"} if e.status == 429 else None
        return JSONResponse({"detail": e.detail}, status_code=e.status, headers=headers)

    @app.exception_handler(LLMError)
    async def _llm(_: Request, e: LLMError) -> JSONResponse:
        return JSONResponse({"detail": f"learning model unavailable: {e}"}, status_code=503,
                            headers={"Retry-After": "30"})

    @app.exception_handler(BudgetExceeded)
    async def _budget(_: Request, e: BudgetExceeded) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=429, headers={"Retry-After": "3600"})

    def need(scope: str):  # type: ignore[no-untyped-def]
        async def dep(request: Request) -> Principal:
            p = await authn.authenticate(request.headers)
            limiter.check(p.subject)
            p.require(scope)
            structlog.contextvars.bind_contextvars(tenant=p.tenant_id)
            return p
        return Depends(dep)

    Learn = Annotated[Principal, need("learn")]
    Review = Annotated[Principal, need("review")]
    Admin = Annotated[Principal, need("admin")]

    async def audit(p: Principal, action: str, resource: str, **details: Any) -> None:
        async with db.session() as ses:
            ses.add(AuditRow(id=new_id("aud"), tenant_id=p.tenant_id, actor=p.subject, action=action,
                             resource=resource, details=details))
            await ses.commit()

    async def enqueue(p: Principal, kind: str, payload: dict[str, Any], key: str) -> None:
        async with db.session() as ses:
            await engine.c.enqueue(ses, p.tenant_id, kind, payload, dedupe_key=key)
            await ses.commit()

    # ---------------------------------------------------------------- health

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict[str, str]:
        async with db.session() as ses:
            await ses.execute(text("SELECT 1"))
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    async def prom() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ---------------------------------------------------------------- learning loop (hot path)

    @app.post("/v1/recall", response_model=RecallResponse, tags=["loop"])
    async def recall(req: RecallRequest, p: Learn) -> RecallResponse:
        """Retrieve learned skills, memories and the user's profile for a task, as ready-to-inject instructions."""
        return await engine.recall(p.tenant_id, req)

    @app.post("/v1/episodes", response_model=EpisodeOut, tags=["loop"])
    async def record(ep: EpisodeIn, p: Learn) -> EpisodeOut:
        """Record a finished run (messages, tool calls, outcome, verification) so the agent can learn from it."""
        return await engine.record(p.tenant_id, ep)

    @app.post("/v1/episodes:batch", tags=["loop"])
    async def record_batch(eps: list[EpisodeIn], p: Learn) -> dict[str, Any]:
        """Bulk import of historical runs (offline warm-up: ACE showed warm-starting lifts online learning)."""
        if len(eps) > 1000:
            raise HTTPException(413, "max 1000 episodes per batch")
        ids = [(await engine.record(p.tenant_id, e)).episode_id for e in eps]
        return {"episode_ids": ids}

    @app.get("/v1/episodes/latest", tags=["loop"])
    async def latest_episode(p: Learn, session_id: Annotated[str, Query(max_length=256)],
                             agent_id: str = "default") -> dict[str, Any]:
        """Most recent episode of a session, so stateless clients can attach feedback to 'the last turn'."""
        async with db.session() as ses:
            row = await ses.scalar(select(EpisodeRow).where(EpisodeRow.tenant_id == p.tenant_id,
                                                            EpisodeRow.agent_id == agent_id,
                                                            EpisodeRow.session_id == session_id)
                                   .order_by(EpisodeRow.created_at.desc()).limit(1))
        if row is None:
            raise NotFound(session_id)
        return {"episode_id": row.id, "task": row.task, "created_at": row.created_at}

    @app.post("/v1/feedback", status_code=204, tags=["loop"])
    async def feedback(fb: FeedbackIn, p: Learn) -> None:
        """Rate a recorded episode (0-1), optionally with a comment or the correct answer."""
        await engine.feedback(p.tenant_id, p.subject, fb)

    async def _otlp(request: Request, p: Principal) -> dict[str, Any]:
        override = request.headers.get("x-agent-id")
        if override and not re.match(AGENT_ID, override):
            raise HTTPException(400, f"invalid X-Agent-Id (must match {AGENT_ID})")
        try:
            payload = decode_otlp(await request.body(), request.headers.get("content-type") or "",
                                  request.headers.get("content-encoding") or "", s.max_request_bytes)
        except LookupError as e:
            raise HTTPException(415, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        out = []
        for trace_id, spans in group_spans(payload).items():
            # Spans of one trace often arrive in several exports (children before the root): never drop them.
            try:
                eid = await engine.ingest_trace(p.tenant_id, trace_id, spans, override)
            except (ValueError, KeyError, TypeError) as e:  # one malformed trace must not fail the batch
                log.warning("otel.trace_skipped", trace_id=trace_id, error=str(e))
                continue
            if eid and eid not in out:
                out.append(eid)
        return {"partialSuccess": {}, "episode_ids": out}

    @app.post("/v1/traces", tags=["loop"])
    async def otlp_traces(request: Request, p: Learn) -> dict[str, Any]:
        """Zero-code ingestion of OpenTelemetry GenAI / OpenInference traces."""
        return await _otlp(request, p)

    @app.post("/otlp/v1/traces", include_in_schema=False)
    async def otlp_traces_alias(request: Request, p: Learn) -> dict[str, Any]:  # OTEL_EXPORTER_OTLP_ENDPOINT=<gw>/otlp
        return await _otlp(request, p)

    @app.post("/v1/ask", response_model=AskOut, tags=["loop"])
    async def ask(req: AskIn, p: Learn) -> AskOut:
        """Answer a question from learned evidence only (skills, memories, profile), with cited evidence ids."""
        return await engine.ask(p.tenant_id, req)

    # ---------------------------------------------------------------- skills

    @app.get("/v1/skills", response_model=list[Skill], tags=["skills"])
    async def list_skills(p: Learn, agent_id: str = "default", status: str | None = None) -> list[Skill]:
        """List an agent's skills (plus tenant-shared ones) with their rules and success rates."""
        async with db.session() as ses:
            q = select(SkillRow).where(SkillRow.tenant_id == p.tenant_id, SkillRow.agent_id.in_([agent_id, "*"]))
            if status:
                q = q.where(SkillRow.status == status)
            rows = (await ses.scalars(q.order_by(SkillRow.name))).all()
            return [skill_out(r, await engine.gov.active_bullets_rows(ses, r.id)) for r in rows]

    @app.get("/v1/skills/{ref}", response_model=Skill, tags=["skills"])
    async def get_skill(ref: str, p: Learn, agent_id: str = "default") -> Skill:
        """Get one skill by id or name."""
        sk, bl = await engine.get_skill(p.tenant_id, agent_id, ref)
        return skill_out(sk, bl)

    @app.get("/v1/skills/{ref}/SKILL.md", response_class=PlainTextResponse, tags=["skills"])
    async def skill_md(ref: str, p: Learn, agent_id: str = "default") -> str:
        """Export a skill as an agentskills.io-compatible SKILL.md."""
        return await engine.skill_markdown(p.tenant_id, agent_id, ref)

    @app.get("/v1/skills/{ref}/versions", tags=["skills"])
    async def skill_versions(ref: str, p: Learn, agent_id: str = "default") -> list[dict[str, Any]]:
        """List a skill's immutable version history."""
        sk, _ = await engine.get_skill(p.tenant_id, agent_id, ref)
        async with db.session() as ses:
            rows = (await ses.scalars(select(SkillVersionRow).where(SkillVersionRow.skill_id == sk.id)
                                      .order_by(SkillVersionRow.version.desc()))).all()
        return [{"version": v.version, "source": v.source, "changeset_id": v.changeset_id, "created_by": v.created_by,
                 "created_at": v.created_at, "description": v.description, "bullets": v.bullets} for v in rows]

    @app.post("/v1/skills", tags=["skills"])
    async def author_skill(body: SkillIn, p: Review) -> dict[str, Any]:
        """Create or replace a skill by hand (applied through a governed changeset)."""
        cs = await engine.author_skill(p.tenant_id, p.subject, body)
        return {"changeset_id": cs.id if cs else None, "status": cs.status if cs else "unchanged"}

    @app.post("/v1/skills/import", tags=["skills"])
    async def import_skill(body: SkillImport, p: Review) -> dict[str, Any]:
        """Bring existing SKILL.md files (Anthropic / Hermes / agentskills.io) under learning and governance."""
        cs = await engine.import_skill(p.tenant_id, p.subject, body)
        return {"changeset_id": cs.id if cs else None, "status": cs.status if cs else "unchanged"}

    @app.post("/v1/skills/{skill_id}/rollback", tags=["skills"])
    async def rollback(skill_id: str, body: RollbackIn, p: Review) -> dict[str, int]:
        """Restore a skill to an earlier version (recorded as a new version)."""
        return {"version": await engine.gov.rollback(p.tenant_id, p.subject, skill_id, body.version)}

    @app.post("/v1/skills/{skill_id}/pin", tags=["skills"])
    async def pin(skill_id: str, body: PinIn, p: Review) -> dict[str, bool]:
        """Pin or unpin a skill. Pinned skills are never archived by the lifecycle."""
        async with db.session() as ses:
            res = await ses.execute(update(SkillRow).where(SkillRow.id == skill_id, SkillRow.tenant_id == p.tenant_id)
                                    .values(pinned=body.pinned))
            if not res.rowcount:
                raise NotFound(skill_id)
            await ses.commit()
        await audit(p, "skill.pin", skill_id, pinned=body.pinned)
        return {"pinned": body.pinned}

    @app.post("/v1/skills/{skill_id}/evolve", status_code=202, tags=["skills"])
    async def trigger_evolution(skill_id: str, p: Review) -> dict[str, str]:
        """Queue an evolution run for a skill."""
        async with db.session() as ses:
            sk = await ses.get(SkillRow, skill_id)
            if sk is None or sk.tenant_id != p.tenant_id:
                raise NotFound(skill_id)
        await enqueue(p, "evolve", {"skill_id": skill_id}, f"evolve:{skill_id}")
        return {"status": "scheduled"}

    # ---------------------------------------------------------------- memories & profiles

    @app.post("/v1/memories", tags=["memory"])
    async def add_memory(m: MemoryIn, p: Learn) -> dict[str, list[str]]:
        """Add a memory. It is reconciled against existing memories (add, update, supersede or no-op)."""
        return {"memory_ids": await engine.add_memory(p.tenant_id, p.subject, m)}

    @app.get("/v1/memories", response_model=list[Memory], tags=["memory"])
    async def list_memories(p: Learn, agent_id: str = "default", user_id: str | None = None,
                            as_of: datetime | None = None, include_history: bool = False, status: str = "active",
                            limit: Annotated[int, Query(le=1000)] = 200) -> list[Memory]:
        """List memories, optionally for a user, at a point in time (`as_of`) or with superseded history."""
        async with db.session() as ses:
            q = select(MemoryRow).where(MemoryRow.tenant_id == p.tenant_id, MemoryRow.agent_id == agent_id,
                                        MemoryRow.status == status)
            if user_id:
                q = q.where(MemoryRow.user_id == user_id)
            if not include_history:
                q = q.where(valid_at(as_of))
            rows = (await ses.scalars(q.order_by(MemoryRow.created_at.desc()).limit(limit))).all()
        return [Memory.model_validate(r, from_attributes=True) for r in rows]

    @app.post("/v1/memories/{memory_id}/approve", tags=["memory"])
    async def approve_memory(memory_id: str, p: Review) -> dict[str, str]:
        """Approve a memory held for review (e.g. flagged by the safety scan)."""
        async with db.session() as ses:
            m = await ses.get(MemoryRow, memory_id)
            if m is None or m.tenant_id != p.tenant_id:
                raise NotFound(memory_id)
            m.status = "active"
            await ses.commit()
        await audit(p, "memory.approved", memory_id)
        return {"status": "active"}

    @app.delete("/v1/memories/{memory_id}", status_code=204, tags=["memory"])
    async def delete_memory(memory_id: str, p: Review) -> None:
        """Hard delete (e.g. data-subject request). Learning itself never hard-deletes; it supersedes."""
        async with db.session() as ses:
            res = await ses.execute(delete(MemoryRow).where(MemoryRow.id == memory_id,
                                                            MemoryRow.tenant_id == p.tenant_id))
            if not res.rowcount:
                raise NotFound(memory_id)
            await ses.commit()
        await audit(p, "memory.deleted", memory_id)

    @app.get("/v1/profiles/{agent_id}/{user_id}", tags=["memory"])
    async def get_profile(agent_id: str, user_id: str, p: Learn) -> dict[str, Any]:
        """Get the profile card for a user of an agent."""
        async with db.session() as ses:
            row = await ses.get(ProfileRow, (p.tenant_id, agent_id, user_id))
        if row is None:
            raise NotFound(user_id)
        return {"card": row.card, "updated_at": row.updated_at, "stale": row.dirty}

    # ---------------------------------------------------------------- governance

    @app.get("/v1/changesets", response_model=list[Changeset], tags=["governance"])
    async def list_changesets(p: Review, status: str | None = "pending", agent_id: str | None = None,
                              limit: Annotated[int, Query(le=500)] = 100) -> list[Changeset]:
        """List changesets (proposed skill changes), by default those pending review."""
        async with db.session() as ses:
            q = select(ChangesetRow).where(ChangesetRow.tenant_id == p.tenant_id)
            if status:
                q = q.where(ChangesetRow.status == status)
            if agent_id:
                q = q.where(ChangesetRow.agent_id == agent_id)
            rows = (await ses.scalars(q.order_by(ChangesetRow.created_at.desc()).limit(limit))).all()
        return [Changeset.model_validate(r, from_attributes=True) for r in rows]

    @app.post("/v1/changesets/{cs_id}/review", tags=["governance"])
    async def review(cs_id: str, body: ReviewIn, p: Review) -> dict[str, str]:
        """Approve or reject a pending changeset. Risky changes to proven skills start a canary unless skipped."""
        return {"status": await engine.gov.review(p.tenant_id, p.subject, cs_id, body.approve, body.comment,
                                                  body.skip_canary)}

    @app.post("/v1/agents/{agent_id}/reflect", status_code=202, tags=["governance"])
    async def trigger_reflection(agent_id: str, p: Review) -> dict[str, str]:
        """Queue reflection over an agent's unreflected episodes now."""
        await enqueue(p, "reflect", {"agent_id": agent_id}, f"reflect:{p.tenant_id}:{agent_id}")
        return {"status": "scheduled"}

    @app.post("/v1/agents/{agent_id}/curate", status_code=202, tags=["governance"])
    async def trigger_curation(agent_id: str, p: Review) -> dict[str, str]:
        """Queue library upkeep for an agent now (dedup, lifecycle, nomination, profiles)."""
        await enqueue(p, "curate", {"agent_id": agent_id}, f"curate:{p.tenant_id}:{agent_id}")
        return {"status": "scheduled"}

    @app.post("/v1/agents/{agent_id}/replay", status_code=202, tags=["governance"])
    async def replay(agent_id: str, body: ReplayIn, p: Review) -> dict[str, Any]:
        """Re-learn from stored episodes (another epoch), e.g. after importing history or changing the model."""
        async with db.session() as ses:
            ids = (await ses.scalars(select(EpisodeRow.id).where(EpisodeRow.tenant_id == p.tenant_id,
                                                                 EpisodeRow.agent_id == agent_id)
                                     .order_by(EpisodeRow.created_at.desc()).limit(body.limit))).all()
            if ids:
                await ses.execute(update(EpisodeRow).where(EpisodeRow.id.in_(ids)).values(reflected=False))
            await ses.commit()
        await enqueue(p, "reflect", {"agent_id": agent_id}, f"reflect:{p.tenant_id}:{agent_id}")
        return {"episodes": len(ids), "status": "scheduled"}

    @app.get("/v1/agents/{agent_id}/insights", tags=["governance"])
    async def insights(agent_id: str, p: Learn) -> dict[str, Any]:
        """Learning lift vs the holdout group, per-skill success rates, canaries, problem rules and learning cost."""
        return await engine.insights(p.tenant_id, agent_id)

    @app.get("/v1/agents/{agent_id}/export", tags=["governance"])
    async def export(agent_id: str, p: Review, limit: Annotated[int, Query(le=100000)] = 10000) -> StreamingResponse:
        """Export episodes with rewards as JSON Lines (for analytics, RL or fine-tuning)."""
        await audit(p, "episodes.exported", agent_id, limit=limit)
        return StreamingResponse(engine.export(p.tenant_id, agent_id, limit), media_type="application/x-ndjson")

    # ---------------------------------------------------------------- admin

    @app.post("/v1/admin/api-keys", tags=["admin"])
    async def create_key(body: KeyIn, p: Admin) -> dict[str, Any]:
        """Create a tenant API key with the given scopes. The key is only shown once."""
        bad = set(body.scopes) - ALL_SCOPES
        if bad:
            raise HTTPException(400, f"unknown scopes {sorted(bad)}")
        raw = generate_key()
        row = ApiKey(id=new_id("key"), tenant_id=p.tenant_id, name=body.name, key_hash=hash_key(raw), scopes=body.scopes)
        async with db.session() as ses:
            ses.add(row)
            await ses.commit()
        await audit(p, "apikey.created", row.id, scopes=body.scopes, name=body.name)
        return {"id": row.id, "key": raw, "scopes": body.scopes, "note": "store this key now; it is not shown again"}

    @app.delete("/v1/admin/api-keys/{key_id}", status_code=204, tags=["admin"])
    async def revoke_key(key_id: str, p: Admin) -> None:
        """Revoke a tenant API key."""
        async with db.session() as ses:
            row = await ses.get(ApiKey, key_id)
            if row is None or row.tenant_id != p.tenant_id:
                raise NotFound(key_id)
            row.revoked = True
            await ses.commit()
        await audit(p, "apikey.revoked", key_id)

    @app.get("/v1/admin/audit", tags=["admin"])
    async def audit_log(p: Admin, limit: Annotated[int, Query(le=1000)] = 200) -> list[dict[str, Any]]:
        """List the tenant's audit log, newest first."""
        async with db.session() as ses:
            rows = (await ses.scalars(select(AuditRow).where(AuditRow.tenant_id == p.tenant_id)
                                      .order_by(AuditRow.created_at.desc()).limit(limit))).all()
        return [{"id": r.id, "actor": r.actor, "action": r.action, "resource": r.resource, "details": r.details,
                 "at": r.created_at} for r in rows]

    @app.get("/v1/admin/usage", tags=["admin"])
    async def usage(p: Admin) -> list[dict[str, Any]]:
        """Learning-side LLM usage per day and purpose (for budgets and chargeback)."""
        async with db.session() as ses:
            rows = (await ses.scalars(select(UsageRow).where(UsageRow.tenant_id == p.tenant_id)
                                      .order_by(UsageRow.day.desc()).limit(500))).all()
        return [{"day": r.day, "purpose": r.purpose, "calls": r.calls, "input_tokens": r.input_tokens,
                 "output_tokens": r.output_tokens} for r in rows]

    @app.delete("/v1/admin/users/{user_id}", tags=["admin"])
    async def erase_user(user_id: str, p: Admin) -> dict[str, int]:
        """Right to erasure: memories, profile and episodes of a user in this tenant."""
        async with db.session() as ses:
            m = await ses.execute(delete(MemoryRow).where(MemoryRow.tenant_id == p.tenant_id,
                                                          MemoryRow.user_id == user_id))
            e = await ses.execute(delete(EpisodeRow).where(EpisodeRow.tenant_id == p.tenant_id,
                                                           EpisodeRow.user_id == user_id))
            await ses.execute(delete(ProfileRow).where(ProfileRow.tenant_id == p.tenant_id,
                                                       ProfileRow.user_id == user_id))
            await ses.execute(delete(RecallRow).where(RecallRow.tenant_id == p.tenant_id,
                                                      RecallRow.user_id == user_id))
            await ses.commit()
        await audit(p, "user.erased", user_id, memories=m.rowcount, episodes=e.rowcount)
        return {"memories": m.rowcount or 0, "episodes": e.rowcount or 0}

    configure_tracing(app)
    return app
