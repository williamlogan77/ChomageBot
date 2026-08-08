"""Match-V5 ingestion primitives: ids walk -> fetch -> archive -> extract.

Single source of truth shared by the in-bot backfill cog
(cogs/backfill.py — stream loop, /backfill_all, /backfill_timelines) and
the standalone deep runner (scripts/backfill_deep_standalone.py, which
runs the same walk on its OWN API key so the bot's budget stays free).
Extracted from the cog verbatim in the 2026-08 capture-remainder pass —
behaviour, SQL and logging are unchanged; only the home moved. Lives in
utils so cogs can import it without utils ever importing cogs.

Everything here is idempotent and commits per row (one short statement
per write), so any number of walkers — the live stream, a Discord-invoked
backfill, the standalone runner — can overlap safely: (match_id, puuid)
is match_stats' PRIMARY KEY, raw archives are ON CONFLICT DO NOTHING, and
the conflict path only fills detail columns that are still NULL.

Rate budget: all Riot calls go through utils.riot_client, i.e. whatever
budget THIS process is configured for (the bot's shared limiter in-bot;
the standalone runner's own limits in that process).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from psycopg.types.json import Jsonb
from utils import db
from utils.match_detail import DETAIL_COLUMNS, participant_detail
from utils.riot_client import get_match, get_match_ids, get_match_timeline

log = logging.getLogger(__name__)

# Match-V5 caps a single page at 100 IDs. We use 100 everywhere and paginate
# for deeper history.
PAGE_SIZE = 100

# queue=None drops match-v5's queue filter: every LoL queue (solo, 5s,
# flex, ARAM, Arena, normals, customs) in ONE request — capture-everything
# at the same ids spend the old solo-only poll had. Downstream consumers
# filter on match_stats.queue_id, so extra queues can't leak into the
# boards/awards (they all pin 420/710 in SQL).
ALL_QUEUES: tuple[None, ...] = (None,)

# Column lists and placeholder counts derive from DETAIL_COLUMNS (shared
# single source in utils/match_detail.py) so the columns, VALUES arity and
# ON CONFLICT healing set can never drift apart. The conflict path fills
# only detail columns and only where the existing row has NULL — so a
# raw-archive heal re-fetch upgrades old rows in place, while rows that
# already carry values are never rewritten.
INSERT_MATCH_STATS_SQL = (
    "INSERT INTO match_stats "
    "(match_id, puuid, game_start, queue_id, champion, "
    " win, kills, deaths, assists, duration_sec, patch_version, "
    " position, team_id, " + ", ".join(DETAIL_COLUMNS) + ") "
    "VALUES (" + ", ".join(["%s"] * (13 + len(DETAIL_COLUMNS))) + ") "
    "ON CONFLICT (match_id, puuid) DO UPDATE SET "
    + ", ".join(f"{col} = COALESCE(match_stats.{col}, EXCLUDED.{col})" for col in DETAIL_COLUMNS)
)


def participant_position(participant: dict) -> str | None:
    """The role Riot says this participant actually played.

    Prefer ``teamPosition`` (Riot's role-classifier output: TOP / JUNGLE /
    MIDDLE / BOTTOM / UTILITY). It's empty "" on remakes and some very old
    matches, so fall back to ``individualPosition`` — same vocabulary, but
    it can read "Invalid". When neither yields a usable value we store
    NULL, and ``load_matches`` resolves the role to "UNKNOWN" (no
    champion-based guessing).

    The raw Riot string is stored verbatim; the MIDDLE->MID / BOTTOM->ADC /
    UTILITY->SUPPORT mapping to display roles happens at read time.
    """
    for key in ("teamPosition", "individualPosition"):
        value = participant.get(key)
        if isinstance(value, str):
            value = value.strip()
            if value and value.lower() != "invalid":
                return value
    return None


async def backfill_player(
    puuid: str,
    count: int,
    all_history: bool = False,
    name: str | None = None,
    queues: tuple[int | None, ...] = ALL_QUEUES,
) -> int:
    """Pull match IDs for the player and insert any not already stored.

    With ``all_history=False``, fetches a single page of up to ``count``
    IDs. With ``all_history=True``, paginates by incrementing ``start``
    until Riot returns a short page (signalling end of history).
    Returns the total number of newly inserted matches.

    ``name`` is purely for log readability; falls back to a truncated
    puuid when not given (the stream loop always passes it).

    ``queues`` defaults to ALL_QUEUES — a single unfiltered ids call
    per page covering every queue (capture-everything). A tuple of
    numeric queue ids still works for targeted pulls. Queue-710 rows
    keep feeding the 5s board's fallback standings exactly as before;
    they just arrive via the unfiltered call now.
    """
    inserted_total = 0
    label = name or f"{puuid[:8]}..."

    for queue in queues:
        start = 0
        page_num = 0
        while True:
            page_num += 1
            page_size = PAGE_SIZE if all_history else count
            ids = await get_match_ids(puuid, count=page_size, queue=queue, start=start)
            if ids is None:
                # Transient failure — NOT end-of-history. Retry once so
                # a blip can't silently truncate a deep walk; then stop
                # loudly. The walk is resumable: already-stored matches
                # pre-filter out, so a re-run continues from here for
                # the cost of the ids pages already walked.
                await asyncio.sleep(5.0)
                ids = await get_match_ids(puuid, count=page_size, queue=queue, start=start)
                if ids is None:
                    log.warning(
                        f"Backfill: {label} ids page failed twice "
                        f"(queue {queue}, start={start}) — stopping this "
                        "player's walk early; re-run to resume"
                    )
                    break
            if not ids:
                break

            # Skip a match only if BOTH extracts already exist:
            #   - a match_stats row FOR THIS puuid. Filtering by match_id
            #     alone would skip games where this puuid hasn't been
            #     backfilled yet but another tracked friend already has —
            #     exactly the "duo's second row" case we need to pick up.
            #   - a match_raw row (raw payloads are per match, not per
            #     puuid). Matches ingested before raw capture existed fail
            #     this leg and get re-fetched once, healing the archive.
            #     Steady state is unaffected: anything fetched after this
            #     shipped has both rows, so the 5-min stream stays cheap.
            placeholders = ",".join(["%s"] * len(ids))
            existing_rows = await db.fetchall(
                f"SELECT ms.match_id FROM match_stats ms "
                f"JOIN match_raw mr ON mr.match_id = ms.match_id "
                f"WHERE ms.puuid = %s AND ms.match_id IN ({placeholders})",
                (puuid, *ids),
            )
            existing = {row[0] for row in existing_rows}

            to_fetch = [mid for mid in ids if mid not in existing]
            if to_fetch:
                page_new = await insert_matches(puuid, to_fetch)
                inserted_total += page_new
                # Page-level log only when the page actually delivered new
                # rows. Steady-state stream calls stay quiet.
                if page_new > 0:
                    log.info(
                        f"Backfill: {label} queue {queue} page {page_num} "
                        f"(start={start}, +{page_new} new, total {inserted_total})"
                    )

            # Stop when Riot returned less than we asked for (end of
            # history) or when we've satisfied a bounded request.
            if len(ids) < page_size:
                break
            if not all_history:
                break
            start += len(ids)

    return inserted_total


async def insert_matches(puuid: str, match_ids: list[str]) -> int:
    """Fetch + insert match details one at a time, one short statement
    per write (raw payload archive, timeline archive, then the
    per-player stats row).

    Why per-match: bundling the whole page under one transaction
    would hold a pooled connection across N network calls
    (~50-500ms each). A one-shot ``db.execute`` per insert returns
    the connection to the pool between Riot fetches, so other
    writers interleave freely.

    Returns the number of matches fetched and written through. During
    a raw-archive heal pass the match_stats write only fills detail
    columns still NULL on the existing row, but the match still
    counts — the fetch genuinely happened.
    """
    inserted = 0
    for mid in match_ids:
        match = await get_match(mid)
        if match is None:
            continue
        # Archive the complete payload first, before any per-participant
        # parsing, so the raw JSON survives even if extraction below
        # ever trips on a malformed match. One row per MATCH: when a
        # second tracked player triggers a fetch of the same game the
        # conflict clause makes this a no-op.
        await db.execute(
            "INSERT INTO match_raw (match_id, payload) VALUES (%s, %s) "
            "ON CONFLICT (match_id) DO NOTHING",
            (mid, Jsonb(match)),
        )
        # Timeline: best-effort, one extra request per NEW match. A
        # duo's second-player pass re-enters here for a match whose
        # timeline already landed — the presence check keeps that from
        # burning a fetch. Failure (transient or 404) is logged and
        # skipped so core ingest never blocks; /backfill_timelines
        # heals whatever this misses.
        try:
            await archive_timeline(mid)
        except Exception as exc:
            log.warning(f"Timeline archive failed for {mid}: {exc!r}")
        # Per-match guard: the all-queue, full-depth walk can surface
        # modes/vintages this extraction never anticipated. A single
        # malformed match must cost exactly that match — its raw
        # payload is already archived above, so the row is recoverable
        # from SQL later — never the rest of the page.
        try:
            inserted += await insert_participant_row(mid, puuid, match)
        except Exception as exc:
            log.error(
                f"Stats extraction failed for {mid}: {exc!r} — "
                "raw payload archived, stats row skipped"
            )
    return inserted


async def insert_participant_row(mid: str, puuid: str, match: dict) -> int:
    """Extract + insert this player's match_stats row. Returns rows written.

    Tolerates old/odd payload vintages discovered by the full-depth
    walk: ``gameStartTimestamp`` only exists from patch 11.20, so fall
    back to ``gameCreation`` (always present); pre-11.20 payloads also
    report ``gameDuration`` in milliseconds — Riot's documented tell is
    the absence of ``gameEndTimestamp``.
    """
    info = match["info"]
    for participant in info.get("participants") or []:
        if participant.get("puuid") != puuid:
            continue
        # game_start is TIMESTAMPTZ — insert a tz-aware datetime
        # (Riot's timestamps are epoch millis, i.e. UTC).
        start_ms = info.get("gameStartTimestamp") or info["gameCreation"]
        game_start = dt.datetime.fromtimestamp(start_ms / 1000.0, tz=dt.UTC)
        duration = info.get("gameDuration") or 0
        if "gameEndTimestamp" not in info:
            duration //= 1000
        await db.execute(
            INSERT_MATCH_STATS_SQL,
            (
                mid,
                puuid,
                game_start,
                info["queueId"],
                participant["championName"],
                1 if participant.get("win") else 0,
                participant["kills"],
                participant["deaths"],
                participant["assists"],
                duration,
                info.get("gameVersion"),
                participant_position(participant),
                participant.get("teamId"),
                *participant_detail(participant),
            ),
        )
        return 1
    return 0


async def missing_timeline_ids() -> list[str]:
    """Stored matches with no timeline yet, newest first (recent games
    are the interesting ones if a bounded run stops early)."""
    rows = await db.fetchall(
        "SELECT ms.match_id FROM "
        "(SELECT match_id, MAX(game_start) AS newest FROM match_stats GROUP BY match_id) ms "
        "LEFT JOIN match_timeline_raw mt ON mt.match_id = ms.match_id "
        "WHERE mt.match_id IS NULL "
        "ORDER BY ms.newest DESC"
    )
    return [row[0] for row in rows]


async def archive_timeline(match_id: str) -> bool:
    """Fetch + archive the match timeline unless already stored.

    Returns True when a new timeline row landed. None from the client
    (transient failure or 404 — timelines age out of Riot's window
    before match details do) is a quiet skip.
    """
    row = await db.fetchone("SELECT 1 FROM match_timeline_raw WHERE match_id = %s", (match_id,))
    if row is not None:
        return False
    timeline = await get_match_timeline(match_id)
    if timeline is None:
        return False
    await db.execute(
        "INSERT INTO match_timeline_raw (match_id, payload) VALUES (%s, %s) "
        "ON CONFLICT (match_id) DO NOTHING",
        (match_id, Jsonb(timeline)),
    )
    return True
