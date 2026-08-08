"""Backfill match_stats' detail columns from archived match_raw payloads.

2026-08 added rich per-participant columns to match_stats (damage, gold,
CS, vision, pings, multikills, time dead, objectives, challenges-derived
KP / team-damage share — see DETAIL_COLUMNS in Bot/utils/match_detail.py). The
ingest stream populates them going forward; rows inserted before then are
NULL. Because every fetched payload is archived verbatim in match_raw,
this backfill is a single SQL UPDATE against the JSONB — zero Riot API
spend (this is exactly why match_raw exists).

What this script does:
  1. Count match_stats rows still missing detail (damage_to_champs IS
     NULL), split into "payload archived" (fixable here) and "no payload"
     (fixable only by /backfill_all all_history=True, which re-fetches
     and now heals detail columns on conflict).
  2. Unless --dry-run: run the UPDATE, joining each row to its
     participant object by puuid inside the payload's participants array.
  3. Report how many rows were updated.

Idempotent: the UPDATE only touches rows where damage_to_champs IS NULL,
and never overwrites the core columns. Rows whose payloads predate Riot's
challenges sub-object simply keep NULL in the challenges-derived columns.

Prefer the Discord-native equivalent when the bot is up: the
``/backfill_detail_from_raw`` command runs the exact same SQL (both
import it from Bot/utils/match_detail.py). This script exists for shell
use / bot-down situations.

Usage (inside container 103, where .env provides DATABASE_URL):
    python3 scripts/backfill_match_detail_from_raw.py --dry-run
    python3 scripts/backfill_match_detail_from_raw.py

Read-heavy but API-free; safe to run while the bot is up — the live
stream writes complete rows anyway, and the NULL guard means the two
can't fight over a value.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Make Bot.* importable when running from the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "Bot"))

from utils import db  # noqa: E402
from utils.match_detail import RAW_BACKFILL_COUNT_SQL, RAW_BACKFILL_UPDATE_SQL  # noqa: E402

_COUNT_SQL = RAW_BACKFILL_COUNT_SQL
_UPDATE_SQL = RAW_BACKFILL_UPDATE_SQL


async def run(dry_run: bool) -> int:
    try:
        row = await db.fetchone(_COUNT_SQL)
        fixable, missing_payload = row if row else (0, 0)
        print(f"[backfill] rows missing detail with an archived payload: {fixable:,}")
        print(f"[backfill] rows missing detail with NO archived payload: {missing_payload:,}")
        if missing_payload:
            print(
                "[backfill]   -> those need /backfill_all all_history=True (re-fetches "
                "through the bot's shared rate limiter, then heals detail on conflict)"
            )
        if dry_run:
            print("[backfill] DRY RUN — no writes performed")
            return 0
        if not fixable:
            print("[backfill] nothing to do")
            return 0

        async with db.connection() as conn:
            cur = await conn.execute(_UPDATE_SQL)
            print(f"[backfill] updated {cur.rowcount:,} match_stats rows")

        row = await db.fetchone(_COUNT_SQL)
        remaining = row[0] if row else 0
        print(f"[backfill] rows with payload still missing detail after run: {remaining:,}")
        return 0
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count what would be backfilled, write nothing",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
