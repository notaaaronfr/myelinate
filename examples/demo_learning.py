"""See Myelinate learn in about a minute: an agent fails a team convention, learns from the feedback, then passes.

Needs ANTHROPIC_API_KEY (the agent is Claude Haiku 5.5; learning uses Claude Opus 5.5). Runs fully in-process with
EmbeddedClient, so no gateway is needed. Costs a few cents.

    uv run python examples/demo_learning.py
"""

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
import time
from pathlib import Path

import logging

import anthropic
import structlog

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "house_rules"))
from tasks import Task, all_tasks  # noqa: E402

from myelinate import EmbeddedClient  # noqa: E402
from myelinate.config import Settings  # noqa: E402
from myelinate.models import EpisodeIn, Message, Outcome, RecallRequest, Scope, Verification  # noqa: E402

B, D, G, R, Y, C, X = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m"
SCOPE = Scope(agent_id="acme-eng")
BASE = "You are a helpful engineering assistant at Acme. Reply with only the commit message."


def say(text: str = "", pause: float = 0.6) -> None:
    print(text, flush=True)
    time.sleep(pause)


async def agent(task: Task, instructions: str) -> str:
    r = await anthropic.AsyncAnthropic().messages.create(
        model="claude-haiku-5-5", max_tokens=1000, output_config={"effort": "low"},
        system=BASE + (f"\n\n{instructions}" if instructions else ""),
        messages=[{"role": "user", "content": task.prompt}])
    text = "".join(b.text for b in r.content if b.type == "text").strip()
    m = re.fullmatch(r"```\w*\n(.*?)\n```", text, re.S)
    return m.group(1) if m else text


def show(out: str, errs: list[str]) -> None:
    for line in out.splitlines():
        say(f"    {C}{line}{X}", 0.05)
    if errs:
        for e in errs:
            say(f"  {R}✗ {e}{X}", 0.3)
    else:
        say(f"  {G}✓ all commit checks passed{X}", 0.3)


async def main() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))  # quiet demo output
    tmp = tempfile.mkdtemp()
    s = Settings(env="dev", database_url=f"sqlite+aiosqlite:///{tmp}/demo.db", holdout_rate=0.0,
                 reflect_debounce_seconds=0, worker_poll_seconds=0.2, auto_approve_medium_risk=True)
    client = await EmbeddedClient("demo", settings=s).start(run_worker=False)
    commits = all_tasks()["commit"]
    first, later = commits[0], commits[6]

    say(f"\n{B}1. The agent writes a commit message. Nobody told it the team's rules.{X}")
    say(f"  {D}{first.prompt}{X}")
    r = await client.recall(RecallRequest(scope=SCOPE, query=first.prompt))
    out = await agent(first, r.instructions)
    errs = first.check(out)
    show(out, errs)

    say(f"\n{B}2. Myelinate records the run with the CI feedback and learns from it.{X}")
    await client.record(EpisodeIn(
        scope=SCOPE, recall_id=r.recall_id, task=first.prompt, outcome=Outcome.failure if errs else Outcome.success,
        messages=[Message(role="user", content=first.prompt), Message(role="assistant", content=out)],
        verification=Verification(passed=not errs, details="\n".join(errs) or "ok", source="commitlint")))
    t0 = time.time()
    print(f"  {Y}reflecting...{X}", end="", flush=True)
    while await client.worker.run_once():
        pass
    print(f"\r  {Y}learned in {time.time() - t0:.0f}s{X}  ", flush=True)
    r2 = await client.recall(RecallRequest(scope=SCOPE, query=later.prompt))
    for sk in r2.skills:
        say(f"  {G}+ skill {B}{sk.name}{X}{G} v{sk.version}{X}", 0.3)
        for b in sk.bullets or []:
            say(f"    {D}• {b.content}{X}", 0.25)

    say(f"\n{B}3. A different task. Myelinate injects what it learned before the run.{X}")
    say(f"  {D}{later.prompt}{X}")
    out2 = await agent(later, r2.instructions)
    show(out2, later.check(out2))
    say(f"\n{D}No fine-tuning and no prompt edits: the agent improved from its own feedback.{X}\n", 1.5)
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
