"""Self-learning loop around a stand-in "agent", using the framework-agnostic adapter.

No LLM is needed: `fake_agent` plays the agent so you can see what Myelinate injects and records.
Replace it with your real framework call (LangGraph, CrewAI, OpenAI Agents SDK, ADK, a plain loop...).

    MYELINATE_URL=http://localhost:8080 uv run python examples/generic_agent.py

Set MYELINATE_API_KEY when the gateway is not in dev mode (needs the `learn` scope;
seeding the demo skill also needs `review`).
"""

from __future__ import annotations

import asyncio
import os

import httpx

from myelinate.adapters.generic import SelfLearning
from myelinate.client import GatewayClient
from myelinate.models import Outcome

AGENT_ID = "generic-demo"
BASE_PROMPT = "You are a release engineer. Be brief."
SKILL_MD = """---
name: publish-python-package
description: Publish a Python package to PyPI
---
## Steps
1. Bump the version in pyproject.toml and tag the release commit.
2. Build with `uv build` and upload with `uv publish` using a scoped token.

## Pitfalls
- When the upload fails with 400 File already exists, bump the version; PyPI never accepts a re-upload.
"""


async def seed_skill() -> None:
    """Import one SKILL.md so recall has something to return (normally skills are learned or imported once)."""
    url = os.environ.get("MYELINATE_URL", "http://localhost:8080")
    key = os.environ.get("MYELINATE_API_KEY")
    headers = {"authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(base_url=url, headers=headers) as http:
        r = await http.post("/v1/skills/import", json={"agent_id": AGENT_ID, "markdown": SKILL_MD})
        r.raise_for_status()


async def fake_agent(system_prompt: str, task: str) -> tuple[str, bool]:
    """Stand-in for a real agent. Returns (answer, checks_passed)."""
    used_skill = "uv publish" in system_prompt
    answer = ("Tagged v1.4.0, built with uv build and published with uv publish." if used_skill
              else "Uploaded the package.")
    return answer, used_skill


async def main() -> None:
    await seed_skill()
    client = GatewayClient()  # reads MYELINATE_URL / MYELINATE_API_KEY, defaults to http://localhost:8080
    learner = SelfLearning(client, agent_id=AGENT_ID, framework="example")
    task = "Release version 1.4.0 of our Python SDK to PyPI"

    async with learner.turn(task, user_id="user-42", session_id="demo-session") as t:
        print("--- learned context injected into the system prompt:\n" + (t.instructions or "(none yet)"))
        answer, checks_ok = await fake_agent(BASE_PROMPT + "\n\n" + t.instructions, task)
        t.add_message("assistant", answer)
        t.add_tool_call("shell", {"cmd": "uv publish"}, result="Uploading ... 200 OK")
        # Ground truth beats everything else: tests, validators, business checks.
        t.verify(passed=checks_ok, details="pip index versions shows 1.4.0")
        t.outcome = Outcome.success if checks_ok else Outcome.failure

    print(f"\n--- recorded episode {t.episode_id} (recall {t.recall_id})")

    # Later, when the user reacts, attach feedback to the same episode.
    if t.episode_id:
        await learner.feedback(t.episode_id, score=1.0, comment="clean release")
        print("--- feedback recorded")
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
