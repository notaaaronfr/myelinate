# Other frameworks

For any Python framework without a dedicated adapter, use the generic adapter `SelfLearning`. It works anywhere you can
run code before and after an agent run: LangGraph, CrewAI, OpenAI Agents SDK, Google ADK, AutoGen, Semantic Kernel,
Strands, or a hand-written loop. For other languages, call the [REST API](#raw-rest-any-language) directly.

## The generic adapter

```bash
pip install myelinate
```

```python
from myelinate.adapters.generic import SelfLearning
from myelinate.client import GatewayClient
from myelinate.models import Outcome

learner = SelfLearning(GatewayClient(), agent_id="research-bot", framework="langgraph")

async with learner.turn(task, user_id="u1", session_id="thread-7") as t:
    system_prompt = BASE_PROMPT + "\n\n" + t.instructions      # 1. inject learned context
    answer = await run_my_agent(system_prompt, task)           # 2. run your agent
    t.add_message("assistant", answer)                          # 3. describe the run
    t.add_tool_call("search", {"q": "..."}, result="...")
    t.verify(passed=tests_ok, details=test_log)                 # 4. ground truth, if you have it
    t.outcome = Outcome.success                                 #    or leave it unknown
# on exit the episode is recorded; t.episode_id is set
```

`SelfLearning(client, agent_id="default", framework="generic", fail_open=True)`:

- `client` is a `GatewayClient` (reads `MYELINATE_URL`, default `http://localhost:8080`, and `MYELINATE_API_KEY`) or an
  in-process `EmbeddedClient`; see [GatewayClient or EmbeddedClient](microsoft-agent-framework.md#gatewayclient-or-embeddedclient).
- `framework` is a free-text label stored on episodes.
- `fail_open=False` makes a recall failure raise instead of running with empty instructions.

`learner.turn(task, *, user_id=None, session_id=None)` recalls on entry and records on exit. Inside the block you get
a `Turn`:

| Member | Use |
|---|---|
| `t.instructions` | Learned context to add to the system prompt. Empty when nothing applies, recall failed, or the run is a holdout. |
| `t.recall_id` | Links the episode to what was injected. Sent automatically. |
| `t.tools` | `[skill_view, remember]` as plain async functions with docstrings, scoped to this agent and user. Most frameworks accept them as tools directly. Skills loaded through this `skill_view` are added to `t.skills_loaded` automatically. |
| `t.add_message(role, content)` | Add messages. The task is already added as the first user message. |
| `t.add_tool_call(name, arguments=None, result=None, error=None)` | Record a tool call. Failed calls are where many lessons come from. |
| `t.verify(passed, details=None, source="verifier")` | Attach a ground-truth check. The strongest learning signal. |
| `t.outcome` | `Outcome.success`, `partial`, `failure` or `unknown` (default). |
| `t.skills_loaded` | Names of skills loaded during the turn; they are credited with the run's outcome. Filled by `t.tools`; append yourself if the agent loads skills another way. |
| `t.metadata` | Free-form dict stored with the episode. |
| `t.episode_id` | Set after the block exits, for feedback. |

If the block raises, the episode is recorded with `outcome=failure` and the exception text as `error`, then the
exception propagates. Recording failures are logged (`myelinate.record_failed`) and never raised.

Send feedback later with `await learner.feedback(t.episode_id, score=0.0, correction="...")`.

## LangGraph / LangChain

Build the agent inside the turn so the system prompt carries the learned context:

```python
from langchain.agents import create_agent

from myelinate.adapters.generic import SelfLearning
from myelinate.client import GatewayClient
from myelinate.models import Outcome

learner = SelfLearning(GatewayClient(), agent_id="research-bot", framework="langgraph")

async def answer(task: str, user_id: str, thread_id: str) -> str:
    async with learner.turn(task, user_id=user_id, session_id=thread_id) as t:
        agent = create_agent("anthropic:claude-opus-5-5", tools=[*my_tools, *t.tools],
                             system_prompt=BASE_PROMPT + "\n\n" + t.instructions)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": task}]})
        out = result["messages"][-1].content
        t.add_message("assistant", out)
        for m in result["messages"]:
            for call in getattr(m, "tool_calls", None) or []:
                t.add_tool_call(call["name"], call["args"])   # skill_view loads are credited automatically
        t.outcome = Outcome.unknown          # set from a real check when you have one
    return out
```

For a custom `StateGraph`, call `learner.turn(...)` around `graph.ainvoke(...)` and put `t.instructions` in the
system message your entry node builds.

## CrewAI

Wrap `kickoff_async` and put the learned context into the agent's backstory:

```python
from crewai import Agent, Crew, Task

from myelinate.adapters.generic import SelfLearning
from myelinate.client import GatewayClient

learner = SelfLearning(GatewayClient(), agent_id="market-research", framework="crewai")

async def run(topic: str) -> str:
    task_text = f"Write a one-page brief on {topic}"
    async with learner.turn(task_text) as t:
        analyst = Agent(role="Analyst", goal="Accurate, sourced briefs",
                        backstory="You are a careful market analyst.\n\n" + t.instructions)
        brief = Task(description=task_text, expected_output="A one-page brief", agent=analyst)
        result = await Crew(agents=[analyst], tasks=[brief]).kickoff_async()
        t.add_message("assistant", str(result))
    return str(result)
```

CrewAI tools must be CrewAI tool objects, so `t.tools` isn't passed here. Wrap them with CrewAI's tool decorator if
you want the agent to load skills or save memories.

## OpenAI Agents SDK

```python
from agents import Agent, Runner, function_tool

from myelinate.adapters.generic import SelfLearning
from myelinate.client import GatewayClient

learner = SelfLearning(GatewayClient(), agent_id="support-bot", framework="openai-agents")

async def handle(task: str, user_id: str) -> str:
    async with learner.turn(task, user_id=user_id) as t:
        agent = Agent(name="Support", instructions=BASE_PROMPT + "\n\n" + t.instructions,
                      tools=[*my_tools, *(function_tool(f) for f in t.tools)])
        result = await Runner.run(agent, task)
        t.add_message("assistant", str(result.final_output))
    return str(result.final_output)
```

## Google ADK

Keep one agent and runner, and pass the learned context through session state with `state_delta`. An instruction
provider function reads it on every run:

```python
from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types

from myelinate.adapters.generic import SelfLearning
from myelinate.client import GatewayClient, make_tools

AGENT_ID = "adk-support"
client = GatewayClient()
learner = SelfLearning(client, agent_id=AGENT_ID, framework="google-adk")

agent = Agent(
    name="support",
    model="gemini-2.5-flash",
    instruction=lambda ctx: BASE_PROMPT + "\n\n" + ctx.state.get("learned_context", ""),
    tools=[*my_tools, *make_tools(client, AGENT_ID)],    # skill_view, remember (agent-level)
)
runner = InMemoryRunner(agent=agent, app_name="support")

async def handle(task: str, user_id: str, session_id: str) -> str:
    # create the session once per conversation:
    # await runner.session_service.create_session(app_name="support", user_id=user_id, session_id=session_id)
    async with learner.turn(task, user_id=user_id, session_id=session_id) as t:
        final = ""
        async for event in runner.run_async(
                user_id=user_id, session_id=session_id,
                new_message=types.Content(role="user", parts=[types.Part(text=task)]),
                state_delta={"learned_context": t.instructions}):
            if event.is_final_response() and event.content and event.content.parts:
                final = "".join(p.text or "" for p in event.content.parts)
        t.add_message("assistant", final)
    return final
```

A callable `instruction` is used as is, so learned text containing `{braces}` is not treated as an ADK state template.

## A plain loop

No framework, no LLM: [`examples/generic_agent.py`](https://github.com/notaaaronfr/myelinate/blob/main/examples/generic_agent.py)
runs the full loop with a stand-in agent function. The shape with a real model client:

```python
import anthropic

from myelinate.adapters.generic import SelfLearning
from myelinate.client import GatewayClient
from myelinate.models import Outcome

llm = anthropic.AsyncAnthropic()
learner = SelfLearning(GatewayClient(), agent_id="sql-helper")

async def answer(question: str) -> str:
    async with learner.turn(question) as t:
        msg = await llm.messages.create(
            model="claude-opus-5-5", max_tokens=2000,
            system="You write PostgreSQL queries.\n\n" + t.instructions,
            messages=[{"role": "user", "content": question}])
        sql = msg.content[0].text
        t.add_message("assistant", sql)
        ok, log = run_against_staging(sql)               # your own check
        t.add_tool_call("psql", {"sql": sql}, result=None if not ok else log, error=None if ok else log)
        t.verify(passed=ok, details=log)
        t.outcome = Outcome.success if ok else Outcome.failure
    return sql
```

## Raw REST (any language)

The loop is three JSON calls. Everything below runs against a dev gateway as is; add
`-H "Authorization: Bearer $MYELINATE_API_KEY"` otherwise. Full schemas are in the [API reference](../reference/api.md)
and at `/docs` on the gateway.

```bash
URL=${MYELINATE_URL:-http://localhost:8080}

# 1. Recall before the run
curl -s -X POST "$URL/v1/recall" -H 'content-type: application/json' -d '{
  "scope": {"agent_id": "billing-bot", "user_id": "user-42"},
  "query": "Customer disputes a duplicate charge on invoice 8812",
  "session_id": "sess-9"
}'
# -> {"recall_id": "rec_...", "instructions": "## Learned skills ...", "skills": [...],
#     "skill_index": [...], "memories": [...], "profile": null, "holdout": false}

# 2. Record after the run (reuse recall_id; set an idempotency_key so retries don't duplicate)
curl -s -X POST "$URL/v1/episodes" -H 'content-type: application/json' -d '{
  "scope": {"agent_id": "billing-bot", "user_id": "user-42"},
  "idempotency_key": "billing-bot:sess-9:turn-1",
  "session_id": "sess-9",
  "recall_id": "rec_...",
  "task": "Customer disputes a duplicate charge on invoice 8812",
  "messages": [
    {"role": "user", "content": "Customer disputes a duplicate charge on invoice 8812"},
    {"role": "assistant", "content": "Found two identical charges; refunded the second one."}
  ],
  "tool_calls": [{"name": "refund", "arguments": {"charge": "ch_2"}, "result": "refunded"}],
  "outcome": "success",
  "verification": {"passed": true, "details": "ledger shows one charge", "source": "ledger"},
  "framework": "my-go-service",
  "duration_ms": 5400
}'
# -> {"episode_id": "ep_...", "reflection_scheduled": false}

# 3. Feedback later (204 No Content)
curl -s -X POST "$URL/v1/feedback" -H 'content-type: application/json' -d '{
  "episode_id": "ep_...", "score": 0.2,
  "comment": "refunded the wrong charge",
  "correction": "Refund the later of two duplicate charges, never the original."
}'
```

Useful extras:

| Call | Purpose |
|---|---|
| `GET /v1/episodes/latest?agent_id=...&session_id=...` | The latest episode of a session, to attach feedback from a stateless service. |
| `POST /v1/memories` `{"scope": {...}, "kind": "preference", "content": "..."}` | Save a memory directly. |
| `GET /v1/skills/{name}/SKILL.md?agent_id=...` | A skill's full text (what `skill_view` returns). |
| `POST /v1/ask` `{"scope": {...}, "question": "..."}` | Ask what has been learned. |
| `POST /v1/episodes:batch` | Import up to 1000 historical episodes to warm-start learning. |

Notes for client authors:

- Recall is on your request path; give it a short timeout and fail open. Record can be fire-and-forget.
- Retry 5xx, 429 and network errors with backoff. Keep the same `idempotency_key` across retries of one episode.
- `agent_id` must match `^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$`.
- `score` must be between 0.0 and 1.0.

[`examples/quickstart.sh`](https://github.com/notaaaronfr/myelinate/blob/main/examples/quickstart.sh) runs the whole
flow, including importing a skill and reading insights.
