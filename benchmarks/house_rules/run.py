"""House-rules benchmark: does an agent get better at team conventions by learning from verifier feedback?

Protocol (fully automatic, no human in the loop):
- Agent: Claude Haiku 5.5 with a fixed generic system prompt plus whatever Myelinate recalls. The rules are never
  in the prompt.
- Training: 24 tasks (6 per family). Each run is checked by the family's verifier and recorded with
  `verification={passed, details}`, the same way CI or a review bot would report.
- Learning: Myelinate (learning model: Claude Opus 5.5) reflects, curates skills and memories in the background.
- Evaluation: 16 held-out tasks (4 per family, never trained on), sampled twice each, before learning and after every
  8 training runs. Evaluation runs are not recorded, so they can't teach.

Run: ANTHROPIC_API_KEY=... uv run python benchmarks/house_rules/run.py [--epochs 1]
Writes benchmarks/house_rules/results.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import time
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).parent))
from tasks import Task, split  # noqa: E402

from myelinate.config import Settings  # noqa: E402
from myelinate.db import Database, UsageRow  # noqa: E402
from myelinate.engine import LearningEngine  # noqa: E402
from myelinate.gateway.app import build_llm  # noqa: E402
from myelinate.models import EpisodeIn, Message, Outcome, RecallRequest, Scope, Verification  # noqa: E402
from myelinate.worker import Worker  # noqa: E402

AGENT_MODEL = "claude-haiku-5-5"
AGENT_ID = "acme-eng"
BASE_PROMPT = ("You are a helpful engineering and support assistant at Acme. Reply with only the requested artifact "
               "(the commit message, SQL, email or code), with no explanation before or after it.")
SAMPLES = 2


class Agent:
    def __init__(self) -> None:
        self.client = anthropic.AsyncAnthropic()
        self.calls = self.input_tokens = self.output_tokens = 0
        self.sem = asyncio.Semaphore(8)

    async def run(self, task: Task, instructions: str) -> str:
        system = BASE_PROMPT + (f"\n\n{instructions}" if instructions else "")
        async with self.sem:
            for attempt in range(4):
                try:
                    r = await self.client.messages.create(
                        model=AGENT_MODEL, max_tokens=4000, system=system, output_config={"effort": "low"},
                        messages=[{"role": "user", "content": task.prompt}])
                    break
                except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError):
                    await asyncio.sleep(2 ** attempt)
            else:
                return ""
        self.calls += 1
        self.input_tokens += r.usage.input_tokens
        self.output_tokens += r.usage.output_tokens
        text = "".join(b.text for b in r.content if b.type == "text").strip()
        m = re.fullmatch(r"```[\w-]*\n(.*?)\n```", text, re.S)  # unwrap a single fenced block
        return m.group(1) if m else text


async def evaluate(engine: LearningEngine, agent: Agent, tasks: list[Task]) -> dict[str, float]:
    async def one(t: Task) -> tuple[str, bool]:
        r = await engine.recall("bench", RecallRequest(scope=Scope(agent_id=AGENT_ID), query=t.prompt),
                                allow_holdout=False)
        out = await agent.run(t, r.instructions)
        return t.family, not t.check(out)

    results = await asyncio.gather(*(one(t) for t in tasks for _ in range(SAMPLES)))
    fams = sorted({f for f, _ in results})
    by = {f: sum(ok for g, ok in results if g == f) / sum(1 for g, _ in results if g == f) for f in fams}
    by["overall"] = sum(ok for _, ok in results) / len(results)
    return by


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--checkpoint-every", type=int, default=8)
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="myelinate-bench-")
    s = Settings(env="dev", database_url=f"sqlite+aiosqlite:///{tmp}/bench.db", worker_enabled=False,
                 holdout_rate=0.0, reflect_every_n_episodes=3, reflect_debounce_seconds=0, canary_enabled=False,
                 auto_approve_medium_risk=True, curate_every_n_episodes=12, daily_token_budget=4_000_000,
                 evolve_min_uses=6)
    db = Database(s.database_url)
    await db.create_all()
    engine = LearningEngine(db, s, build_llm(s))
    worker = Worker(engine)
    agent = Agent()
    train, test = split()
    t0 = time.time()

    curve = [{"train_runs": 0, **await evaluate(engine, agent, test)}]
    print(f"baseline: {json.dumps(curve[-1])}", flush=True)
    n = 0
    train_pass = []
    for _ in range(args.epochs):
        for task in train:
            r = await engine.recall("bench", RecallRequest(scope=Scope(agent_id=AGENT_ID), query=task.prompt),
                                    allow_holdout=False)
            out = await agent.run(task, r.instructions)
            errs = task.check(out)
            train_pass.append(not errs)
            await engine.record("bench", EpisodeIn(
                scope=Scope(agent_id=AGENT_ID), recall_id=r.recall_id, task=task.prompt, framework="benchmark",
                messages=[Message(role="user", content=task.prompt), Message(role="assistant", content=out)],
                outcome=Outcome.failure if errs else Outcome.success,
                verification=Verification(passed=not errs, details="\n".join(errs) or "all checks passed",
                                          source=f"{task.family}-ci")))
            while await worker.run_once():  # let learning finish before the next run (deterministic protocol)
                pass
            n += 1
            if n % args.checkpoint_every == 0:
                curve.append({"train_runs": n, **await evaluate(engine, agent, test)})
                print(f"after {n} runs: {json.dumps(curve[-1])}", flush=True)

    async with db.session() as ses:
        from sqlalchemy import select

        usage = (await ses.scalars(select(UsageRow))).all()
        from myelinate.db import SkillRow
        skills = (await ses.scalars(select(SkillRow))).all()
    md = {sk.name: await engine.skill_markdown("bench", AGENT_ID, sk.name) for sk in skills}
    learn_in = sum(u.input_tokens for u in usage)
    learn_out = sum(u.output_tokens for u in usage)
    result = {
        "agent_model": AGENT_MODEL, "learning_model": s.llm_model, "epochs": args.epochs,
        "train_tasks": len(train), "test_tasks": len(test), "samples_per_test_task": SAMPLES,
        "curve": curve, "train_pass_rate_in_order": train_pass,
        "learning_llm": {"calls": sum(u.calls for u in usage), "input_tokens": learn_in, "output_tokens": learn_out,
                         "by_purpose": {u.purpose: u.calls for u in usage}},
        "agent_llm": {"calls": agent.calls, "input_tokens": agent.input_tokens, "output_tokens": agent.output_tokens},
        "skills_learned": md, "wall_seconds": round(time.time() - t0),
    }
    out = Path(__file__).parent / "results.json"
    out.write_text(json.dumps(result, indent=2))
    print(f"\nwrote {out}\nbaseline {curve[0]['overall']:.0%} -> final {curve[-1]['overall']:.0%} on held-out tasks; "
          f"skills: {list(md)}; learning calls {result['learning_llm']['calls']}", flush=True)
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
