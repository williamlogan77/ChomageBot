"""Backfill ``match_stats.position`` from Match-V5 for historical rows.

We added a nullable ``position`` column to ``match_stats`` and the
backfill cog started populating it from each participant's
``teamPosition`` going forward. Existing rows are NULL. This script
re-fetches Match-V5 for each distinct match_id that still has a NULL
position on any of its rows and writes the played position back per
``(match_id, puuid)``.

Unlike the patch_version backfill, position is **per-participant**: one
match fetch yields a different position for each tracked puuid in that
match. We build a puuid -> position map from ``info.participants`` and
UPDATE every NULL row for that match in a single pass.

Idempotent: a re-run only processes match_ids that still have a NULL
position, and the UPDATE re-asserts ``position IS NULL`` so it never
clobbers a value the live ``stream_matches`` loop wrote between SELECT
and UPDATE.

Rows that resolve to an empty/Invalid Riot position are written as the
empty string "" (not left NULL) so a re-run doesn't keep re-fetching a
match that genuinely has no position (remakes). load_matches treats ""
the same as NULL — it falls back to the CHAMPION_ROLES heuristic.

Rate limiting: uses ``Bot.utils.riot_client`` so the dev-tier global
budget (20 req/s, 100 req/2min) is honoured by the same limiter the live
bot uses — roughly 0.83 req/s sustained.

Usage:
    python scripts/backfill_position.py [path/to/database.sqlite] [--dry-run] [--limit N]

    --dry-run   Count + sample 5 IDs, no API calls, no DB writes.
    --limit N   Stop after N matches successfully processed.

Requires ``riot_key`` env var (loaded from .env at the project root, same
as Bot/main.py).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

# Make Bot.utils importable when running from project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "Bot"))

from dotenv import load_dotenv  # noqa: E402

# .env lives at the project root (same convention as Bot/main.py).
load_dotenv(_PROJECT_ROOT / ".env")

from utils.riot_client import REGION_HOST, _get_json  # noqa: E402


def _participant_position(participant: dict) -> str:
    """Riot's recorded played position, or "" when none is usable.

    Mirrors ``cogs.backfill._participant_position`` (kept in sync by hand
    — this one-shot script avoids importing the discord-dependent cog).
    Returns "" rather than None so the backfill can mark genuinely
    position-less matches (remakes) as processed.
    """
    for key in ("teamPosition", "individualPosition"):
        value = participant.get(key)
        if isinstance(value, str):
            value = value.strip()
            if value and value.lower() != "invalid":
                return value
    return ""


async def _fetch_positions(match_id: str) -> tuple[int, dict[str, str] | None]:
    """Fetch Match-V5 and return ``(status, {puuid: position})``.

    Status is the HTTP status (0 on network failure). The dict maps every
    participant's puuid to its position string ("" if none usable), or
    None on any failure / malformed body.
    """
    url = f"{REGION_HOST}/lol/match/v5/matches/{match_id}"
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, dict):
        return status, None
    info = body.get("info")
    if not isinstance(info, dict):
        return status, None
    participants = info.get("participants")
    if not isinstance(participants, list):
        return status, None
    positions: dict[str, str] = {}
    for p in participants:
        if isinstance(p, dict) and isinstance(p.get("puuid"), str):
            positions[p["puuid"]] = _participant_position(p)
    return status, positions


def _queue_null_match_ids(con: sqlite3.Connection) -> list[str]:
    rows = con.execute(
        "SELECT DISTINCT match_id FROM match_stats "
        "WHERE position IS NULL "
        "ORDER BY match_id"
    ).fetchall()
    return [r[0] for r in rows]


async def backfill(db_path: Path, limit: int | None) -> int:
    if not db_path.exists():
        print(f"[backfill] ERROR: file does not exist: {db_path}")
        return 1

    if not os.environ.get("riot_key"):
        print("[backfill] ERROR: riot_key env var not set (looked in .env at project root)")
        return 2

    con = sqlite3.connect(db_path)
    try:
        match_ids = _queue_null_match_ids(con)
        total = len(match_ids)
        print(f"[backfill] {total:,} distinct match_ids with NULL position")
        if limit is not None:
            print(f"[backfill] --limit {limit} (will stop after {limit} matches processed)")

        # 100 req / 2min => ~0.83 req/s sustained.
        eta_minutes = (total * 120) / 100 / 60
        print(
            f"[backfill] dev-tier limit ~0.83 req/s sustained => "
            f"ETA ~{eta_minutes:.0f} minutes for {total:,} matches"
        )
        print("[backfill] using Bot.utils.riot_client (shared rate limiter)")

        succeeded = 0  # matches with at least one row updated
        rows_updated = 0
        not_found = 0  # 404 — Match-V5 prunes matches older than ~2 years
        failed = 0  # other non-200 / network errors

        for idx, match_id in enumerate(match_ids, start=1):
            status, positions = await _fetch_positions(match_id)
            if status == 200 and positions is not None:
                match_rows = 0
                for puuid, position in positions.items():
                    # Re-assert position IS NULL so we never clobber a value
                    # stream_matches may have written between SELECT and UPDATE.
                    cur = con.execute(
                        "UPDATE match_stats SET position = ? "
                        "WHERE match_id = ? AND puuid = ? AND position IS NULL",
                        (position, match_id, puuid),
                    )
                    match_rows += cur.rowcount
                con.commit()
                succeeded += 1
                rows_updated += match_rows
                print(f"[{idx}/{total}] {match_id} -> {match_rows} row(s) updated")
                if limit is not None and succeeded >= limit:
                    print(f"[backfill] --limit reached ({limit}); stopping.")
                    break
            elif status == 404:
                not_found += 1
                print(f"[{idx}/{total}] {match_id} -> 404 (expired, skipped)")
            else:
                failed += 1
                print(f"[{idx}/{total}] {match_id} -> status={status} (failed)")

        print("[backfill] summary:")
        print(f"  matches processed: {succeeded} ({rows_updated} rows updated)")
        print(f"  not_found (404 expired): {not_found}")
        print(f"  failed (other): {failed}")
        remaining = total - succeeded - not_found - failed
        if remaining > 0:
            print(f"  not_processed (limit/abort): {remaining}")
        return 0
    finally:
        con.close()


def _dry_run(db_path: Path) -> int:
    if not db_path.exists():
        print(f"[backfill] ERROR: file does not exist: {db_path}")
        return 1
    con = sqlite3.connect(db_path)
    try:
        match_ids = _queue_null_match_ids(con)
        total = len(match_ids)
        print(f"[backfill] DRY RUN — {total:,} distinct match_ids would be fetched")
        for sample in match_ids[:5]:
            print(f"  sample: {sample}")
        eta_minutes = (total * 120) / 100 / 60
        print(f"[backfill] DRY RUN — estimated wall time: ~{eta_minutes:.0f} minutes")
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
        help="count NULL rows + show 5 sample IDs, no API calls",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="stop after N matches processed (for incremental runs)",
    )
    args = parser.parse_args()

    if args.dry_run:
        return _dry_run(args.db_path)
    return asyncio.run(backfill(args.db_path, limit=args.limit))


if __name__ == "__main__":
    sys.exit(main())
