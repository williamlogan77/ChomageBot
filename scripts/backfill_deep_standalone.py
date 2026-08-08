"""Full-depth Riot backfill as a standalone process on its OWN API key.

Runs the same all-queue, end-of-retention match walk + timeline heal as
the in-bot /backfill_all all_history=True + /backfill_timelines commands,
but OUTSIDE the bot process — so it spends a SEPARATE key's budget and
the bot's prod key stays free for live polling (boards, spectator,
stream). The ingestion code paths are shared, not copied: this script
imports utils.match_ingest (ids walk, match/timeline fetch + archive,
stats extraction) and utils.riot_client, exactly what the cog uses, so
inserts are byte-identical and idempotent — (match_id, puuid) PK,
ON CONFLICT DO NOTHING raw archives, NULL-only detail healing.

Separate process = separate rate limiter instance; on startup this
replaces the module's budget with ~90% of a personal dev key (18/1s,
90/120s) so a transient clock skew or Riot-side accounting difference
never 429-loops the run.

Safe to run while the bot is live: every write commits per row (same
one-shot statements as the in-bot path), and the two processes can walk
the same player concurrently without conflict — whoever fetches a match
second hits the pre-filter or the conflict clause.

Resume: progress checkpoints into bot_config under keys DISTINCT from
the in-bot backfill's marker, so the two never clash. The real data
checkpoint is the tables themselves (already-stored matches pre-filter
out); the marker just records which players' walks completed and which
phase was running, so a rerun skips straight to the remaining work.
Ctrl-C / container restart mid-run loses nothing — rerun to resume. The
marker clears on completion; delete the bot_config row manually to force
a from-scratch re-walk (harmless — it only re-reads ids pages).

Usage (inside the bot container, where DATABASE_URL is already set;
riot_key comes from the environment — override it with the spare key):

    docker exec -e riot_key=<SPARE_DEV_KEY> <container> \
        python scripts/backfill_deep_standalone.py --dry-run
    docker exec -e riot_key=<SPARE_DEV_KEY> <container> \
        python scripts/backfill_deep_standalone.py

(-e wins over the .env file: python-dotenv never overrides variables
already present in the process environment.)

Watch progress via docker logs / the attached terminal — every player
and every 100 timelines print a line. After the run, mop up detail
columns with scripts/backfill_match_detail_from_raw.py (zero API).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

# Make Bot.* importable when running from the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "Bot"))

import logging  # noqa: E402

from utils import config, db, match_ingest, riot_client  # noqa: E402

# ~90% of a personal dev key (20/1s, 100/120s). This process's own budget —
# the bot's limiter lives in the bot's process and is untouched.
STANDALONE_LIMITS: list[tuple[int, float]] = [(18, 1.0), (90, 120.0)]

# bot_config checkpoint key. MUST stay distinct from the in-bot backfill's
# "backfill_resume_state" — both may exist at once (bot resuming its own
# run while this script runs on the spare key).
RESUME_KEY = "deep_backfill_standalone_state"

PROGRESS_EVERY_TIMELINES = 100


def _now() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def say(text: str) -> None:
    print(f"[deep {_now()}] {text}", flush=True)


async def _load_marker() -> dict:
    row = await db.fetchone("SELECT value FROM bot_config WHERE key = %s", (RESUME_KEY,))
    if row is None:
        return {}
    try:
        state = json.loads(row[0])
        return state if isinstance(state, dict) else {}
    except (TypeError, ValueError):
        say(f"resume marker unreadable ({row[0]!r}) — starting fresh")
        return {}


async def _save_marker(state: dict) -> None:
    await db.execute(
        "INSERT INTO bot_config (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (RESUME_KEY, json.dumps(state)),
    )


async def _clear_marker() -> None:
    await db.execute("DELETE FROM bot_config WHERE key = %s", (RESUME_KEY,))


async def _tracked_players() -> list[tuple[str, str]]:
    """(puuid, display name) per tracked account, deduped by puuid."""
    rows = await db.fetchall(
        "SELECT puuid, league_username FROM league_players "
        "WHERE puuid IS NOT NULL AND puuid != ''"
    )
    seen: set[str] = set()
    players = []
    for puuid, name in rows:
        if puuid in seen:
            continue
        seen.add(puuid)
        players.append((puuid, name))
    return players


async def _dry_run() -> None:
    players = await _tracked_players()
    marker = await _load_marker()
    done = set(marker.get("players_done") or [])
    phase = marker.get("phase") or "matches"
    remaining = [name for puuid, name in players if puuid not in done]

    row = await db.fetchone("SELECT COUNT(DISTINCT match_id) FROM match_stats")
    stored_matches = row[0] if row else 0
    missing_timelines = len(await match_ingest.missing_timeline_ids())

    say("DRY RUN — no API calls, no writes")
    say(f"tracked players: {len(players)}")
    if marker:
        say(f"resume marker found: phase={phase}, {len(done)} players already walked")
    if phase == "matches":
        say(f"match walk pending for {len(remaining)} players: {', '.join(remaining) or '(none)'}")
    else:
        say("match walk already complete per marker — would go straight to timelines")
    say(f"stored matches (all players, deduped): {stored_matches:,}")
    say(f"timelines currently missing: {missing_timelines:,} (1 request each)")
    say("request cost model:")
    say("  ids pages: 1 request per 100 matches of history per player")
    say("  each NEW match: 2 requests (details + timeline)")
    say(f"  budget: {STANDALONE_LIMITS[1][0]} req/2min sustained (~45/min, ~2,700/h)")
    say(
        "  e.g. 10,000 new matches ≈ 20,200 requests ≈ 7.5 h;"
        f" the timeline heal alone ≈ {max(1, missing_timelines // 45)} min"
    )
    say("rerun without --dry-run to start; Ctrl-C any time — the run resumes")


async def _run() -> None:
    players = await _tracked_players()
    if not players:
        say("no tracked players with a puuid — nothing to do")
        return

    marker = await _load_marker()
    players_done: list[str] = list(marker.get("players_done") or [])
    phase = marker.get("phase") or "matches"
    if marker:
        say(f"resuming: phase={phase}, {len(players_done)} players already walked")
    else:
        marker = {
            "phase": "matches",
            "players_done": players_done,
            "started_at": dt.datetime.now(dt.UTC).isoformat(),
        }
        await _save_marker(marker)

    # --- phase 1: full-depth all-queue match walk -----------------------
    total_new = 0
    if phase == "matches":
        todo = [(puuid, name) for puuid, name in players if puuid not in set(players_done)]
        say(f"match walk: {len(todo)} of {len(players)} players to go")
        for index, (puuid, name) in enumerate(todo, start=1):
            say(f"player {index}/{len(todo)} {name}: walking all queues, full depth...")
            try:
                inserted = await match_ingest.backfill_player(
                    puuid, count=match_ingest.PAGE_SIZE, all_history=True, name=name
                )
            except Exception as exc:
                # Player NOT marked done — a rerun retries them. The walk
                # is cheap to redo: stored matches pre-filter out.
                say(f"player {index}/{len(todo)} {name}: FAILED ({exc!r}) — rerun to retry")
                continue
            total_new += inserted
            players_done.append(puuid)
            marker["players_done"] = players_done
            await _save_marker(marker)
            say(
                f"player {index}/{len(todo)} {name}: done, +{inserted} new matches "
                f"(run total {total_new})"
            )
        walked = sum(1 for puuid, _ in players if puuid in set(players_done))
        say(f"match walk complete: {walked}/{len(players)} players, +{total_new} new matches")
        marker["phase"] = "timelines"
        await _save_marker(marker)

    # --- phase 2: timeline heal -----------------------------------------
    match_ids = await match_ingest.missing_timeline_ids()
    say(f"timeline heal: {len(match_ids)} matches missing a timeline (newest first)")
    fetched = 0
    unavailable = 0
    for index, mid in enumerate(match_ids, start=1):
        try:
            if await match_ingest.archive_timeline(mid):
                fetched += 1
            else:
                unavailable += 1
        except Exception as exc:
            unavailable += 1
            say(f"timeline {mid} failed: {exc!r}")
        if index % PROGRESS_EVERY_TIMELINES == 0 and index < len(match_ids):
            say(
                f"timelines {index}/{len(match_ids)} "
                f"(stored {fetched}, unavailable {unavailable})"
            )
    say(
        f"timeline heal complete: {fetched} stored, {unavailable} unavailable "
        "(aged out of Riot's window or transient — rerun retries the transient ones)"
    )

    await _clear_marker()
    say("all done. marker cleared. mop-up: python scripts/backfill_match_detail_from_raw.py")


async def amain(dry_run: bool, budget: int) -> int:
    if not config.riot_api_key():
        say(
            "riot_key is not set — pass it via the environment "
            "(docker exec -e riot_key=<SPARE_DEV_KEY> ...)"
        )
        return 1
    try:
        if dry_run:
            await _dry_run()
        else:
            # --budget caps this process's 2-minute window. When sharing the
            # bot's own key (puuids are key-scoped, so a spare key can only
            # walk puuids it resolved itself), a low budget leaves the rest
            # of the window to the bot's limiter, which knows nothing of us.
            limits = [(min(18, budget), 1.0), (budget, 120.0)]
            riot_client.set_rate_limits(limits)
            say(
                "rate budget for THIS process: "
                + ", ".join(f"{n}/{int(w)}s" for n, w in limits)
                + " (the bot's own limiter is a different process — unaffected)"
            )
            await _run()
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
        help="show planned work + request estimate, call nothing, write nothing",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=STANDALONE_LIMITS[1][0],
        help="requests per 2-minute window for this process (default %(default)s; "
        "use a low value like 40 when running on the bot's own key)",
    )
    args = parser.parse_args()

    # Page/ids-walk INFO lines from utils.match_ingest and budget warnings
    # from utils.riot_client belong in the same stdout stream as the say()
    # progress lines — this run is watched via docker logs.
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if sys.platform == "win32":
        # psycopg's async pool can't run on Windows' default ProactorEventLoop.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(amain(args.dry_run, max(1, args.budget)))
    except KeyboardInterrupt:
        print("\n[deep] interrupted — progress is saved; rerun to resume", flush=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
