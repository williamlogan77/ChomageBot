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
        self.watch_endings.start()

    def cog_unload(self) -> None:
        # discord.py does NOT cancel @tasks.loop tasks on cog unload —
        # without this, every hot reload leaves the old loop running.
        self.poll_live.cancel()
        self.watch_endings.cancel()

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
            await self._record_live(puuid, game)
        # Backstop for rows nothing refreshed or deleted (account removed
        # from tracking, persistent API failure, loop gaps).
        await db.execute(
            "DELETE FROM live_games WHERE seen_at < now() - make_interval(mins => %s)",
            (STALE_MINUTES,),
        )
        if in_game:
            self.bot.logging.info(f"Live games: {in_game} tracked account(s) in game")
        self.poll_live_last_fired = dt.datetime.now()

    async def _record_live(self, puuid: str, game: dict) -> None:
        """Upsert the player's current game and drop rows for any previous
        game — a player can only be in one game, and finish-A-queue-into-B
        must not show them in both."""
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
        await db.execute(
            "DELETE FROM live_games WHERE puuid = %s AND game_id <> %s",
            (puuid, game.get("gameId")),
        )

    @tasks.loop(minutes=1)
    async def watch_endings(self):
        """Fast end-of-game detector: fresh LP on the board within ~a minute.

        People finish a game and immediately check the board for the
        movement, so waiting for the 4-minute discovery poll + the next
        2-minute board cycle reads as "the board is stale". This loop
        only polls accounts CURRENTLY in live_games (zero Riot calls
        when nobody is playing) and, the moment a game ends, updates the
        table and triggers the board loop right away — its entries gating
        sees the game vanish from live_games and fresh-fetches the players.

        A game "ends" in two shapes: the player is no longer in any game
        (spectator 404), OR they're already in a DIFFERENT game — the
        finish-then-requeue case, where mere presence polling would never
        show a gap. A changed gameId IS the end of the previous game.
        """
        await self.bot.wait_until_ready()
        try:
            rows = await db.fetchall("SELECT puuid, game_id FROM live_games")
        except Exception:
            return  # table missing pre-restart; the main poll logs enough
        known_games: dict[str, set[int]] = {}
        for puuid, game_id in rows:
            known_games.setdefault(puuid, set()).add(game_id)
        ended: list[str] = []
        for puuid, game_ids in known_games.items():
            known, game = await get_active_game(puuid)
            if not known:
                continue
            if game is None or not game.get("gameId"):
                await db.execute("DELETE FROM live_games WHERE puuid = %s", (puuid,))
                ended.append(puuid)
            elif game.get("gameId") not in game_ids:
                # Already in the NEXT game — the previous one ended.
                await self._record_live(puuid, game)
                ended.append(puuid)
        if ended:
            self.bot.logging.info(
                f"Live: {len(ended)} game(s) just ended — refreshing the board now"
            )
            board = self.bot.get_cog("FetchFromRiot")
            if board is not None:
                # Announce the finishes directly — authoritative, and immune
                # to the gate's diff state being reset by a sweep or reload.
                board.note_finished(ended)
                await board.post_ranks()

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

    @watch_endings.error
    async def watch_endings_error(self, exc: BaseException) -> None:
        self.bot.logging.error(f"watch_endings errored: {exc!r}, restarting in 60s")
        restart_loop_later(
            self.watch_endings,
            name="watch_endings",
            log=self.bot.logging,
            still_active=lambda: self.bot.get_cog("LiveGames") is self,
        )


async def setup(bot: MyDiscordBot):
    await bot.add_cog(LiveGames(bot))
