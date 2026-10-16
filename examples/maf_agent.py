"""Microsoft Agent Framework agent with Myelinate as a context provider.

    pip install "myelinate[maf]"                       # or: uv sync --extra maf
    MYELINATE_URL=http://localhost:8080 python examples/maf_agent.py

With ANTHROPIC_API_KEY set and `agent-framework-anthropic` installed, it uses a real Claude model through
MAF's AnthropicClient (model from ANTHROPIC_MODEL, default claude-opus-5-5). Otherwise it falls back to
`ScriptedChatClient` below, a stand-in that echoes what it was given so the example runs without an LLM.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from agent_framework import Agent, BaseChatClient, ChatResponse, Message

from myelinate.adapters.maf import SelfLearningProvider
from myelinate.client import GatewayClient
from myelinate.models import FeedbackIn, Outcome, Verification

AGENT_ID = "maf-demo"
SKILL_MD = """---
name: refund-request
description: Handle a customer refund request
---
## Steps
1. Look up the order and confirm it is inside the 30-day refund window.
2. Issue the refund to the original payment method and give the customer the refund reference.

## Pitfalls
- When the order is a gift, refund the purchaser, not the recipient.
"""


# --- stand-in model, ONLY for running this example without an API key --------------------------------
class ScriptedChatClient(BaseChatClient):
    """Not a real model. Replies with a canned answer and reports whether learned context reached it."""

    async def _reply(self, messages: Any, options: Any) -> ChatResponse:
        seen = str(messages) + str(options)
        text = ("Refund issued to the original card, reference RF-1042." if "30-day refund window" in seen
                else "I have processed your refund.")
        return ChatResponse(messages=[Message("assistant", [text])])

    def _inner_get_response(self, *, messages: Any, stream: bool, options: Any, **kwargs: Any) -> Any:
        return self._reply(messages, options)


def make_chat_client() -> BaseChatClient:
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from agent_framework.anthropic import AnthropicClient

            return AnthropicClient(model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"))
        except ImportError:
            print("agent-framework-anthropic is not installed; using the scripted stand-in client")
    return ScriptedChatClient()


async def seed_skill() -> None:
    """Import one SKILL.md so recall returns something on the first run."""
    url = os.environ.get("MYELINATE_URL", "http://localhost:8080")
    key = os.environ.get("MYELINATE_API_KEY")
    async with httpx.AsyncClient(base_url=url, headers={"authorization": f"Bearer {key}"} if key else {}) as http:
        (await http.post("/v1/skills/import", json={"agent_id": AGENT_ID, "markdown": SKILL_MD})).raise_for_status()


def refund_verified(ctx: Any) -> Verification | None:
    """Ground truth from your own systems. Here: the reply must carry a refund reference."""
    text = getattr(ctx.response, "text", "") or ""
    return Verification(passed="RF-" in text, details="refund reference present" if "RF-" in text else "no reference",
                        source="refund-ledger")


async def main() -> None:
    await seed_skill()
    gateway = GatewayClient()  # MYELINATE_URL / MYELINATE_API_KEY, default http://localhost:8080
    learner = SelfLearningProvider(
        gateway, agent_id=AGENT_ID,
        verify_fn=refund_verified,
        outcome_fn=lambda ctx: Outcome.success if ctx.response is not None else Outcome.failure,
    )
    agent = Agent(make_chat_client(), instructions="You are a support agent for an online shop.",
                  context_providers=[learner])

    session = agent.create_session()
    session.state["myelinate_user_id"] = "customer-77"  # per-user memories and preferences
    result = await agent.run("I want a refund for order 5521, it arrived broken.", session=session)
    print("agent:", result.text)

    await learner.flush()  # episodes upload in the background; flush before reading the id or exiting
    episode_id = session.state.get("myelinate", {}).get("last_episode_id")  # keyed by the provider's source_id
    print("recorded episode", episode_id)

    if episode_id:  # later, when the customer reacts:
        await gateway.feedback(FeedbackIn(episode_id=episode_id, score=1.0, comment="customer confirmed refund"))
        print("feedback recorded")
    await gateway.aclose()


if __name__ == "__main__":
    asyncio.run(main())
