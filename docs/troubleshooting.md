# Troubleshooting

Each entry gives the **symptom**, the likely **cause**, and the **fix**. Start with the gateway logs (JSON in
`staging`/`prod`) and `GET /readyz`. Error responses are JSON with a `detail` field, and every response carries an
`x-request-id` header you can search for in the logs.

## Gateway refuses to start

**Symptom:** the process exits with
`RuntimeError: prod requires MYELINATE_BOOTSTRAP_ADMIN_KEY or OIDC configuration`.

**Cause:** `MYELINATE_ENV` defaults to `prod`, and `prod` won't start without credentials.

**Fix:** set `MYELINATE_BOOTSTRAP_ADMIN_KEY` or `MYELINATE_OIDC_JWKS_URL` (plus issuer and audience; see
[Security](operations/security.md#oidc-jwt)).

A second start-up error, `RuntimeError: OIDC requires MYELINATE_OIDC_ISSUER and MYELINATE_OIDC_AUDIENCE outside dev`,
means the JWKS URL is set without both of the other two. Set them. For local work only, set `MYELINATE_ENV=dev`. Remember that a `.env` file
in the working directory is also read and can override what you expect.

**Related:** in `staging` the gateway starts without credentials, but every request gets `401`. In `dev`, auth is only
disabled when *no* credentials are configured. Setting a bootstrap key in `dev` turns auth on.

## 401 Unauthorized

| `detail` | Cause | Fix |
|---|---|---|
| `missing bearer token` | No `Authorization: Bearer …` header | Send the header. MCP clients need it configured as a request header. |
| `invalid api key` | Unknown or revoked `myl_` key | Check for copy errors. Revocation takes effect within 30 s per process. |
| `invalid credentials` | The token isn't a `myl_` key, the bootstrap key, or (with OIDC off) a JWT | Configure OIDC, or use an API key |
| `invalid token: Invalid audience` | `MYELINATE_OIDC_AUDIENCE` differs from the token's `aud` | Set it to the token's `aud`, for example the client ID for Entra v2 tokens |
| `invalid token: Invalid issuer` | Issuer mismatch, often a trailing slash (Auth0) or a v1/v2 mismatch (Entra) | Copy `iss` from a decoded token exactly |
| `invalid token: Signature has expired` | Expired JWT | Refresh the token, and check clock skew |
| `invalid token: Unable to find a signing key…` | Wrong JWKS URL, or a key rotated at the IdP | Check `MYELINATE_OIDC_JWKS_URL` |

## 400 bootstrap admin key requires X-Tenant-Id

The bootstrap key isn't bound to a tenant. Add `-H "X-Tenant-Id: <tenant>"`.

## 403 Forbidden

- `missing scope 'review'` (or `learn`/`admin`): the credential lacks that scope. **Scopes aren't hierarchical.** An
  `admin` key can't review changesets unless it also has `review`. Mint a key with every scope the caller needs, or
  fix the IdP scopes or roles. See the scope table in [Security](operations/security.md#authorisation-scopes).
- `token has no 'tid' claim`: the JWT lacks the configured tenant claim. Set `MYELINATE_OIDC_TENANT_CLAIM`, or add the
  claim at the IdP.
- `Invalid Origin header` on `/mcp`: a browser-based client sent an `Origin` that isn't in `MYELINATE_CORS_ORIGINS`.

## 429 Too Many Requests

**Cause:** either the per-principal limit (`MYELINATE_RATE_LIMIT_PER_MINUTE`, default 600) or the pre-auth per-IP
limit (`MYELINATE_IP_RATE_LIMIT_PER_MINUTE`, default 3000) was hit. Responses carry `Retry-After: 60`. A `429` with
`Retry-After: 3600` from `/v1/ask` is the daily token budget instead (see below).

**Fix:**

- Behind a load balancer, all clients may share the load balancer's IP. Set `FORWARDED_ALLOW_IPS` to the proxy
  address or CIDR so `X-Forwarded-For` is used.
- Bulk imports: use `POST /v1/episodes:batch` (up to 1000 episodes per request) instead of single calls.
- Raise the limits, or give each agent its own key (the principal limit is per key or OIDC subject).
- All bootstrap-key requests share one bucket. Don't use the bootstrap key for traffic.

## 413 Request Entity Too Large

**Cause:** the body exceeds `MYELINATE_MAX_REQUEST_BYTES` (default 4 MB), or a batch has more than 1000 episodes.

**Fix:** trim transcripts (huge tool outputs are rarely useful for learning), split batches, or raise the limit. A
reverse proxy may also impose its own limit (for example NGINX `client_max_body_size`).

## 422 Unprocessable Entity

**Cause:** request validation. The `detail` array names the field. Common cases:

- `agent_id` with spaces or other characters: it must match `^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$`.
- `agent_id="*"` in a recall, episode, memory, feedback or ask body: the shared namespace can only be written through
  `POST /v1/skills` or `POST /v1/skills/import` (`review` scope). Agents read shared knowledge automatically.
- Skill names must be lowercase kebab-case (`^[a-z0-9][a-z0-9-]{1,63}$`).
- `outcome` must be `success`, `failure`, `partial` or `unknown`. `score` must be between 0 and 1.
- Field length limits (recall query 100k chars, memory 4000), or a `limit` query parameter above its maximum.

## 409 Conflict

`changeset is applied|rejected|canary|…`: only `pending` changesets can be reviewed. A running canary can't be
cancelled by review; roll the skill back instead ([Governance](operations/governance.md#rollback)).

## 503 or 429 from `/v1/ask`

`/v1/ask` (and the MCP `ask` tool) calls the LLM synchronously.

- `503 {"detail": "learning model unavailable: …"}` with `Retry-After: 30`: no provider (`MYELINATE_LLM_PROVIDER=none`),
  a missing or invalid API key, a provider outage, or provider rate limiting. The detail names the cause. Configure
  `ANTHROPIC_API_KEY`, or retry later.
- `429` with `Retry-After: 3600` and a detail mentioning the daily learning token budget: the tenant is over
  `MYELINATE_DAILY_TOKEN_BUDGET`. Wait for the UTC day to roll over, or raise the budget.

`POST /v1/memories` doesn't fail in these situations. A memory that resembles an existing one needs the LLM to
reconcile, so without it the memory is stored as `pending` (and logged as `memory.reconcile_unavailable`). Approve
it with `POST /v1/memories/{id}/approve`.

## MCP returns 421 "Invalid Host header"

**Cause:** DNS-rebinding protection. The `Host` header isn't in `MYELINATE_MCP_ALLOWED_HOSTS`, which by default allows
only `localhost:*` and `127.0.0.1:*`.

**Fix:**

```bash
MYELINATE_MCP_ALLOWED_HOSTS='["learner.example.com","myelinate.default.svc.cluster.local:*"]'
```

Entries must match the `Host` header exactly (`host:*` allows any port). Include every hostname clients use.

**MCP tool calls return "Error executing tool …"** with `isError: true`: this is usually an auth failure (missing or
invalid bearer token, or missing `learn` scope). The MCP transport reports it as a tool error, not an HTTP 401. Check
the token configured in the MCP client.

## Nothing gets learned

Work through these in order.

1. **Is reflection being scheduled?** `POST /v1/episodes` returns `"reflection_scheduled": true` when an episode
   triggers it: a failure, at least `MYELINATE_REFLECT_MIN_TOOL_CALLS` tool calls, or every
   `MYELINATE_REFLECT_EVERY_N_EPISODES` episodes. Reflection starts after `MYELINATE_REFLECT_DEBOUNCE_SECONDS` of
   quiet. You can force it with `POST /v1/agents/{id}/reflect` (`review`).
2. **Is a worker running?** With `MYELINATE_WORKER_ENABLED=false` on gateways, you need a separate `myelinate worker`
   process. Check `SELECT kind, status, count(*) FROM jobs GROUP BY 1, 2`. A growing number of `pending` jobs with
   `run_after` in the past means no worker is consuming them.
3. **Is the LLM configured?** With `MYELINATE_LLM_PROVIDER=none`, nothing is learned. With a missing or invalid API
   key, each LLM call fails individually: logs show `reflect.failed` / `judge_outcome.failed` warnings for each
   episode (after 3 failed attempts an episode is marked reflected and skipped), and curation failures surface as
   `job.failed`. `myelinate_llm_calls_total` keeps rising while `myelinate_llm_tokens_total` stays flat. Set
   `ANTHROPIC_API_KEY` (or `MYELINATE_ANTHROPIC_API_KEY`).
4. **Is the budget exhausted?** Jobs deferred with `last_error` mentioning "daily learning token budget". See
   [Cost control](operations/cost-control.md).
5. **Is there evidence?** Skills only change from episodes with a reliable signal: verification, feedback, an outcome
   label, or a judge verdict with confidence ≥ `MYELINATE_MIN_JUDGE_CONFIDENCE`. Episodes recorded as `unknown`, with
   the judge disabled or unsure, only produce memories. The log line `reflection.no_curation` confirms this. Fix it by
   sending outcomes, `verification`, or feedback.
6. **Is the curator deciding nothing needs to change?** An empty operation list is the expected answer for most
   batches. Look for `changeset.dropped_ops` logs, which mean proposed ops were invalid.
7. **Is a changeset waiting?** It may be held for review: `GET /v1/changesets?agent_id=…`.

## Skills are not recalled

- **Agent ID mismatch.** Skills are scoped per `agent_id` (plus shared `*`). Recall with the same `agent_id` the
  skills were learned or authored under, and check `GET /v1/skills?agent_id=…`.
- **Holdout.** By default, 5% of sessions get no skills on purpose. The recall response shows `"holdout": true`, and it
  is sticky for the session. Set `MYELINATE_HOLDOUT_RATE=0` while debugging.
- **Relevance threshold.** Skills scoring below `MYELINATE_RECALL_MIN_SCORE` (0.12) are skipped. Lexical retrieval
  needs word overlap between the query and the skill's name, description or rules. Write descriptions that say *when*
  to use the skill, or enable embeddings. Rows created before embeddings were enabled have no vectors until they
  change ([Scaling](operations/scaling-and-ha.md#embeddings-for-large-libraries)).
- **Status.** Only `active` and `stale` skills are served. Archived skills (unused for
  `MYELINATE_ARCHIVE_AFTER_DAYS`) aren't. Any applied change reactivates one, or pin important skills.
- **Ranking cut.** Only `MYELINATE_RECALL_MAX_SKILLS` (3) are injected in full. The next `MYELINATE_SKILL_INDEX_SIZE`
  appear by name in `skill_index`.

## Changesets stuck in `pending`

This is expected when any of these hold: the risk is `high` (always human); it's `medium` and
`MYELINATE_AUTO_APPROVE_MEDIUM_RISK=false`; it has safety findings; or its `eval_score` is below
`MYELINATE_AUTO_APPROVE_MIN_EVAL_SCORE`. Review it: [Governance](operations/governance.md#reviewing-through-the-api).

## Changesets stuck in `canary` or `approved`

- **`canary`**: the skill doesn't get enough traffic to reach `MYELINATE_CANARY_MIN_SAMPLES` on both arms. Episodes
  only count if they carry a reward (outcome, verification, feedback or a confident judge). Experiments expire after
  `MYELINATE_CANARY_MAX_DAYS`, but expiry is only checked by the curate job. Trigger
  `POST /v1/agents/{id}/curate`, or roll back to stop it now. Check progress in `/insights` (`experiments`).
- **`approved`**: the changeset is queued behind a running canary on the same skill. It is re-evaluated when that
  canary ends, and often becomes `failed` (stale) if the canary was promoted, after which learning re-proposes.

## OTLP ingestion returns 415

**Cause:** `POST /v1/traces` accepts only JSON-encoded OTLP. Most SDKs default to protobuf.

**Fix:** set `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` in the agent, or put an OpenTelemetry Collector in front that
re-exports as JSON:

```yaml
exporters:
  otlphttp/myelinate:
    endpoint: https://learner.internal/otlp      # the collector appends /v1/traces
    encoding: json
    headers:
      Authorization: Bearer myl_...
      X-Agent-Id: support-bot
```

Also make sure the gateway's own tracing (`OTEL_EXPORTER_OTLP_ENDPOINT` on the gateway) doesn't point at itself.

## SQLite: "unable to open database file"

**Cause:** the database directory doesn't exist or isn't writable by the process user. The container runs as UID 10001
and defaults to `sqlite+aiosqlite:////data/myelinate.db`. A host bind mount owned by root, or a relative path such as
`sqlite+aiosqlite:///./myelinate.db` resolved against an unexpected working directory, causes this.

**Fix:** mount a volume at `/data` writable by UID 10001 (`chown 10001 <dir>`, or an `fsGroup` in Kubernetes). Use
four slashes for absolute paths (`sqlite+aiosqlite:////abs/path.db`). For anything multi-process, use PostgreSQL.
SQLite with several processes (`--workers > 1`, separate worker containers) causes `database is locked` errors.

## PostgreSQL errors

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: psycopg2` or "asyncio extension requires an async driver" | URL uses `postgresql://` | Use `postgresql+asyncpg://user:pass@host:5432/db` |
| `too many connections` / `remaining connection slots are reserved` | Each process pools up to `MYELINATE_DB_POOL_SIZE` + `MYELINATE_DB_MAX_OVERFLOW` (30 by default) connections | Lower those settings (especially on workers), size `max_connections` ([Scaling](operations/scaling-and-ha.md#connection-pools)), or run fewer processes |
| SSL required / connection refused by `pg_hba` | Server enforces TLS | Append `?ssl=require` to the URL |
| `column "…" does not exist` after an upgrade | Schema drift; there are no managed migrations | See [Backup and upgrades](operations/backup-and-upgrades.md#schema-management-current-state) |
| `/readyz` fails intermittently | DB failover or network | Connections are pre-pinged and re-established. Check DB health. |

## OpenClaw plugin not loading or not learning

- **Node version.** The plugin needs Node `>=24.16.0 <25` or `>=26.1.0`, and targets `openclaw@2026.10.4`. Check
  `node --version`.
- **Conversation access.** The hooks read the conversation only if they're allowed to:

    ```json5
    plugins: { entries: { myelinate: {
      enabled: true,
      hooks: { allowConversationAccess: true },
      config: { gatewayUrl: "https://learner.internal" }
    } } }
    ```

  `gatewayUrl` is required by the plugin's config schema. Put the key in `MYELINATE_API_KEY` (preferred) or in
  `apiKey`. It needs the `learn` scope.
- **Diagnose** with `openclaw plugins doctor`, and check that the hooks and tools (`skill_view`, `remember`,
  `rate_last_turn`) are registered in the runtime inspection output.
- **Fails open.** If the gateway is unreachable or rejects the key, turns run normally and OpenClaw logs warnings such
  as `[myelinate] recall failed, continuing without learned context` or
  `[myelinate] failed to record episode`. Look for these before assuming nothing is happening.
- After upgrading OpenClaw, type-check the plugin against the new SDK: `npm run typecheck` in `adapters/openclaw`.

## Duplicate episodes

**Cause:** the client retried a `POST /v1/episodes` without an `idempotency_key`, or generated a new key per attempt.

**Fix:** send a stable `idempotency_key` per run (≤ 256 chars). A repeat with the same key in the same tenant returns
the original `episode_id`. The Python SDK and the OpenClaw client set a key per call, so their transport retries are
safe, but an application-level retry that calls them again creates a new key. OTLP ingestion uses `otel:<trace_id>`,
so re-exported traces don't duplicate.

## Still stuck?

Collect the `x-request-id`, the relevant log lines, `GET /v1/agents/{id}/insights`, and the job-queue query output,
then open an issue at [github.com/notaaaronfr/myelinate](https://github.com/notaaaronfr/myelinate/issues). Report
security problems privately ([SECURITY.md](https://github.com/notaaaronfr/myelinate/blob/main/SECURITY.md)).

See also: [Observability](operations/observability.md), [Configuration reference](reference/configuration.md),
[Quickstart](getting-started/quickstart.md).
