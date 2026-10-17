"""Microsoft Agent Framework (Python) adapter.

    from agent_framework import Agent
    from myelinate.adapters.maf import SelfLearningProvider
    from myelinate.client import GatewayClient

    learner = SelfLearningProvider(GatewayClient(), agent_id="support-bot")
    agent = Agent(client=chat_client, instructions="...", context_providers=[learner])
    session = agent.create_session()
    session.state["myelinate_user_id"] = "user-42"   # optional, enables per-user preferences
    await agent.run("...", session=session)

Before each run it injects learned skills + memories as instructions and adds `skill_view` / `remember`
tools. After each run it records the episode (messages, tool calls, outcome) without blocking the response.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import logging

try:
    from agent_framework import AgentSession, ContextProvider, SessionContext
except ImportError as e:  # pragma: no cover
    raise ImportError("install the Microsoft Agent Framework: pip install agent-framework") from e

from ..client import LearnerClient, make_tools
from ..models import EpisodeIn, Message, Outcome, RecallRequest, Scope, ToolCall, Verification

log = logging.getLogger(__name__)
OutcomeFn = Callable[[SessionContext], Outcome]
VerifyFn = Callable[[SessionContext], Verification | None]


def _text(m: Any) -> str:
    return (getattr(m, "text", None) or "").strip()


def _role(m: Any) -> str:
    r = getattr(m, "role", "user")
    return str(getattr(r, "value", r))


def _tool_calls(messages: list[Any]) -> list[ToolCall]:
    calls: dict[str, ToolCall] = {}
    for m in messages:
        for c in getattr(m, "contents", None) or []:
            kind = getattr(c, "type", "")
            cid = getattr(c, "call_id", None) or str(len(calls))
            if kind == "function_call":
                args = getattr(c, "arguments", None)
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        pass
                calls[cid] = ToolCall(name=getattr(c, "name", "?"), arguments=args)
            elif kind == "function_result" and cid in calls:
                exc = getattr(c, "exception", None)
                calls[cid].error = str(exc) if exc else None
                calls[cid].result = None if exc else str(getattr(c, "result", ""))[:4000]
    return list(calls.values())


class SelfLearningProvider(ContextProvider):
    def __init__(self, client: LearnerClient, agent_id: str = "default", *, user_id: str | None = None,
                 source_id: str = "myelinate", add_tools: bool = True, outcome_fn: OutcomeFn | None = None,
                 verify_fn: VerifyFn | None = None, fail_open: bool = True) -> None:
        super().__init__(source_id)
        self.client, self.agent_id, self.user_id = client, agent_id, user_id
        self.add_tools, self.outcome_fn, self.verify_fn, self.fail_open = add_tools, outcome_fn, verify_fn, fail_open
        self._pending: set[asyncio.Task[Any]] = set()

    def _user(self, session: AgentSession) -> str | None:
        return session.state.get("myelinate_user_id") or self.user_id

    async def before_run(self, *, agent: Any, session: AgentSession, context: SessionContext,
                         state: dict[str, Any]) -> None:
        query = "\n".join(t for t in (_text(m) for m in context.input_messages) if t)
        state["t0"] = time.monotonic()
        state.pop("recall_id", None)
        if not query:
            return
        try:
            r = await self.client.recall(RecallRequest(scope=Scope(agent_id=self.agent_id, user_id=self._user(session)),
                                                       query=query, session_id=session.session_id))
        except Exception:
            if not self.fail_open:
                raise
            log.warning("myelinate.recall_failed", exc_info=True)  # the agent still runs, just without learning
            return
        state["recall_id"] = r.recall_id
        if r.instructions:
            context.extend_instructions(self.source_id, r.instructions)
        if self.add_tools:
            context.extend_tools(self.source_id, make_tools(self.client, self.agent_id, self._user(session)))

    async def after_run(self, *, agent: Any, session: AgentSession, context: SessionContext,
                        state: dict[str, Any]) -> None:
        resp = context.response
        out_msgs = list(getattr(resp, "messages", None) or [])
        msgs = [Message(role=_role(m), content=_text(m)) for m in [*context.input_messages, *out_msgs] if _text(m)]
        if self.outcome_fn:
            outcome = self.outcome_fn(context)
        else:
            outcome = Outcome.unknown if resp is not None else Outcome.failure
        ep = EpisodeIn(
            scope=Scope(agent_id=self.agent_id, user_id=self._user(session)), session_id=session.session_id,
            recall_id=state.get("recall_id"), messages=msgs, tool_calls=_tool_calls(out_msgs), outcome=outcome,
            verification=self.verify_fn(context) if self.verify_fn else None,
            skills_loaded=[str(c.arguments["name"]) for c in _tool_calls(out_msgs)
                           if c.name == "skill_view" and isinstance(c.arguments, dict) and c.arguments.get("name")],
            duration_ms=int((time.monotonic() - state.get("t0", time.monotonic())) * 1000),
            framework="microsoft-agent-framework", metadata={"agent_name": getattr(agent, "name", None)},
        )
        task = asyncio.create_task(self._record(ep, state))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _record(self, ep: EpisodeIn, state: dict[str, Any]) -> None:
        try:
            out = await self.client.record(ep)
            state["last_episode_id"] = out.episode_id  # use with client.feedback(...)
        except Exception:
            log.warning("myelinate.record_failed", exc_info=True)

    async def flush(self) -> None:
        """Await in-flight episode uploads (call before shutdown)."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
