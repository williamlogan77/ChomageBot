"""Migrate match_stats from PRIMARY KEY (match_id) to PRIMARY KEY (match_id, puuid).

Why: under the single-column key, the backfill's INSERT OR IGNORE silently
drops the SECOND tracked player's row whenever two friends share a game.
That breaks duo and head-to-head detection. With a composite key, both
players' rows can coexist, and a re-backfill picks up everything that was
silently dropped.

What this script does, in one transaction:
  1. Take a timestamped backup of the .sqlite file next to the original.
  2. Detect the current PK on match_stats. If already composite, exit.
  3. CREATE TABLE match_stats_new with PRIMARY KEY (match_id, puuid).
  4. INSERT INTO match_stats_new SELECT * FROM match_stats.
  5. DROP TABLE match_stats; ALTER TABLE match_stats_new RENAME.
  6. Recreate the (puuid, game_start DESC) index.
  7. Verify the row count is unchanged.

What it does NOT do:
  * Touch any other table.
  * Re-populate the rows that were silently dropped during prior
    backfills — those rows never made it into the DB and have to be
    re-fetched via /backfill_all all_history=True after the migration.

Idempotent: a second run sees the composite PK is already in place and
exits without writing.

Usage:
    python scripts/migrate_match_stats_composite_pk.py path/to/database.sqlite
    python scripts/migrate_match_stats_composite_pk.py --dry-run path/to/database.sqlite

Run against a synced local copy first to verify before doing it on prod.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def _detect_pk_columns(con: sqlite3.Connection) -> tuple[str, ...]:
    """Return the PK column names (in PK-position order) for match_stats."""
    cols = con.execute("PRAGMA table_info(match_stats)").fetchall()
    return tuple(name for _cid, name, _typ, _nn, _df, pk in sorted(cols, key=lambda r: r[5]) if pk)


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

        current_pk = _detect_pk_columns(con)
        before = _row_count(con)
        print(f"[migrate] current PK on match_stats: {current_pk}")
        print(f"[migrate] {before:,} rows currently in match_stats")

        if current_pk == ("match_id", "puuid"):
            print("[migrate] composite PK already in place — nothing to do")
            return 0
        if current_pk != ("match_id",):
            print(f"[migrate] ERROR: unexpected PK {current_pk!r}; aborting")
            return 2

        if dry_run:
            print(
                "[migrate] DRY RUN — would rewrite match_stats with composite PK (match_id, puuid)"
            )
            print(f"[migrate] DRY RUN — expected post-migration rows: {before:,}")
            return 0

        backup_path = db_path.with_name(
            f"{db_path.stem}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}{db_path.suffix}"
        )
        print(f"[migrate] creating backup: {backup_path}")
        # Close any WAL state on the backup snapshot too.
        shutil.copy2(db_path, backup_path)

        print("[migrate] rewriting match_stats with composite PK…")
        con.execute("BEGIN")
        con.execute(
            """
            CREATE TABLE match_stats_new (
                match_id      TEXT     NOT NULL,
                puuid         TEXT     NOT NULL,
                game_start    DATETIME NOT NULL,
                queue_id      INTEGER  NOT NULL,
                champion      TEXT     NOT NULL,
                win           INTEGER  NOT NULL,
                kills         INTEGER  NOT NULL,
                deaths        INTEGER  NOT NULL,
                assists       INTEGER  NOT NULL,
                duration_sec  INTEGER  NOT NULL,
                PRIMARY KEY (match_id, puuid)
            )
            """
        )
        con.execute(
            """
            INSERT INTO match_stats_new (
                match_id, puuid, game_start, queue_id, champion,
                win, kills, deaths, assists, duration_sec
            )
            SELECT
                match_id, puuid, game_start, queue_id, champion,
                win, kills, deaths, assists, duration_sec
            FROM match_stats
            """
        )
        # Old index is bound to the old table; drop and recreate against the new one.
        con.execute("DROP INDEX IF EXISTS idx_match_stats_puuid_time")
        con.execute("DROP TABLE match_stats")
        con.execute("ALTER TABLE match_stats_new RENAME TO match_stats")
        con.execute(
            "CREATE INDEX idx_match_stats_puuid_time ON match_stats (puuid, game_start DESC)"
        )
        con.commit()

        after = _row_count(con)
        new_pk = _detect_pk_columns(con)
        print(f"[migrate] new PK: {new_pk}")
        print(f"[migrate] post-migration row count: {after:,}")
        if after != before:
            print(
                f"[migrate] ERROR: row count changed ({before} -> {after}); backup at {backup_path}"
            )
            return 3

        print("[migrate] done.")
        print("[migrate] Next step: re-run /backfill_all all_history=True so the rows that")
        print("[migrate] were silently dropped on prior backfills get re-fetched.")
        return 0
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("db_path", type=Path, help="path to database.sqlite")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="diagnose current PK + row count, don't modify the DB",
    )
    args = parser.parse_args()
    return migrate(args.db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
