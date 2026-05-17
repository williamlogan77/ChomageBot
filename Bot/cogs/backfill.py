"""Backfill per-match stats from Riot's Match-V5 into the match_stats table.

Runs inside the bot process so Riot API calls share the global rate
limiter in :mod:`utils.riot_client`. Triggered manually via admin slash
commands; the work is dispatched to a background asyncio task so the
command returns immediately and the rest of the bot keeps running.

Idempotent: match_id is the table's PRIMARY KEY and we skip fetching
matches already stored before calling Match-V5, so re-runs are cheap.
"""

import asyncio
import datetime as dt
import logging

import aiosqlite as sqa
import discord
from discord import app_commands
from discord.ext import commands
from utils.riot_client import get_match, get_match_ids

log = logging.getLogger(__name__)

# Match-V5 returns up to 100 IDs per page; one page is the default scope.
# Bump or paginate further for deeper history (~2y exposed by Riot).
DEFAULT_MATCHES_PER_PLAYER = 100


class Backfill(commands.Cog):
    """Admin commands to backfill match_stats from Riot's Match-V5."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        self._task: asyncio.Task | None = None
        self._progress: dict[str, int] = {}

    @app_commands.command(
        name="backfill_all",
        description="(admin) Backfill recent match_stats for every tracked player",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(count="Matches to attempt per player (max 100). Default 100.")
    async def backfill_all(self, ctx: discord.Interaction, count: int = DEFAULT_MATCHES_PER_PLAYER):
        await ctx.response.defer()

        if self._task is not None and not self._task.done():
            await ctx.followup.send("Backfill already running. Use /backfill_status.")
            return

        count = max(1, min(count, 100))

        async with sqa.connect(self.bot.db_path) as db:
            rows = await db.execute_fetchall(
                "SELECT puuid, league_username FROM league_players "
                "WHERE puuid IS NOT NULL AND puuid != ''"
            )

        if not rows:
            await ctx.followup.send("No players to backfill.")
            return

        self._progress = {name: -1 for _, name in rows}
        self._task = asyncio.create_task(self._do_backfill(list(rows), count))
        await ctx.followup.send(
            f"Backfilling {len(rows)} players (up to {count} matches each). "
            f"Use /backfill_status to check progress."
        )

    @app_commands.command(
        name="backfill_status",
        description="Report progress of the running (or last) backfill",
    )
    async def backfill_status(self, ctx: discord.Interaction):
        if self._task is None:
            await ctx.response.send_message("No backfill has been started.")
            return

        lines = []
        for name, inserted in self._progress.items():
            if inserted < 0:
                lines.append(f"  {name}: queued")
            else:
                lines.append(f"  {name}: {inserted} new matches")

        if self._task.done():
            try:
                self._task.result()
                header = "Backfill complete."
            except Exception as exc:
                header = f"Backfill errored: {exc!r}"
        else:
            header = "Backfill running..."

        body = "\n".join(lines[:25])
        more = f"\n  …and {len(lines) - 25} more" if len(lines) > 25 else ""
        await ctx.response.send_message(f"{header}\n```\n{body}{more}\n```")

    async def _do_backfill(self, players: list[tuple[str, str]], count: int) -> None:
        for puuid, name in players:
            try:
                inserted = await self._backfill_player(puuid, count)
                self._progress[name] = inserted
                self.bot.logging.info(f"Backfill: {name} +{inserted} matches")
            except Exception as exc:
                self._progress[name] = 0
                self.bot.logging.error(f"Backfill failed for {name}: {exc!r}")
        self.bot.logging.info("Backfill complete")

    async def _backfill_player(self, puuid: str, count: int) -> int:
        match_ids = await get_match_ids(puuid, count=count)
        if not match_ids:
            return 0

        # Skip matches we already have.
        async with sqa.connect(self.bot.db_path) as db:
            placeholders = ",".join("?" * len(match_ids))
            existing_rows = await db.execute_fetchall(
                f"SELECT match_id FROM match_stats WHERE match_id IN ({placeholders})",
                tuple(match_ids),
            )
            existing = {row[0] for row in existing_rows}

        to_fetch = [mid for mid in match_ids if mid not in existing]
        if not to_fetch:
            return 0

        inserted = 0
        async with sqa.connect(self.bot.db_path) as db:
            for mid in to_fetch:
                match = await get_match(mid)
                if match is None:
                    continue
                for participant in match["info"]["participants"]:
                    if participant["puuid"] != puuid:
                        continue
                    game_start = dt.datetime.fromtimestamp(
                        match["info"]["gameStartTimestamp"] / 1000.0
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    await db.execute(
                        "INSERT OR IGNORE INTO match_stats "
                        "(match_id, puuid, game_start, queue_id, champion, "
                        " win, kills, deaths, assists, duration_sec) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        ),
                    )
                    inserted += 1
                    break
            await db.commit()
        return inserted


async def setup(bot: commands.Bot):
    await bot.add_cog(Backfill(bot))
