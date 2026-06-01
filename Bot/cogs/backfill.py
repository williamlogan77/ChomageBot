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
match_id is the table's PRIMARY KEY, we pre-filter against existing IDs
before any match-detail fetch, and the write uses INSERT OR IGNORE as a
belt-and-braces guard.
"""

import asyncio
import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks
from utils.db import aconnect
from utils.riot_client import get_match, get_match_ids

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
        all_history="Paginate through Riot's entire exposed history (~2y). Slow.",
    )
    async def backfill_all(
        self,
        ctx: discord.Interaction,
        count: int = DEFAULT_BACKFILL_COUNT,
        all_history: bool = False,
    ):
        await ctx.response.defer(ephemeral=True)

        if self._task is not None and not self._task.done():
            await ctx.followup.send(
                "Backfill already running. Use /backfill_status.", ephemeral=True
            )
            return

        count = max(1, min(count, PAGE_SIZE))

        async with aconnect(self.bot.db_path) as db:
            rows = await db.execute_fetchall(
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
        async with aconnect(self.bot.db_path) as db:
            rows = await db.execute_fetchall(
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
        await asyncio.sleep(60)
        if not self.stream_matches.is_running():
            self.stream_matches.start()

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
        start = 0
        page_num = 0
        label = name or f"{puuid[:8]}..."

        while True:
            page_num += 1
            page_size = PAGE_SIZE if all_history else count
            ids = await get_match_ids(puuid, count=page_size, start=start)
            if not ids:
                break

            # Filter out match IDs we already have stored FOR THIS puuid.
            # Filtering by match_id alone would skip games where this puuid
            # hasn't been backfilled yet but another tracked friend already
            # has — exactly the "duo's second row" case we need to pick up.
            async with aconnect(self.bot.db_path) as db:
                placeholders = ",".join("?" * len(ids))
                existing_rows = await db.execute_fetchall(
                    f"SELECT match_id FROM match_stats "
                    f"WHERE puuid = ? AND match_id IN ({placeholders})",
                    (puuid, *ids),
                )
                existing = {row[0] for row in existing_rows}

            to_fetch = [mid for mid in ids if mid not in existing]
            if to_fetch:
                page_new = await self._insert_matches(puuid, to_fetch)
                inserted_total += page_new
                # Page-level log only when the page actually delivered new rows.
                # Steady-state stream calls (all matches already in DB) stay quiet.
                if page_new > 0:
                    self.bot.logging.info(
                        f"Backfill: {label} page {page_num} "
                        f"(start={start}, +{page_new} new, total {inserted_total})"
                    )

            # Stop when Riot returned less than we asked for (end of history)
            # or when we've satisfied a bounded request.
            if len(ids) < page_size:
                break
            if not all_history:
                break
            start += len(ids)

        return inserted_total

    async def _insert_matches(self, puuid: str, match_ids: list[str]) -> int:
        """Fetch + insert match details one at a time, opening a fresh
        connection per write.

        Why per-match: bundling the whole page under one transaction
        held the writer lock across N network calls (~50-500ms each),
        which starved /refresh_ranks and other writers and tripped the
        5-second busy_timeout. Opening a new connection only around the
        insert holds the lock for milliseconds — other writers can
        interleave freely under WAL.
        """
        inserted = 0
        for mid in match_ids:
            match = await get_match(mid)
            if match is None:
                continue
            for participant in match["info"]["participants"]:
                if participant["puuid"] != puuid:
                    continue
                game_start = dt.datetime.fromtimestamp(
                    match["info"]["gameStartTimestamp"] / 1000.0
                ).strftime("%Y-%m-%d %H:%M:%S")
                async with aconnect(self.bot.db_path) as db:
                    await db.execute(
                        "INSERT OR IGNORE INTO match_stats "
                        "(match_id, puuid, game_start, queue_id, champion, "
                        " win, kills, deaths, assists, duration_sec, patch_version, "
                        " position) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    await db.commit()
                inserted += 1
                break
        return inserted


async def setup(bot: commands.Bot):
    await bot.add_cog(Backfill(bot))
