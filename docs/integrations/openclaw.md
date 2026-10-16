# OpenClaw

The OpenClaw plugin in [`adapters/openclaw/`](https://github.com/notaaaronfr/myelinate/tree/main/adapters/openclaw)
gives any OpenClaw agent the learning loop: it recalls learned context before each turn, records the turn after it, and
adds three tools. It needs no changes to your agents.

## Requirements

- OpenClaw `>=2026.10.0` (the plugin is type-checked against `openclaw@2026.10.4`).
- Node.js `>=24.16.0 <25` or `>=26.1.0` (the plugin's `engines` field; Node 25 is not supported).
- A reachable Myelinate gateway and, outside dev mode, an API key with the `learn` scope.

## Install

From a checkout of the repository:

```bash
cd adapters/openclaw
npm install --no-audit --no-fund          # installs the plugin's dependency (@sinclair/typebox)
cd ../..

openclaw plugins install ./adapters/openclaw --link --force --accept-capabilities
openclaw config set plugins.entries.myelinate.config.gatewayUrl "https://learner.internal:8080"
openclaw config set plugins.entries.myelinate.hooks.allowConversationAccess true
openclaw plugins enable myelinate
```

What the flags do:

- `--link` loads the plugin from the directory in place (it is added to `plugins.load.paths`) instead of copying it,
  so `git pull` updates it. Run `openclaw plugins reload myelinate` after pulling.
- `--force` confirms the local source for a non-interactive install. Review the source first.
- `--accept-capabilities` records consent for the capabilities the plugin declares (the three tools).

`hooks.allowConversationAccess` is **required**. The plugin uses the `before_prompt_build` and `agent_end` hooks, and
OpenClaw only lets non-bundled plugins use conversation hooks with this grant. Note that it sits under `hooks`, not
under the plugin's `config`.

Check the result:

```bash
openclaw plugins doctor
openclaw plugins inspect myelinate --runtime
```

`inspect --runtime` should list the hooks `before_prompt_build` and `agent_end` and the tools `skill_view`, `remember`
and `rate_last_turn`.

Instead of `config set`, you can merge the entry from
[`examples/openclaw-config.json5`](https://github.com/notaaaronfr/myelinate/blob/main/examples/openclaw-config.json5):

```bash
openclaw config patch --file examples/openclaw-config.json5 --dry-run
openclaw config patch --file examples/openclaw-config.json5
```

```json5
{
  plugins: {
    entries: {
      myelinate: {
        enabled: true,
        hooks: { allowConversationAccess: true },
        config: {
          gatewayUrl: "http://localhost:8080",
          agentId: "openclaw",
          perUserMemory: true,
          timeoutMs: 4000,
        },
      },
    },
  },
}
```

## Configuration

Plugin config lives under `plugins.entries.myelinate.config` (schema in `openclaw.plugin.json`):

| Field | Type | Default | Description |
|---|---|---|---|
| `gatewayUrl` | string | (required) | Myelinate base URL, e.g. `https://learner.internal:8080`. A path prefix is kept, so a gateway behind `https://host/myelinate` works. |
| `apiKey` | string | none | Tenant API key (`myl_...`) with the `learn` scope. Prefer the `MYELINATE_API_KEY` env var. |
| `agentId` | string | `openclaw` | Knowledge namespace. OpenClaw agents with the same `agentId` share skills and memories. |
| `perUserMemory` | boolean | `true` | Scope preferences and memories to the message sender. |
| `timeoutMs` | number | `4000` | Timeout per gateway request. Requests are retried twice on network errors, 5xx and 429. |

Environment variables, used when the config field is not set:

| Variable | Used for |
|---|---|
| `MYELINATE_URL` | `gatewayUrl` (falls back to `http://localhost:8080`) |
| `MYELINATE_API_KEY` | `apiKey` |

They must be set in the environment of the OpenClaw gateway process. Because `gatewayUrl` is required by the schema,
set it in config; the env fallback mainly matters for the API key.

To give different OpenClaw agents separate knowledge, run them with different `agentId`s (one plugin config per
OpenClaw instance). To share a skill library with agents in other frameworks, use the same `agentId` there.

## What it does

### Hooks

| Hook | Behaviour |
|---|---|
| `before_prompt_build` | Takes the current user message, calls recall with `agentId`, the sender as `user_id` (when `perUserMemory` is on) and the OpenClaw session key as `session_id`. If anything was learned, it prepends `<learned-context>…</learned-context>` to the turn's context. |
| `agent_end` | Records the turn: the user and assistant messages (last 40), tool calls with results and errors, skills loaded with `skill_view`, duration, the `recall_id`, and the outcome. |

The outcome is `failure` when OpenClaw reports the run as unsuccessful or with an error, and `unknown` otherwise.
Unknown outcomes are judged by the gateway's LLM (when configured) and refined by `rate_last_turn` feedback.

### Tools

| Tool | Parameters | Behaviour |
|---|---|---|
| `skill_view` | `name` | Loads a learned skill's full text (the ones listed under "Other learned skills"). The skill is credited with this turn's outcome. |
| `remember` | `content`, `kind` (`fact` \| `preference` \| `lesson`, default `fact`) | Saves a durable memory, scoped to the sender when `perUserMemory` is on. |
| `rate_last_turn` | `score` (0–1), `comment?`, `correction?` | Records the user's verdict on the previous answer when they clearly approve or complain. Finds the previous episode in memory, or via `GET /v1/episodes/latest` for the session (so it works across restarts and replicas). |

`rate_last_turn` is the plugin's main feedback channel. A correction ("no, the staging cluster is `stg-2`") is the
most useful thing a user can give; see [what to send](overview.md#what-to-send-after-a-run-and-why-it-matters).

### Fail-open

Every gateway call fails open. If recall fails the turn runs without learned context and the plugin logs
`[myelinate] recall failed, continuing without learned context`. If recording fails it logs
`[myelinate] failed to record episode`. The turn itself is never blocked beyond `timeoutMs` per request (plus retries).

## Zero-code alternative: diagnostics-otel

OpenClaw's bundled `diagnostics-otel` plugin can export traces, which Myelinate can ingest to record turns without this
plugin: point its `http/protobuf` trace exporter at the gateway's `/v1/traces`. It does not inject
learned context. See [OpenTelemetry](opentelemetry.md#openclaw). Use this plugin when you want agents to use what was
learned.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Hooks don't run; `inspect --runtime` shows no hooks, or doctor reports a blocked conversation hook | Set `plugins.entries.myelinate.hooks.allowConversationAccess` to `true` (under `hooks`, not `config`). Also check `hooks.allowPromptInjection` is not `false` and, if you use `plugins.allow`, that it includes `myelinate`. |
| Plugin fails to load: `Cannot find module '@sinclair/typebox'` | Run `npm install` in `adapters/openclaw` (linked installs use the directory as is). |
| Unsupported Node version | Use Node 24.16+ (not 25) or 26.1+. |
| Config validation error on `myelinate` | The schema rejects unknown keys and requires `gatewayUrl`. Allowed keys: `gatewayUrl`, `apiKey`, `agentId`, `perUserMemory`, `timeoutMs`. |
| Log: `recall failed ... myelinate 401` / `403` | Missing or wrong API key, or the key lacks the `learn` scope. |
| Log: `recall failed ... TimeoutError` / `fetch failed` | Gateway unreachable from the OpenClaw host, or slower than `timeoutMs`. Check `curl $MYELINATE_URL/healthz`. |
| No learned context injected | Normal until something has been learned, and for the 5% holdout runs. Check with `curl -X POST $MYELINATE_URL/v1/recall ...` using the same `agent_id`. |
| `rate_last_turn` says "No previous turn to rate." | No episode was recorded for this session yet (first turn, or recording failed). |
| Changed plugin source, nothing happens | `openclaw plugins reload myelinate`. |

To develop against the plugin, type-check it with `npm run typecheck` in `adapters/openclaw`. The plugin's REST
client (`client.ts`) is reusable outside OpenClaw; see [TypeScript](typescript.md).
