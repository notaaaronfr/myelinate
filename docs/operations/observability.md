# Observability

Myelinate exposes Prometheus metrics, structured JSON logs, optional OpenTelemetry tracing for the gateway, health
endpoints, a worker heartbeat file, and a per-tenant audit log. There is no built-in dashboard.

## Prometheus metrics

`GET /metrics` on the gateway serves the Prometheus text format **without authentication**. Scrape it over the
private network and don't expose it publicly. Metrics are per process, so aggregate across replicas with `sum(...)`.
None carry a tenant label. For per-tenant spend, use `GET /v1/admin/usage` (see [Cost control](cost-control.md)).

Each process exports the metrics for the work **it** did. With dedicated workers (`myelinate worker`), the job, LLM,
evolution, canary and most changeset counters are produced in the worker processes, and the gateway shows HTTP,
recall and episode metrics. Standalone workers serve their metrics on a separate port, set by
`MYELINATE_WORKER_METRICS_PORT` (default `9464`; `0` disables). Scrape both gateways and workers.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `myelinate_http_seconds` | histogram | `route`, `method`, `status` | Request latency. `route` is the route template (for example `/v1/skills/{ref}`), or `unmatched`. Requests rejected by the pre-auth IP limiter aren't recorded. |
| `myelinate_recalls_total` | counter | | Recall calls served |
| `myelinate_recall_skills` | histogram | | Skills injected per recall (buckets 0, 1, 2, 3, 5, 8). Holdout recalls observe 0. |
| `myelinate_episodes_total` | counter | `outcome`, `framework` | Episodes recorded, by reported outcome and framework (`unknown` if not given). Idempotent replays aren't counted. |
| `myelinate_jobs_enqueued_total` | counter | `kind` | New background jobs (`reflect`, `curate`, `evolve`). Debounced re-enqueues of a pending job aren't counted. |
| `myelinate_jobs_total` | counter | `kind`, `status` | Jobs finished by a worker: `done`, `pending` (will retry: a failure with attempts left, or deferred for budget), or `failed` (out of attempts). |
| `myelinate_llm_calls_total` | counter | `purpose` | Learning-side LLM calls attempted, including failed ones. Purposes: `judge_outcome`, `reflect`, `curate`, `memory`, `dedup`, `profile`, `judge_skill`, `evolve`, `ask`. |
| `myelinate_llm_tokens_total` | counter | `purpose`, `kind` | Tokens by purpose. `kind` is `input` (uncached + cache writes + cache reads) or `output`. |
| `myelinate_changesets_total` | counter | `source`, `status` | Changeset transitions: proposals (`status="proposed"`), review outcomes (`applied`, `canary`, `approved`, `rejected`, `failed`), canary promotion or rejection, re-evaluation of queued changesets, and rejections caused by a rollback |
| `myelinate_experiments_total` | counter | `verdict` | Canary conclusions: `promoted`, `rejected` (including experiments stopped by a rollback), `expired` |
| `myelinate_evolutions_total` | counter | `result` | Evolution runs that reached the final gate: `improved` (a changeset was proposed) or `no_gain`. |

`prometheus_client` also exports `*_created` series and default process and Python metrics.

### Suggested alerts

```yaml
groups:
- name: myelinate
  rules:
  - alert: MyelinateHighErrorRate
    expr: |
      sum(rate(myelinate_http_seconds_count{status=~"5.."}[5m]))
        / sum(rate(myelinate_http_seconds_count[5m])) > 0.02
    for: 10m

  - alert: MyelinateRecallSlow           # recall sits on the agent's critical path
    expr: |
      histogram_quantile(0.95, sum by (le) (rate(myelinate_http_seconds_bucket{route="/v1/recall"}[5m]))) > 0.5
    for: 10m

  - alert: MyelinateJobsFailing          # jobs exhausted their retries
    expr: sum by (kind) (increase(myelinate_jobs_total{status="failed"}[1h])) > 0

  - alert: MyelinateLearningStalled      # episodes arrive but no reflection finishes
    expr: |
      sum(increase(myelinate_episodes_total[2h])) > 50
        and sum(increase(myelinate_jobs_total{kind="reflect",status="done"}[2h])) == 0
    for: 30m

  - alert: MyelinateAuthFailures         # credential misconfiguration or probing
    expr: sum(rate(myelinate_http_seconds_count{status=~"401|403"}[5m])) > 1
    for: 15m

  - alert: MyelinateRateLimited
    expr: sum(rate(myelinate_http_seconds_count{status="429"}[5m])) > 0.5
    for: 15m

  - alert: MyelinateLLMSpendSpike        # compare with your daily budget
    expr: sum(increase(myelinate_llm_tokens_total[1h])) > 1e6

  - alert: MyelinateCanaryRejections     # many learned edits failing live
    expr: increase(myelinate_experiments_total{verdict="rejected"}[7d]) > 5
```

Tune the thresholds to your traffic. `MyelinateLearningStalled` catches a disabled worker, a missing LLM key, and an
exhausted budget (see [Troubleshooting](../troubleshooting.md#nothing-gets-learned)).

## Structured logs

In `staging` and `prod`, logs are one JSON object per line on stdout. In `dev` they are human-readable console output.

```json
{"error": "no LLM configured (MYELINATE_LLM_PROVIDER=none)", "event": "memory.reconcile_failed",
 "request_id": "req_8d1fc7ae4f7d4e7d9d610935bf4ea9ce", "tenant": "acme", "level": "warning",
 "timestamp": "2026-10-16T09:52:31.474789Z"}
```

| Field | Notes |
|---|---|
| `event` | Event name, for example `job.failed`, `reflection.no_curation`, `evolution.no_gain`, `curation.done`, `changeset.dropped_ops`, `embeddings.failed`, `usage.meter_failed`, `auth.disabled`, `otel.missing` |
| `level`, `timestamp` | ISO-8601 UTC |
| `request_id` | Taken from the inbound `X-Request-Id` header or generated (`req_…`), and echoed in the `x-request-id` response header. Present on logs emitted while handling a request. |
| `tenant` | Bound after authentication |
| event-specific | For example `job`, `kind`, `attempt`, `error` on `job.failed` |

Things to know:

- There is no per-request access log (uvicorn's access log is disabled). Use your ingress's access logs, or the
  `myelinate_http_seconds` metric, for request-level data.
- Background jobs run outside a request, so their log lines carry job fields (`job`, `kind`, `tenant`/`agent` where
  relevant) rather than `request_id`.
- Standalone `myelinate worker` processes use the same format rules (JSON outside `dev`).
- Logs contain error messages, which can include fragments of LLM or database errors. They don't contain transcripts.

## OpenTelemetry tracing

The gateway can export its own server spans over OTLP/HTTP (protobuf).

1. Install the extra: `uv sync --extra otel` (or `pip install 'myelinate[otel]'`). The Docker image already includes it.
2. Set the standard exporter variables:

    ```bash
    OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318   # spans go to <endpoint>/v1/traces
    OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer …"        # optional
    ```

If `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, tracing is off. If it is set but the extra is missing, an `otel.missing`
warning is logged and the gateway runs without tracing.

Scope and limits:

- Only FastAPI server spans are produced, with `service.name=myelinate-gateway` (fixed in code). Database queries, LLM
  calls and background jobs aren't instrumented, and standalone `myelinate worker` processes don't export traces.
- Don't confuse this with **ingestion**. Agents send *their* traces to Myelinate at `/v1/traces` (or
  `OTEL_EXPORTER_OTLP_ENDPOINT=<gateway>/otlp`) in JSON encoding. Don't point the gateway's own
  `OTEL_EXPORTER_OTLP_ENDPOINT` at itself: its exporter uses protobuf, which ingestion rejects with `415`, and it
  would create a feedback loop.

## Health endpoints

| Endpoint | Checks | Use for |
|---|---|---|
| `GET /healthz` | Process is serving HTTP. Returns `{"status": "ok"}` and doesn't touch the database. | Liveness probe |
| `GET /readyz` | Runs `SELECT 1` against the database. Returns `{"status": "ready"}`, or an error if the DB is unreachable. | Readiness probe |

Both are unauthenticated but subject to the per-IP rate limiter. Neither checks the worker, the LLM provider or the
embeddings endpoint.

## Worker heartbeat

Dedicated worker processes (`myelinate worker`) serve no API. Set `MYELINATE_WORKER_HEARTBEAT_FILE` to a writable path,
and a background task touches that file every 10 seconds, independently of how long jobs take. The heartbeat proves
the process and its event loop are alive. It doesn't prove jobs are succeeding; alert on job metrics for that.

`myelinate healthcheck` is a ready-made probe command for both process types:

- If `MYELINATE_WORKER_HEARTBEAT_FILE` is set and the file exists, it exits 0 when the file is younger than
  `--max-age` seconds (default 60).
- Otherwise it requests `http://127.0.0.1:<--port>/healthz` (default port 8080).

The Docker image's `HEALTHCHECK` and the worker service in `deploy/docker-compose.yml` use it.

```yaml
# Kubernetes liveness probe for a worker pod
livenessProbe:
  exec: { command: ["myelinate", "healthcheck"] }
  periodSeconds: 30
```

`MYELINATE_WORKER_HEARTBEAT_FILE` is read directly from the environment. It isn't a `Settings` field and can't be set
in `.env`. Only standalone workers write it. The worker embedded in the gateway is covered by `/healthz`.

## Audit log

`GET /v1/admin/audit?limit=200` (`admin` scope, `limit` ≤ 1000) returns the tenant's most recent audit entries, newest
first: `id`, `actor`, `action`, `resource`, `details`, `at`. There is no filtering or pagination, so export it regularly
to your SIEM if you need long-term search.

```bash
curl -s "$GW/v1/admin/audit?limit=1000" -H "Authorization: Bearer $ADMIN_KEY" | jq -c '.[]' >> myelinate-audit.jsonl
```

Recorded actions include `changeset.proposed`, `changeset.applied`, `changeset.rejected`, `changeset.failed`,
`changeset.canary`, `changeset.queued`, `canary.promoted`, `canary.rejected`, `canary.expired`, `skill.rollback`,
`skill.pin`, `skills.lifecycle`, `memory.add`/`update`/`supersede` (from reconciliation), `memory.approved`,
`memory.deleted`, `feedback`, `episodes.exported`, `apikey.created`, `apikey.revoked` and `user.erased`. Actors are
`key:<id>`, `oidc:<sub>`, `bootstrap-admin`, or `system:<component>` (`system:policy`, `system:canary`,
`system:reflection`, `system:curation`, `system:evolution`).

`myelinate purge` deletes audit entries older than `MYELINATE_AUDIT_RETENTION_DAYS` (default 365). Export first if you
need them for longer.

## Inspecting the job queue

There is no API for the job queue. Query the `jobs` table directly (read-only):

```sql
-- Backlog by kind and status
SELECT kind, status, count(*) FROM jobs GROUP BY 1, 2 ORDER BY 1, 2;

-- Jobs waiting longer than 10 minutes past their start time
SELECT id, tenant_id, kind, dedupe_key, attempts, run_after, last_error
FROM jobs WHERE status = 'pending' AND run_after < now() - interval '10 minutes'
ORDER BY run_after LIMIT 50;

-- Failed jobs and why
SELECT id, tenant_id, kind, attempts, last_error, created_at
FROM jobs WHERE status = 'failed' ORDER BY created_at DESC LIMIT 50;

-- Jobs deferred because a tenant hit its daily budget
SELECT tenant_id, kind, run_after FROM jobs
WHERE status = 'pending' AND last_error LIKE '%daily learning token budget%';

-- Currently running, with lease expiry
SELECT id, kind, locked_by, run_after AS lease_expires FROM jobs WHERE status = 'running';
```

Finished jobs (`done`, `failed`) stay in the table until `myelinate purge` removes those older than
`MYELINATE_EPISODE_RETENTION_DAYS`. For queue semantics,
see [Scaling and HA](scaling-and-ha.md#job-queue).

See also: [Troubleshooting](../troubleshooting.md), [Cost control](cost-control.md), [Kubernetes](../deployment/kubernetes.md).
