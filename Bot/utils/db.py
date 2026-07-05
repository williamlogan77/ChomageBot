"""Single shared async Postgres access layer.

Every DB touch in the codebase goes through this module so there is exactly
one pool, one DSN source, and one place to change if the database moves
(e.g. the planned RDS migration).

Connection string comes from ``DATABASE_URL`` via utils/config.py —
required, no baked-in credential fallback.

Postgres notes for query authors:
  - placeholders are ``%s`` (psycopg3), not sqlite's ``?``
  - never quote identifiers — schema identifiers are all lower-case folded
  - ``REPLACE INTO`` becomes ``INSERT ... ON CONFLICT (...) DO UPDATE``
  - timestamp columns come back as tz-aware ``datetime``, not str
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool
from utils import config

# Survive auto_reload's importlib.reload of utils modules: re-executing this
# module body must not orphan a live pool (leaked connections would pile up
# on every hot deploy until Postgres hits max_connections).
_pool: AsyncConnectionPool | None = globals().get("_pool")


def dsn() -> str:
    return config.database_url()


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        pool = AsyncConnectionPool(dsn(), min_size=1, max_size=5, open=False)
        await pool.open(wait=True, timeout=30.0)
        _pool = pool
    return _pool


@asynccontextmanager
async def connection():
    """Async context manager yielding a pooled connection.

    The connection runs one transaction for the duration of the block:
    committed on clean exit, rolled back on exception (psycopg_pool
    behaviour). Use this when several statements must commit atomically;
    for one-shot statements prefer the helpers below.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        yield conn


async def fetchall(sql: str, params: tuple = ()) -> list[tuple]:
    async with connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def fetchone(sql: str, params: tuple = ()) -> tuple | None:
    async with connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def execute(sql: str, params: tuple = ()) -> None:
    async with connection() as conn:
        await conn.execute(sql, params)


async def executemany(sql: str, param_seq: list[tuple]) -> None:
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(sql, param_seq)


async def apply_schema(path: str) -> None:
    """Apply a schema file. Safe to run on every boot — the schema uses
    CREATE ... IF NOT EXISTS throughout, so this is a no-op when current.
    """
    with open(path, encoding="utf-8") as f:
        script = f.read()
    async with connection() as conn:
        await conn.execute(script)


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
