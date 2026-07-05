"""Backfill + ongoing-stream of per-match stats into the match_stats table.

Runs inside the bot process so Riot API calls share the global rate
limiter in :mod:`utils.riot_client`. Two flows:

* Slash command ``/backfill_all`` — one-shot historical pull. By
  default the most recent 100 matches per player; with ``all_history=True``
  it paginates Match-V5 to the end of Riot's exposed window (~2 years).
  Work runs as a background asyncio task so the command returns
  immediately and the rest of the bot keeps serving. Responses are
  ephemeral (only the invoker sees them) so triggering it doesn't spam
  the channel.
* ``stream_matches`` ``@tasks.loop(minutes=5)`` — always-on. Polls the
  most recent 5 match IDs per tracked player, inserts any not already
  stored. Catches new games soon after they happen without needing a
  manual trigger.

Both paths share the same per-player routine and are fully idempotent:
(match_id, puuid) is the table's PRIMARY KEY, we pre-filter against
existing IDs before any match-detail fetch, and the write uses
INSERT ... ON CONFLICT DO NOTHING as a belt-and-braces guard.

Every fetched payload is also archived verbatim into ``match_raw``
(one row per match, JSONB) so future stat columns can be filled from
SQL instead of a Riot re-fetch backfill. The pre-filter skips a match
only when it is in match_stats (for this puuid) AND in match_raw:
matches ingested before match_raw existed are therefore re-fetched —
through the shared limiter — exactly once, healing the raw archive.
"""

import asyncio
import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks
from psycopg.types.json import Jsonb
from utils import db
from utils.loop_restart import restart_loop_later
from utils.riot_client import (
    RANKED_5S_QUEUE_ID,
    RANKED_SOLO_QUEUE_ID,
    get_match,
    get_match_ids,
)

log = logging.getLogger(__name__)

# Match-V5 caps a single page at 100 IDs. We use 100 everywhere and paginate
# for deeper history.
PAGE_SIZE = 100
DEFAULT_BACKFILL_COUNT = 100
STREAM_RECENT_COUNT = 5  # How many recent IDs the stream checks per player


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
        self.stream_matches.start()

    def cog_unload(self) -> None:
        self.stream_matches.cancel()

    # --- slash commands -----------------------------------------------

    @app_commands.command(
        name="backfill_all",
        description="Backfill match_stats for every tracked player (ephemeral)",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        count="Max matches per player (max 100). Ignored if all_history=True.",
        all_history="Paginate Riot's full history (~2y); also heals matches missing raw JSON. Slow.",
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
        existed fail the pre-filter and get re-fetched (match_stats
        re-writes are ON CONFLICT DO NOTHING no-ops, so this is harmless,
        but progress counts include those healed matches). Run with
        all_history=True once after deploying raw capture to backfill
        payloads for the whole history window.
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

        self._progress = {name: -1 for _, name in rows}
        self._task = asyncio.create_task(self._do_backfill(list(rows), count, all_history))
        scope = "all of Riot's exposed history (~2y)" if all_history else f"up to {count} matches"
        await ctx.followup.send(
            f"Backfilling {len(rows)} players ({scope}). "
            f"Use /backfill_status to check progress.",
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
        self.bot.logging.info("Backfill cancelled by /backfill_cancel")
        await ctx.response.send_message(
            "Cancelled. Re-run /backfill_all later to pick up where it left off.",
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
        for puuid, name in rows:
            try:
                inserted = await self._backfill_player(
                    puuid, count=STREAM_RECENT_COUNT, all_history=False, name=name
                )
                if inserted > 0:
                    self._stream_total_inserts += inserted
                    self.bot.logging.info(f"Stream: {name} +{inserted} matches")
            except Exception as exc:
                self.bot.logging.error(f"Stream failed for {name}: {exc!r}")
        self._stream_last_ran = dt.datetime.now()

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
    ) -> None:
        for puuid, name in players:
            self.bot.logging.info(
                f"Backfill: starting {name} (all_history={all_history}, count={count})"
            )
            try:
                inserted = await self._backfill_player(puuid, count, all_history, name=name)
                self._progress[name] = inserted
                self.bot.logging.info(f"Backfill: {name} done, +{inserted} matches total")
            except Exception as exc:
                self._progress[name] = -2
                self.bot.logging.error(f"Backfill failed for {name}: {exc!r}")
        self.bot.logging.info("Backfill: all players complete")

    async def _backfill_player(
        self, puuid: str, count: int, all_history: bool = False, name: str | None = None
    ) -> int:
        """Pull match IDs for the player and insert any not already stored.

        With ``all_history=False``, fetches a single page of up to ``count``
        IDs. With ``all_history=True``, paginates by incrementing ``start``
        until Riot returns a short page (signalling end of history).
        Returns the total number of newly inserted matches.

        ``name`` is purely for log readability; falls back to a truncated
        puuid when not given (the stream loop always passes it).
        """
        inserted_total = 0
        label = name or f"{puuid[:8]}..."

        # Track BOTH queues: ranked solo/duo and the weekend Ranked 5s
        # (710). Riot's league API doesn't expose the 5s ladder yet, so
        # these match rows are also what the 5s board's fallback standings
        # are built from. 5s history is tiny (limited-test queue): steady
        # state costs one extra id-page request per player per pass.
        for queue in (RANKED_SOLO_QUEUE_ID, RANKED_5S_QUEUE_ID):
            start = 0
            page_num = 0
            while True:
                page_num += 1
                page_size = PAGE_SIZE if all_history else count
                ids = await get_match_ids(puuid, count=page_size, queue=queue, start=start)
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
        per write (raw payload archive, then the per-player stats row).

        Why per-match: bundling the whole page under one transaction
        would hold a pooled connection across N network calls
        (~50-500ms each). A one-shot ``db.execute`` per insert returns
        the connection to the pool between Riot fetches, so other
        writers interleave freely.

        Returns the number of matches fetched and written through. During
        a raw-archive heal pass the match_stats write is a conflict no-op
        but the match still counts — the fetch genuinely happened.
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
            for participant in match["info"]["participants"]:
                if participant["puuid"] != puuid:
                    continue
                # game_start is TIMESTAMPTZ — insert a tz-aware datetime
                # (Riot's gameStartTimestamp is epoch millis, i.e. UTC).
                game_start = dt.datetime.fromtimestamp(
                    match["info"]["gameStartTimestamp"] / 1000.0,
                    tz=dt.UTC,
                )
                await db.execute(
                    "INSERT INTO match_stats "
                    "(match_id, puuid, game_start, queue_id, champion, "
                    " win, kills, deaths, assists, duration_sec, patch_version, "
                    " position) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (match_id, puuid) DO NOTHING",
                    (
                        mid,
                        puuid,
                        game_start,
                        match["info"]["queueId"],
                        participant["championName"],
                        1 if participant["win"] else 0,
                        participant["kills"],
                        participant["deaths"],
                        participant["assists"],
                        match["info"]["gameDuration"],
                        match["info"].get("gameVersion"),
                        _participant_position(participant),
                    ),
                )
                inserted += 1
                break
        return inserted


async def setup(bot: commands.Bot):
    await bot.add_cog(Backfill(bot))
