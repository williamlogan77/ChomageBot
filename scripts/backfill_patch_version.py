"""Backfill ``match_stats.patch_version`` from Match-V5 for historical rows.

Iter 72 added a nullable ``patch_version`` column to ``match_stats`` and
``stream_matches`` started populating it from ``info.gameVersion`` going
forward. Existing rows (~6500 at the time of writing) are NULL. This
script re-fetches Match-V5 for each distinct match_id with a NULL
``patch_version`` and writes the value back.

Idempotent: a re-run only processes rows that are still NULL, so an
interrupted run resumes cleanly. The UPDATE also re-asserts
``patch_version IS NULL`` to avoid clobbering a value that ``stream_matches``
may have written between SELECT and UPDATE.

Rate limiting: uses ``Bot.utils.riot_client`` so the dev-tier global
budget (20 req/s, 100 req/2min) is honoured by the same limiter the live
bot uses. At ~0.83 req/s sustained that's roughly **130 minutes** for
6500 distinct match_ids — plan accordingly.

Usage:
    python scripts/backfill_patch_version.py [path/to/database.sqlite] [--dry-run] [--limit N]

    --dry-run   Count + sample 5 IDs, no API calls, no DB writes.
    --limit N   Stop after N successful updates (handy for incremental runs).

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


async def _fetch_game_version(match_id: str) -> tuple[int, str | None]:
    """Fetch Match-V5 and return ``(status, gameVersion or None)``.

    Status is the HTTP status (0 on network failure). gameVersion is the
    raw ``info.gameVersion`` string from the response (e.g. "14.18.612.4234"),
    or None on any failure / missing field.
    """
    url = f"{REGION_HOST}/lol/match/v5/matches/{match_id}"
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, dict):
        return status, None
    info = body.get("info")
    if not isinstance(info, dict):
        return status, None
    game_version = info.get("gameVersion")
    return status, game_version if isinstance(game_version, str) else None


def _queue_null_match_ids(con: sqlite3.Connection) -> list[str]:
    rows = con.execute(
        "SELECT DISTINCT match_id FROM match_stats "
        "WHERE patch_version IS NULL "
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
        print(f"[backfill] {total:,} distinct match_ids with NULL patch_version")
        if limit is not None:
            print(f"[backfill] --limit {limit} (will stop after {limit} successful updates)")

        # 100 req / 2min => ~0.83 req/s sustained.
        eta_minutes = (total * 120) / 100 / 60
        print(
            f"[backfill] dev-tier limit ~0.83 req/s sustained => "
            f"ETA ~{eta_minutes:.0f} minutes for {total:,} matches"
        )
        print("[backfill] using Bot.utils.riot_client (shared rate limiter)")

        succeeded = 0
        not_found = 0  # 404 — Match-V5 prunes matches older than ~2 years
        failed = 0  # other non-200 / network errors

        for idx, match_id in enumerate(match_ids, start=1):
            status, game_version = await _fetch_game_version(match_id)
            if status == 200 and game_version:
                # Re-assert patch_version IS NULL so we never clobber a value
                # stream_matches may have written between SELECT and UPDATE.
                cur = con.execute(
                    "UPDATE match_stats SET patch_version = ? "
                    "WHERE match_id = ? AND patch_version IS NULL",
                    (game_version, match_id),
                )
                con.commit()
                succeeded += 1
                print(
                    f"[{idx}/{total}] {match_id} -> {game_version} "
                    f"({cur.rowcount} row{'s' if cur.rowcount != 1 else ''})"
                )
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
        print(f"  succeeded: {succeeded}")
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
        help="stop after N successful updates (for incremental runs)",
    )
    args = parser.parse_args()

    if args.dry_run:
        return _dry_run(args.db_path)
    return asyncio.run(backfill(args.db_path, limit=args.limit))


if __name__ == "__main__":
    sys.exit(main())
