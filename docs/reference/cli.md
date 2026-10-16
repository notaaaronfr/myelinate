# CLI

Installing Myelinate provides one command, `myelinate`, with three subcommands. All of them read
[configuration](configuration.md) from environment variables and `.env`.

## `myelinate serve`

Runs the gateway: REST API, MCP endpoint (`/mcp`), OTLP ingestion, health and metrics. Unless
`MYELINATE_WORKER_ENABLED=false`, it also runs a background job worker in the same process.

```bash
myelinate serve [--host 0.0.0.0] [--port 8080] [--workers 1]
```

| Option | Default | Meaning |
|---|---|---|
| `--host` | `0.0.0.0` | Interface to bind |
| `--port` | `8080` | Port to listen on |
| `--workers` | `1` | Uvicorn worker processes. Each one is a full gateway (and job worker, if enabled). Job claiming is safe across processes |

Behind a load balancer, set `FORWARDED_ALLOW_IPS` (read by Uvicorn, default `127.0.0.1`) to the proxy's address or
range so `X-Forwarded-For` is trusted and per-IP rate limits see real client addresses. For example, use
`FORWARDED_ALLOW_IPS="*"` only when the gateway is reachable exclusively through your proxy.

## `myelinate worker`

Runs only the background job worker: reflection, curation, evolution and profile refresh. Use it to scale learning
separately from the API, with `MYELINATE_WORKER_ENABLED=false` on the gateways. Several workers can share one
database; each job runs once.

```bash
myelinate worker
```

Set `MYELINATE_WORKER_HEARTBEAT_FILE=/tmp/worker.alive` to have the worker touch that file every loop, so a container
liveness probe can check it (see [Kubernetes](../deployment/kubernetes.md)).

## `myelinate purge`

Deletes episodes older than `MYELINATE_EPISODE_RETENTION_DAYS` (default 90), then exits. Run it daily from cron or a
Kubernetes CronJob.

```bash
myelinate purge
# purged 1234 episodes
```
