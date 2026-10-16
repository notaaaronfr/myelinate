# Integrations overview

Myelinate does not run your agent. Your agent stays in its own framework and talks to the Myelinate gateway at two
points in every run. This page explains that contract, how to pick an integration path, and which signals to send so
the system learns the right things.

## The loop

```text
            ┌──────────── 1. recall (before) ────────────┐
            │  query = the task / user message           │
            │  ← instructions, recall_id                 │
  your agent│                                            │ Myelinate
            │  2. run with instructions in the prompt    │
            │                                            │
            │  3. record (after)                         │
            │  messages, tool calls, outcome,            │
            │  verification, recall_id  →                │
            │                                            │
            │  4. feedback (any time later, optional)    │
            └────────────────────────────────────────────┘
```

1. **Recall** (`POST /v1/recall`). Send the task text and a scope (`agent_id`, optional `user_id`). You get back
   `instructions`, a ready-to-inject block of learned skills, memories and the user's profile, plus a `recall_id`.
   Put `instructions` into the system prompt (or wherever your framework takes instructions).
2. **Run** the agent as usual. If you expose the `skill_view` and `remember` tools, the agent can load more skills
   and save memories during the run.
3. **Record** (`POST /v1/episodes`). Send what happened: messages, tool calls, an outcome, an optional verification
   result, and the `recall_id`. The `recall_id` tells Myelinate exactly which skills and rules were in the prompt, so it
   credits (or blames) those and nothing else.
4. **Feedback** (`POST /v1/feedback`). When a user or downstream system reacts later, attach a score from 0.0 to
   1.0 and optionally a correction to the episode.

Reflection, curation, skill evolution and canary rollout all happen in the background from the recorded episodes.
None of it is on your request path. See [Concepts](../getting-started/concepts.md) for how that works.

### Scope: agent_id and user_id

- `agent_id` is the knowledge namespace. Agents that share an id share skills and memories. It must match
  `^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$` (letters, digits and `._:@/-`; no spaces). `*` is not a valid
  `agent_id` for recall, record or memories; shared skills are published by reviewers under `agent_id="*"` through
  `POST /v1/skills` or `POST /v1/skills/import`, and then apply to every agent in the tenant.
- `user_id` is optional. When set, preferences and a per-user profile card are recalled for that user, and facts
  learned from that user's runs stay scoped to them.
- The tenant always comes from the credential, never from the request body.

### Fail-open

Learning must never take your agent down. All official integrations fail open:

- If recall fails (gateway down, timeout, 5xx after retries), the agent runs without learned context and a warning is
  logged.
- If record fails, the episode is dropped with a warning; the agent's answer is unaffected.

The Python adapters take `fail_open=True` by default; set `fail_open=False` if you would rather a recall failure raise.
(The flag only affects recall; record failures are always logged and swallowed.) The Python `GatewayClient` retries
transport errors, 5xx and 429 twice with backoff and uses a 10 s timeout. The TypeScript client and OpenClaw plugin
default to a 4 s timeout.

Recording is idempotent: the SDKs set an `idempotency_key` on every episode, so a retried upload never counts twice.
OTLP ingestion derives the key from the trace id.

## Choosing an integration path

| Path | Use it when | Recall | Record | Effort |
|---|---|---|---|---|
| [Microsoft Agent Framework adapter](microsoft-agent-framework.md) | Python MAF agents | automatic | automatic | 3 lines |
| [OpenClaw plugin](openclaw.md) | OpenClaw agents | automatic (`before_prompt_build`) | automatic (`agent_end`) | config only |
| [Generic Python adapter](other-frameworks.md) | LangGraph, CrewAI, OpenAI Agents SDK, Google ADK, custom loops | `learner.turn(...)` | automatic on exit | ~5 lines |
| [TypeScript client](typescript.md) | Mastra, LangGraph.js, Vercel AI SDK, any Node code | `recall()` | `record()` | ~15 lines |
| [MCP](mcp.md) | MCP-capable clients you don't control the code of (Claude Code, Cursor, Claude Desktop) | the model calls `recall` | the model calls `record_episode` | config only |
| [OpenTelemetry](opentelemetry.md) | You already export GenAI traces and want learning with no code changes | not covered, needs one of the above | automatic from traces | config only |
| [REST](other-frameworks.md#raw-rest-any-language) | Any other language (Go, Java, .NET, Rust...) | `POST /v1/recall` | `POST /v1/episodes` | small |

Rules of thumb:

- **Prefer an adapter or an explicit call** whenever you own the agent code. Recall and record then happen on every
  run, deterministically, with the `recall_id` linking the two.
- **Use MCP** when you can only configure the client. It works, but the model decides whether to call the tools, so
  coverage is best-effort.
- **Use OpenTelemetry** to get recording for free across many agents, then add a recall call (adapter, REST or the MCP
  `recall` tool) where you want agents to *use* what was learned. OTel ingestion alone builds knowledge but never
  injects it.
- Paths combine. A common setup is OTel for recording plus a one-line recall call, or an adapter in production plus MCP
  for developers' coding agents, all pointing at the same `agent_id`.

## What to send after a run, and why it matters

Myelinate learns only as well as the signal it gets. It ranks the evidence for each episode in this order:

| Signal | How to send it | Strength |
|---|---|---|
| **Verification**: a ground-truth check passed or failed | `verification: {passed, details, source}` on the episode (`t.verify(...)`, `verify_fn`, `tests_passed` in MCP, `myelinate.verification.passed` in OTel) | strongest |
| **Feedback**: a user or system rated the result | `POST /v1/feedback` with `score` (0.0–1.0), `comment`, `correction` | strong |
| **Outcome**: your code's own label | `outcome: success \| partial \| failure \| unknown` | strong when not `unknown` |
| **LLM judge**: the gateway's own assessment | automatic when the outcome is `unknown` (needs an LLM configured) | used only above a confidence threshold |

The highest available signal wins: a verification result overrides feedback, which overrides the outcome label, which
overrides the judge. Feedback that arrives after a judged outcome replaces the earlier credit instead of adding to it.

Why this matters:

- **Evidence gating.** An episode with no reliable signal (outcome `unknown` and no confident judge) can still add
  memories, but it can never change a skill. If you send nothing but `unknown`, the system stays conservative and
  learns memories only.
- **Wrong labels teach wrong lessons.** Learning from unreliable feedback has been shown to push agents below their
  baseline. Don't mark every run `success` just because it returned without an exception; leave it `unknown` and let
  verification, feedback or the judge decide.
- **Corrections are the richest feedback.** `correction` ("the right answer was X") gives the reflector a concrete fix
  instead of a bare score. A feedback score below 0.5, a comment or a correction re-queues the episode for reflection.

Practical guidance:

- Wire `verification` to whatever objective check you already have: unit tests, a schema validator, "the ticket was
  closed", "the SQL ran", "the refund exists in the ledger". Put the check's output in `details`; the reflector reads it.
- Set `outcome="failure"` and `error` when the run clearly failed. The adapters do this automatically when the run raises
  (the generic adapter) or returns no response (MAF).
- Include tool calls with their results and errors. Failed tool calls are often where the lesson is.
- Send `user_id` when you have one, so preferences stay per user.
- Pass the same `session_id` to recall and record. Canary assignment is sticky per session, and stateless clients can
  look up a session's last episode with `GET /v1/episodes/latest` to attach feedback.

## Holdout runs

By default 5% of recalls are a holdout (`"holdout": true` in the recall response) and return no learned
instructions. This is deliberate: comparing those runs with the rest gives the causal lift reported by
`GET /v1/agents/{id}/insights`. Record holdout runs exactly like any other run. Change the rate with
`MYELINATE_HOLDOUT_RATE` (see [Configuration](../reference/configuration.md)).

## Credentials

Every path uses the same bearer credential: `Authorization: Bearer myl_...` (a tenant API key) or an OIDC JWT. The
recall/record/feedback loop needs the `learn` scope. Importing or authoring skills needs `review`. A gateway started with
`MYELINATE_ENV=dev` and no credentials configured accepts unauthenticated requests as tenant `dev`; never expose it.
See [Security](../operations/security.md).
