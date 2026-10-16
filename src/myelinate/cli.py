"""`myelinate serve` runs the gateway; `myelinate worker` runs a standalone job worker."""

from __future__ import annotations

import argparse
import asyncio


def main() -> None:
    ap = argparse.ArgumentParser(prog="myelinate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="run the gateway (REST + MCP)")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8080)
    sv.add_argument("--workers", type=int, default=1)
    sub.add_parser("worker", help="run only the background learning worker")
    sub.add_parser("purge", help="delete episodes older than the retention window")
    hc = sub.add_parser("healthcheck", help="exit 0 if this container's gateway or worker is healthy")
    hc.add_argument("--port", type=int, default=8080)
    hc.add_argument("--max-age", type=float, default=60.0, help="max heartbeat age in seconds for workers")
    args = ap.parse_args()

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("myelinate.gateway.app:create_app", factory=True, host=args.host, port=args.port,
                    workers=args.workers, proxy_headers=True, access_log=False)
    elif args.cmd == "worker":
        asyncio.run(_worker())
    elif args.cmd == "purge":
        asyncio.run(_purge())
    elif args.cmd == "healthcheck":
        raise SystemExit(_healthcheck(args.port, args.max_age))


async def _engine():  # type: ignore[no-untyped-def]
    from .config import get_settings
    from .db import Database
    from .engine import LearningEngine
    from .gateway.app import build_embedder, build_llm

    s = get_settings()
    db = Database(s.database_url, s.db_pool_size, s.db_max_overflow)
    await db.create_all()
    return LearningEngine(db, s, build_llm(s), build_embedder(s))


async def _worker() -> None:
    import os
    import signal

    from prometheus_client import start_http_server

    from .config import get_settings
    from .telemetry import configure_logging
    from .worker import Worker

    s = get_settings()
    configure_logging(json_logs=s.env != "dev")
    if s.worker_metrics_port:
        start_http_server(s.worker_metrics_port)  # job / LLM / evolution counters live in this process
    w = Worker(await _engine(), heartbeat_file=os.environ.get("MYELINATE_WORKER_HEARTBEAT_FILE"))
    w.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await w.stop()  # finish in-flight jobs instead of leaving them to lease expiry


async def _purge() -> None:
    n = await (await _engine()).purge_expired()
    print("purged " + ", ".join(f"{v} {k}" for k, v in n.items()))


def _healthcheck(port: int, max_age: float) -> int:
    """Healthy if the local gateway answers /healthz, or a standalone worker's heartbeat file is fresh."""
    import os
    import time
    import urllib.request

    beat = os.environ.get("MYELINATE_WORKER_HEARTBEAT_FILE")
    if beat and os.path.exists(beat):
        return 0 if time.time() - os.path.getmtime(beat) <= max_age else 1
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as r:
            return 0 if r.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    main()
