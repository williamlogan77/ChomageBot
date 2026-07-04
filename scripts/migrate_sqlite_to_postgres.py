"""One-shot, idempotent copy of the legacy sqlite database into Postgres.

Usage (path defaults are repo-root-relative, resolved from this script's
own location so they work both locally and inside the bot container):

    python scripts/migrate_sqlite_to_postgres.py \
        --sqlite <repo>/Bot/db/database.sqlite \
        --database-url $DATABASE_URL \
        --apply-schema <repo>/Bot/db/setup.postgres.sql

Design notes:
  - stdlib sqlite3 + SYNC psycopg. No async — nothing here needs it, and
    sync psycopg sidesteps the Windows proactor-event-loop issue entirely.
  - Copies tables in FK-safe order (users and discord_channels before
    discord_events; league_players before nothing but kept early anyway).
  - Tables or columns missing from older sqlite files are skipped
    gracefully: the sqlite side is introspected with PRAGMA table_info and
    only the intersection of columns is copied. The committed snapshot,
    for example, lacks match_stats / command_usage / league_history.queue —
    the live prod file has them.
  - Identity values are preserved (INSERT ... OVERRIDING SYSTEM VALUE),
    then each sequence is setval'd to max(id) so future inserts don't
    collide.
  - sqlite timestamps are 'YYYY-MM-DD HH:MM:SS[.ffffff]' UTC strings;
    they're parsed and inserted as tz-aware UTC datetimes (TIMESTAMPTZ).
  - Idempotent: every INSERT is ON CONFLICT DO NOTHING, so re-running
    against a partially/fully migrated Postgres is safe.

Exits non-zero if any copied table ends up with fewer rows in Postgres
than sqlite has.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys

import psycopg

BATCH_SIZE = 1000

# Path defaults resolve from the repo root (this script's parent's parent),
# NOT the CWD: the runbook's in-container invocation mounts scripts/ at
# /scripts with WORKDIR /Bot, where a CWD-relative "Bot/db/..." would
# wrongly resolve to /Bot/Bot/db/... . /scripts/..  ->  /Bot/db/... works.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SQLITE = os.path.join(_REPO_ROOT, "Bot", "db", "database.sqlite")
# Fallback mirrors utils/db.dsn(); prod passes the Postgres LXC's URL.
DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://chomage:chomage@localhost:5432/chomage")
DEFAULT_SCHEMA = os.path.join(_REPO_ROOT, "Bot", "db", "setup.postgres.sql")

# (table, identity column to preserve or None, timestamp columns)
# Order is FK-safe: discord_events references users + discord_channels.
TABLES: list[tuple[str, str | None, set[str]]] = [
    ("users", None, set()),
    ("discord_channels", None, set()),
    ("discord_events", "event_id", {"timestamp"}),
    ("league_players", None, set()),
    ("league_history", "id", {"timestamp"}),
    ("match_stats", None, {"game_start"}),
    ("command_usage", "id", {"timestamp"}),
]


def parse_timestamp(value) -> dt.datetime | None:
    """sqlite 'YYYY-MM-DD HH:MM:SS[.ffffff]' UTC string -> aware datetime."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    ts = dt.datetime.fromisoformat(str(value).strip())
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.UTC)
    return ts


def sqlite_tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def sqlite_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]


def postgres_columns(pg: psycopg.Connection, table: str) -> set[str]:
    rows = pg.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def copy_table(
    lite: sqlite3.Connection,
    pg: psycopg.Connection,
    table: str,
    identity_col: str | None,
    ts_cols: set[str],
) -> int:
    """Copy one table; returns rows read from sqlite."""
    src_cols = sqlite_columns(lite, table)
    dst_cols = postgres_columns(pg, table)
    # Copy the intersection. sqlite's legacy mixed-case "leagueId" folds to
    # the postgres lower-case leagueid.
    cols = [c for c in src_cols if c.lower() in dst_cols]
    skipped = [c for c in src_cols if c.lower() not in dst_cols]
    if skipped:
        print(f"  {table}: skipping sqlite-only column(s): {', '.join(skipped)}")
    if not cols:
        print(f"  {table}: no common columns, skipped")
        return 0

    select_sql = "SELECT {} FROM {}".format(", ".join(f'"{c}"' for c in cols), table)
    overriding = (
        "OVERRIDING SYSTEM VALUE "
        if identity_col is not None and identity_col in {c.lower() for c in cols}
        else ""
    )
    insert_sql = "INSERT INTO {} ({}) {}VALUES ({}) ON CONFLICT DO NOTHING".format(
        table,
        ", ".join(c.lower() for c in cols),
        overriding,
        ", ".join(["%s"] * len(cols)),
    )

    ts_idx = [i for i, c in enumerate(cols) if c.lower() in ts_cols]
    total = 0
    batch: list[tuple] = []
    with pg.cursor() as cur:
        for row in lite.execute(select_sql):
            row = list(row)
            for i in ts_idx:
                row[i] = parse_timestamp(row[i])
            batch.append(tuple(row))
            total += 1
            if len(batch) >= BATCH_SIZE:
                cur.executemany(insert_sql, batch)
                batch.clear()
        if batch:
            cur.executemany(insert_sql, batch)

        if identity_col is not None:
            cur.execute(f"SELECT COALESCE(MAX({identity_col}), 0) FROM {table}")
            max_id = cur.fetchone()[0]
            if max_id > 0:
                cur.execute(
                    "SELECT setval(pg_get_serial_sequence(%s, %s), %s, true)",
                    (table, identity_col, max_id),
                )
                print(f"  {table}: sequence for {identity_col} set to {max_id}")
    pg.commit()
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sqlite", default=DEFAULT_SQLITE, help="path to the sqlite file")
    parser.add_argument(
        "--database-url",
        default=DEFAULT_DSN,
        help="Postgres DSN (default: DATABASE_URL env var, else localhost test DSN)",
    )
    parser.add_argument(
        "--apply-schema",
        default=DEFAULT_SCHEMA,
        metavar="PATH",
        help="schema file applied (idempotently) before copying",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.sqlite):
        print(f"sqlite file not found: {args.sqlite}", file=sys.stderr)
        return 2

    lite = sqlite3.connect(args.sqlite)
    pg = psycopg.connect(args.database_url)

    print(f"Applying schema {args.apply_schema}")
    with open(args.apply_schema, encoding="utf-8") as f:
        pg.execute(f.read())
    pg.commit()

    present = sqlite_tables(lite)
    results: list[tuple[str, int | None, int]] = []  # (table, sqlite_count, pg_count)
    for table, identity_col, ts_cols in TABLES:
        if table not in present:
            print(f"  {table}: not in sqlite file, skipped")
            pg_count = pg.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            results.append((table, None, pg_count))
            continue
        print(f"Copying {table}...")
        lite_count = lite.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        copy_table(lite, pg, table, identity_col, ts_cols)
        pg_count = pg.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        results.append((table, lite_count, pg_count))

    lite.close()

    print()
    print(f"{'table':<18} {'sqlite':>8} {'postgres':>9}")
    failed = False
    for table, lite_count, pg_count in results:
        lite_display = "-" if lite_count is None else str(lite_count)
        marker = ""
        if lite_count is not None and pg_count < lite_count:
            marker = "  <-- MISMATCH"
            failed = True
        elif lite_count is not None and pg_count > lite_count:
            marker = "  (postgres has extra rows — OK)"
        print(f"{table:<18} {lite_display:>8} {pg_count:>9}{marker}")

    pg.close()
    if failed:
        print("\nFAILED: postgres is missing rows for at least one table", file=sys.stderr)
        return 1
    print("\nOK: every sqlite row is present in postgres")
    return 0


if __name__ == "__main__":
    sys.exit(main())
