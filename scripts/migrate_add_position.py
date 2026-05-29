"""Add a nullable ``position`` column to ``match_stats``.

Match-V5 participant objects include ``teamPosition`` (TOP / JUNGLE /
MIDDLE / BOTTOM / UTILITY) — the role Riot recorded as actually played.
We started capturing it on INSERT, but existing DBs were created before
the column existed. This script adds the column idempotently so old DBs
catch up to setup.sql.

What this script does, in one transaction:
  1. Take a timestamped backup of the .sqlite file next to the original.
  2. Detect whether ``position`` already exists on match_stats. If so,
     exit 0 (idempotent).
  3. ALTER TABLE match_stats ADD COLUMN position TEXT.
  4. Verify the row count is unchanged.

What it does NOT do:
  * Backfill ``position`` for existing rows. That requires a fresh
    Match-V5 fetch per match and is a separate (expensive) operation —
    see scripts/backfill_position.py.
  * Touch any other table.

Idempotent: a second run sees the column is already present and exits
without writing.

Usage:
    python scripts/migrate_add_position.py path/to/database.sqlite
    python scripts/migrate_add_position.py --dry-run path/to/database.sqlite

Run against a synced local copy first to verify before doing it on prod.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def _column_names(con: sqlite3.Connection) -> tuple[str, ...]:
    cols = con.execute("PRAGMA table_info(match_stats)").fetchall()
    return tuple(name for _cid, name, *_rest in cols)


def _row_count(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) FROM match_stats").fetchone()[0]


def migrate(db_path: Path, dry_run: bool = False) -> int:
    print(f"[migrate] target: {db_path}")
    if not db_path.exists():
        print(f"[migrate] ERROR: file does not exist: {db_path}")
        return 1

    con = sqlite3.connect(db_path)
    try:
        # Ensure WAL writers have flushed before we rewrite the table.
        con.execute("PRAGMA wal_checkpoint(FULL)")

        columns = _column_names(con)
        before = _row_count(con)
        print(f"[migrate] current columns on match_stats: {columns}")
        print(f"[migrate] {before:,} rows currently in match_stats")

        if "position" in columns:
            print("[migrate] position column already present — nothing to do")
            return 0

        if dry_run:
            print("[migrate] DRY RUN — would ADD COLUMN position TEXT to match_stats")
            print(f"[migrate] DRY RUN — expected post-migration rows: {before:,}")
            return 0

        backup_path = db_path.with_name(
            f"{db_path.stem}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}{db_path.suffix}"
        )
        print(f"[migrate] creating backup: {backup_path}")
        shutil.copy2(db_path, backup_path)

        print("[migrate] adding position column…")
        con.execute("BEGIN")
        con.execute("ALTER TABLE match_stats ADD COLUMN position TEXT")
        con.commit()

        after = _row_count(con)
        new_columns = _column_names(con)
        print(f"[migrate] new columns: {new_columns}")
        print(f"[migrate] post-migration row count: {after:,}")
        if after != before:
            print(
                f"[migrate] ERROR: row count changed ({before} -> {after}); backup at {backup_path}"
            )
            return 3
        if "position" not in new_columns:
            print(f"[migrate] ERROR: position not added; backup at {backup_path}")
            return 4

        print("[migrate] done.")
        print("[migrate] Existing rows have NULL position. New inserts via the")
        print("[migrate] backfill cog will populate it from participant['teamPosition'].")
        print("[migrate] Backfill historical rows with scripts/backfill_position.py.")
        return 0
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "db_path",
        type=Path,
        nargs="?",
        default=Path("Bot/db/database.sqlite"),
        help="path to database.sqlite (default: Bot/db/database.sqlite)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="diagnose current columns + row count, don't modify the DB",
    )
    args = parser.parse_args()
    return migrate(args.db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
