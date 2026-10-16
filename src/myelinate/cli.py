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
    args = ap.parse_args()

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("myelinate.gateway.app:create_app", factory=True, host=args.host, port=args.port,
                    workers=args.workers, proxy_headers=True, access_log=False)
    elif args.cmd == "worker":
        asyncio.run(_worker())
    elif args.cmd == "purge":
        asyncio.run(_purge())


async def _engine():  # type: ignore[no-untyped-def]
    from .config import get_settings
    from .db import Database
    from .engine import LearningEngine
    from .gateway.app import build_embedder, build_llm

    s = get_settings()
    db = Database(s.database_url)
    await db.create_all()
    return LearningEngine(db, s, build_llm(s), build_embedder(s))


async def _worker() -> None:
    from .worker import Worker

    w = Worker(await _engine())
    w.start()
    await asyncio.Event().wait()


async def _purge() -> None:
    n = await (await _engine()).purge_expired()
    print(f"purged {n} episodes")


if __name__ == "__main__":
    main()
