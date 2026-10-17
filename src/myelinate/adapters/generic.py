"""Framework-agnostic adapter. Use it with LangGraph, CrewAI, AutoGen, Semantic Kernel, OpenAI Agents SDK,
Google ADK, Strands, or any hand-rolled loop: anything where you can run code before and after a turn.

    learner = SelfLearning(GatewayClient(), agent_id="research-bot")

    async with learner.turn(user_input, user_id="u1") as t:
        system_prompt = BASE_PROMPT + "\\n\\n" + t.instructions     # inject learned knowledge
        answer = await my_agent(system_prompt, user_input)
        t.add_message("assistant", answer)
        t.add_tool_call("search", {"q": "..."}, result="...")
        t.verify(passed=tests_ok, details=test_log)                 # ground truth when you have it
        t.outcome = Outcome.success                                 # or leave unknown; exceptions => failure

LangGraph: call `learner.turn(...)` in your entry node (or wrap `graph.ainvoke`) and put `t.instructions`
into the system message. CrewAI: wrap `crew.kickoff_async`. OpenAI Agents SDK: wrap `Runner.run` and
pass `t.instructions` into `Agent(instructions=...)`.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import logging

from ..client import LearnerClient, make_tools
from ..models import EpisodeIn, FeedbackIn, Message, Outcome, RecallRequest, Scope, ToolCall, Verification

log = logging.getLogger(__name__)


@dataclass
class Turn:
    instructions: str = ""
    recall_id: str | None = None
    outcome: Outcome = Outcome.unknown
    messages: list[Message] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    episode_id: str | None = None
    tools: list[Any] = field(default_factory=list)
    verification: Verification | None = None
    skills_loaded: list[str] = field(default_factory=list)

    def add_message(self, role: str, content: str) -> None:
        self.messages.append(Message(role=role, content=content))

    def verify(self, passed: bool, details: str | None = None, source: str = "verifier") -> None:
        """Attach a ground-truth check (tests, validators). The strongest learning signal available."""
        self.verification = Verification(passed=passed, details=details, source=source)

    def add_tool_call(self, name: str, arguments: Any = None, result: str | None = None,
                      error: str | None = None) -> None:
        self.tool_calls.append(ToolCall(name=name, arguments=arguments, result=result, error=error))


class SelfLearning:
    def __init__(self, client: LearnerClient, agent_id: str = "default", framework: str = "generic",
                 fail_open: bool = True) -> None:
        self.client, self.agent_id, self.framework, self.fail_open = client, agent_id, framework, fail_open

    @contextlib.asynccontextmanager
    async def turn(self, task: str, *, user_id: str | None = None, session_id: str | None = None) -> AsyncIterator[Turn]:
        scope = Scope(agent_id=self.agent_id, user_id=user_id)
        t = Turn(messages=[Message(role="user", content=task)])
        t.tools = make_tools(self.client, self.agent_id, user_id, on_skill_loaded=t.skills_loaded.append)
        try:
            r = await self.client.recall(RecallRequest(scope=scope, query=task, session_id=session_id))
            t.instructions, t.recall_id = r.instructions, r.recall_id
        except Exception:
            if not self.fail_open:
                raise
            log.warning("myelinate.recall_failed", exc_info=True)
        t0 = time.monotonic()
        error: str | None = None
        try:
            yield t
        except BaseException as e:
            t.outcome, error = Outcome.failure, f"{type(e).__name__}: {e}"
            raise
        finally:
            try:
                out = await self.client.record(EpisodeIn(
                    scope=scope, session_id=session_id, recall_id=t.recall_id, task=task, messages=t.messages,
                    tool_calls=t.tool_calls, outcome=t.outcome, error=error, framework=self.framework,
                    verification=t.verification, skills_loaded=t.skills_loaded,
                    duration_ms=int((time.monotonic() - t0) * 1000), metadata=t.metadata))
                t.episode_id = out.episode_id
            except Exception:
                log.warning("myelinate.record_failed", exc_info=True)

    async def feedback(self, episode_id: str, score: float, comment: str | None = None,
                       correction: str | None = None) -> None:
        await self.client.feedback(FeedbackIn(episode_id=episode_id, score=score, comment=comment,
                                              correction=correction))
