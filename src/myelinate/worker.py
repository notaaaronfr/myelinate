"""Background worker over the durable `jobs` table.

Claiming is a conditional UPDATE (pending -> running), so several gateway replicas or dedicated worker
processes can share one database without double-processing. Running jobs hold a lease; if a worker dies,
the lease expires and another worker picks the job up.
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
from datetime import timedelta

import structlog
from sqlalchemy import exists, or_, select, update
from sqlalchemy.orm import aliased

from .core import BudgetExceeded
from .db import JobRow, new_id, utcnow
from .engine import LearningEngine
from .telemetry import metrics

log = structlog.get_logger()
LEASE = timedelta(minutes=10)


HEARTBEAT_SECONDS = 10.0


class Worker:
    def __init__(self, engine: LearningEngine, heartbeat_file: str | None = None) -> None:
        self.engine = engine
        self.id = new_id("wrk")
        self.heartbeat_file = heartbeat_file  # touched every few seconds, independent of job duration
        self._task: asyncio.Task[None] | None = None
        self._beat_task: asyncio.Task[None] | None = None
        self._sem = asyncio.Semaphore(engine.s.worker_concurrency)
        self._inflight: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="myelinate-worker")
        if self.heartbeat_file:
            self._beat_task = asyncio.create_task(self._beat(), name="myelinate-worker-heartbeat")

    async def stop(self) -> None:
        """Stop claiming new jobs and let in-flight jobs finish (up to 30s) so they aren't left to lease expiry."""
        for t in (self._task, self._beat_task):
            if t:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        if self._inflight:
            await asyncio.wait(self._inflight, timeout=30)

    async def _beat(self) -> None:
        """Liveness for container probes: proves the event loop is alive even during long jobs."""
        path = pathlib.Path(self.heartbeat_file or "")
        while True:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(path.touch)
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def _loop(self) -> None:
        while True:
            try:
                n = await self.run_once()
            except Exception:
                log.exception("worker.loop_error")
                n = 0
            if n == 0:
                await asyncio.sleep(self.engine.s.worker_poll_seconds)

    async def _claim(self, limit: int) -> list[JobRow]:
        now = utcnow()
        db = self.engine.db
        async with db.session() as ses:
            # Recover jobs whose worker died mid-run.
            await ses.execute(update(JobRow).where(JobRow.status == "running", JobRow.run_after < now)
                              .values(status="pending", locked_by=None))
            # One running job per dedupe key (e.g. one reflection per agent): same-key jobs wait their turn.
            busy = aliased(JobRow)
            free = ~exists().where(busy.dedupe_key == JobRow.dedupe_key, busy.status == "running")
            rows = (await ses.execute(select(JobRow.id, JobRow.dedupe_key).where(
                JobRow.status == "pending", JobRow.run_after <= now, or_(JobRow.dedupe_key.is_(None), free))
                .order_by(JobRow.created_at).limit(limit))).all()
            claimed, keys = [], set()
            for jid, key in rows:
                if key and key in keys:
                    continue
                res = await ses.execute(update(JobRow).where(JobRow.id == jid, JobRow.status == "pending")
                                        .values(status="running", locked_by=self.id, run_after=now + LEASE,
                                                attempts=JobRow.attempts + 1))
                if res.rowcount == 1:
                    claimed.append(jid)
                    if key:
                        keys.add(key)
            await ses.commit()
            if not claimed:
                return []
            return list((await ses.scalars(select(JobRow).where(JobRow.id.in_(claimed)))).all())

    async def run_once(self) -> int:
        jobs = await self._claim(self.engine.s.worker_concurrency)
        tasks = [asyncio.create_task(self._run(job)) for job in jobs]
        for t in tasks:
            self._inflight.add(t)
            t.add_done_callback(self._inflight.discard)
        await asyncio.gather(*tasks, return_exceptions=True)
        return len(jobs)

    async def _run(self, job: JobRow) -> None:
        async with self._sem:
            status, err = "done", None
            backoff = utcnow() + timedelta(seconds=30 * 2 ** job.attempts)
            attempts = job.attempts
            beat = asyncio.create_task(self._heartbeat(job.id))
            try:
                await self.engine.run_job(job.tenant_id, job.kind, job.payload)
            except BudgetExceeded as e:  # not the job's fault: wait for budget, don't burn an attempt
                status, err, attempts = "pending", str(e), job.attempts - 1
                backoff = utcnow() + timedelta(hours=1)
            except Exception as e:  # noqa: BLE001 - every failure is recorded on the job
                err = f"{type(e).__name__}: {e}"
                status = "failed" if job.attempts >= self.engine.s.job_max_attempts else "pending"
                log.warning("job.failed", job=job.id, kind=job.kind, attempt=job.attempts, error=err)
            finally:
                beat.cancel()
            async with self.engine.db.session() as ses:
                # Only the lease holder may finish the job (a re-claimed job belongs to someone else now).
                await ses.execute(update(JobRow).where(JobRow.id == job.id, JobRow.locked_by == self.id).values(
                    status=status, last_error=err, locked_by=None, attempts=attempts,
                    run_after=backoff if status == "pending" else utcnow()))
                await ses.commit()
            metrics.jobs_done.labels(kind=job.kind, status=status).inc()

    async def _heartbeat(self, job_id: str) -> None:
        """Extend the lease while a long job (e.g. an evolution run) is still working."""
        while True:
            await asyncio.sleep(LEASE.total_seconds() / 3)
            with contextlib.suppress(Exception):
                async with self.engine.db.session() as ses:
                    await ses.execute(update(JobRow).where(JobRow.id == job_id, JobRow.locked_by == self.id)
                                      .values(run_after=utcnow() + LEASE))
                    await ses.commit()
