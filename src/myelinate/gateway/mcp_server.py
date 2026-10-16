"""MCP surface: any MCP-capable agent (Claude, OpenClaw, MAF, Hermes, Cursor...) gets the learning tools.

Exposed at /mcp (streamable HTTP). Auth uses the same bearer credentials as the REST API.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..core import NotFound
from ..engine import LearningEngine
from ..models import (
    AskIn,
    EpisodeIn,
    FeedbackIn,
    MemoryIn,
    MemoryKind,
    Message,
    Outcome,
    RecallRequest,
    Scope,
    Verification,
)
from .auth import Authenticator, AuthError, Principal, RateLimiter

INSTRUCTIONS = """Self-learning memory for this agent. At the start of a non-trivial task call `recall` and \
follow the instructions it returns. Load a listed skill with `skill_view`. Save durable facts or user \
preferences with `remember`. When the task ends call `record_episode` with the outcome (and test results \
if you have them) so the agent learns from it."""


def build_mcp(engine: LearningEngine, auth: Authenticator, limiter: RateLimiter | None = None) -> MCPServer:
    mcp = MCPServer(name="myelinate", instructions=INSTRUCTIONS, version="0.2.0")

    async def principal(ctx: Context, scope: str = "learn") -> Principal:
        try:
            p = await auth.authenticate(dict(ctx.headers or {}))
            if limiter is not None:
                limiter.check(p.subject)
            p.require(scope)
            return p
        except AuthError as e:
            raise PermissionError(e.detail) from e

    @mcp.tool(description="Load learned skills, memories and the user profile relevant to a task. Returns "
                          "instructions to follow and a recall_id to pass to record_episode.")
    async def recall(query: str, ctx: Context, agent_id: str = "default", user_id: str | None = None,
                     session_id: str | None = None) -> dict[str, Any]:
        p = await principal(ctx)
        r = await engine.recall(p.tenant_id, RecallRequest(scope=Scope(agent_id=agent_id, user_id=user_id),
                                                           query=query, session_id=session_id))
        return {"recall_id": r.recall_id, "instructions": r.instructions or "(nothing learned yet for this task)"}

    @mcp.tool(description="Load the full text of a learned skill by name (SKILL.md format).")
    async def skill_view(name: str, ctx: Context, agent_id: str = "default") -> str:
        p = await principal(ctx)
        try:
            return await engine.skill_markdown(p.tenant_id, agent_id, name)
        except NotFound:
            return f"No skill named '{name}'."

    @mcp.tool(description="Save a durable memory. kind: 'fact' (environment/project), 'preference' (about the "
                          "user), or 'lesson' (a mistake to avoid). One short sentence; never secrets.")
    async def remember(content: str, ctx: Context, kind: str = "fact", agent_id: str = "default",
                       user_id: str | None = None) -> str:
        p = await principal(ctx)
        ids = await engine.add_memory(p.tenant_id, p.subject, MemoryIn(scope=Scope(agent_id=agent_id, user_id=user_id),
                                                                        kind=MemoryKind(kind), content=content))
        return "Saved." if ids else "Already known."

    @mcp.tool(description="Record how a task went so the agent learns from it. outcome: success, failure, partial "
                          "or unknown. summary: what was asked, what you did, what happened. If tests or checks "
                          "ran, set tests_passed and put their output in test_details.")
    async def record_episode(task: str, outcome: str, summary: str, ctx: Context, recall_id: str | None = None,
                             error: str | None = None, tests_passed: bool | None = None,
                             test_details: str | None = None, skills_used: list[str] | None = None,
                             agent_id: str = "default", user_id: str | None = None,
                             session_id: str | None = None) -> dict[str, Any]:
        p = await principal(ctx)
        out = await engine.record(p.tenant_id, EpisodeIn(
            scope=Scope(agent_id=agent_id, user_id=user_id), recall_id=recall_id, task=task, session_id=session_id,
            messages=[Message(role="user", content=task), Message(role="assistant", content=summary)],
            outcome=Outcome(outcome) if outcome in Outcome.__members__ else Outcome.unknown, error=error,
            verification=Verification(passed=tests_passed, details=test_details) if tests_passed is not None else None,
            skills_loaded=skills_used or [], framework="mcp"))
        return out.model_dump()

    @mcp.tool(description="Rate a previous episode 0.0 (bad) to 1.0 (good). If the user said what the right "
                          "answer or approach was, pass it as correction.")
    async def feedback(episode_id: str, score: float, ctx: Context, comment: str | None = None,
                       correction: str | None = None) -> str:
        p = await principal(ctx)
        await engine.feedback(p.tenant_id, p.subject, FeedbackIn(episode_id=episode_id, score=score, comment=comment,
                                                                 correction=correction))
        return "Feedback recorded."

    @mcp.tool(description="Ask what has been learned, e.g. 'What does the user prefer for reports?' or 'How do we "
                          "deploy service X?'. Answers only from learned evidence.")
    async def ask(question: str, ctx: Context, agent_id: str = "default", user_id: str | None = None) -> dict[str, Any]:
        p = await principal(ctx)
        r = await engine.ask(p.tenant_id, AskIn(scope=Scope(agent_id=agent_id, user_id=user_id), question=question))
        return r.model_dump()

    return mcp
