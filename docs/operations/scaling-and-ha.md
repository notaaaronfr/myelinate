# Scaling and high availability

## Architecture

```
            agents / adapters / MCP clients / OTLP exporters
                              │  HTTPS
                     ┌────────▼────────┐
                     │ ingress / LB    │  TLS, shared rate limiting
                     └────────┬────────┘
            ┌─────────────────┼─────────────────┐
     ┌──────▼─────┐    ┌──────▼─────┐    ┌──────▼─────┐
     │ gateway    │    │ gateway    │    │ gateway    │   stateless: REST, MCP, OTLP; hot path never calls an LLM
     └──────┬─────┘    └──────┬─────┘    └──────┬─────┘   (MYELINATE_WORKER_ENABLED=false)
            └─────────────────┼─────────────────┘
                       ┌──────▼──────┐
                       │ PostgreSQL  │  all state, including the job queue
                       └──────▲──────┘
            ┌─────────────────┼─────────────────┐
     ┌──────┴─────┐    ┌──────┴─────┐    ┌──────┴─────┐
     │ worker     │    │ worker     │    │ worker     │   `myelinate worker`: reflect / curate / evolve jobs,
     └────────────┘    └────────────┘    └────────────┘   LLM calls to Anthropic
```

- **Gateways** (`myelinate serve`) are stateless apart from three in-process caches: rate-limiter buckets, the 30-second
  API-key cache, and the JWKS key cache. Add replicas behind a load balancer; no session affinity is needed.
- **Workers** claim jobs from the `jobs` table. Run them inside each gateway (`MYELINATE_WORKER_ENABLED=true`, the
  default) for small installs, or as a separate deployment (`myelinate worker`, with
  `MYELINATE_WORKER_ENABLED=false` on gateways) so learning load doesn't compete with recall latency.
  `deploy/docker-compose.yml` uses the separate layout.
- **PostgreSQL** holds all state. It is the only stateful component and the availability bottleneck. Use a managed or
  replicated Postgres with automatic failover. The connection pool uses `pool_pre_ping`, so connections are
  re-established after a failover.

See [Kubernetes](../deployment/kubernetes.md) and [Docker](../deployment/docker.md) for manifests.

## Job queue

Learning runs as durable jobs in the `jobs` table:

| Kind | Enqueued when | Dedupe key |
|---|---|---|
| `reflect` | A failed episode (`MYELINATE_REFLECT_ON_FAILURE`), an episode with ≥ `MYELINATE_REFLECT_MIN_TOOL_CALLS` tool calls, every `MYELINATE_REFLECT_EVERY_N_EPISODES` episodes, negative or commented feedback, `POST /v1/agents/{id}/reflect` or `…/replay` | `reflect:<tenant>:<agent>` |
| `curate` | Every `MYELINATE_CURATE_EVERY_N_EPISODES` episodes, or `POST /v1/agents/{id}/curate` | `curate:<tenant>:<agent>` |
| `evolve` | A skill with ≥ `MYELINATE_EVOLVE_MIN_USES` new uses and a success rate below `MYELINATE_EVOLVE_MAX_SUCCESS_RATE` (or rules tagged harmful repeatedly), or `POST /v1/skills/{id}/evolve` | `evolve:<skill_id>` |

Semantics:

- **Debounce.** Enqueuing with a dedupe key while a *pending* job with that key exists doesn't add a job. Instead it
  pushes the pending job's start back (reflection waits `MYELINATE_REFLECT_DEBOUNCE_SECONDS` of quiet, default 20 s)
  and merges payloads.
- **Exclusivity.** At most one job per dedupe key runs at a time across all workers, so only one reflection per agent
  runs at once. A new job for a key that is currently running waits until the running one finishes.
- **Claiming** is a conditional `UPDATE … WHERE status='pending'`, so several workers never run the same job.
- **Leases and heartbeat.** A claimed job holds a 10-minute lease (stored in `run_after`). While it runs, the worker
  extends the lease every ~200 s. If a worker dies, its jobs return to `pending` when the lease expires and another
  worker picks them up. That attempt counts.
- **Retries.** A failed job is retried with exponential backoff (60 s, then 120 s, …) up to `MYELINATE_JOB_MAX_ATTEMPTS`
  (default 3), then marked `failed` with `last_error`. Only the current lease holder can finish a job.
- **Budget deferral.** When a tenant has used its `MYELINATE_DAILY_TOKEN_BUDGET`, the job is put back to `pending`
  for one hour **without** using up an attempt (see [Cost control](cost-control.md)).
- **Batching.** Each worker loop claims up to `MYELINATE_WORKER_CONCURRENCY` jobs (default 4) and waits for the whole
  batch before claiming more. One slow job, such as an evolution run, holds the other slots idle until it finishes.
  Add worker processes rather than raising concurrency very high.
- **Shutdown.** On `SIGTERM`/`SIGINT` (standalone worker) or gateway shutdown (embedded worker), the worker stops
  claiming and waits up to 30 s for in-flight jobs. Jobs still running after that, or interrupted by a hard kill,
  return to the queue when their lease expires (up to 10 minutes later) and are retried, using up an attempt. Expect
  some repeated LLM calls in that case. Give worker pods a termination grace period of at least 40 s.

To inspect the queue, see [Observability](observability.md#inspecting-the-job-queue).

## SQLite vs PostgreSQL

| | SQLite (`sqlite+aiosqlite:///…`) | PostgreSQL (`postgresql+asyncpg://…`) |
|---|---|---|
| Use for | Local development, demos, single-node evaluation | Anything shared or long-lived |
| Writers | One at a time (file lock) | Concurrent; row locks |
| Replicas | **One process only.** Don't combine with `--workers > 1` or separate worker containers. | Any number of gateways and workers |
| Backups | File copy or `.backup` | Standard Postgres tooling, PITR |

The test suite passes on both. `docker compose` with Postgres and two workers has been exercised. **No load or
performance testing has been published**, so size from your own measurements.

## Sizing guidance

There are no benchmarks yet, so use this as a starting point and measure.

**Gateway (hot path).** A recall loads, per request, every skill and active rule visible to the agent and up to 2000
active memories, then ranks them in Python (BM25, plus cosine similarity if embeddings are on). Cost grows linearly
with library size. A record does a handful of writes, and takes a row lock on the agent's counters, so very hot single
agents serialise briefly on that row.

- Start with 2 replicas at 0.5–1 vCPU and 512 MiB–1 GiB each. Scale on CPU and on p95 `/v1/recall` latency.
- Lexical retrieval over a few thousand skills per agent is fine. Beyond that, see [Embeddings](#embeddings-for-large-libraries).
- `myelinate serve --workers N` runs N uvicorn processes per container. Prefer more replicas with one process each,
  so metrics, rate limits and caches are easier to reason about.

**Workers.** These are I/O-bound on LLM latency. A reflection job handles up to 20 unreflected episodes with up to 4
concurrent LLM calls. An evolution run can make dozens of judge calls. Start with 2 workers (concurrency 4, 256–512
MiB each), and add more when the pending backlog grows (`SELECT count(*) FROM jobs WHERE status='pending' AND run_after
< now()`). Anthropic API rate limits are often the real ceiling.

**Postgres.** Start small (2 vCPU, 4 GiB). Episodes dominate storage, roughly the size of the transcripts you send,
so retention ([`myelinate purge`](data-and-privacy.md#retention), which also prunes recalls, finished jobs and old
audit entries) is the main lever.

## Connection pools

Each process creates a SQLAlchemy pool of `MYELINATE_DB_POOL_SIZE` connections (default 10) plus up to
`MYELINATE_DB_MAX_OVERFLOW` extra under burst (default 20), so it holds up to **30 connections** by default. Plan
Postgres `max_connections` as:

```
(gateway replicas × uvicorn workers per replica + worker processes) × (DB_POOL_SIZE + DB_MAX_OVERFLOW) + headroom
```

For example, 3 gateways and 2 workers with defaults can open up to 150 connections. Workers rarely need more than
`MYELINATE_WORKER_CONCURRENCY` plus a few, so you can lower their pool. If you put PgBouncer in front, use **session**
pooling. Transaction pooling is untested with asyncpg's prepared statements.

## Rate limiting across replicas

The built-in limiters ([Security](security.md#rate-limits)) keep their buckets in each process's memory. With several
replicas, each one enforces the limit independently, so the effective limit multiplies. For a hard global limit, use
your ingress or API gateway (NGINX `limit_req`, Envoy global rate limiting, a cloud API gateway), keyed on the
`Authorization` header or client IP. Keep the built-in limits as a per-process backstop, and set `FORWARDED_ALLOW_IPS`
so the per-IP limiter sees real client addresses.

## Embeddings for large libraries

Set `MYELINATE_EMBEDDINGS_URL` (any `/v1/embeddings`-compatible endpoint) and `MYELINATE_EMBEDDINGS_MODEL` to add dense
retrieval, fused with BM25 through reciprocal rank fusion. Notes:

- Embeddings are computed when a skill, rule or memory is created or changed. Rows that existed before you enabled
  embeddings stay lexical-only. There is no backfill command; re-saving content (for example re-importing skills)
  embeds it.
- Every recall embeds the query, adding one HTTP round trip to the hot path. Embedding failures degrade to lexical
  retrieval (an `embeddings.failed` log line).
- Vectors are stored as JSON and compared in Python. There is no pgvector index yet, so embeddings improve relevance
  but not the linear cost of scanning a large library.
- Don't change the embeddings model on a populated database. Vectors of different dimensions are ignored (similarity
  0), so retrieval quietly falls back to lexical for old rows.

## High-availability checklist

- [ ] At least 2 gateway replicas behind a load balancer; readiness probe on `/readyz`, liveness on `/healthz`.
- [ ] Dedicated worker deployment with at least 2 replicas, a `myelinate healthcheck` liveness probe
      ([Observability](observability.md#worker-heartbeat)), and metrics scraped on `MYELINATE_WORKER_METRICS_PORT`.
- [ ] Replicated Postgres with automatic failover and backups ([Backup and upgrades](backup-and-upgrades.md)).
- [ ] `max_connections` sized for every process × (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`).
- [ ] A shared rate limiter at the ingress.
- [ ] Graceful termination period of at least 40 s for gateways and workers.
- [ ] Alerts on 5xx, recall latency, failed jobs and stalled learning.

Adapters fail open: if the gateway is unreachable, agents run without learned context rather than failing. An outage
therefore degrades quality, not availability.

See also: [Observability](observability.md), [Cost control](cost-control.md), [Troubleshooting](../troubleshooting.md).
