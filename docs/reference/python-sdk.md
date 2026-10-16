# Python SDK

The `myelinate` package includes clients and framework adapters. Install it with the extras you need:

```bash
pip install myelinate             # client + gateway
pip install "myelinate[maf]"      # + Microsoft Agent Framework adapter
pip install "myelinate[otel]"     # + OpenTelemetry tracing of the gateway itself
```

## Clients

Both clients implement the same `LearnerClient` protocol, so adapters work with either.

### `GatewayClient`

Talks to a remote gateway over HTTP. This is recommended for teams: knowledge and governance are shared across
replicas and frameworks.

```python
from myelinate import GatewayClient

client = GatewayClient(base_url="https://myelinate.internal", api_key="myl_...", timeout=10.0, retries=2)
```

`base_url` and `api_key` default to the `MYELINATE_URL` and `MYELINATE_API_KEY` environment variables. Requests that
fail with a connection error, 429 or 5xx are retried with backoff. `record()` attaches an idempotency key, so a
retried upload never records an episode twice.

### `EmbeddedClient`

Runs the whole engine in your process, with no server. It suits notebooks, tests, edge devices and single-process apps.

```python
from myelinate import EmbeddedClient

client = await EmbeddedClient(tenant_id="local").start()   # starts a background worker too
...
await client.aclose()
```

It uses the same [configuration](configuration.md), for example `MYELINATE_DATABASE_URL` and `ANTHROPIC_API_KEY`.

### Methods

| Method | Purpose |
|---|---|
| `await recall(RecallRequest)` → `RecallResponse` | Skills, memories and profile for a task, plus ready-to-inject `instructions` |
| `await record(EpisodeIn)` → `EpisodeOut` | Record a finished run |
| `await feedback(FeedbackIn)` | Rate a run, optionally with a correction |
| `await remember(MemoryIn)` → `list[str]` | Add a memory (reconciled against existing ones) |
| `await ask(AskIn)` → `AskOut` | Answer a question from learned evidence |
| `await skill_markdown(agent_id, name)` → `str or None` | A skill as SKILL.md |
| `await aclose()` | Close connections |

The models live in `myelinate.models`: `RecallRequest`, `EpisodeIn`, `FeedbackIn`, `MemoryIn`, `AskIn`, `Scope`,
`Message`, `ToolCall`, `Outcome` and `Verification`. Their fields are listed in the [REST API schemas](api.md#schemas).

```python
from myelinate.models import EpisodeIn, Message, Outcome, RecallRequest, Scope, Verification

r = await client.recall(RecallRequest(scope=Scope(agent_id="support-bot", user_id="alice"), query=task))
system_prompt = BASE_PROMPT + "\n\n" + r.instructions
...
await client.record(EpisodeIn(
    scope=Scope(agent_id="support-bot", user_id="alice"), recall_id=r.recall_id, task=task,
    messages=[Message(role="user", content=task), Message(role="assistant", content=answer)],
    outcome=Outcome.success, verification=Verification(passed=True, details="order refunded"),
))
```

## Tools for your agent

`make_tools(client, agent_id, user_id=None)` returns two plain async functions with docstrings. Most frameworks
accept these directly as tools:

- `skill_view(name)` loads a skill listed under "Other learned skills".
- `remember(content, kind="fact")` saves a fact, preference or lesson.

## Adapters

| Adapter | Import | Guide |
|---|---|---|
| `SelfLearningProvider` | `myelinate.adapters.maf` | [Microsoft Agent Framework](../integrations/microsoft-agent-framework.md) |
| `SelfLearning` (context manager per turn) | `myelinate.adapters.generic` | [LangGraph, CrewAI and others](../integrations/other-frameworks.md) |

Both fail open by default: if the gateway is unreachable, your agent runs without learned context and a warning is
logged.
