# MCP

The gateway serves a [Model Context Protocol](https://modelcontextprotocol.io/) endpoint, so any MCP client can use
Myelinate's learning tools: Claude Code, Claude Desktop, Cursor, Hermes, or an agent framework's MCP client.

- **URL:** `https://<gateway>/mcp`
- **Transport:** streamable HTTP (stateless, JSON responses). There is no stdio or SSE transport.
- **Auth:** the same bearer credential as the REST API: `Authorization: Bearer myl_...`. Every request to `/mcp`,
  including `initialize` and `tools/list`, needs a valid credential; without one the gateway answers `401` with
  `WWW-Authenticate: Bearer`. The tools need the `learn` scope, which is checked per call. Requests are rate limited like
  the REST API. A dev gateway with no credentials configured needs no header.

## Connect a client

### Claude Code

```bash
claude mcp add --transport http myelinate https://learner.internal/mcp \
  --header "Authorization: Bearer $MYELINATE_API_KEY"
```

Or commit a project-level `.mcp.json` that reads the key from the environment:

```json
{
  "mcpServers": {
    "myelinate": {
      "type": "http",
      "url": "${MYELINATE_URL:-http://localhost:8080}/mcp",
      "headers": {
        "Authorization": "Bearer ${MYELINATE_API_KEY}"
      }
    }
  }
}
```

### Cursor

`.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` for all projects:

```json
{
  "mcpServers": {
    "myelinate": {
      "url": "https://learner.internal/mcp",
      "headers": {
        "Authorization": "Bearer ${env:MYELINATE_API_KEY}"
      }
    }
  }
}
```

### Claude Desktop

`claude_desktop_config.json` launches local (stdio) servers. Bridge to the gateway with
[`mcp-remote`](https://github.com/geelen/mcp-remote):

```json
{
  "mcpServers": {
    "myelinate": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://learner.internal/mcp", "--header", "Authorization:${AUTH_HEADER}"],
      "env": { "AUTH_HEADER": "Bearer myl_..." }
    }
  }
}
```

The header value is passed through `env` without a space after the colon because some clients mangle spaces inside
`args`.

### Any other client

Point a streamable-HTTP MCP client at `<gateway>/mcp` and send the `Authorization` header on every request. The server
is stateless, so no session affinity is needed behind a load balancer. To check connectivity by hand:

```bash
curl -s -X POST "${MYELINATE_URL:-http://localhost:8080}/mcp" \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -H "Authorization: Bearer $MYELINATE_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Tools

All tools take an optional `agent_id` (default `"default"`). Set it, either in the client's instructions ("use
agent_id `acme-coding`") or by relying on the default, so all of a team's sessions share one knowledge namespace.

| Tool | Arguments | Returns |
|---|---|---|
| `recall` | `query` (required), `agent_id`, `user_id`, `session_id` | `{recall_id, instructions}`. `instructions` is `"(nothing learned yet for this task)"` when there is nothing relevant. |
| `skill_view` | `name` (required), `agent_id` | The skill as SKILL.md text, or `No skill named '<name>'.` |
| `remember` | `content` (required), `kind` (`fact` \| `preference` \| `lesson`, default `fact`), `agent_id`, `user_id` | `Saved.`, or `Already known.` for a duplicate. |
| `record_episode` | `task`, `outcome`, `summary` (required); `recall_id`, `error`, `tests_passed`, `test_details`, `skills_used`, `agent_id`, `user_id`, `session_id` | `{episode_id, reflection_scheduled}` |
| `feedback` | `episode_id`, `score` (0.0–1.0) (required); `comment`, `correction` | `Feedback recorded.` |
| `ask` | `question` (required), `agent_id`, `user_id` | `{answer, evidence}`: an answer drawn only from learned skills, memories and the user profile. |

Details:

- `record_episode.outcome` is `success`, `failure`, `partial` or `unknown`; anything else is stored as `unknown`.
  `summary` becomes the assistant message of the episode, so describe what was asked, what was done and what
  happened.
- `tests_passed` (boolean) with `test_details` becomes the episode's verification, the strongest learning signal.
  Encourage the model to pass real test or check output there.
- `skills_used` credits skills the model loaded with `skill_view`.
- `ask` needs an LLM configured on the gateway; without anything learned it answers "Nothing has been learned about
  that yet."

The server also sends instructions on `initialize` telling the model to call `recall` at the start of a non-trivial
task, load listed skills with `skill_view`, save durable facts with `remember`, and call `record_episode` when the task
ends.

## Allowed hosts

The MCP endpoint has DNS-rebinding protection: it only accepts requests whose `Host` header is in
`MYELINATE_MCP_ALLOWED_HOSTS`. The default is `["localhost:*", "127.0.0.1:*"]`, so **a gateway reached through any
other hostname answers `421 Invalid Host header` on `/mcp`** until you add that hostname. The value is a JSON list;
`host:*` matches any port, and a bare `host` matches requests without a port (the usual case behind TLS on 443):

```bash
MYELINATE_MCP_ALLOWED_HOSTS='["learner.internal","learner.internal:*","localhost:*"]'
```

Behind a reverse proxy, list the host name the proxy forwards in `Host`. Requests that carry an `Origin` header (from
browsers) must also match `MYELINATE_CORS_ORIGINS`. See [Configuration](../reference/configuration.md).

## Limitations

- **The model decides.** With MCP, recall and recording happen only if the model chooses to call the tools. Coverage is
  typically partial: short tasks often skip `recall`, and sessions that end abruptly skip `record_episode`. Where you
  control the agent code, an [adapter or explicit call](overview.md#choosing-an-integration-path) is deterministic.
  You can raise coverage by telling the model in its system prompt or project instructions (e.g. `CLAUDE.md`) to call
  `recall` first and `record_episode` last.
- **Self-reported outcomes.** The model grades its own run in `record_episode`. Ask it to report `unknown` unless tests
  or the user confirmed the result, and to pass real test output via `tests_passed` / `test_details`. Use the
  `feedback` tool or `POST /v1/feedback` for user verdicts.
- **No per-user identity.** The MCP credential identifies the tenant, not the end user. Pass `user_id` explicitly if
  you need per-user memories.
- **Recording only what the model summarizes.** An MCP episode holds the task and the model's summary, not the full
  transcript or tool calls, so reflection has less to work with than with an adapter.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `421 Invalid Host header` | Add the gateway's hostname to `MYELINATE_MCP_ALLOWED_HOSTS`. |
| `403 Invalid Origin header` | The client sends an `Origin` header that is not in `MYELINATE_CORS_ORIGINS`. |
| `401 Unauthorized` on connect | Missing or invalid token. Check the `Authorization` header your client sends. |
| Tool calls return `isError: true` ("Error executing tool ...") | The key lacks the `learn` scope, or an argument is invalid (e.g. an unknown `kind`). The error text is generic, so check both. |
| `remember` returns an error | `kind` must be `fact`, `preference` or `lesson`. |
| The model never calls the tools | Add explicit instructions to call `recall` at the start and `record_episode` at the end. |
