# Kubernetes

[`deploy/kubernetes/`](https://github.com/notaaaronfr/myelinate/tree/main/deploy/kubernetes) holds plain manifests
you can apply with `kubectl apply -k` or pull into your own kustomize overlay.

| File | What it is | In `kustomization.yaml` |
|---|---|---|
| `namespace.yaml` | `myelinate` namespace, Pod Security `restricted` enforced | yes |
| `configmap.yaml` | non-secret `MYELINATE_*` settings | yes |
| `secret.example.yaml` | placeholders for the bootstrap key, `ANTHROPIC_API_KEY`, database URL | **no** (create the Secret yourself) |
| `gateway-deployment.yaml` | gateway, 2 replicas, probes, resources, hardened `securityContext`, `PodDisruptionBudget` | yes |
| `gateway-service.yaml` | ClusterIP Service, port 80 → 8080 | yes |
| `worker-deployment.yaml` | learning workers, 2 replicas, heartbeat-file probes | yes |
| `hpa.yaml` | gateway CPU autoscaling, 2–8 replicas | yes |
| `networkpolicy.yaml` | default-deny ingress, gateway reachable only from the ingress controller | optional |
| `ingress.example.yaml` | ingress-nginx + cert-manager example | optional |
| `postgres-dev.yaml` | single-replica Postgres StatefulSet, **for trying it out only** | optional |

!!! note "Images"
    The manifests use the official multi-arch image `ghcr.io/notaaaronfr/myelinate`, pinned to `0.2.0` in
    `kustomization.yaml`. Bump `newTag` (or pin a digest) to upgrade. To run your own build, see
    [Docker](docker.md#build-and-publish-your-own-image) and change `newName`.

## Architecture

```
            Ingress (TLS)
                 │
       Service myelinate-gateway :80
                 │
   ┌─────────────┴─────────────┐
 gateway pod  …  gateway pod        (HPA 2–8, MYELINATE_WORKER_ENABLED=false)
   │                           │
   └──────────┬────────────────┘
              │  PostgreSQL (managed)  ◄─── worker pods × N (claim jobs with a lease)
```

Gateways are stateless. Workers coordinate through the `jobs` table (conditional claim plus a 10-minute lease), so
you can run any number of each. See [Scaling and HA](../operations/scaling-and-ha.md).

## 1. Database

Use **managed PostgreSQL** in production (RDS, Cloud SQL, Azure Database for PostgreSQL, AlloyDB, or an operator such
as CloudNativePG) with automated backups, point-in-time recovery and TLS. Create a database and a user, then build the
URL:

```text
postgresql+asyncpg://myelinate:<url-encoded-password>@<host>:5432/myelinate?ssl=require
```

The schema is created automatically on startup.

To try Myelinate without a database, `postgres-dev.yaml` runs a single Postgres pod with a 5 Gi volume. It has no
backups, no TLS and a hard-coded password. Do not use it for anything you want to keep.

## 2. Secret

Create `myelinate-secrets` out of band so `kubectl apply -k` never overwrites it:

```bash
kubectl apply -f deploy/kubernetes/namespace.yaml

kubectl -n myelinate create secret generic myelinate-secrets \
  --from-literal=MYELINATE_BOOTSTRAP_ADMIN_KEY="$(openssl rand -base64 48)" \
  --from-literal=ANTHROPIC_API_KEY="sk-ant-..." \
  --from-literal=MYELINATE_DATABASE_URL='postgresql+asyncpg://myelinate:...@db.internal:5432/myelinate?ssl=require'
```

In production, source it from your secret manager with External Secrets Operator, Sealed Secrets or SOPS.
`secret.example.yaml` shows the expected keys. Add `MYELINATE_EMBEDDINGS_API_KEY` if you use embeddings. If you use
OIDC instead of (or as well as) a bootstrap key, set the `MYELINATE_OIDC_*` values in the ConfigMap.

## 3. Configure

Edit `configmap.yaml`. At minimum:

- `MYELINATE_MCP_ALLOWED_HOSTS`: a JSON list that **must include your public host** (for example
  `["myelinate.example.com"]`), or `/mcp` answers `421 Misdirected Request`.
- `MYELINATE_CORS_ORIGINS`: browser origins that call the API directly, if any.
- `MYELINATE_LLM_MODEL`, `MYELINATE_LLM_EFFORT`, `MYELINATE_DAILY_TOKEN_BUDGET`.

Every key is `MYELINATE_` plus a field name from the [configuration reference](../reference/configuration.md).

Two values are set directly in the Deployments rather than the ConfigMap:

- Gateway: `MYELINATE_WORKER_ENABLED=false` (learning runs in the worker Deployment), and `FORWARDED_ALLOW_IPS="*"`
  so Uvicorn trusts `X-Forwarded-For` from the ingress and per-IP rate limiting sees real clients. That is only safe
  when nothing but the ingress controller can reach the pods; enable `networkpolicy.yaml` or narrow it to your
  ingress pod CIDR.
- Worker: `MYELINATE_WORKER_HEARTBEAT_FILE=/tmp/worker.alive`.

For your environment, prefer an overlay over editing the base:

```yaml
# overlays/prod/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../deploy/kubernetes
  - ../../deploy/kubernetes/networkpolicy.yaml
  - ingress.yaml
images:
  - name: ghcr.io/notaaaronfr/myelinate
    newName: registry.example.com/myelinate
    newTag: "0.2.0"
patches:
  - target: { kind: ConfigMap, name: myelinate-config }
    patch: |-
      - op: replace
        path: /data/MYELINATE_MCP_ALLOWED_HOSTS
        value: '["myelinate.example.com"]'
```

## 4. Apply

```bash
kubectl apply -k deploy/kubernetes            # or your overlay
kubectl -n myelinate rollout status deploy/myelinate-gateway
kubectl -n myelinate rollout status deploy/myelinate-worker
```

If the database is not reachable yet (for example `postgres-dev` is still starting), gateway and worker pods exit
and restart until it is, so a few restarts on first deploy are expected. Pods that start together on an empty
database are safe: schema creation is serialized with a PostgreSQL advisory lock.

Smoke test through a port-forward:

```bash
kubectl -n myelinate port-forward svc/myelinate-gateway 8080:80 &
curl -s localhost:8080/readyz

BOOT=$(kubectl -n myelinate get secret myelinate-secrets \
  -o jsonpath='{.data.MYELINATE_BOOTSTRAP_ADMIN_KEY}' | base64 -d)
curl -s -X POST localhost:8080/v1/admin/api-keys \
  -H "Authorization: Bearer $BOOT" -H "X-Tenant-Id: acme" \
  -H "Content-Type: application/json" -d '{"name":"support-bot","scopes":["learn"]}'
```

## Probes and security

| | Gateway | Worker |
|---|---|---|
| Readiness | `GET /readyz` (runs `SELECT 1`) | `myelinate healthcheck --max-age 60` (heartbeat file) |
| Liveness | `GET /healthz` (process only, never the DB) | `myelinate healthcheck --max-age 60` (heartbeat file) |
| Startup | `GET /healthz`, up to 60 s | — |

A dedicated task touches the heartbeat file every 10 seconds, independently of job length, so a long evolution
run never fails the probe. On SIGTERM the worker stops claiming jobs and finishes in-flight ones (up to 30 seconds,
within the default termination grace period).

Every pod runs as uid 10001 with `runAsNonRoot`, `readOnlyRootFilesystem`, all capabilities dropped and the
`RuntimeDefault` seccomp profile; `/tmp` is an `emptyDir`. Service-account tokens are not mounted. The namespace
enforces the `restricted` Pod Security Standard, which `postgres-dev.yaml` also satisfies (it runs as uid 999).

On `SIGTERM` a worker exits immediately; a job it was running is picked up by another worker after its lease
expires (up to 10 minutes). The gateway has a 5-second `preStop` delay so the endpoint is removed before Uvicorn
shuts down.

## Ingress and TLS

`ingress.example.yaml` targets ingress-nginx with a cert-manager `ClusterIssuer` named `letsencrypt`. Change the
host, class and issuer, and keep `proxy-body-size` at or above `MYELINATE_MAX_REQUEST_BYTES` (4 MB by default).
`/mcp` is plain request/response HTTP (stateless, JSON responses), so no sticky sessions or WebSocket settings are
needed. Add the host to `MYELINATE_MCP_ALLOWED_HOSTS`.

On k3s, which ships Traefik, set `ingressClassName: traefik` and drop the nginx annotations.

## Scaling

- **Gateway**: `hpa.yaml` scales 2–8 replicas at 70% CPU (needs metrics-server). Idle gateways use about
  130 MiB; requests are 250m CPU and 384 MiB, limit 1 GiB.
- **Workers**: scale by hand to match learning backlog (`kubectl -n myelinate scale deploy/myelinate-worker
  --replicas=4`). Each worker runs up to `MYELINATE_WORKER_CONCURRENCY` jobs. Workers serve Prometheus metrics on
  port 9464 (`MYELINATE_WORKER_METRICS_PORT`), including job, learning-LLM and evolution counters. The backlog is
  also visible in the database (`SELECT status, count(*) FROM jobs GROUP BY status`).
- **Rate limits** (`MYELINATE_RATE_LIMIT_PER_MINUTE`, `MYELINATE_IP_RATE_LIMIT_PER_MINUTE`) are in memory per
  replica, so the effective limit grows with replica count. Enforce hard limits at the ingress or API gateway.
- **PodDisruptionBudget**: at least one gateway stays up during node drains.

More in [Scaling and HA](../operations/scaling-and-ha.md).

## Upgrades and rollouts

1. Read the [changelog](../changelog.md) and take a database backup (see
   [Backup and upgrades](../operations/backup-and-upgrades.md)).
2. Bump the image tag in your overlay and `kubectl apply -k`.
3. Both Deployments roll with `maxSurge: 1, maxUnavailable: 0`; the gateway waits for `/readyz` before taking
   traffic.
4. Roll back with `kubectl -n myelinate rollout undo deploy/myelinate-gateway` (and `deploy/myelinate-worker`).

Pin images by tag or digest; with `imagePullPolicy: IfNotPresent`, a moving `latest` tag is not re-pulled on nodes
that already have it.

## Observability

Gateway pods carry `prometheus.io/scrape` annotations for `/metrics` on port 8080. If you use the Prometheus
Operator, create a `ServiceMonitor` for the `myelinate-gateway` Service instead, and allow your monitoring namespace
in `networkpolicy.yaml`. Worker pods expose metrics on port 9464 and carry the same scrape annotations. Gateway and worker logs are
JSON outside `dev`. See [Observability](../operations/observability.md).

## What we verified

The manifests were applied to a single-node k3s cluster (Colima, Kubernetes v1.35, Docker runtime) with a locally
built image (`myelinate:dev`, overridden through a kustomize overlay), `postgres-dev.yaml`, `networkpolicy.yaml` and
`MYELINATE_LLM_PROVIDER=none`:

- All pods were admitted under Pod Security `restricted`. Gateway (2) and worker (2) Deployments rolled out after
  restarting until Postgres was ready.
- `/readyz` returned `{"status":"ready"}`; `/healthz` and `/metrics` responded.
- An unauthenticated `POST /v1/recall` returned 401.
- The bootstrap key plus `X-Tenant-Id: acme` minted an API key with `learn` and `review` scopes.
- `POST /v1/skills/import` with a SKILL.md returned `status: applied`; `POST /v1/recall` returned that skill and
  its bullets.
- `POST /v1/episodes` with `outcome: failure` scheduled a reflection; a worker pod claimed and finished the
  `reflect` job (skipping LLM work, as expected with no provider).
- Worker pods were Ready via the heartbeat probe; the probe exits 0 with a fresh file and 1 when the file is missing.
- The root filesystem was read-only in the containers, which ran as uid 10001.
- With `networkpolicy.yaml`, a worker pod could not reach the gateway Service, while kubelet probes and
  port-forward still worked.
- `/mcp` accepted `Host: localhost:<port>` (allowed) and returned 421 for a host not in
  `MYELINATE_MCP_ALLOWED_HOSTS`.
- `kubectl rollout restart` of the gateway completed with the PDB in place; the HPA read CPU from metrics-server.
- On an empty schema, starting four pods at once made one gateway restart once. That race has since been fixed
  with an advisory lock; the fix was verified with Docker Compose (gateway, 2 workers and Postgres starting together:
  zero restarts).

Not verified: ingress with real TLS, managed Postgres with `ssl=require`, and a real LLM provider.
