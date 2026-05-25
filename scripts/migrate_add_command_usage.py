"""Idempotent migration: create command_usage table + index.

Captures slash-command + button + select-menu usage so we can later prune
unused features. The bot's on_interaction listener writes rows; this
script is for getting the table onto an existing DB that was created
before the schema added it.

Usage:
    python3 scripts/migrate_add_command_usage.py path/to/database.sqlite [--dry-run]
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

CREATE_TABLE_SQL = """
create table if not exists command_usage (
    id INTEGER not null primary key autoincrement,
    timestamp DATETIME not null DEFAULT CURRENT_TIMESTAMP,
    command_name TEXT not null,
    user_id TEXT,
    guild_id TEXT,
    interaction_type TEXT not null
)
"""

CREATE_INDEX_SQL = (
    "create index if not exists idx_command_usage_time_cmd "
    "on command_usage (timestamp DESC, command_name)"
)


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def migrate(db_path: Path, dry_run: bool = False) -> int:
    print(f"[migrate] target: {db_path}")
    if not db_path.exists():
        print(f"[migrate] ERROR: file does not exist: {db_path}")
        return 1

    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA wal_checkpoint(FULL)")

        already = _table_exists(con, "command_usage")
        if already:
            print("[migrate] command_usage table already present — nothing to do")
            return 0

        if dry_run:
            print("[migrate] DRY RUN — would CREATE TABLE command_usage + index")
            return 0

        backup_path = db_path.with_name(
            f"{db_path.stem}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}{db_path.suffix}"
        )
        print(f"[migrate] creating backup: {backup_path}")
        shutil.copy2(db_path, backup_path)

        print("[migrate] creating command_usage table + index...")
        con.execute("BEGIN")
        con.execute(CREATE_TABLE_SQL)
        con.execute(CREATE_INDEX_SQL)
        con.commit()
        print("[migrate] done.")
        return 0
    finally:
        con.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("db_path", type=Path, default=Path("Bot/db/database.sqlite"), nargs="?")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    return migrate(args.db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
