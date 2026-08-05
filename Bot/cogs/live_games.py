"""Live-game tracking: spectator-v5 polled for every tracked account.

Every POLL_MINUTES the loop asks Riot whether each tracked player is in
a game right now (one request per unique puuid through the shared
riot_client budget — "not in game" is a quiet 404). Live games land in
the live_games table (one row per game+player, raw payload included)
and rows go stale-pruned once the game ends, so the table always
mirrors "who is playing at this moment". The dashboard's /live page is
the consumer; the bot itself posts nothing.
"""

import datetime as dt

from discord.ext import commands, tasks
from main import MyDiscordBot
from psycopg.types.json import Jsonb
from utils import db
from utils.loop_restart import restart_loop_later
from utils.riot_client import get_active_game

POLL_MINUTES = 4

# Rows older than two polls (plus slack) belong to finished games.
STALE_MINUTES = POLL_MINUTES * 2 + 1

# Accounts with no recorded game in this many days only get polled every
# DORMANT_EVERY_CYCLES cycles (~16 min): roughly half the roster is
# dormant at any time, and spectator polls come out of the shared Riot
# budget. Trade-off: a dormant player's FIRST live game can be seen up
# to ~16 minutes late — after it finishes they're active again and back
# on the fast cadence. Accounts currently in live_games always stay on
# the fast cadence so an in-progress game keeps refreshing.
DORMANT_AFTER_DAYS = 14
DORMANT_EVERY_CYCLES = 4


class LiveGames(commands.Cog):
    def __init__(self, bot: MyDiscordBot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        # Watchdog input, same contract as the other loops
        # (cogs/heartbeat.py reloads us if this goes stale).
        self.poll_live_last_fired: dt.datetime | None = None
        self._cycle = 0
        self.poll_live.start()

    def cog_unload(self) -> None:
        # discord.py does NOT cancel @tasks.loop tasks on cog unload —
        # without this, every hot reload leaves the old loop running.
        self.poll_live.cancel()

    @tasks.loop(minutes=POLL_MINUTES)
    async def poll_live(self):
        await self.bot.wait_until_ready()
        rows = await db.fetchall(
            "SELECT DISTINCT puuid FROM league_players WHERE puuid IS NOT NULL AND puuid != ''"
        )
        self._cycle += 1
        fast_cadence = {
            r[0]
            for r in await db.fetchall(
                "SELECT DISTINCT puuid FROM match_stats "
                "WHERE game_start > now() - make_interval(days => %s) "
                "UNION SELECT puuid FROM live_games",
                (DORMANT_AFTER_DAYS,),
            )
        }
        in_game = 0
        for (puuid,) in rows:
            if puuid not in fast_cadence and self._cycle % DORMANT_EVERY_CYCLES:
                continue
            known, game = await get_active_game(puuid)
            if not known:
                # Transient API failure — leave existing rows alone; the
                # staleness backstop below covers a persistent outage.
                continue
            if game is None or not game.get("gameId"):
                # Authoritative "not in a game": remove immediately rather
                # than letting a finished game linger until stale-pruned.
                await db.execute("DELETE FROM live_games WHERE puuid = %s", (puuid,))
                continue
            in_game += 1
            start_ms = game.get("gameStartTime") or 0
            # 0 during the loading screen — better no timestamp than 1970.
            started = dt.datetime.fromtimestamp(start_ms / 1000, dt.UTC) if start_ms > 0 else None
            await db.execute(
                "INSERT INTO live_games (game_id, puuid, queue_id, game_start, payload, seen_at) "
                "VALUES (%s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (game_id, puuid) DO UPDATE SET "
                "payload = EXCLUDED.payload, seen_at = now()",
                (
                    game.get("gameId"),
                    puuid,
                    game.get("gameQueueConfigId"),
                    started,
                    Jsonb(game),
                ),
            )
            # A player can only be in one game — drop any row from a
            # previous game they've since left (finish A -> queue into B
            # would otherwise show them in both until the prune).
            await db.execute(
                "DELETE FROM live_games WHERE puuid = %s AND game_id <> %s",
                (puuid, game.get("gameId")),
            )
        # Backstop for rows nothing refreshed or deleted (account removed
        # from tracking, persistent API failure, loop gaps).
        await db.execute(
            "DELETE FROM live_games WHERE seen_at < now() - make_interval(mins => %s)",
            (STALE_MINUTES,),
        )
        if in_game:
            self.bot.logging.info(f"Live games: {in_game} tracked account(s) in game")
        self.poll_live_last_fired = dt.datetime.now()

    @poll_live.error
    async def poll_live_error(self, exc: BaseException) -> None:
        """Auto-restart on unhandled error — default @tasks.loop behaviour
        is log + stop, which would silently end live tracking."""
        self.bot.logging.error(f"poll_live errored: {exc!r}, restarting in 60s")
        restart_loop_later(
            self.poll_live,
            name="poll_live",
            log=self.bot.logging,
            still_active=lambda: self.bot.get_cog("LiveGames") is self,
        )


async def setup(bot: MyDiscordBot):
    await bot.add_cog(LiveGames(bot))
