import asyncio
import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands, tasks
from main import MyDiscordBot
from utils import db, leaderboard
from utils.loop_restart import restart_loop_later
from utils.rank_sorting_class import Ranker
from utils.riot_client import (
    RANKED_SOLO_QUEUE_ID,
    get_account_by_puuid,
    get_league_entries,
)
from utils.riot_stats import fetch_recent_kd

# Want to fetch ranks to post from the database
# want to fetch from rito every 30s

# Channel + threshold for the loss-streak callout. When a tracked player's
# most-recent N consecutive games are all losses, the bot pings them once
# in this channel. The ping rearms after they win again.
STREAK_PING_CHANNEL = 667751882260742167  # #general
STREAK_THRESHOLD = 7

# league_history.queue tag for this board's rows (also the column default).
SOLO_QUEUE = "RANKED_SOLO_5x5"


class FetchFromRiot(commands.Cog):
    def __init__(self, bot: MyDiscordBot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        self.post_ranks.start()
        # self.fetch_ranks_from_riot.start()
        self.previous_ranks = {}
        self.min_games_played = 0
        self.last_updated_by: list[str] = []
        self.streak_pinged: set[str] = set()
        self.previous_positions: dict[str, int] = {}
        self.post_ranks_last_fired: dt.datetime | None = None

        self.ranked_dict: dict | None = None

    def cog_unload(self) -> None:
        # discord.py does NOT cancel @tasks.loop tasks on cog unload —
        # without this, every hot reload (auto_reload / heartbeat) leaves
        # the old instance's loop running next to the new one's.
        self.post_ranks.cancel()

    async def fetch_users_rank(self, users):
        users_ranks = {}
        seen: set[str] = set()

        # League-entries uses platform routing (euw1), not regional (europe).
        for puuid, name, user_id in users:
            if puuid in seen:
                continue
            seen.add(puuid)
            self.bot.logging.info(f"Fetching rank for: {name}")
            user_rank = await get_league_entries(puuid)
            if user_rank is None:
                self.bot.logging.error(f"Failed to fetch rank for {name}")
                continue

            fivev5 = list(filter(lambda x: x["queueType"] == "RANKED_SOLO_5x5", user_rank))
            if len(fivev5) > 0:
                fivev5 = fivev5[0]
                fivev5["user_id"] = user_id
                fivev5["discord_name"] = name
                fivev5["sorted_rank"] = Ranker(
                    fivev5["tier"], fivev5["rank"], fivev5["leaguePoints"]
                )
                fivev5["GamesPlayed"] = fivev5["wins"] + fivev5["losses"]
                fivev5["WinRate"] = (fivev5["wins"] / fivev5["GamesPlayed"]) * 100
                fivev5["summonerName"] = await self.get_name(puuid)

                users_ranks[fivev5["summonerName"]] = fivev5
                if fivev5["GamesPlayed"] < self.min_games_played:
                    del users_ranks[fivev5["summonerName"]]
                    continue
            else:
                fivev5 = []

        return users_ranks

    # @tasks.loop(seconds=30)
    async def fetch_ranks_from_riot(self):
        rows = await db.fetchall(
            """SELECT puuid,
                    CASE WHEN nickname = '' THEN discord_tag ELSE nickname END,
                    discord_user_id
                FROM league_players
                    LEFT JOIN users ON user_id = discord_user_id"""
        )
        # Fetch current ranks and store them in a dict with updated values.
        # Per-player API failures are handled inside fetch_users_rank
        # (riot_client returns None rather than raising).
        self.ranked_dict = await self.fetch_users_rank(rows)
        return

    async def get_last_five_games(self, puuid):
        """Duo-aware Last 5 from actual match results (match_stats).

        A game is a duo game when another tracked player has a row for the
        same match on the same team (match_stats holds only tracked
        players). Sourced from match data instead of the old
        league_history diff reconstruction — exact per-game results and
        ordering; a game finished within the last ~5 min (stream interval)
        can show one refresh late. NULL team_id (rows predating the
        column, not yet backfilled) never counts as duo.
        """
        rows = await db.fetchall(
            "SELECT ms.win, EXISTS ("
            "    SELECT 1 FROM match_stats o"
            "    WHERE o.match_id = ms.match_id"
            "      AND o.team_id = ms.team_id"
            "      AND o.puuid <> ms.puuid"
            ") AS duo "
            "FROM match_stats ms "
            "WHERE ms.puuid = %s AND ms.queue_id = %s "
            "ORDER BY ms.game_start DESC LIMIT 5",
            (puuid, RANKED_SOLO_QUEUE_ID),
        )
        return leaderboard.build_last_five_with_duo(rows)

    async def get_recent_streak(self, puuid):
        """Return the count of consecutive losses ending at the most recent game.

        Looks at the last ~15 history rows (both legacy-summonerId-keyed
        and real-puuid-keyed). Returns 0 if the most recent game was a win
        or there's no history — see leaderboard.count_leading_losses for
        the in-cycle ordering caveat (false-negatives over false-positives
        for the ping).
        """
        rows = await leaderboard.fetch_history_wl(puuid, SOLO_QUEUE, 15, legacy_dual_key=True)
        return leaderboard.count_leading_losses(rows)

    async def send_streak_ping(self, user_id: int, streak: int, puuid: str) -> None:
        channel = self.bot.get_channel(STREAK_PING_CHANNEL)
        if channel is None:
            self.bot.logging.warning(
                f"Streak channel {STREAK_PING_CHANNEL} not found, skipping ping"
            )
            return
        # Pull K/D over roughly the streak window (capped at 20). If the
        # ratio is below 1.0, mock with the raw kills/deaths; otherwise
        # the emoji + ping speak for themselves.
        kills, deaths, _assists, _wins, games = await fetch_recent_kd(
            puuid, count=min(max(streak, 5), 20)
        )
        extra = ""
        if games > 0 and deaths > 0 and (kills / deaths) < 1.0:
            extra = f" {kills}/{deaths}"
        # \U0001FAF5 = pointing at viewer, \U0001F602 = face with tears of joy
        await channel.send(f"\U0001faf5\U0001f602 <@{user_id}>{extra}")

    async def get_name(self, puuid):
        row = await db.fetchone(
            "SELECT league_username FROM league_players WHERE puuid = %s",
            (puuid,),
        )
        if row is not None:
            return row[0]
        return "Unknown"

    async def check_name(self, puuid):
        row = await db.fetchone(
            "SELECT puuid, league_username FROM league_players WHERE puuid = %s",
            (puuid,),
        )
        if row is None:
            return
        puuid, stored_name = row

        account = await get_account_by_puuid(puuid)
        if account is None:
            return  # transient account-v1 failure; retry next cycle
        name = account["gameName"]

        if name != stored_name:
            self.bot.logging.info(f"updating {stored_name} to {name}")
            await db.execute(
                "UPDATE league_players SET league_username = %s WHERE puuid = %s",
                (name, puuid),
            )

    @tasks.loop(seconds=120)
    async def post_ranks(self):
        await self.bot.wait_until_ready()
        await self.fetch_ranks_from_riot()
        # if len(self.previous_ranks) == 0:
        #     self.previous_ranks = self.ranked_dict
        #     return

        if (self.ranked_dict != self.previous_ranks) or (not self.previous_ranks):
            updated_users: list[str] = []
            for user in self.ranked_dict.keys():
                await self.check_name(self.ranked_dict[user]["puuid"])

                if self.previous_ranks and user in self.previous_ranks.keys():
                    if (
                        self.ranked_dict[user]["leaguePoints"]
                        != self.previous_ranks[user]["leaguePoints"]
                    ):
                        print(f"{user} updated", flush=True)
                        await self.update_table(user, self.ranked_dict[user])
                        updated_users.append(user)

            if updated_users:
                self.last_updated_by = updated_users

            # Streak ping: only check players whose LP changed this cycle so
            # we don't spam Riot's DB on every refresh. Posts once when a
            # player crosses STREAK_THRESHOLD; rearms when their streak breaks.
            for user in updated_users:
                posting_data = self.ranked_dict[user]
                streak = await self.get_recent_streak(posting_data["puuid"])
                if streak >= STREAK_THRESHOLD and user not in self.streak_pinged:
                    await self.send_streak_ping(
                        posting_data["user_id"], streak, posting_data["puuid"]
                    )
                    self.streak_pinged.add(user)
                elif streak < STREAK_THRESHOLD and user in self.streak_pinged:
                    self.streak_pinged.discard(user)

            self.bot.logging.info("Posting ranks")
            self.previous_ranks = self.ranked_dict
            to_post = filter(
                lambda x: isinstance(x, dict),
                [data for data in self.ranked_dict.values()],
            )
            # Sort by rank
            sorted_results = sorted(to_post, key=lambda d: d["sorted_rank"], reverse=True)

            # Sort by winrate
            # sorted_results = sorted(to_post,
            #                         key=lambda d: d["WinRate"],
            #                         reverse=True)

            # apex_omits_games_word: this board's apex entries have always
            # read "Played: N with a ..." — see utils/leaderboard.py.
            # previous_positions feeds next cycle's arrow comparisons.
            output_list, self.previous_positions = await leaderboard.render_board_entries(
                sorted_results,
                self.previous_positions,
                self.last_updated_by,
                self.get_last_five_games,
                apex_omits_games_word=True,
            )
            if output_list:
                # Key at the top of the board (William's call) — explains
                # the per-entry squares, which carry no label of their own.
                output_list.insert(
                    0,
                    "-# Key: \U0001f7e9 solo win · ❎ duo win · "
                    "\U0001f7e5 solo loss · ❌ duo loss",
                )

            paste = self.bot.get_channel(919981835428179988)
            # Blocks, not a joined string — the board can exceed Discord's
            # 2000-char message cap and gets split on entry boundaries.
            await leaderboard.wipe_and_post(paste, output_list, self.bot.logging)

        # Watchdog input: heartbeat cog reads this and reloads us if it
        # goes stale (typically because a Gateway disconnect left the
        # @tasks.loop in a state where it never fires again).
        self.post_ranks_last_fired = dt.datetime.now()
        return

    @post_ranks.error
    async def post_ranks_error(self, exc: BaseException) -> None:
        """Auto-restart post_ranks on unhandled error.

        Default @tasks.loop behaviour on exception is to log + stop the
        loop. That leaves the leaderboard frozen until manual recovery.
        The restart must run detached — this callback executes inside the
        dying loop task, where is_running() is still True (see
        utils/loop_restart.py).
        """
        self.bot.logging.error(f"post_ranks errored: {exc!r}, restarting in 60s")
        restart_loop_later(
            self.post_ranks,
            name="post_ranks",
            log=self.bot.logging,
            still_active=lambda: self.bot.get_cog("FetchFromRiot") is self,
        )

    @app_commands.command(
        name="set_minimum_games_played",
        description="Set the minimum amount of games played for a user to appear on the leaderboard",
    )
    @app_commands.describe(
        number="The number of games a user must have played to appear on the leaderboard"
    )
    async def min_games_played_setter(self, ctx: discord.Interaction, number: int):
        await ctx.response.defer()
        if not isinstance(number, int) or number > 200:
            await ctx.followup.send("please enter a reasonable number....", ephemeral=True)
        self.bot.logging.info(
            f"Updating minimum number of games played from {self.min_games_played} to {number}"
        )

        await ctx.followup.send(
            f"updating minimum number of games played from {self.min_games_played} to {number}"
        )
        self.min_games_played = number

        return

    @app_commands.command(
        name="refresh_ranks", description="Refreshes the league ranking discord channel"
    )
    async def refresh_ranks(self, ctx: discord.Interaction):
        await ctx.response.defer()
        msg = await ctx.followup.send("Refreshing ranks...", wait=True, ephemeral=True)
        await self.post_ranks()
        await msg.edit(content="Sucessfully refreshed rank leaderboard")

    @app_commands.command(
        name="stop_rank_refresh",
        description="Pauses the league ranking table refreshing",
    )
    async def stop_ranks(self, ctx: discord.Interaction):
        await ctx.response.defer()
        msg = await ctx.followup.send("Stopping...", wait=True, ephemeral=True)
        self.post_ranks.stop()
        await asyncio.sleep(30)
        self.post_ranks.cancel()
        await msg.edit(content="Stopped refreshing of ranks")
        self.bot.logging.info("Stopped the refreshing of ranks posting")

    @app_commands.command(
        name="start_rank_refresh",
        description="Restarts the league ranking table refreshing",
    )
    async def start_ranks(self, ctx: discord.Interaction):
        await ctx.response.defer()
        msg = await ctx.followup.send("Starting...", wait=True, ephemeral=True)
        if self.post_ranks.is_running():
            await msg.edit(content="Already running, cannot start")
        else:
            self.post_ranks.start()
            await msg.edit(content="Started refreshing of ranks")
            self.bot.logging.info("Started rank refresh")

    # Needs updating to grab last match from the table
    async def update_table(self, user, user_stats_dict):
        # Keeps this board's historical logging/except shape (including the
        # harmless "Exception as list index out of range" info line on a
        # player's first-ever insert) around the shared history helpers.
        try:
            self.bot.logging.info(f"updating table, logging {user_stats_dict}")
            last_values = await leaderboard.latest_history_wl(user_stats_dict["puuid"], SOLO_QUEUE)
        except Exception as e:
            self.bot.logging.error(f"Failed to update table with error: {e}")
            return
        try:
            if last_values[0] == (
                user_stats_dict["wins"],
                user_stats_dict["losses"],
            ):
                return
        except Exception as e:
            self.bot.logging.info(f"Exception as {e}")

        await leaderboard.insert_history_snapshot(user_stats_dict, SOLO_QUEUE)
        return


async def setup(bot: MyDiscordBot):
    await bot.add_cog(FetchFromRiot(bot))
