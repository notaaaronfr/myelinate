"""Client SDK used by framework adapters.

`GatewayClient` talks to a remote gateway (recommended for enterprise: central governance, shared knowledge
across replicas and frameworks). `EmbeddedClient` runs the engine in-process (dev, edge, single-node).
Both satisfy the same `LearnerClient` protocol, so adapters work with either.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from .models import (
    AskIn,
    AskOut,
    EpisodeIn,
    EpisodeOut,
    FeedbackIn,
    MemoryIn,
    MemoryKind,
    RecallRequest,
    RecallResponse,
    Scope,
)


class LearnerClient(Protocol):
    async def recall(self, req: RecallRequest) -> RecallResponse: ...
    async def record(self, ep: EpisodeIn) -> EpisodeOut: ...
    async def feedback(self, fb: FeedbackIn) -> None: ...
    async def remember(self, m: MemoryIn) -> list[str]: ...
    async def skill_markdown(self, agent_id: str, name: str) -> str | None: ...
    async def ask(self, req: AskIn) -> AskOut: ...


class GatewayError(RuntimeError):
    pass


class GatewayClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 10.0,
                 retries: int = 2, transport: httpx.AsyncBaseTransport | None = None) -> None:
        base_url = base_url or os.environ.get("MYELINATE_URL", "http://localhost:8080")
        api_key = api_key or os.environ.get("MYELINATE_API_KEY")
        headers = {"user-agent": "myelinate-py/0.2"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        self._http = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout, transport=transport)
        self._retries = retries

    async def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        for attempt in range(self._retries + 1):
            try:
                r = await self._http.request(method, path, **kw)
            except httpx.TransportError as e:
                if attempt == self._retries:
                    raise GatewayError(f"gateway unreachable: {e}") from e
            else:
                if r.status_code < 500 and r.status_code != 429:
                    if r.status_code >= 400:
                        raise GatewayError(f"{r.status_code}: {r.text}")
                    return r
                if attempt == self._retries:
                    raise GatewayError(f"{r.status_code}: {r.text}")
            await asyncio.sleep(0.2 * 2**attempt)
        raise AssertionError("unreachable")

    async def recall(self, req: RecallRequest) -> RecallResponse:
        r = await self._req("POST", "/v1/recall", json=req.model_dump(mode="json"))
        return RecallResponse.model_validate(r.json())

    async def record(self, ep: EpisodeIn) -> EpisodeOut:
        if ep.idempotency_key is None:  # stable across this call's retries: a retried upload can't duplicate
            ep = ep.model_copy(update={"idempotency_key": f"py:{uuid.uuid4().hex}"})
        r = await self._req("POST", "/v1/episodes", json=ep.model_dump(mode="json"))
        return EpisodeOut.model_validate(r.json())

    async def feedback(self, fb: FeedbackIn) -> None:
        await self._req("POST", "/v1/feedback", json=fb.model_dump(mode="json"))

    async def remember(self, m: MemoryIn) -> list[str]:
        return (await self._req("POST", "/v1/memories", json=m.model_dump(mode="json"))).json()["memory_ids"]

    async def ask(self, req: AskIn) -> AskOut:
        return AskOut.model_validate((await self._req("POST", "/v1/ask", json=req.model_dump(mode="json"))).json())

    async def skill_markdown(self, agent_id: str, name: str) -> str | None:
        try:
            return (await self._req("GET", f"/v1/skills/{name}/SKILL.md", params={"agent_id": agent_id})).text
        except GatewayError as e:
            if str(e).startswith("404"):
                return None
            raise

    async def aclose(self) -> None:
        await self._http.aclose()


class EmbeddedClient:
    """In-process engine. Call `await start()` once; runs its own background worker."""

    def __init__(self, tenant_id: str = "local", settings: Any = None, llm: Any = None) -> None:
        try:
            import sqlalchemy  # noqa: F401
        except ImportError:
            raise ImportError('EmbeddedClient runs the engine in-process: pip install "myelinate[server]"') from None
        from .config import get_settings
        from .db import Database
        from .engine import LearningEngine
        from .gateway.app import build_embedder, build_llm
        from .worker import Worker

        s = settings or get_settings()
        self.tenant = tenant_id
        self.db = Database(s.database_url, s.db_pool_size, s.db_max_overflow)
        self.engine = LearningEngine(self.db, s, llm or build_llm(s), build_embedder(s))
        self.worker = Worker(self.engine)
        self._started = False

    async def start(self, run_worker: bool = True) -> EmbeddedClient:
        if not self._started:
            await self.db.create_all()
            if run_worker:
                self.worker.start()
            self._started = True
        return self

    async def recall(self, req: RecallRequest) -> RecallResponse:
        return await self.engine.recall(self.tenant, req)

    async def record(self, ep: EpisodeIn) -> EpisodeOut:
        return await self.engine.record(self.tenant, ep)

    async def feedback(self, fb: FeedbackIn) -> None:
        await self.engine.feedback(self.tenant, "embedded", fb)

    async def remember(self, m: MemoryIn) -> list[str]:
        return await self.engine.add_memory(self.tenant, "embedded", m)

    async def ask(self, req: AskIn) -> AskOut:
        return await self.engine.ask(self.tenant, req)

    async def skill_markdown(self, agent_id: str, name: str) -> str | None:
        from .core import NotFound

        try:
            return await self.engine.skill_markdown(self.tenant, agent_id, name)
        except NotFound:
            return None

    async def aclose(self) -> None:
        await self.worker.stop()
        await self.db.close()


def make_tools(client: LearnerClient, agent_id: str, user_id: str | None = None,
               on_skill_loaded: Callable[[str], None] | None = None) -> list[Any]:
    """Plain async functions with docstrings: most frameworks (MAF, LangChain, CrewAI...) accept these as tools.

    on_skill_loaded is called with each skill name the agent loads, so the run's outcome can be credited to it."""

    async def skill_view(name: str) -> str:
        """Load the full instructions of a learned skill by its name."""
        md = await client.skill_markdown(agent_id, name)
        if md and on_skill_loaded:
            on_skill_loaded(name)
        return md or f"No skill named '{name}'."

    async def remember(content: str, kind: str = "fact") -> str:
        """Save one short durable memory. kind is 'fact', 'preference' (about the user) or 'lesson' (a mistake
        to avoid). Never store secrets or credentials."""
        await client.remember(MemoryIn(scope=Scope(agent_id=agent_id, user_id=user_id), kind=MemoryKind(kind),
                                       content=content))
        return "Saved."

    return [skill_view, remember]
