"""Async Postgres access — one pool, same shape as the bot's utils/db.

The dashboard connects with its own role (see README: SELECT on
everything, INSERT/UPDATE only on bot_config) so a bug here can't
corrupt match history.
"""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from app import config

_pool: AsyncConnectionPool | None = None


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        pool = AsyncConnectionPool(config.database_url(), min_size=1, max_size=4, open=False)
        await pool.open(wait=True, timeout=30.0)
        _pool = pool
    return _pool


async def fetchall(sql: str, params: tuple = ()) -> list[tuple]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def fetchone(sql: str, params: tuple = ()) -> tuple | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def execute(sql: str, params: tuple = ()) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(sql, params)


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
