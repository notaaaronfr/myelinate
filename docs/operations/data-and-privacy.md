# Data and privacy

This page lists what Myelinate stores, what it sends to third parties, and the tools for retention, erasure and
export. It describes the system's behaviour; it isn't legal advice. Map it to your own obligations (GDPR, HIPAA,
internal policy).

## What is stored

All tables live in the database configured by `MYELINATE_DATABASE_URL`, and every row carries a `tenant_id`.

| Table | Contents | Personal data? |
|---|---|---|
| `episodes` | Agent runs: task, messages, tool calls (arguments, results, errors), outcome, judged outcome, verification details, feedback score, comment and correction, `user_id`, `session_id`, metadata, the reflection output | **Yes**: transcripts (redacted, see below) |
| `recalls` | Each recall: query text (redacted), `user_id`, `session_id`, IDs of what was served | Yes |
| `memories` | Facts, preferences and lessons, with `user_id`, bi-temporal validity, provenance (episode IDs) | Yes, especially `preference` |
| `profiles` | Per-user profile card summarising preference memories | Yes |
| `skills`, `bullets`, `skill_versions` | Learned procedures and rules; provenance as episode IDs; immutable version snapshots | Normally no, but rules are derived from transcripts |
| `changesets` | Proposed edits with rationale, ops, `episode_ids`, reviewer, comments | Rarely (derived text, reviewer identity) |
| `experiments` | Canary state and candidate rule text | No |
| `pair_decisions`, `agent_state` | Dedup decisions; per-agent counters and lift posteriors | No |
| `llm_usage` | Calls and tokens per tenant, day and purpose | No |
| `jobs` | Background jobs (payloads hold agent and skill IDs), last error | No |
| `api_keys` | Key name, SHA-256 hash, scopes, revoked flag | No secrets |
| `audit_log` | Actor, action, resource ID, details | Actor identities (`oidc:<sub>`), user IDs in `user.erased` entries |

Embeddings, if enabled, are stored as JSON arrays on skills, bullets and memories.

## PII and secret redaction

With `MYELINATE_REDACT_PII=true` (the default), text is redacted with regular expressions **before it is stored**:

| Label | Pattern (summary) |
|---|---|
| `[EMAIL]` | email addresses |
| `[CARD]` | 13–19 digit sequences, optionally separated by spaces or dashes |
| `[SSN]` | `123-45-6789` |
| `[PHONE]` | international and national phone-number shapes |
| `[IBAN]` | IBAN-shaped strings |
| `[REDACTED aws_access_key]` | `AKIA…` |
| `[REDACTED private_key]` | `-----BEGIN … PRIVATE KEY-----` headers |
| `[REDACTED api_key]` | `sk-`, `pk-`, `rk-` style keys (20+ chars) |
| `[REDACTED github_token]`, `[REDACTED slack_token]`, `[REDACTED jwt]` | GitHub, Slack and JWT tokens |
| `[REDACTED password_assignment]` | `password=…`, `secret: …`, `api_key=…` |

**Redacted:** episode task, message content, tool-call arguments, results and errors, the error field, verification
details, feedback comments and corrections, recall queries, and memory content (including learned memories).

**Not redacted:** episode `metadata` (stored as sent), `user_id`, `session_id`, `framework`, `skills_loaded`, the text
of skills authored or imported through the API (it is scanned for secrets, and findings block auto-approval), and text
sent to the embeddings endpoint at recall time (the query is embedded before redaction).

Limitations:

- This is pattern matching. Names, postal addresses, IP addresses, account numbers in other formats and free-text
  health or financial details are **not** detected. If transcripts may hold such data, redact upstream (in the agent or
  adapter) or put a DLP proxy in front.
- The card and phone patterns can over-match long digit runs, such as order numbers or timestamps. That can remove
  details the learner would have used.
- Setting `MYELINATE_REDACT_PII=false` stores text verbatim.

## Retention

Episodes are the bulk of stored personal data. `myelinate purge` deletes, in one run:

| Data | Older than |
|---|---|
| `episodes` | `MYELINATE_EPISODE_RETENTION_DAYS` (default 90) |
| `recalls` (redacted query text, `user_id`) | `MYELINATE_EPISODE_RETENTION_DAYS` |
| finished `jobs` (`done`, `failed`) | `MYELINATE_EPISODE_RETENTION_DAYS` |
| `audit_log` entries | `MYELINATE_AUDIT_RETENTION_DAYS` (default 365) |

```bash
myelinate purge          # prints e.g. "purged 1200 episodes, 3400 recalls, 310 jobs, 0 audit"
```

Nothing runs it automatically. Schedule it, for example with a daily Kubernetes `CronJob` or cron, using the same
environment as the gateway.

What `purge` does **not** delete: `memories`, `profiles`, skills and their versions, `changesets`, `experiments` and
`llm_usage`. Learned skills and memories keep their provenance as episode IDs, which simply stop resolving. Set
`MYELINATE_AUDIT_RETENTION_DAYS` to match your compliance requirement, and export the audit log first if you need it
for longer.

## Right to erasure

```bash
curl -s -X DELETE "$GW/v1/admin/users/<user_id>" -H "Authorization: Bearer $ADMIN_KEY"
# → {"memories": 3, "episodes": 41}
```

`DELETE /v1/admin/users/{user_id}` (`admin` scope) hard-deletes, **within the caller's tenant**, every row whose
`user_id` matches in `memories` (all versions, including superseded history), `episodes`, `profiles` and `recalls`. It
writes a `user.erased` audit entry with the counts.

What it doesn't reach:

- **Shared facts.** With `MYELINATE_SHARE_LEARNED_FACTS=true`, non-preference facts learned from a user's runs are
  stored with no `user_id`, so erasure can't find them. Delete them individually.
- **Skills.** Rules are written to generalise (names replaced by placeholders), but they are derived from transcripts.
  They aren't erased. Review `skill_versions` if a rule could contain personal data, and remove it with an authored
  change. Version snapshots stay.
- **Free text in other users' data**, or data recorded without a `user_id`.
- **The audit log**, which keeps the erased `user_id` as the resource of the `user.erased` entry.
- **Backups.** Handle these under your backup retention policy.
- **Data already sent to the LLM provider** (see below).

`user_id` is whatever the integration sends. Erasure only works if your adapters send a stable user identifier.

## Memory: hard delete vs supersede

- **Learning never deletes.** When reconciliation decides a new memory updates or contradicts an old one, the old row
  gets `invalid_at` and `superseded_by`, and stays queryable.
- **Hard delete** (`DELETE /v1/memories/{id}`, `review` scope) removes one row. Earlier versions in its supersede
  chain are separate rows. Delete them too, or use user erasure.

### Bi-temporal history and `as_of`

```bash
# Current view (default)
curl -s "$GW/v1/memories?agent_id=support-bot&user_id=u-42" -H "$AUTH"
# What was believed at a point in time
curl -s "$GW/v1/memories?agent_id=support-bot&user_id=u-42&as_of=2026-09-01T00:00:00Z" -H "$AUTH"
# Everything, including superseded versions
curl -s "$GW/v1/memories?agent_id=support-bot&user_id=u-42&include_history=true" -H "$AUTH"
# Memories awaiting review
curl -s "$GW/v1/memories?agent_id=support-bot&status=pending" -H "$AUTH"
```

`POST /v1/recall` also accepts `as_of` for a point-in-time view of memories, which is useful when auditing what an agent
was told.

## User-scoped facts

Memories with a `user_id` are only visible to recalls for that user. Memories without one are visible to every user of
the agent. Memories under `agent_id="*"` would be visible to every agent in the tenant, but `learn`-scoped requests
can't write there (`422`), and learned memories are always stored under the recording agent.

- Preferences learned from a run always belong to that run's user.
- Other facts learned from a user's run stay with that user unless `MYELINATE_SHARE_LEARNED_FACTS=true`. In that case
  they become agent-wide and erasure no longer reaches them.
- Memories written explicitly (`POST /v1/memories`, MCP `remember`) use the `user_id` in the request.

`user_id` is not authenticated. Any credential in the tenant can read any user's memories (see
[Security](security.md#threat-model)).

## What is sent to third parties

### The LLM provider

Learning runs in background jobs and calls the Anthropic API (`MYELINATE_LLM_PROVIDER=anthropic`, model
`MYELINATE_LLM_MODEL`). These calls send:

| Purpose | Data sent |
|---|---|
| `judge_outcome`, `reflect` | The redacted episode: task, messages (each clipped to 3000 chars), tool calls (clipped), errors, verification details, feedback comment and correction; the rules that were injected |
| `curate` | Reflection summaries (task, diagnosis, insights), relevant skills and rules, previously rejected edits |
| `memory` | Candidate memories and similar existing memories (redacted) |
| `profile` | A user's preference memories |
| `dedup` | Pairs of similar rules |
| `judge_skill`, `evolve` | Skill text and redacted episodes that used the skill |
| `ask` | The question (**not redacted**) plus matching memories, profile and rules |

The hot path (`/v1/recall`, `/v1/episodes`, `/v1/feedback`) never calls the LLM. `/v1/ask` and `POST /v1/memories`
(when similar memories exist) call it synchronously.

Data residency considerations:

- Requests go to `api.anthropic.com` unless `ANTHROPIC_BASE_URL` is set; the Anthropic SDK honours it. You can route
  through an egress proxy or an LLM gateway in your region, as long as it passes through the Messages API features
  used (structured JSON output, the `effort` setting and the beta header). Amazon Bedrock and Google Vertex clients
  aren't supported directly.
- Check your Anthropic agreement for retention and zero-data-retention terms.
- `MYELINATE_LLM_PROVIDER=none` disables all LLM calls. Recording and recall still work, explicit memories can still
  be added (one that resembles an existing memory would need the LLM to reconcile, so it is stored as `pending` for
  review instead), and human-authored skills are still served. Nothing is learned automatically.

### The embeddings endpoint

If `MYELINATE_EMBEDDINGS_URL` is set, recall queries (unredacted), skill descriptions, rule text and memory content are
sent there. Use a self-hosted endpoint (TEI, vLLM, Ollama) to keep this in your network.

## Export

```bash
curl -s "$GW/v1/agents/support-bot/export?limit=10000" -H "Authorization: Bearer $REVIEW_KEY" > episodes.jsonl
```

`GET /v1/agents/{agent_id}/export` (`review` scope, `limit` ≤ 100,000) streams the agent's episodes as NDJSON, oldest
first: `episode_id`, `task`, `messages`, `tool_calls`, `reward`, `reward_strength`, `served`, `holdout`, `framework`,
`created_at`. Each export is audited as `episodes.exported`. It's meant for RL or fine-tuning datasets and for audits.

For a data-subject access request there is no single endpoint. Combine `GET /v1/memories?user_id=…&include_history=true`
for each agent, `GET /v1/profiles/{agent_id}/{user_id}`, and a database query on `episodes` and `recalls` filtered by
`user_id`.

See also: [Security](security.md), [Backup and upgrades](backup-and-upgrades.md),
[Configuration reference](../reference/configuration.md).
