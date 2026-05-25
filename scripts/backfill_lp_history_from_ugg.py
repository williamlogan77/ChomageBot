"""Backfill league_history for the Dec 2024 -> Jan 2026 bot-outage gap.

Data source: U.GG profile pages (Apollo state -> `getHistoricRanks` query).
U.GG exposes per-split final tier/division/LP for each tracked Riot account.
This is REAL, factual data — not interpolation. Output is sparse (a handful of
anchor rows per player covering the gap), which is the truthful ceiling on
what is recoverable for the period.

Schema target (league_history actual columns, not setup.sql):
  puuid TEXT  -- 78-char puuid (matches what the live bot writes post-gap)
  timestamp DATETIME
  lp INTEGER
  division TEXT (I, II, III, IV)
  tier TEXT (IRON..CHALLENGER)
  wins INTEGER  -- NULL on backfilled rows: see note below.
  losses INTEGER -- NULL on backfilled rows: see note below.

The U.GG `season` integer maps to Riot's split numbering:
  22  S14 Split 1   ended 2024-05-14
  23  S14 Split 2   ended 2024-09-25
  24  S14 Split 3   ended 2025-01-09  <-- in gap
  25  S15 Split 1   ended 2025-06-04  <-- in gap
  26  S15 Split 2   ended 2025-09-17  <-- in gap
We confirmed mapping #23 by matching one player's recorded EMERALD IV 23 LP
on 2024-09-06 (the last pre-gap poll for 8BlitZ-Keith) to the U.GG season=23
final value for the same account.

Per-row wins/losses are inserted as NULL. The bot stores cumulative
SEASON wins/losses (the value Riot's league-v4 returns); recomputing this
from match_stats would require knowing season boundaries AND requires
that match_stats covers every ranked game, which it doesn't for the
players whose match-history we backfilled later. Rather than write a
plausible-but-wrong number we leave the column NULL. Downstream consumers
in `Bot/utils/match_analysis.compute_lp_events` already drop rows where
the W/L delta isn't exactly 1 game, and gap straddlers are filtered by
the >2h-since-prev-snapshot guard at line 670, so NULLs don't poison the
analysis.

Usage (inside container 103):
  python3 backfill_lp_history_from_ugg.py --apply
Without --apply it does a dry run and prints the rows that WOULD be inserted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DB = "/root/ChomageBot/Bot/db/database.sqlite"
PAGE_CACHE = "/tmp/sources/ugg"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HDR = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
RATE_LIMIT_SEC = 1.5

# U.GG `season` integer -> split end date (UTC).
# Anchor 23 (= 2024-09-25) verified against tracked player 8BlitZ's last
# pre-gap recorded rank (EMERALD IV 23 LP on 2024-09-06).
SPLIT_END: dict[int, dt.datetime] = {
    22: dt.datetime(2024, 5, 14, 12, 0, 0),
    23: dt.datetime(2024, 9, 25, 12, 0, 0),
    24: dt.datetime(2025, 1, 9, 12, 0, 0),
    25: dt.datetime(2025, 6, 4, 12, 0, 0),
    26: dt.datetime(2025, 9, 17, 12, 0, 0),
}

# Only insert anchors that fall WITHIN the bot-outage gap.
GAP_START = dt.datetime(2024, 11, 30)
GAP_END = dt.datetime(2026, 1, 5)


def fetch_ugg_page(name: str, tag: str) -> str | None:
    """Return cached U.GG profile HTML, fetching if not cached."""
    slug = f"{name}-{tag}"
    path = f"{PAGE_CACHE}/{slug}.html"
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        with open(path, encoding="utf-8") as f:
            body = f.read()
        # 404 pages are still cached (~155KB shells). Differentiate.
        if "getHistoricRanks" not in body:
            return None
        return body

    os.makedirs(PAGE_CACHE, exist_ok=True)
    url = "https://u.gg/lol/profile/euw1/" f"{urllib.parse.quote(slug)}/overview"
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=30)
        body = r.read().decode("utf-8", errors="replace")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        time.sleep(RATE_LIMIT_SEC)
        return body if "getHistoricRanks" in body else None
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"  ugg fetch {slug}: HTTP {e.code}\n")
        return None
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"  ugg fetch {slug}: {e}\n")
        return None


def extract_apollo_state(body: str) -> dict | None:
    m = re.search(r"window\.__APOLLO_STATE__\s*=\s*", body)
    if not m:
        return None
    start = m.end()
    depth = 0
    i = start
    while i < len(body):
        c = body[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(body[start : i + 1])
                except json.JSONDecodeError:
                    return None
        elif c == '"':
            i += 1
            while i < len(body) and body[i] != '"':
                if body[i] == "\\":
                    i += 1
                i += 1
        i += 1
    return None


def find_historic_ranks(state: dict) -> list[dict]:
    """Pull the `getHistoricRanks` array out of the Apollo ROOT_QUERY entries.

    The key includes the query args inline (`getHistoricRanks({...})`), so we
    match by prefix rather than constructing the exact name.
    """
    root = state.get("ROOT_QUERY", {})
    for k, v in root.items():
        if k.startswith("getHistoricRanks(") and isinstance(v, list):
            return v
    return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Actually write rows. Without this flag, dry-run only.",
    )
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    players = con.execute(
        "SELECT league_username, tag, puuid FROM league_players WHERE puuid IS NOT NULL"
    ).fetchall()

    total_inserted = 0
    total_skipped_existing = 0
    total_no_data = 0
    print(f"{'name':25s} {'split':>5s}  {'date':10s}  {'tier':9s} {'div':3s}  {'lp':3s}  status")

    for p in players:
        name, tag, puuid = p["league_username"], p["tag"], p["puuid"]
        body = fetch_ugg_page(name, tag)
        if body is None:
            total_no_data += 1
            print(f"{name:25s}  --   ---         (no u.gg profile)")
            continue
        state = extract_apollo_state(body)
        if state is None:
            total_no_data += 1
            print(f"{name:25s}  --   ---         (apollo parse failed)")
            continue
        ranks = find_historic_ranks(state)
        anchors_for_player = 0
        for r in sorted(ranks, key=lambda x: x.get("season", 0)):
            season_id = r.get("season")
            end_ts = SPLIT_END.get(season_id)
            if end_ts is None:
                continue
            if not (GAP_START <= end_ts <= GAP_END):
                continue
            tier = r.get("tier")
            division = r.get("rank")
            lp = r.get("lp", 0)
            if not (tier and division is not None and lp is not None):
                continue
            ts_str = end_ts.strftime("%Y-%m-%d %H:%M:%S")

            # Idempotent: skip if a row already exists for this puuid+timestamp.
            already = con.execute(
                "SELECT 1 FROM league_history WHERE puuid=? AND timestamp=?",
                (puuid, ts_str),
            ).fetchone()
            if already:
                total_skipped_existing += 1
                status = "exists"
            elif args.apply:
                con.execute(
                    """
                    INSERT INTO league_history (puuid, timestamp, lp, division, tier, wins, losses)
                    VALUES (?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (puuid, ts_str, lp, division, tier),
                )
                total_inserted += 1
                anchors_for_player += 1
                status = "INSERTED"
            else:
                total_inserted += 1
                anchors_for_player += 1
                status = "would-insert"
            print(
                f"{name:25s} {season_id:>5}  {ts_str[:10]}  {tier:9s} {division:3s}  {lp:3}  {status}"
            )

        if anchors_for_player == 0:
            print(f"{name:25s}  --   ---         (no in-gap splits in u.gg history)")

    if args.apply:
        con.commit()
    con.close()

    print()
    print(f"Total rows inserted: {total_inserted}")
    print(f"Total existing rows skipped: {total_skipped_existing}")
    print(f"Players with no usable data: {total_no_data}")
    if not args.apply:
        print("DRY RUN — re-run with --apply to actually write.")


if __name__ == "__main__":
    main()
