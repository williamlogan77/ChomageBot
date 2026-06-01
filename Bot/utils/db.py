"""Central database connection factory.

Every database connection in the bot is opened through this module so the
storage backend lives behind a single seam. Today that backend is SQLite —
synchronous access via :mod:`sqlite3`, asynchronous via :mod:`aiosqlite` —
and the connection target is a filesystem path.

The factories are deliberately thin pass-throughs that preserve the exact
behaviour of the ``sqlite3.connect`` / ``aiosqlite.connect`` calls they
replace, so swapping them in is behaviour-neutral.

Scope note: centralising the *connection* is what makes a future backend
move (e.g. Postgres) tractable — there's one place to change instead of
~30 scattered call sites. It is **not** a literal connection-string swap on
its own: the SQL dialect (``INSERT OR IGNORE``, ``pandas.read_sql_query``)
and the async driver (aiosqlite vs asyncpg) still differ and would need
their own handling. This module intentionally stays a connection factory,
not an ORM / dialect-translation layer.
"""

from __future__ import annotations

import sqlite3

import aiosqlite

# Process-wide default DB target, set once at startup via configure(). Call
# sites may still pass an explicit path (and currently do, via bot.db_path);
# the default is the single knob to change when the location moves.
_db_path: str | None = None


def configure(db_path: str) -> None:
    """Set the process-wide default database target.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database file. Call once during startup.
    """
    global _db_path
    _db_path = db_path


def _resolve(db_path: str | None) -> str:
    """Return the explicit ``db_path`` if given, else the configured default.

    Raises
    ------
    RuntimeError
        If no path is supplied and :func:`configure` has not been called.
    """
    target = db_path if db_path is not None else _db_path
    if target is None:
        raise RuntimeError("database not configured: pass db_path or call db.configure() first")
    return target


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a synchronous SQLite connection.

    Parameters
    ----------
    db_path : str, optional
        Target database; falls back to the configured default.

    Returns
    -------
    sqlite3.Connection
        A connection usable as a context manager, exactly as
        ``sqlite3.connect`` returns.
    """
    return sqlite3.connect(_resolve(db_path))


def aconnect(db_path: str | None = None) -> aiosqlite.Connection:
    """Open an asynchronous (aiosqlite) SQLite connection.

    Parameters
    ----------
    db_path : str, optional
        Target database; falls back to the configured default.

    Returns
    -------
    aiosqlite.Connection
        An awaitable connection usable with ``async with``, exactly as
        ``aiosqlite.connect`` returns.
    """
    return aiosqlite.connect(_resolve(db_path))
