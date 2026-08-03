"""Auto-detected season/split boundaries, persisted in the seasons table.

Within a split a player's league-entries wins+losses total only ever
grows, so a tracked account whose games total shrinks between two
consecutive league_history snapshots crossed a ladder reset. A single
account can shrink for other reasons (region transfer, API quirk);
awards.RESET_MIN_ACCOUNTS distinct accounts shrinking within one
CLUSTER_WINDOW marks a real reset. Validated against 2023-2026 history:
real resets (Jan/May/Sept 2024, Jan 2026) cluster at 4-8 accounts over
up to four weeks as players trickle back into placements, noise never
exceeded one account — and years where Riot left W/L intact across a
split (2025) correctly produce no boundary, because nothing was wiped.

The recorded started_at is the first post-reset snapshot observed —
Riot's actual reset moment is a little earlier, bounded by how fast the
first player re-placed. That's exact enough for "current ladder only"
filters: between the real reset and the first re-placement no snapshots
exist at all (players have no ranked entry until placements finish).

sync_seasons() is idempotent and cheap. It runs at boot (backfilling
every historical boundary on first deploy) and again whenever the solo
board observes a games-total shrink on a live account
(cogs/league_table_updater.update_table).
"""

from __future__ import annotations

import datetime as dt
import logging

from utils import db
from utils.awards import RESET_MIN_ACCOUNTS

log = logging.getLogger(__name__)

# Shrink events this close together belong to the same reset — players
# trickle back into placements over weeks (Sept 2024 spanned four).
# Real split gaps are months apart, so clusters can't run together.
CLUSTER_WINDOW = dt.timedelta(days=21)

# A derived boundary this close to an existing seasons row is the same
# reset re-derived (its cluster's first event can't move once seen, but
# belt-and-braces against clock/ordering edge cases), not a new season.
DEDUPE_WINDOW = dt.timedelta(days=30)


async def _shrink_events() -> list[tuple]:
    """(timestamp, puuid) of every consecutive-snapshot games shrink."""
    return await db.fetchall(
        """WITH ordered AS (
            SELECT puuid, timestamp, wins + losses AS games,
                   LAG(wins + losses) OVER (
                       PARTITION BY puuid ORDER BY timestamp, id
                   ) AS prev_games
            FROM league_history
            WHERE queue = 'RANKED_SOLO_5x5'
              AND wins IS NOT NULL AND losses IS NOT NULL
        )
        SELECT timestamp, puuid FROM ordered
        WHERE prev_games IS NOT NULL AND games < prev_games
        ORDER BY timestamp"""
    )


def cluster_boundaries(events: list[tuple]) -> list[dt.datetime]:
    """First-event timestamp of each cluster with enough distinct accounts.

    Pure: consecutive events gapped <= CLUSTER_WINDOW share a cluster;
    a cluster of at least RESET_MIN_ACCOUNTS distinct accounts is a real
    reset and contributes its earliest timestamp as the boundary.
    """
    boundaries: list[dt.datetime] = []
    cluster_start: dt.datetime | None = None
    last_seen: dt.datetime | None = None
    accounts: set[str] = set()

    def flush() -> None:
        if cluster_start is not None and len(accounts) >= RESET_MIN_ACCOUNTS:
            boundaries.append(cluster_start)

    for timestamp, puuid in events:
        if last_seen is None or timestamp - last_seen > CLUSTER_WINDOW:
            flush()
            cluster_start = timestamp
            accounts = set()
        accounts.add(puuid)
        last_seen = timestamp
    flush()
    return boundaries


async def sync_seasons() -> int:
    """Derive boundaries and insert any not yet recorded. Returns count."""
    boundaries = cluster_boundaries(await _shrink_events())
    existing = [row[0] for row in await db.fetchall("SELECT started_at FROM seasons")]
    inserted = 0
    for boundary in boundaries:
        if any(abs(boundary - seen) <= DEDUPE_WINDOW for seen in existing):
            continue
        await db.execute(
            "INSERT INTO seasons (started_at) VALUES (%s) ON CONFLICT DO NOTHING",
            (boundary,),
        )
        inserted += 1
        log.info(f"Season boundary recorded: ladder reset detected around {boundary:%Y-%m-%d}")
    return inserted


async def current_season_start() -> dt.datetime | None:
    """started_at of the latest detected season, or None before any."""
    row = await db.fetchone("SELECT MAX(started_at) FROM seasons")
    return row[0] if row is not None else None
