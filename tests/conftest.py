"""Run the suite on SQLite by default, or on PostgreSQL with MYELINATE_TEST_DATABASE_URL set."""

from __future__ import annotations

import asyncio
import os

import pytest

PG = os.environ.get("MYELINATE_TEST_DATABASE_URL")


def _reset_pg() -> None:
    from myelinate.db import Base, Database

    async def go() -> None:
        db = Database(PG)
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await db.close()

    asyncio.run(go())


@pytest.fixture
def db_url(tmp_path):
    if PG:
        _reset_pg()
        return PG
    return f"sqlite+aiosqlite:///{tmp_path}/t.db"
