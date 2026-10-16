# Microsoft Agent Framework

`SelfLearningProvider` is a Microsoft Agent Framework (MAF) [context provider](https://learn.microsoft.com/agent-framework/).
Attach it to an agent and every run recalls learned skills and memories first and records the episode afterwards.
Your agent code does not change.

## Install

```bash
pip install "myelinate[maf]"     # pulls agent-framework-core
```

The `maf` extra installs `agent-framework-core`. Install the MAF chat client package you use separately, for example
`agent-framework-anthropic` or `agent-framework-openai`.

## Minimal example

```python
from agent_framework import Agent

from myelinate.adapters.maf import SelfLearningProvider
from myelinate.client import GatewayClient

learner = SelfLearningProvider(GatewayClient(), agent_id="support-bot")
agent = Agent(chat_client, instructions="You are a support agent.", context_providers=[learner])

session = agent.create_session()
session.state["myelinate_user_id"] = "user-42"     # optional: per-user memories and preferences
result = await agent.run("I want a refund for order 5521", session=session)

await learner.flush()                               # before shutdown: wait for background uploads
```

A complete, runnable version is in [`examples/maf_agent.py`](https://github.com/notaaaronfr/myelinate/blob/main/examples/maf_agent.py).
It uses MAF's `AnthropicClient` when `ANTHROPIC_API_KEY` is set and `agent-framework-anthropic` is installed, and
otherwise a scripted stand-in client so it runs without a model.

## What the provider does

**Before each run** (`before_run`):

1. Joins the text of the run's input messages into a query. Runs with no text input are skipped.
2. Calls recall with `agent_id`, the user id (see below) and the MAF `session.session_id`.
3. Adds the returned `instructions` to the run with `context.extend_instructions(...)`.
4. If `add_tools=True` (the default), adds two tools for this run:
    - `skill_view(name)` loads the full text of a learned skill listed under "Other learned skills".
    - `remember(content, kind="fact")` saves a durable memory. `kind` is `fact`, `preference` or `lesson`.

**After each run** (`after_run`):

1. Collects input and response messages and the function calls/results in the response.
2. Determines the outcome: `outcome_fn(context)` if given; otherwise `unknown` when there is a response and `failure`
   when there is none.
3. Attaches `verify_fn(context)` if given.
4. Credits any skill the model loaded with `skill_view` during the run.
5. Uploads the episode in a background task, so the response is not delayed. The resulting episode id is stored in
   the session state under the provider's `source_id`: `session.state["myelinate"]["last_episode_id"]`.

!!! note
    If `agent.run(...)` raises (for example the model call fails), MAF does not call `after_run`, so no episode is
    recorded for that run. Use `outcome_fn` / `verify_fn` for failures that still produce a response.

## Constructor

```python
SelfLearningProvider(
    client,                    # GatewayClient or EmbeddedClient
    agent_id="default",        # knowledge namespace
    *,
    user_id=None,              # fallback user id when the session doesn't set one
    source_id="myelinate",     # MAF provider id; also the session.state key for provider state
    add_tools=True,            # add skill_view and remember
    outcome_fn=None,           # Callable[[SessionContext], Outcome]
    verify_fn=None,            # Callable[[SessionContext], Verification | None]
    fail_open=True,            # False: a recall failure raises instead of running without learned context
)
```

## GatewayClient or EmbeddedClient

Both implement the same `LearnerClient` protocol, so the provider works with either.

### GatewayClient (recommended)

```python
from myelinate.client import GatewayClient

client = GatewayClient()   # MYELINATE_URL (default http://localhost:8080), MYELINATE_API_KEY
# or explicitly:
client = GatewayClient("https://learner.internal", api_key="myl_...", timeout=10.0, retries=2)
```

Talks to a shared gateway over HTTP. Use this in production: knowledge is shared across replicas, frameworks and
teams, and governance, review and audit happen centrally. Retries transport errors, 5xx and 429.

### EmbeddedClient

```python
from myelinate.client import EmbeddedClient

client = await EmbeddedClient(tenant_id="local").start()   # creates tables, starts a background worker
...
await client.aclose()
```

Runs the learning engine in-process against `MYELINATE_DATABASE_URL` (default `./myelinate.db`), with the same
settings as the gateway (`MYELINATE_*` env vars). Good for local development, single-node or edge deployments. There is
no auth and no REST API for reviewers, and the knowledge lives with that database.

## Who is the user

The user id is read per run from `session.state["myelinate_user_id"]`, falling back to the `user_id` passed to the
constructor. Set it once per session:

```python
session = agent.create_session()
session.state["myelinate_user_id"] = current_user.id
```

With a user id, recall includes that user's preferences and profile card, and `remember` calls from the agent are
scoped to that user. Without one, everything is agent-level.

## Outcome and verification

`outcome_fn` and `verify_fn` receive MAF's `SessionContext` after the run; `context.response` is the agent response
and `context.input_messages` the input. Verification is the strongest learning signal: wire it to a real check if you
have one. See [what to send](overview.md#what-to-send-after-a-run-and-why-it-matters).

```python
from myelinate.models import Outcome, Verification

def outcome(ctx) -> Outcome:
    text = ctx.response.text if ctx.response else ""
    return Outcome.failure if "I can't help" in text else Outcome.unknown

def verify(ctx) -> Verification | None:
    ticket = lookup_ticket(ctx)                     # your system of record
    if ticket is None:
        return None                                 # no ground truth for this run
    return Verification(passed=ticket.resolved, details=f"ticket {ticket.id}: {ticket.status}",
                        source="helpdesk")

learner = SelfLearningProvider(GatewayClient(), agent_id="support-bot", outcome_fn=outcome, verify_fn=verify)
```

Return `Outcome.unknown` when you don't know. With an LLM configured on the gateway, unknown outcomes are judged, and
only confident judgements can change skills.

## Feedback after the run

Uploads are asynchronous. Call `flush()` first, then read the episode id from the session state:

```python
from myelinate.models import FeedbackIn

await learner.flush()
episode_id = session.state["myelinate"]["last_episode_id"]
await client.feedback(FeedbackIn(episode_id=episode_id, score=0.0,
                                 correction="Refunds for gift orders go to the purchaser."))
```

In a stateless service, look the episode up by session instead:
`GET /v1/episodes/latest?agent_id=support-bot&session_id=<session.session_id>`.

## flush() and shutdown

Episodes upload in background tasks. Call `await learner.flush()` before the process exits (and in tests) so pending
uploads complete. `flush()` never raises; failed uploads are logged as `myelinate.record_failed`.

## Combining with MAF telemetry

MAF emits OpenTelemetry GenAI spans. You can also send those to Myelinate for zero-code recording (see
[OpenTelemetry](opentelemetry.md)), but don't do both for the same agent: each run would be recorded twice under
different idempotency keys. Use the provider when you want recall; it records too.

## .NET

The adapter is Python only. For MAF on .NET:

- Call the [REST API](other-frameworks.md#raw-rest-any-language) from an `AIContextProvider`
  (or middleware): `POST /v1/recall` before the run, `POST /v1/episodes` after.
- Or connect the agent to the gateway's [MCP endpoint](mcp.md) as an MCP tool source and let the model call `recall`
  and `record_episode`.
- Or export MAF's OpenTelemetry traces to the gateway for recording ([OpenTelemetry](opentelemetry.md)), plus a recall
  call.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ImportError: install the Microsoft Agent Framework` | Install the extra: `pip install "myelinate[maf]"`. |
| Warning `myelinate.recall_failed` | Gateway unreachable, wrong `MYELINATE_URL`, or 401/403. The run continued without learned context. |
| Warning `myelinate.record_failed` | Same causes for the upload. Check the API key has the `learn` scope. |
| No episodes appear in short scripts | The process exited before the background upload finished. `await learner.flush()`. |
| Instructions are empty on some runs | Nothing relevant learned yet, or the run is in the 5% holdout group. Both are normal. |
| "The provided chat client does not support function invoking" | The chat client has no function-calling layer, so `skill_view`/`remember` can't be used. Use a client with function calling, or `add_tools=False`. |
