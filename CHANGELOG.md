# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.2] - 2026-10-16

### Changed
- All three registries now publish without stored credentials: PyPI and npm through trusted publishing (OIDC), GHCR
  through the workflow token. Every artifact carries signed provenance.

## [0.2.1] - 2026-10-16

### Fixed
- The OpenClaw plugin ships compiled JavaScript (`dist/`), so `openclaw plugins install @myelinate/openclaw-plugin`
  works from npm. 0.2.0 contained only TypeScript sources, which OpenClaw loads only from local checkouts.

### Changed
- Install from the registries: `pip install myelinate`, `ghcr.io/notaaaronfr/myelinate`, and
  `@myelinate/openclaw-plugin` on npm.

## [0.2.0] - 2026-10-16

### Added
- Documentation site (MkDocs Material, published to GitHub Pages): quickstart, concepts, integration guides, Docker
  and Kubernetes deployment, a production checklist, operations guides (security and SSO, governance, observability,
  data and privacy, scaling and HA, backup and upgrades, cost control), troubleshooting, FAQ, and configuration and
  API references generated from the code.
- Kubernetes manifests (`deploy/kubernetes/`), verified on k3s.
- Runnable examples (`examples/`).
- `myelinate healthcheck` for container probes. Standalone workers now get a heartbeat that is independent of job
  length, graceful SIGTERM shutdown, JSON logs, and Prometheus metrics on `MYELINATE_WORKER_METRICS_PORT` (9464).
- `GET /v1/episodes/latest`, so stateless clients can attach feedback to a session's last turn.
- New settings: `MYELINATE_AUDIT_RETENTION_DAYS`, `MYELINATE_DB_POOL_SIZE`, `MYELINATE_DB_MAX_OVERFLOW` and
  `MYELINATE_WORKER_METRICS_PORT`.
- OIDC scopes are now also read from Auth0 `permissions` and Keycloak `realm_access.roles`.

### Changed
- `agent_id="*"` (knowledge shared tenant-wide) can only be written through review-scoped skill endpoints.
- OIDC outside `dev` requires both `MYELINATE_OIDC_ISSUER` and `MYELINATE_OIDC_AUDIENCE`.
- `myelinate purge` also removes old recall logs, finished jobs and (per `MYELINATE_AUDIT_RETENTION_DAYS`) audit
  entries.
- Redaction placeholders are now `[REDACTED <label>]`.
- The insights field `learning_cost_30d` is renamed `tenant_learning_cost_30d`.
- Docker Compose services restart automatically. The image healthcheck works for both gateways and workers.

### Fixed
- Recall injects a skill's rules in their authored order.
- Gateways and workers starting together on an empty PostgreSQL no longer race on schema creation.
- When the learning LLM is unavailable or the budget is exhausted, the API returns 503 or 429 with `Retry-After`,
  instead of 500.
- A missing Anthropic API key fails individual learning steps instead of whole jobs.
- Memories that can't be reconciled without the LLM are held for review instead of being dropped.
- Redaction no longer mangles secret labels or swallows text after card numbers.
- Rate-limit responses include `Retry-After`.
- Changeset and experiment metrics count transitions from canary promotion, queue release and rollback.
- The Docker image runs standalone with SQLite (data on a `/data` volume).

## [0.1.0] - 2026-10-16

### Added
- Gateway with REST, MCP and OTLP ingestion; adapters for Microsoft Agent Framework, OpenClaw and any framework.
- Skills as individually credited rules, evidence-gated reflection and curation, and GEPA-style evolution.
- Bayesian canary rollout and a holdout group measuring causal lift.
- Memory with reconciliation and validity intervals, and per-user profile cards.
- Multi-tenancy, scoped API keys, OIDC, audit log, PII redaction, erasure, budgets and idempotent ingestion.
