"""Backfill + ongoing-stream of per-match stats into the match_stats table.

Runs inside the bot process so Riot API calls share the global rate
limiter in :mod:`utils.riot_client`. Two flows:

* Slash command ``/backfill_all`` — one-shot historical pull. By
  default the most recent 100 matches per player; with ``all_history=True``
  it paginates Match-V5 ids (start += 100 until a short page) to the end
  of whatever Riot still retains — no startTime filter is sent, so
  nothing is truncated on our side; the matchlist itself only goes back
  to 2021-06-16 by Riot's own docs, and in practice EUW has served ~2
  years. Work runs as a background asyncio task so the command returns
  immediately and the rest of the bot keeps serving. Responses are
  ephemeral (only the invoker sees them) so triggering it doesn't spam
  the channel.
* ``stream_matches`` ``@tasks.loop(minutes=5)`` — always-on. Polls the
  most recent 5 match IDs per tracked player, inserts any not already
  stored. Catches new games soon after they happen without needing a
  manual trigger.
* Slash command ``/backfill_timelines`` — one-shot API-based heal for
  historical matches missing a timeline (timelines are a separate
  endpoint, so unlike stat columns they can't be filled from match_raw).
* Slash command ``/backfill_detail_from_raw`` — zero-API SQL fill of the
  match_stats detail columns from archived match_raw payloads (the
  Discord-native wrapper around scripts/backfill_match_detail_from_raw.py).

Since the 2026-08 capture-everything pass both ingest paths cover EVERY
queue (the match-ids call simply drops the queue filter — same request
count as the old solo-only poll), and each newly fetched match also pulls
its Match-V5 timeline (one extra request per new match), archived into
``match_timeline_raw``. Timeline fetch failures never block core ingest —
the match row + payload land regardless, and /backfill_timelines can heal
the gap later.

Every API-hitting backfill runs INSIDE the bot as a background task so it
shares the process-wide rate limiter with the live boards — never as a
standalone process (one of those starved the live loops once; see
docs/riot-api-reference.md). Long runs post periodic progress to the
invoking channel, checkpoint durably (each match/timeline commits as its
own row + a bot_config resume marker), pace themselves below the budget
cap so live traffic effectively keeps priority, and auto-resume after a
bot restart or hot reload.

Both paths share the same per-player routine and are fully idempotent:
(match_id, puuid) is the table's PRIMARY KEY, we pre-filter against
existing IDs before any match-detail fetch, and on conflict the write
only fills detail columns that are still NULL (COALESCE keeps every
existing value) — so re-fetching a stored match can upgrade it but
never rewrite it.

Every fetched payload is also archived verbatim into ``match_raw``
(one row per match, JSONB) so future stat columns can be filled from
SQL instead of a Riot re-fetch backfill. The pre-filter skips a match
only when it is in match_stats (for this puuid) AND in match_raw:
matches ingested before match_raw existed are therefore re-fetched —
through the shared limiter — exactly once, healing the raw archive.
"""

import asyncio
import datetime as dt
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks
from psycopg.types.json import Jsonb
from utils import db
from utils.loop_restart import restart_loop_later
from utils.match_detail import (
    DETAIL_COLUMNS,
    RAW_BACKFILL_COUNT_SQL,
    RAW_BACKFILL_UPDATE_SQL,
    participant_detail,
)
from utils.queue_windows import is_ranked5s_tracking
from utils.riot_client import (
    RANKED_5S_QUEUE_ID,
    RANKED_SOLO_QUEUE_ID,
    get_match,
    get_match_ids,
    get_match_timeline,
)

log = logging.getLogger(__name__)

# Match-V5 caps a single page at 100 IDs. We use 100 everywhere and paginate
# for deeper history.
PAGE_SIZE = 100
DEFAULT_BACKFILL_COUNT = 100
STREAM_RECENT_COUNT = 5  # How many recent IDs the stream checks per player

# queue=None drops match-v5's queue filter: every LoL queue (solo, 5s,
# flex, ARAM, normals, customs) in ONE request — capture-everything at
# the same ids spend the old solo-only poll had. Downstream consumers
# filter on match_stats.queue_id, so extra queues can't leak into the
# boards/awards (they all pin 420/710 in SQL).
ALL_QUEUES: tuple[None, ...] = (None,)

# Pace between timeline fetches during the /backfill_timelines crawl:
# 2.0s -> ~30 requests/min -> at most ~60 of the key's 100/120s budget,
# leaving the live boards/stream comfortably served. The shared limiter
# has no priority lanes, so the crawl self-restrains instead. (Inline
# timeline fetches during normal ingest are NOT paced — they ride the
# same budget as the match fetch they accompany.)
TIMELINE_BACKFILL_PACE_SECONDS = 2.0

# Progress posts to the invoking channel: every N players for the match
# walk, every N timelines for the crawl. Silent sends — informative, not
# pinging anyone.
PROGRESS_EVERY_PLAYERS = 5
PROGRESS_EVERY_TIMELINES = 500

# bot_config key holding the resume marker for an in-flight backfill.
# Written when a backfill starts, cleared on completion or explicit
# /backfill_cancel — so a bot restart/hot reload mid-run auto-resumes
# (the data checkpoint is the tables themselves; the marker only records
# THAT a run was in flight, plus where to post progress).
RESUME_KEY = "backfill_resume_state"

# Column lists and placeholder counts derive from DETAIL_COLUMNS (shared
# single source in utils/match_detail.py) so the columns, VALUES arity and
# ON CONFLICT healing set can never drift apart. The conflict path fills
# only detail columns and only where the existing row has NULL — so a
# raw-archive heal re-fetch upgrades old rows in place, while rows that
# already carry values are never rewritten.
_INSERT_MATCH_STATS_SQL = (
    "INSERT INTO match_stats "
    "(match_id, puuid, game_start, queue_id, champion, "
    " win, kills, deaths, assists, duration_sec, patch_version, "
    " position, team_id, " + ", ".join(DETAIL_COLUMNS) + ") "
    "VALUES (" + ", ".join(["%s"] * (13 + len(DETAIL_COLUMNS))) + ") "
    "ON CONFLICT (match_id, puuid) DO UPDATE SET "
    + ", ".join(f"{col} = COALESCE(match_stats.{col}, EXCLUDED.{col})" for col in DETAIL_COLUMNS)
)


def _participant_position(participant: dict) -> str | None:
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


class Backfill(commands.Cog):
    """Backfill commands + always-on streaming for match_stats."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        self._task: asyncio.Task | None = None
        self._progress: dict[str, int] = {}
        self._stream_last_ran: dt.datetime | None = None
        self._stream_total_inserts: int = 0
        self._resume_task: asyncio.Task | None = None
        self.stream_matches.start()

    async def cog_load(self) -> None:
        # Auto-resume an in-flight backfill that a restart/hot reload
        # interrupted (see RESUME_KEY). Detached task: cog_load must not
        # block on wait_until_ready.
        self._resume_task = asyncio.create_task(self._maybe_resume_backfill())

    def cog_unload(self) -> None:
        self.stream_matches.cancel()
        if self._resume_task is not None and not self._resume_task.done():
            self._resume_task.cancel()
        # Cancel the background backfill so a hot reload can't leave the
        # old instance's crawl running next to the new instance's resumed
        # one. The resume marker survives (it's only cleared by completion
        # or /backfill_cancel), so the new instance picks the work up.
        if self._task is not None and not self._task.done():
            self._task.cancel()
            self.bot.logging.info("Backfill task cancelled by cog unload — marker will resume it")

    # --- resume marker ---------------------------------------------------

    async def _set_resume_marker(self, payload: dict) -> None:
        await db.execute(
            "INSERT INTO bot_config (key, value, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (RESUME_KEY, json.dumps(payload)),
        )

    async def _clear_resume_marker(self) -> None:
        await db.execute("DELETE FROM bot_config WHERE key = %s", (RESUME_KEY,))

    async def _maybe_resume_backfill(self) -> None:
        """Restart an interrupted backfill after boot/reload, if one was
        in flight. The tables are the real checkpoint — already-stored
        matches/timelines pre-filter out — so resuming costs only the
        cheap re-walk of what's already done."""
        await self.bot.wait_until_ready()
        try:
            row = await db.fetchone("SELECT value FROM bot_config WHERE key = %s", (RESUME_KEY,))
        except Exception as exc:
            self.bot.logging.warning(f"Backfill resume check failed: {exc!r}")
            return
        if row is None:
            return
        try:
            state = json.loads(row[0])
        except (TypeError, ValueError):
            self.bot.logging.warning(f"Backfill resume marker unreadable ({row[0]!r}) — cleared")
            await self._clear_resume_marker()
            return
        if self._task is not None and not self._task.done():
            return  # something is already running; leave the marker to it

        kind = state.get("kind")
        channel_id = state.get("channel_id")
        if kind == "matches":
            players = await db.fetchall(
                "SELECT puuid, league_username FROM league_players "
                "WHERE puuid IS NOT NULL AND puuid != ''"
            )
            self._progress = {name: -1 for _, name in players}
            self._task = asyncio.create_task(
                self._do_backfill(
                    list(players),
                    int(state.get("count", DEFAULT_BACKFILL_COUNT)),
                    bool(state.get("all_history", False)),
                    channel_id=channel_id,
                )
            )
        elif kind == "timelines":
            match_ids = await self._missing_timeline_ids()
            if not match_ids:
                await self._clear_resume_marker()
                return
            self._progress = {"timelines": 0}
            self._task = asyncio.create_task(
                self._do_backfill_timelines(match_ids, channel_id=channel_id)
            )
        else:
            self.bot.logging.warning(f"Unknown backfill resume kind {kind!r} — cleared")
            await self._clear_resume_marker()
            return

        self.bot.logging.info(f"Resumed interrupted {kind} backfill after restart")
        await self._post_progress(
            channel_id, f"Resuming interrupted {kind} backfill after restart."
        )

    # --- progress posts ---------------------------------------------------

    async def _post_progress(self, channel_id: int | None, text: str) -> None:
        """Silent, best-effort progress note to the invoking channel."""
        if not channel_id:
            return
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            await channel.send(text, silent=True)
        except Exception as exc:
            self.bot.logging.warning(f"Backfill progress post failed: {exc!r}")

    # --- slash commands -----------------------------------------------

    @app_commands.command(
        name="backfill_all",
        description="Backfill match_stats for every tracked player (ephemeral)",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        count="Max matches per player (max 100). Ignored if all_history=True.",
        all_history="Walk ALL queues to the end of Riot's retention; also heals missing raw JSON. Slow.",
    )
    async def backfill_all(
        self,
        ctx: discord.Interaction,
        count: int = DEFAULT_BACKFILL_COUNT,
        all_history: bool = False,
    ):
        """One-shot historical pull for every tracked player.

        Besides inserting missing match_stats rows, this also auto-heals
        the match_raw archive: matches stored before raw-payload capture
        existed fail the pre-filter and get re-fetched. Re-writes of
        existing match_stats rows only fill detail columns that are still
        NULL (core columns are never touched), and progress counts include
        those healed matches. Run with all_history=True once after
        deploying raw capture to backfill payloads for the whole history
        window.
        """
        await ctx.response.defer(ephemeral=True)

        if self._task is not None and not self._task.done():
            await ctx.followup.send(
                "Backfill already running. Use /backfill_status.", ephemeral=True
            )
            return

        count = max(1, min(count, PAGE_SIZE))

        rows = await db.fetchall(
            "SELECT puuid, league_username FROM league_players "
            "WHERE puuid IS NOT NULL AND puuid != ''"
        )

        if not rows:
            await ctx.followup.send("No players to backfill.", ephemeral=True)
            return

        await self._set_resume_marker(
            {
                "kind": "matches",
                "channel_id": ctx.channel_id,
                "count": count,
                "all_history": all_history,
            }
        )
        self._progress = {name: -1 for _, name in rows}
        self._task = asyncio.create_task(
            self._do_backfill(list(rows), count, all_history, channel_id=ctx.channel_id)
        )
        scope = (
            "all queues, as deep as Riot's retention goes"
            if all_history
            else f"up to {count} matches (all queues)"
        )
        await ctx.followup.send(
            f"Backfilling {len(rows)} players ({scope}). "
            f"Use /backfill_status to check progress; survives restarts.",
            ephemeral=True,
        )

    @app_commands.command(
        name="backfill_cancel",
        description="Cancel the running backfill (resumable — already-stored matches are kept)",
    )
    @app_commands.guild_only()
    async def backfill_cancel(self, ctx: discord.Interaction):
        if self._task is None or self._task.done():
            await ctx.response.send_message("No backfill is running.", ephemeral=True)
            return
        self._task.cancel()
        # Explicit cancel also clears the resume marker — the user said
        # stop, so a restart must NOT resurrect the run.
        await self._clear_resume_marker()
        self.bot.logging.info("Backfill cancelled by /backfill_cancel")
        await ctx.response.send_message(
            "Cancelled (won't auto-resume). Re-run the backfill command later "
            "to pick up where it left off — already-stored data is kept.",
            ephemeral=True,
        )

    @app_commands.command(
        name="backfill_status",
        description="Progress of the running (or last) backfill + stream (ephemeral)",
    )
    @app_commands.guild_only()
    async def backfill_status(self, ctx: discord.Interaction):
        stream_when = (
            self._stream_last_ran.strftime("%Y-%m-%d %H:%M:%S")
            if self._stream_last_ran
            else "never"
        )
        stream_line = f"stream: last ran {stream_when}, {self._stream_total_inserts} total inserts"

        if self._task is None:
            await ctx.response.send_message(
                f"No /backfill_all has been started.\n{stream_line}", ephemeral=True
            )
            return

        lines = []
        for name, inserted in self._progress.items():
            if inserted == -1:
                lines.append(f"  {name}: queued")
            elif inserted == -2:
                lines.append(f"  {name}: errored")
            else:
                lines.append(f"  {name}: {inserted} matches")

        if self._task.done():
            try:
                self._task.result()
                header = "Backfill complete."
            except Exception as exc:
                header = f"Backfill errored: {exc!r}"
        else:
            header = "Backfill running..."

        body = "\n".join(lines[:25])
        more = f"\n  ...and {len(lines) - 25} more" if len(lines) > 25 else ""
        await ctx.response.send_message(
            f"{header}\n{stream_line}\n```\n{body}{more}\n```", ephemeral=True
        )

    @app_commands.command(
        name="backfill_timelines",
        description="Fetch Match-V5 timelines for stored matches that lack one (ephemeral)",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        limit="Max timelines this run (0 = all missing). Newest matches first.",
    )
    async def backfill_timelines(self, ctx: discord.Interaction, limit: int = 0):
        """One-shot heal: fetch timelines for every stored match missing one.

        One request per missing match, newest first (recent games are the
        interesting ones if the run is bounded or interrupted). Fully
        resumable — the checkpoint IS the match_timeline_raw table: each
        timeline commits as its own row, so a restart or /backfill_cancel
        loses nothing and re-running the command continues with whatever
        is still missing. 404s (timeline aged out of Riot's window) store
        no row, so each later run re-asks about them once — acceptable:
        they're rare, cheap, and the runs are manual.
        """
        await ctx.response.defer(ephemeral=True)

        if self._task is not None and not self._task.done():
            await ctx.followup.send(
                "A backfill is already running. Use /backfill_status.", ephemeral=True
            )
            return

        match_ids = await self._missing_timeline_ids()
        total_missing = len(match_ids)
        if limit > 0:
            match_ids = match_ids[:limit]
        if not match_ids:
            await ctx.followup.send("Every stored match already has a timeline.", ephemeral=True)
            return

        # Paced at TIMELINE_BACKFILL_PACE_SECONDS -> ~30 requests/min.
        eta_min = max(1, len(match_ids) // 30)
        await self._set_resume_marker({"kind": "timelines", "channel_id": ctx.channel_id})
        self._progress = {"timelines": 0}
        self._task = asyncio.create_task(
            self._do_backfill_timelines(match_ids, channel_id=ctx.channel_id)
        )
        await ctx.followup.send(
            f"Fetching {len(match_ids)} of {total_missing} missing timelines "
            f"(newest first, paced ~30/min => ~{eta_min} min; ~100-250 KB stored "
            f"per match). Survives restarts; /backfill_status for progress, "
            f"/backfill_cancel to stop.",
            ephemeral=True,
        )

    @app_commands.command(
        name="backfill_detail_from_raw",
        description="Fill match_stats detail columns from archived payloads — SQL only, no API",
    )
    @app_commands.guild_only()
    async def backfill_detail_from_raw(self, ctx: discord.Interaction):
        """Discord-native wrapper around the zero-API detail backfill.

        One UPDATE joining match_stats rows still missing detail
        (damage_to_champs IS NULL) to their archived match_raw
        participants. Idempotent, touches no core columns, costs zero
        Riot budget — safe to run any time, even alongside the stream.
        (scripts/backfill_match_detail_from_raw.py is the same operation
        for shell users.)
        """
        await ctx.response.defer(ephemeral=True)
        row = await db.fetchone(RAW_BACKFILL_COUNT_SQL)
        fixable, missing_payload = row if row else (0, 0)
        if not fixable:
            await ctx.followup.send(
                f"Nothing to fill from the archive. Rows missing detail with no "
                f"archived payload: {missing_payload} (those need "
                f"/backfill_all all_history=True).",
                ephemeral=True,
            )
            return
        async with db.connection() as conn:
            cur = await conn.execute(RAW_BACKFILL_UPDATE_SQL)
            updated = cur.rowcount
        self.bot.logging.info(f"Raw-detail backfill via command: {updated} rows updated")
        await ctx.followup.send(
            f"Updated {updated} match_stats rows from archived payloads. "
            f"Rows still missing detail with no archived payload: {missing_payload} "
            f"(heal via /backfill_all all_history=True, then re-run this).",
            ephemeral=True,
        )

    # --- error handling -----------------------------------------------

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.NoPrivateMessage):
            msg = "Run this in the server, not DMs."
        else:
            self.bot.logging.error(f"backfill cog error: {error!r}")
            msg = f"Command failed: {error!r}"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # --- always-on stream ---------------------------------------------

    @tasks.loop(minutes=5)
    async def stream_matches(self) -> None:
        """Pull the last STREAM_RECENT_COUNT match IDs for every tracked
        player and insert anything new. Cheap on steady state — most calls
        return IDs we already have and skip the match-detail fetch.
        """
        rows = await db.fetchall(
            "SELECT puuid, league_username FROM league_players "
            "WHERE puuid IS NOT NULL AND puuid != ''"
        )
        # ALL_QUEUES (unfiltered ids call) replaced the old solo+weekend-5s
        # queue juggling: one request per player covers every queue, so the
        # weekend gating that existed to save the second request is moot.
        for puuid, name in rows:
            try:
                inserted = await self._backfill_player(
                    puuid,
                    count=STREAM_RECENT_COUNT,
                    all_history=False,
                    name=name,
                    queues=ALL_QUEUES,
                )
                if inserted > 0:
                    self._stream_total_inserts += inserted
                    self.bot.logging.info(f"Stream: {name} +{inserted} matches")
            except Exception as exc:
                self.bot.logging.error(f"Stream failed for {name}: {exc!r}")
        self._stream_last_ran = dt.datetime.now()

    async def ingest_recent(self, puuids) -> int:
        """Targeted on-demand pull for players whose game just ended.

        Called by the live-games cog's end-of-game fast path so the board
        refresh it triggers can render the finished game (last-played,
        squares, 🚩) in the same post as the fresh LP — otherwise the
        board shows new LP next to a last-played that's one game stale
        until the next stream_matches tick, up to ~5 minutes later.
        Best-effort: if match-v5 hasn't indexed the game yet this inserts
        nothing and the stream picks it up as before. Gated to the board
        queues (solo, plus 5s when tracked) for speed — the all-queue
        stream loop sweeps up everything else — with the same per-player
        error isolation.
        """
        puuids = set(puuids)
        if not puuids:
            return 0
        placeholders = ",".join(["%s"] * len(puuids))
        rows = await db.fetchall(
            f"SELECT puuid, league_username FROM league_players "
            f"WHERE puuid IN ({placeholders})",
            tuple(puuids),
        )
        queues = (RANKED_SOLO_QUEUE_ID,) + ((RANKED_5S_QUEUE_ID,) if is_ranked5s_tracking() else ())
        inserted_total = 0
        for puuid, name in rows:
            try:
                inserted = await self._backfill_player(
                    puuid, count=STREAM_RECENT_COUNT, all_history=False, name=name, queues=queues
                )
            except Exception as exc:
                self.bot.logging.error(f"Fast-path ingest failed for {name}: {exc!r}")
                continue
            if inserted > 0:
                self._stream_total_inserts += inserted
                inserted_total += inserted
                self.bot.logging.info(f"Fast-path ingest: {name} +{inserted} matches")
        return inserted_total

    @stream_matches.before_loop
    async def before_stream(self) -> None:
        await self.bot.wait_until_ready()

    @stream_matches.error
    async def stream_matches_error(self, exc: BaseException) -> None:
        """Auto-restart stream_matches on unhandled error.

        Default @tasks.loop behaviour on exception is log + stop. Without
        this, a single transient error (e.g. flaky Riot response) would
        permanently halt the stream and new matches would silently stop
        being recorded.
        """
        self.bot.logging.error(f"stream_matches errored: {exc!r}, restarting in 60s")
        # Detached: this callback runs inside the dying loop task, where
        # is_running() is still True and a direct start() would be skipped.
        restart_loop_later(
            self.stream_matches,
            name="stream_matches",
            log=self.bot.logging,
            still_active=lambda: self.bot.get_cog("Backfill") is self,
        )

    # --- shared per-player routine ------------------------------------

    async def _do_backfill(
        self,
        players: list[tuple[str, str]],
        count: int,
        all_history: bool,
        channel_id: int | None = None,
    ) -> None:
        done = 0
        total_new = 0
        for puuid, name in players:
            self.bot.logging.info(
                f"Backfill: starting {name} (all_history={all_history}, count={count})"
            )
            try:
                inserted = await self._backfill_player(puuid, count, all_history, name=name)
                self._progress[name] = inserted
                total_new += inserted
                self.bot.logging.info(f"Backfill: {name} done, +{inserted} matches total")
            except Exception as exc:
                self._progress[name] = -2
                self.bot.logging.error(f"Backfill failed for {name}: {exc!r}")
            done += 1
            if done % PROGRESS_EVERY_PLAYERS == 0 and done < len(players):
                await self._post_progress(
                    channel_id,
                    f"Backfill progress: {done}/{len(players)} players walked, "
                    f"+{total_new} new matches so far.",
                )
        await self._clear_resume_marker()
        self.bot.logging.info("Backfill: all players complete")
        await self._post_progress(
            channel_id,
            f"Backfill complete: {len(players)} players, +{total_new} new matches.",
        )

    async def _backfill_player(
        self,
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
                        self.bot.logging.warning(
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
                    page_new = await self._insert_matches(puuid, to_fetch)
                    inserted_total += page_new
                    # Page-level log only when the page actually delivered new
                    # rows. Steady-state stream calls stay quiet.
                    if page_new > 0:
                        self.bot.logging.info(
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

    async def _insert_matches(self, puuid: str, match_ids: list[str]) -> int:
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
                await self._archive_timeline(mid)
            except Exception as exc:
                self.bot.logging.warning(f"Timeline archive failed for {mid}: {exc!r}")
            # Per-match guard: the all-queue, full-depth walk can surface
            # modes/vintages this extraction never anticipated. A single
            # malformed match must cost exactly that match — its raw
            # payload is already archived above, so the row is recoverable
            # from SQL later — never the rest of the page.
            try:
                inserted += await self._insert_participant_row(mid, puuid, match)
            except Exception as exc:
                self.bot.logging.error(
                    f"Stats extraction failed for {mid}: {exc!r} — "
                    "raw payload archived, stats row skipped"
                )
        return inserted

    async def _insert_participant_row(self, mid: str, puuid: str, match: dict) -> int:
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
                _INSERT_MATCH_STATS_SQL,
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
                    _participant_position(participant),
                    participant.get("teamId"),
                    *participant_detail(participant),
                ),
            )
            return 1
        return 0

    async def _missing_timeline_ids(self) -> list[str]:
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

    async def _do_backfill_timelines(
        self, match_ids: list[str], channel_id: int | None = None
    ) -> None:
        """Timeline crawl worker: one paced request per missing match.

        Each timeline commits as its own row, so this is interruptible
        anywhere; the resume marker restarts it after a reboot and
        _missing_timeline_ids recomputes what's left. 404s (aged out of
        Riot's window) store nothing and are counted as unavailable.
        """
        fetched = 0
        unavailable = 0
        total = len(match_ids)
        for index, mid in enumerate(match_ids, start=1):
            try:
                if await self._archive_timeline(mid):
                    fetched += 1
                else:
                    unavailable += 1
            except Exception as exc:
                unavailable += 1
                self.bot.logging.error(f"Timeline backfill failed for {mid}: {exc!r}")
            self._progress["timelines"] = fetched
            if index % PROGRESS_EVERY_TIMELINES == 0 and index < total:
                eta_min = max(1, (total - index) // 30)
                await self._post_progress(
                    channel_id,
                    f"Timeline backfill: {index}/{total} processed "
                    f"({fetched} stored, {unavailable} unavailable), ~{eta_min} min left.",
                )
            # Self-pacing: the shared limiter has no priority lanes, so
            # the crawl stays well under the budget cap and live polling
            # never queues behind it.
            await asyncio.sleep(TIMELINE_BACKFILL_PACE_SECONDS)
        await self._clear_resume_marker()
        self.bot.logging.info(
            f"Timeline backfill complete: +{fetched} timelines, {unavailable} unavailable"
        )
        await self._post_progress(
            channel_id,
            f"Timeline backfill complete: {fetched} timelines stored, "
            f"{unavailable} unavailable (aged out or transient failures — "
            f"re-run to retry the transient ones).",
        )

    async def _archive_timeline(self, match_id: str) -> bool:
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


async def setup(bot: commands.Bot):
    await bot.add_cog(Backfill(bot))
