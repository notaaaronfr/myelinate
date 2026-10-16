"""Shared services for the learning subsystems: DB, LLM with budgets, embeddings, jobs, audit."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .db import AgentStateRow, AuditRow, Database, JobRow, UsageRow, new_id, utcnow
from .embeddings import Embedder
from .llm import LLM
from .retrieval import HybridRetriever
from .telemetry import metrics

log = structlog.get_logger()


class BudgetExceeded(RuntimeError):
    """Deliberately not an LLMError: per-item handlers must not swallow it; the job is deferred instead."""


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


def clip(text: str | None, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n // 2] + "\n…[truncated]…\n" + text[-n // 2 :]


class Core:
    def __init__(self, db: Database, settings: Settings, llm: LLM, embedder: Embedder | None = None) -> None:
        self.db, self.s, self.llm, self.embedder = db, settings, llm, embedder
        self.retriever = HybridRetriever()

    # ---------------------------------------------------------------- LLM with metering

    async def llm_json(self, tenant: str, purpose: str, system: str, prompt: str,
                       schema: dict[str, Any]) -> dict[str, Any]:
        if self.s.daily_token_budget > 0 and await self.tokens_today(tenant) >= self.s.daily_token_budget:
            raise BudgetExceeded(f"tenant {tenant} exceeded its daily learning token budget")
        used = [0, 0]

        def sink(i: int, o: int) -> None:
            used[0] += i
            used[1] += o

        try:
            return await self.llm.complete_json(system, prompt, schema, usage=sink)
        finally:
            if used != [0, 0]:
                await self._meter(tenant, purpose, used[0], used[1])
            metrics.llm_calls.labels(purpose=purpose).inc()

    async def _meter(self, tenant: str, purpose: str, inp: int, out: int) -> None:
        day = utcnow().date()
        key = (UsageRow.tenant_id == tenant, UsageRow.day == day, UsageRow.purpose == purpose)
        inc = {"calls": UsageRow.calls + 1, "input_tokens": UsageRow.input_tokens + inp,
               "output_tokens": UsageRow.output_tokens + out}
        try:
            for _ in range(3):  # atomic increment; concurrent first-writers race on insert, so retry
                async with self.db.session() as ses:
                    if (await ses.execute(update(UsageRow).where(*key).values(**inc))).rowcount:
                        await ses.commit()
                        break
                    ses.add(UsageRow(tenant_id=tenant, day=day, purpose=purpose, calls=1, input_tokens=inp,
                                     output_tokens=out))
                    try:
                        await ses.commit()
                        break
                    except IntegrityError:
                        await ses.rollback()
        except Exception as e:  # noqa: BLE001 - metering must never break learning
            log.warning("usage.meter_failed", error=str(e))
        metrics.llm_tokens.labels(purpose=purpose, kind="input").inc(inp)
        metrics.llm_tokens.labels(purpose=purpose, kind="output").inc(out)

    async def tokens_today(self, tenant: str) -> int:
        async with self.db.session() as ses:
            rows = (await ses.scalars(select(UsageRow).where(UsageRow.tenant_id == tenant,
                                                             UsageRow.day == utcnow().date()))).all()
        return sum(r.input_tokens + r.output_tokens for r in rows)

    # ---------------------------------------------------------------- embeddings (best effort)

    async def embed(self, texts: Sequence[str]) -> list[list[float]] | None:
        if self.embedder is None or not texts:
            return None
        try:
            return await self.embedder.embed(list(texts))
        except Exception as e:  # noqa: BLE001 - retrieval degrades to lexical, never fails
            log.warning("embeddings.failed", error=str(e))
            return None

    # ---------------------------------------------------------------- jobs & audit

    async def enqueue(self, ses: AsyncSession, tenant: str, kind: str, payload: dict[str, Any],
                      dedupe_key: str | None = None, delay_s: float = 0.0) -> None:
        """With a dedupe_key, a pending job is debounced (its start pushed back) instead of duplicated."""
        run_after = utcnow() + timedelta(seconds=delay_s)
        if dedupe_key:
            job = await ses.scalar(select(JobRow).where(JobRow.dedupe_key == dedupe_key, JobRow.status == "pending"))
            if job is not None:
                job.run_after = run_after  # debounce: restart the quiet-period timer
                job.payload = {**job.payload, **payload}
                return
        ses.add(JobRow(id=new_id("job"), tenant_id=tenant, kind=kind, payload=payload, dedupe_key=dedupe_key,
                       run_after=run_after))
        metrics.jobs_enqueued.labels(kind=kind).inc()

    async def ensure_agent_state(self, ses: AsyncSession, tenant: str, agent: str) -> None:
        """Insert-if-missing that is safe under concurrency (first episodes of a new agent arrive together)."""
        values = {"tenant_id": tenant, "agent_id": agent, "episodes_since_reflect": 0, "episodes_since_curate": 0,
                  "holdout_alpha": 1.0, "holdout_beta": 1.0, "treated_alpha": 1.0, "treated_beta": 1.0}
        if self.db.engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(AgentStateRow).values(**values).on_conflict_do_nothing()
        else:
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            stmt = sqlite_insert(AgentStateRow).values(**values).on_conflict_do_nothing()
        await ses.execute(stmt)

    @staticmethod
    def audit(ses: AsyncSession, tenant: str, actor: str, action: str, resource: str, **details: Any) -> None:
        ses.add(AuditRow(id=new_id("aud"), tenant_id=tenant, actor=actor, action=action, resource=resource,
                         details=details))
