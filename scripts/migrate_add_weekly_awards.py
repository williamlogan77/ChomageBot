"""Idempotent migration: create the weekly_awards table.

The weekly awards ceremony (cogs/weekly_awards.py) records each week's
winners here; the trophy cabinet board renders all-time title counts from
it. main.py auto-applies Bot/db/setup.postgres.sql on every boot, but
deploys hot-reload cogs without restarting the process — this script gets
the table onto the live DB without waiting for the next full restart.

The DDL below is kept identical to the weekly_awards block in
Bot/db/setup.postgres.sql.

Usage:
    python3 scripts/migrate_add_weekly_awards.py [--database-url DSN] [--dry-run]

DSN default: the DATABASE_URL env var — same source as utils/config.py,
no baked-in credential fallback.
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg

# DSN comes from --database-url or the DATABASE_URL env var — like
# utils/config.py, no baked-in credential fallback.
DEFAULT_DSN = os.environ.get("DATABASE_URL")

CREATE_TABLE_SQL = """
create table if not exists weekly_awards (
    id BIGINT generated always as identity primary key,
    week_start DATE not null,
    award TEXT not null,
    discord_user_id BIGINT not null,
    display_name TEXT,
    value NUMERIC,
    detail JSONB,
    created_at TIMESTAMPTZ not null default now(),
    unique (week_start, award, discord_user_id)
)
"""


def migrate(dsn: str, dry_run: bool = False) -> int:
    with psycopg.connect(dsn) as conn:
        exists = conn.execute("SELECT to_regclass('weekly_awards')").fetchone()[0]
        if exists is not None:
            print("[migrate] weekly_awards table already present — nothing to do")
            return 0

        if dry_run:
            print("[migrate] DRY RUN — would CREATE TABLE weekly_awards (+ unique constraint)")
            return 0

        print("[migrate] creating weekly_awards table...")
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()

        check = conn.execute("SELECT to_regclass('weekly_awards')").fetchone()[0]
        if check is None:
            print("[migrate] ERROR: weekly_awards still missing after CREATE")
            return 2

        print("[migrate] done. The weekly_awards cog can now record ceremonies.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--database-url",
        default=DEFAULT_DSN,
        help="Postgres DSN (default: the DATABASE_URL env var; required one way or the other)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report whether the table exists, don't modify the DB",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("no DSN: pass --database-url or set the DATABASE_URL env var")
    return migrate(args.database_url, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
