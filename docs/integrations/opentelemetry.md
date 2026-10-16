# OpenTelemetry

If your agents already emit OpenTelemetry traces, Myelinate can record every run from those traces with no code
changes. Point an OTLP exporter at the gateway and each trace becomes an episode that feeds reflection and learning.

!!! warning "Recording only"
    OTel ingestion records runs; it does not put anything into your agent's prompt. For agents to *use* what was
    learned you still need one recall call per run: an [adapter](overview.md#choosing-an-integration-path), a
    `POST /v1/recall`, or the MCP `recall` tool. A common setup is OTel for recording plus a one-line recall call.

## Endpoint

| Endpoint | Use |
|---|---|
| `POST /v1/traces` | Set as the full traces URL (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://learner.internal/v1/traces`). |
| `POST /otlp/v1/traces` | Alias for exporters that append `/v1/traces` to a base URL (`OTEL_EXPORTER_OTLP_ENDPOINT=https://learner.internal/otlp`, or a Collector `endpoint`). |

Requirements:

- **OTLP/HTTP**, encoded as protobuf (`Content-Type: application/x-protobuf`) or JSON (`application/json`). Other
  content types get `415`. **gRPC is not supported**: set the exporter protocol to `http/protobuf`, or put a Collector
  in front.
- Bodies may be gzip-compressed (`Content-Encoding: gzip`, the default of most exporters) or uncompressed. Other
  encodings get `415`; bodies that can't be decoded get `400`.
- The credential needs the `learn` scope.

The response is `{"partialSuccess": {}, "episode_ids": [...]}`.

Headers:

| Header | Purpose |
|---|---|
| `Authorization: Bearer myl_...` | Tenant API key or OIDC token. Not needed against a dev gateway. |
| `X-Agent-Id: support-bot` | The `agent_id` for these traces, unless a span sets `myelinate.agent_id` (see [agent id](#agent-and-user)). Must be a valid agent id, otherwise the request gets `400`. |

### Exporting directly

The standard OTLP/HTTP exporters of the OpenTelemetry SDKs (Python, JavaScript, Java, .NET, Go) can send straight to the
gateway. With the usual environment variables:

```bash
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://learner.internal/v1/traces
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf       # not grpc
OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer myl_...,X-Agent-Id=support-bot"
```

Only traces are ingested. If your app also exports metrics and logs, set the traces endpoint
(`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`) rather than a base endpoint that would also receive those signals.

### Through a Collector

An [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/) is optional. It is useful when you already run
one, need gRPC on the agent side, or want to fan the same traces out to your observability backend and to Myelinate:

```yaml
# otel-collector.yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
      grpc:
        endpoint: 0.0.0.0:4317

processors:
  batch: {}

exporters:
  otlp_http/myelinate:                      # named "otlphttp" in older Collector releases
    endpoint: https://learner.internal/otlp # the exporter appends /v1/traces
    headers:
      Authorization: "Bearer ${env:MYELINATE_API_KEY}"
      X-Agent-Id: support-bot

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlp_http/myelinate]
```

The exporter's defaults (protobuf, gzip) work as is. Your agents then export to the Collector as usual
(`OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`).

## How traces become episodes

**One trace is one episode.** Spans are grouped by trace id, and the episode's idempotency key is `otel:<traceId>`.

**Traces split across exports are merged.** Exporters often send a trace in several requests (child spans end, and
export, before the root agent span). A later request for a trace that already has an episode is folded into it: new
tool calls are appended, a missing outcome, verification or error is filled in, newly loaded skills are credited, and
the reward is recomputed. An exporter retry of the same spans changes nothing, so retries never double count.

What is set by the first request that created the episode stays as it was: the `agent_id`, `user_id` and session,
and the messages once present. Spans that arrive with no agent or model-call span in their request (for example a
batch holding only tool spans) can't start an episode on their own. Put `X-Agent-Id` on the request (or
`myelinate.agent_id` on the resource) so the agent is right whichever span arrives first. To keep traces whole anyway,
use the Collector-contrib `groupbytrace` processor (optional).

**The root span.** Within a trace, the earliest span whose operation is an agent invocation is the root. If there is
none, the earliest model-call span is used. Traces with neither are ignored. The operation is read from
`gen_ai.operation.name`, or else the lower-cased `openinference.span.kind`:

| Role | Operation values |
|---|---|
| Agent (root) | `invoke_agent`, `agent`, `invoke_workflow` |
| Model call | `chat`, `generate_content`, `text_completion`, `llm` |
| Tool call | `execute_tool`, `tool`, or a span name starting with `tool.execution` |

Resource attributes are merged into every span's attributes (span attributes win), so anything below that is read
from the root span can also be set as a resource attribute, for example with `OTEL_RESOURCE_ATTRIBUTES`.

### Recognized attributes

Messages:

| Episode field | Source, in order of preference |
|---|---|
| Input messages | root `gen_ai.input.messages`; first model-call span's `gen_ai.input.messages`; root OpenInference `llm.input_messages.<N>.message.role` / `.content`; root `input.value` (as one user message) |
| Output messages | root `gen_ai.output.messages`; last model-call span's `gen_ai.output.messages`; root `output.value` (as one assistant message) |
| Task | first user message |

`gen_ai.*.messages` may be a JSON string or an array in the OTel GenAI format
`[{"role": "user", "parts": [{"type": "text", "content": "..."}]}]`, or the legacy `[{"role": ..., "content": "..."}]`.
Only `text` parts are used, and `system` messages are dropped. A string that is not JSON is taken as one user message.

Tool calls (one per tool span):

| Field | Source |
|---|---|
| name | `gen_ai.tool.name`, `tool.name`, or the span name |
| arguments | `gen_ai.tool.call.arguments` or `input.value` |
| result | `gen_ai.tool.call.result` or `output.value` (only when the call succeeded) |
| failed when | span status is `ERROR`, or `error.type` is set, or `process.exit.code` is set and not `0` |
| error | span status message, else `error.type`, else `"error"` |

Everything else:

| Episode field | Source |
|---|---|
| `agent_id` | root `myelinate.agent_id`, else the `X-Agent-Id` header, else `gen_ai.agent.name`, else `default` |
| `user_id` | root `myelinate.user_id`, else `user.id` |
| `session_id` | first `gen_ai.conversation.id` on any span of the trace, else root `session.id` |
| `skills_loaded` | every `gen_ai.skill.name` on any span (credits those skills with this run's outcome) |
| `outcome` | root `myelinate.outcome` if it is `success`, `failure`, `partial` or `unknown`; else `failure` if the root span status is `ERROR`; else `unknown` |
| `verification` | root `myelinate.verification.passed` (boolean, or the strings `true` / `1` / `yes`) with `myelinate.verification.details`; source `otel` |
| `recall_id` | root `myelinate.recall_id` |
| `error` | root span status message when the root failed |
| `duration_ms` | root span start to end |
| `framework` | `gen_ai.provider.name`, else `telemetry.sdk.name`, else `otel` |

### Agent and user

Agent ids must match `^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$` (no spaces). Precedence is `myelinate.agent_id` on the root
span or resource, then the `X-Agent-Id` header, then `gen_ai.agent.name`, then `default`. A `gen_ai.agent.name` such as
`Support Bot` is not a valid id and falls back to `default`, so set `X-Agent-Id` when your framework uses display names.
An invalid `X-Agent-Id` is rejected with `400`.

### myelinate.* attributes

Set these on the root (agent) span, or as resource attributes, when your code knows them:

| Attribute | Type | Effect |
|---|---|---|
| `myelinate.agent_id` | string | Overrides the agent id. |
| `myelinate.user_id` | string | Scopes the episode to a user. |
| `myelinate.outcome` | `success` \| `failure` \| `partial` \| `unknown` | Your own outcome label. |
| `myelinate.verification.passed` | boolean | Ground-truth check result; the strongest learning signal. |
| `myelinate.verification.details` | string | What the check saw (test output, validator message). |
| `myelinate.recall_id` | string | The `recall_id` from your recall call; links the run to exactly what was injected. No adapter sets this for you. |

Example in Python, on the span that wraps one agent run:

```python
from opentelemetry import trace

tracer = trace.get_tracer("my-app")

with tracer.start_as_current_span("invoke_agent support-bot") as span:
    span.set_attribute("gen_ai.operation.name", "invoke_agent")
    span.set_attribute("gen_ai.agent.name", "support-bot")
    span.set_attribute("myelinate.recall_id", recall.recall_id)
    result = await run_agent(...)
    span.set_attribute("myelinate.verification.passed", tests_passed)
    span.set_attribute("myelinate.verification.details", test_log[-2000:])
```

### Content capture

Message text is only available if the exporter captures it, which most frameworks treat as sensitive and turn off by
default. Without content, episodes still carry tool calls, errors, timing and outcome, but reflection has much less
to learn from. Enable content capture where your data policy allows; PII and secrets are redacted by the gateway before
storage (see [Security](../operations/security.md)).

## Try it with curl

```bash
curl -s -X POST "${MYELINATE_URL:-http://localhost:8080}/v1/traces" \
  -H 'content-type: application/json' \
  -H 'X-Agent-Id: support-bot' \
  -d '{
  "resourceSpans": [{
    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "support-bot"}}]},
    "scopeSpans": [{"spans": [
      {"traceId": "5b8efff798038103d269b633813fc60c", "spanId": "eee19b7ec3c1b174",
       "name": "invoke_agent support-bot",
       "startTimeUnixNano": "1760000000000000000", "endTimeUnixNano": "1760000002500000000",
       "attributes": [
         {"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
         {"key": "gen_ai.agent.name", "value": {"stringValue": "support-bot"}},
         {"key": "gen_ai.conversation.id", "value": {"stringValue": "conv-42"}},
         {"key": "gen_ai.input.messages", "value": {"stringValue": "[{\"role\":\"user\",\"parts\":[{\"type\":\"text\",\"content\":\"Reset the password for user 1234\"}]}]"}},
         {"key": "gen_ai.output.messages", "value": {"stringValue": "[{\"role\":\"assistant\",\"parts\":[{\"type\":\"text\",\"content\":\"Done, a reset link was sent.\"}]}]"}},
         {"key": "myelinate.verification.passed", "value": {"boolValue": true}},
         {"key": "myelinate.verification.details", "value": {"stringValue": "reset email delivered"}}]},
      {"traceId": "5b8efff798038103d269b633813fc60c", "spanId": "eee19b7ec3c1b175",
       "parentSpanId": "eee19b7ec3c1b174", "name": "execute_tool send_reset_email",
       "startTimeUnixNano": "1760000001000000000", "endTimeUnixNano": "1760000001200000000",
       "attributes": [
         {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}},
         {"key": "gen_ai.tool.name", "value": {"stringValue": "send_reset_email"}},
         {"key": "gen_ai.tool.call.arguments", "value": {"stringValue": "{\"user_id\":\"1234\"}"}},
         {"key": "gen_ai.tool.call.result", "value": {"stringValue": "queued"}}]}
    ]}]
  }]
}'
```

Sending the same payload again returns the same episode id.

## Framework setup

The snippets below show where each framework's telemetry is switched on. Flags and variable names belong to those
frameworks and change between versions; check the linked docs for your version. Each one can export
[directly](#exporting-directly) to the gateway over `http/protobuf`, or [through a Collector](#through-a-collector).

### Microsoft Agent Framework

MAF (Python) emits GenAI spans (`invoke_agent`, `chat`, `execute_tool`) with `gen_ai.agent.name` and
`gen_ai.conversation.id`. Configure the exporter once at startup and enable sensitive data to capture messages:

```python
from agent_framework.observability import configure_otel_providers

configure_otel_providers(enable_sensitive_data=True)   # or ENABLE_SENSITIVE_DATA=true
```

```bash
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://learner.internal/v1/traces
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf    # MAF's default is grpc, which the gateway doesn't accept
OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Bearer myl_...,X-Agent-Id=support-bot"
OTEL_SERVICE_NAME=support-bot
```

Use the traces-specific endpoint: a base `OTEL_EXPORTER_OTLP_ENDPOINT` would make MAF send logs and metrics to the
gateway too. Without `X-Agent-Id`, the agent's `name` becomes the `agent_id` if it is a valid id. If you
use the [`SelfLearningProvider`](microsoft-agent-framework.md), don't also ingest MAF traces for the same agent: each
run would be recorded twice. See the
[MAF observability docs](https://learn.microsoft.com/agent-framework/agents/observability).

### Google ADK

ADK emits OpenTelemetry spans for agent runs, model calls and tool execution. Point its OTLP/HTTP trace exporter at
the gateway with the standard `OTEL_EXPORTER_OTLP_*` variables (as in [Exporting directly](#exporting-directly)), and set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`
to include prompts and responses. See the [ADK observability docs](https://adk.dev/observability/)
for how your ADK version sets up its tracer provider.

### OpenClaw

OpenClaw's `diagnostics-otel` plugin exports traces (model calls, `execute_tool` / `tool.execution` spans) over
`http/protobuf`, which the gateway accepts directly:

```json5
{
  plugins: { entries: { "diagnostics-otel": { enabled: true } } },
  diagnostics: {
    enabled: true,
    otel: {
      enabled: true,
      tracesEndpoint: "https://learner.internal/v1/traces",
      protocol: "http/protobuf",
      headers: { Authorization: "Bearer myl_...", "X-Agent-Id": "openclaw" },
      traces: true,
      metrics: false,         // the gateway ingests traces only
      logs: false,
      captureContent: true,   // off by default; needed for message text
    },
  },
}
```

See OpenClaw's *OpenTelemetry export* docs for the full `diagnostics.otel` reference, and
`OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` for the latest GenAI span shapes. For recall and per-turn
feedback, use the [Myelinate OpenClaw plugin](openclaw.md) instead.

### Strands Agents

```python
from strands.telemetry import StrandsTelemetry

StrandsTelemetry().setup_otlp_exporter()   # reads the OTEL_EXPORTER_OTLP_* variables
```

See the Strands Agents docs on traces (user guide, *Observability & Evaluation*) for exporter options.

### Pydantic AI

Pydantic AI instruments agents with OpenTelemetry GenAI conventions. Without a Logfire account, configure Logfire to
only export via OTLP:

```python
import logfire

logfire.configure(send_to_logfire=False)   # exports via the OTEL_EXPORTER_OTLP_* variables
logfire.instrument_pydantic_ai()
```

See the Pydantic AI [Logfire integration docs](https://pydantic.dev/docs/ai/integrations/logfire/) ("Using OpenTelemetry") for
using the plain OpenTelemetry SDK instead.

### LangGraph, LangChain and CrewAI (OpenInference)

These frameworks don't emit GenAI spans themselves; use the [OpenInference](https://github.com/Arize-ai/openinference)
instrumentors, whose `openinference.span.kind`, `input.value` / `output.value`, `tool.name` and `llm.input_messages.*`
attributes Myelinate understands.

```python
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
trace.set_tracer_provider(provider)

# LangGraph / LangChain: pip install openinference-instrumentation-langchain
from openinference.instrumentation.langchain import LangChainInstrumentor
LangChainInstrumentor().instrument(tracer_provider=provider)

# CrewAI: pip install openinference-instrumentation-crewai
from openinference.instrumentation.crewai import CrewAIInstrumentor
CrewAIInstrumentor().instrument(tracer_provider=provider)
```

OpenInference spans carry no agent name Myelinate recognizes, so set `X-Agent-Id` in the exporter headers or
`myelinate.agent_id` as a resource attribute (`OTEL_RESOURCE_ATTRIBUTES=myelinate.agent_id=research-bot`).

## Troubleshooting

| Symptom | Cause |
|---|---|
| `415 unsupported content-type` | The exporter uses gRPC or another encoding. Set `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` (or `http/json`), or go through a Collector. |
| `400 invalid X-Agent-Id` | The header must match `^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$`. |
| `400 invalid OTLP protobuf` / `invalid JSON` | The body doesn't match its `Content-Type`, or is truncated. |
| `404` for `/v1/metrics` or `/v1/logs` | Only traces are ingested. Use the traces-specific endpoint variable, or disable metrics and logs export. |
| `episode_ids` is empty | No span in the request has an agent or model-call operation (check `gen_ai.operation.name` / `openinference.span.kind`). Requests holding only tool spans are skipped until the trace's agent span arrives. |
| Episodes have no messages | Content capture is off in the framework. |
| Episodes land under `default` | No `X-Agent-Id` and `gen_ai.agent.name` is missing or not a valid id. Set the header or `myelinate.agent_id`. |
| Labels or outcome missing | `myelinate.*` attributes must be on the root agent span or the resource. |
| 401 / 403 | Missing token or missing `learn` scope. |
