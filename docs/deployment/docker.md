# Docker

Myelinate ships as one image that runs either role:

- `myelinate serve` (the default command): the gateway, serving REST, MCP (`/mcp`) and OTLP ingest on port 8080.
- `myelinate worker`: a background learning worker with no HTTP port.

The image runs as uid/gid `10001`, sets `MYELINATE_ENV=prod`, and defaults to SQLite at `/data/myelinate.db` (a
declared volume). Because `MYELINATE_ENV=prod`, the gateway **refuses to start** unless you set
`MYELINATE_BOOTSTRAP_ADMIN_KEY` or OIDC (`MYELINATE_OIDC_JWKS_URL` plus issuer and audience).

!!! note "Images"
    Official images are published to `ghcr.io/notaaaronfr/myelinate` for `linux/amd64` and `linux/arm64`, tagged
    `X.Y.Z`, `X.Y` and `latest`, with signed build provenance. Pin a version in production. You can verify an image
    with `gh attestation verify oci://ghcr.io/notaaaronfr/myelinate:0.2.2 --owner notaaaronfr`.

## Single container (SQLite)

Good for a pilot or a single team: one container, SQLite on a volume, with the learning worker running inside the
gateway process (`MYELINATE_WORKER_ENABLED` defaults to `true`).

```bash
docker volume create myelinate-data
docker run -d --name myelinate \
  -p 8080:8080 \
  -v myelinate-data:/data \
  -e MYELINATE_BOOTSTRAP_ADMIN_KEY="$(openssl rand -base64 48)" \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  -e MYELINATE_MCP_ALLOWED_HOSTS='["localhost:*","127.0.0.1:*"]' \
  ghcr.io/notaaaronfr/myelinate:0.2.2
```

Read the bootstrap key back with `docker inspect` if you generated it inline, or better, keep it in an env file you
pass with `--env-file` and store in your vault.

Check it is up, then mint a scoped key for your agent:

```bash
curl -s localhost:8080/readyz          # {"status":"ready"}: database reachable

curl -s -X POST localhost:8080/v1/admin/api-keys \
  -H "Authorization: Bearer $MYELINATE_BOOTSTRAP_ADMIN_KEY" \
  -H "X-Tenant-Id: acme" \
  -H "Content-Type: application/json" \
  -d '{"name": "support-bot", "scopes": ["learn"]}'
```

The response contains a `myl_...` key, shown once. Agents use it as `Authorization: Bearer myl_...`.

Things to know:

- **Do not scale SQLite.** Run exactly one container against a SQLite file. For more than one replica or separate
  workers, use PostgreSQL.
- **Bind mounts must be writable by uid 10001**: `-v /srv/myelinate:/data` needs `chown 10001:10001 /srv/myelinate`.
  Named volumes work as-is.
- **Without an LLM key**, set `MYELINATE_LLM_PROVIDER=none`: recall, record and governance work, but reflection,
  judging and evolution are skipped.
- **Trying it locally without credentials**: `-e MYELINATE_ENV=dev` serves everything as tenant `dev` with all
  scopes. Never expose that.
- **Backups**: copy the volume while the container is stopped, or use `sqlite3 .backup`. See
  [Backup and upgrades](../operations/backup-and-upgrades.md).
- **Health**: the image's `HEALTHCHECK` calls `/healthz`. Use `/readyz` (runs `SELECT 1`) for load-balancer
  readiness and `/metrics` for Prometheus.

## Docker Compose (PostgreSQL + workers)

[`deploy/docker-compose.yml`](https://github.com/notaaaronfr/myelinate/blob/main/deploy/docker-compose.yml) runs a
production-like stack on one host:

| Service | What it runs |
|---|---|
| `postgres` | `postgres:17` with a named volume `pgdata` and a `pg_isready` healthcheck |
| `gateway` | `myelinate serve` on port 8080, `MYELINATE_WORKER_ENABLED=false` |
| `worker` | `myelinate worker` × 2 replicas, healthy while its heartbeat file stays fresh |

Both Myelinate services build the image from the repository and read `../.env` (the repository root). Create it from
the example and add the Postgres password:

```bash
cp .env.example .env
# edit .env and set at least:
#   MYELINATE_BOOTSTRAP_ADMIN_KEY=<long random string>
#   ANTHROPIC_API_KEY=sk-ant-...
#   POSTGRES_PASSWORD=<random, URL-safe: it is embedded in the database URL>
#   MYELINATE_MCP_ALLOWED_HOSTS=["myelinate.example.com","localhost:*"]
```

Start it from the repository root. Pass `--env-file .env` explicitly: Compose resolves `${POSTGRES_PASSWORD}` from a
`.env` in the *project directory*, which is `deploy/` when you use `-f deploy/docker-compose.yml`, so without the
flag it stops with `required variable POSTGRES_PASSWORD is missing a value`.

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d --build
docker compose --env-file .env -f deploy/docker-compose.yml ps
curl -s localhost:8080/readyz
```

Notes:

- `POSTGRES_PASSWORD` is interpolated into `postgresql+asyncpg://myelinate:<password>@postgres:5432/myelinate`. Use a
  URL-safe value (for example `openssl rand -hex 32`) or URL-encode it.
- Workers have no API port. Their healthcheck (`myelinate healthcheck`) checks `/tmp/worker.alive`, which a
  dedicated task touches every 10 seconds (`MYELINATE_WORKER_HEARTBEAT_FILE`), so long jobs never make a worker
  look dead. Workers serve Prometheus metrics on port 9464 (`MYELINATE_WORKER_METRICS_PORT`).
- Every service has `restart: unless-stopped`. Schema creation is serialized with a PostgreSQL advisory lock, so
  the gateway and workers can start together on an empty database. A clean first start was verified with zero
  restarts.
- `docker compose stop` sends SIGTERM: workers stop claiming jobs and finish in-flight ones (up to 30 seconds).
- Scale workers with `--scale worker=4`. Workers coordinate through the database, so any number is safe.
- Port 8080 is published on all interfaces. Put a TLS-terminating reverse proxy in front for anything beyond
  localhost, and add its host to `MYELINATE_MCP_ALLOWED_HOSTS`.

## Build and publish your own image

```bash
docker build -t ghcr.io/<you>/myelinate:0.2.2 .
docker push ghcr.io/<you>/myelinate:0.2.2
```

For mixed-architecture clusters, build both platforms:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/<you>/myelinate:0.2.2 --push .
```

The build installs the `otel` extra. Tag every release with a version (and deploy by digest if you can); avoid
running `latest` in production.

## Next steps

- [Kubernetes](kubernetes.md) for multi-replica gateways, worker pools and autoscaling.
- [Production checklist](production-checklist.md) before you go live.
- [Configuration reference](../reference/configuration.md) for every `MYELINATE_*` setting.
