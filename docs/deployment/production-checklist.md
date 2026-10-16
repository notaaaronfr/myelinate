# Production checklist

Go through this before real agents depend on Myelinate. Details are in [Security](../operations/security.md),
[Scaling and HA](../operations/scaling-and-ha.md), [Observability](../operations/observability.md) and
[Backup and upgrades](../operations/backup-and-upgrades.md).

## Identity and access

- [ ] `MYELINATE_ENV=prod` (the image default). Never `dev` outside a laptop: it disables authentication.
- [ ] SSO through OIDC (`MYELINATE_OIDC_ISSUER`, `MYELINATE_OIDC_AUDIENCE`, `MYELINATE_OIDC_JWKS_URL`,
      `MYELINATE_OIDC_TENANT_CLAIM`) for people and services that have an identity provider.
- [ ] `MYELINATE_BOOTSTRAP_ADMIN_KEY` is long and random, kept in a vault, used only to mint tenant keys, and rotated
      after setup. Remove it once OIDC covers administration.
- [ ] Least-privilege scopes: agents get `learn` only; `review` goes to people or pipelines that approve changes;
      `admin` to operators. One API key per agent or service, so you can revoke one without the others.

## Data

- [ ] PostgreSQL, not SQLite, for anything with more than one replica or separate workers. Use a managed service.
- [ ] TLS to the database (for example `?ssl=require` on `MYELINATE_DATABASE_URL`).
- [ ] Automated backups with point-in-time recovery, and a restore you have actually tested.
- [ ] `MYELINATE_EPISODE_RETENTION_DAYS` set to your retention policy. Expired episodes are removed by
      `myelinate purge`; schedule it (cron or a Kubernetes `CronJob`).
- [ ] `MYELINATE_REDACT_PII=true` (the default) unless you have a reason and approval to turn it off.
- [ ] A process for erasure requests (`DELETE /v1/admin/users/{user_id}`; see the
      [API reference](../reference/api.md)).

## Secrets

- [ ] Secrets (bootstrap key, `ANTHROPIC_API_KEY`, database URL, `MYELINATE_EMBEDDINGS_API_KEY`) come from a secret
      manager, not from files in Git or images.
- [ ] Keys are rotated on a schedule and when people leave.

## Network

- [ ] TLS terminated at the ingress or load balancer.
- [ ] `MYELINATE_MCP_ALLOWED_HOSTS` lists your public host(s); otherwise `/mcp` returns 421.
- [ ] `MYELINATE_CORS_ORIGINS` is empty, or lists only the exact browser origins that need it.
- [ ] Rate limiting in front (ingress, API gateway or WAF). The built-in limits are in-memory per replica.
- [ ] `FORWARDED_ALLOW_IPS` set to your proxy's address so per-IP limits see real clients, and the gateway is not
      reachable except through that proxy (NetworkPolicy or security groups).
- [ ] Egress limited to Postgres, the LLM API, your embeddings endpoint and your OTLP collector.

## Runtime

- [ ] Separate workers: `MYELINATE_WORKER_ENABLED=false` on gateways, at least two `myelinate worker` replicas.
- [ ] At least two gateway replicas, a PodDisruptionBudget, and spread across nodes or zones.
- [ ] CPU/memory requests and memory limits set (see the [Kubernetes](kubernetes.md) manifests for a starting point).
- [ ] Liveness and readiness: `/healthz` and `/readyz` for gateways; `myelinate healthcheck` (heartbeat file,
      `MYELINATE_WORKER_HEARTBEAT_FILE`) for workers.
- [ ] Image pinned to a version tag or digest, not `latest`. Upgrades go through staging first.

## Observability

- [ ] Prometheus scrapes `/metrics` on every gateway (port 8080) and every worker (port 9464).
- [ ] Alerts on: `/readyz` failures, 5xx rate, recall latency, worker restarts, budget exhaustion, database
      connections and storage.
- [ ] Job health watched via `myelinate_jobs_total{status="failed"}` and the pending backlog in the `jobs` table.
- [ ] Logs shipped to your log platform (gateway and worker logs are JSON outside `dev`).
- [ ] Traces exported via OpenTelemetry if you run a collector.

## Governance and learning

- [ ] Governance policy reviewed: `MYELINATE_AUTO_APPROVE_MEMORIES`, `MYELINATE_AUTO_APPROVE_LOW_RISK`,
      `MYELINATE_AUTO_APPROVE_MEDIUM_RISK` (default off), `MYELINATE_AUTO_APPROVE_MIN_EVAL_SCORE`.
- [ ] Someone owns the review queue for pending changesets.
- [ ] Canary and holdout settings chosen (`MYELINATE_CANARY_ENABLED`, `MYELINATE_HOLDOUT_RATE`).
- [ ] `MYELINATE_DAILY_TOKEN_BUDGET` set per tenant, with an alert before it runs out.
- [ ] Audit log (`/v1/admin/audit`) retained or exported per your compliance needs.

## LLM data handling

- [ ] You know where learning calls go. Reflection, judging, curation and evolution send episode content (after PII
      redaction) to the configured LLM provider. Check that provider's region and data-retention terms against your
      data-residency requirements, and the same for your embeddings endpoint.
- [ ] `MYELINATE_LLM_PROVIDER=none` for tenants or environments where no data may leave your network (learning-side
      LLM features are then disabled).
