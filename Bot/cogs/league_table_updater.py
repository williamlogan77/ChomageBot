import asyncio

import aiosqlite as sqa
import discord
from discord import app_commands
from discord.ext import commands, tasks
from main import MyDiscordBot
from pantheon.utils.exceptions import ServerError
from utils.rank_sorting_class import Ranker
from utils.riot_client import get_json
from utils.riot_stats import fetch_recent_kd

# Want to fetch ranks to post from the database
# want to fetch from rito every 30s

# Channel + threshold for the loss-streak callout. When a tracked player's
# most-recent N consecutive games are all losses, the bot pings them once
# in this channel. The ping rearms after they win again.
STREAK_PING_CHANNEL = 667751882260742167  # #general
STREAK_THRESHOLD = 7


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

        self.ranked_dict: dict | None = None

    async def fetch_users_rank(self, users):
        users_ranks = {}
        seen: set[str] = set()

        # League-entries uses platform routing (euw1), not regional (europe).
        for puuid, name, user_id in users:
            if puuid in seen:
                continue
            seen.add(puuid)
            self.bot.logging.info(f"Fetching rank for: {name}")
            league_url = f"https://euw1.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
            status, user_rank = await get_json(league_url)
            if status != 200 or user_rank is None:
                self.bot.logging.error(f"Failed to fetch rank for {name}: HTTP {status}")
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

    async def fetch_ranks_from_db(self):
        async with sqa.connect(self.bot.db_path) as connection:
            async with connection.execute_fetchall(
                """SELECT league_username,
                        lp,
                        division,
                        tier
                    FROM (
                            SELECT DISTINCT puuid,
                                MAX(timestamp),
                                lp,
                                division,
                                tier
                            FROM league_history
                            GROUP BY puuid
                        ) as history,
                        (
                            SELECT *
                            FROM league_players
                        ) as players
                    WHERE history.puuid = players.leagueId"""
            ) as cursor:
                db_dict = {}
                for user in cursor:
                    current_user, lp, tier, div = user
                    db_dict[current_user] = {}
                    db_dict[current_user]["queueType"] = "RANKED_SOLO_5x5"
                    db_dict[current_user]["leaguePoints"] = lp
                    db_dict[current_user]["tier"] = tier
                    db_dict[current_user]["rank"] = div

            return db_dict

    # @tasks.loop(seconds=30)
    async def fetch_ranks_from_riot(self):
        # self.bot.logging.info("fetching ranks")
        # fetch from db
        # await self.fetch_ranks_from_db()
        async with sqa.connect(self.bot.db_path) as connection:
            async with connection.execute_fetchall(
                """SELECT puuid,
                        IIF(nickname = '', discord_tag, nickname),
                        discord_user_id
                    FROM (
                            SELECT *
                            FROM league_players
                                LEFT JOIN users ON user_id = discord_user_id
                        )"""
            ) as cursor:
                # Fetch current ranks and store them in a dict with updated values
                try:
                    self.ranked_dict = await self.fetch_users_rank(cursor)
                except ServerError as exc:
                    self.bot.logging.error(f"Error of: {exc}, trying again in 60 seconds")
                    await asyncio.sleep(60)
        return

    async def get_last_five_games(self, puuid):
        # Older history rows are keyed by the legacy encrypted summoner ID
        # (league_players.leagueId, ~47 chars), newer rows by the real
        # puuid (~78 chars). Query both so legacy-tracked players still
        # surface their game history.
        async with sqa.connect(self.bot.db_path) as connection:
            async with connection.execute(
                "SELECT wins, losses FROM league_history "
                "WHERE puuid IN ("
                "    SELECT leagueId FROM league_players WHERE puuid = ?"
                "    UNION"
                "    SELECT puuid    FROM league_players WHERE puuid = ?"
                ") "
                "AND wins IS NOT NULL AND losses IS NOT NULL "
                "ORDER BY id DESC LIMIT 6",
                (puuid, puuid),
            ) as cursor:
                rows = await cursor.fetchall()
        if not rows:
            return ""
        sequence = []
        for i in range(len(rows) - 1):
            newer_w, newer_l = rows[i]
            older_w, older_l = rows[i + 1]
            delta_w = max(newer_w - older_w, 0)
            delta_l = max(newer_l - older_l, 0)
            sequence.extend(["\U0001f7e9"] * delta_w + ["\U0001f7e5"] * delta_l)
        # Implicit (0,0) baseline for brand-new accounts: only valid when the
        # oldest fetched row's cumulative game count is small.
        oldest_w, oldest_l = rows[-1]
        if oldest_w + oldest_l <= 5:
            sequence.extend(["\U0001f7e9"] * oldest_w + ["\U0001f7e5"] * oldest_l)
        # Cap to last 5; reverse so newest game is on the right.
        return "".join(reversed(sequence[:5]))

    async def get_recent_streak(self, puuid):
        """Return the count of consecutive losses ending at the most recent game.

        Looks at the last ~15 history rows for the player (covering both
        legacy-summonerId-keyed and real-puuid-keyed rows), reconstructs the
        outcome sequence (newest first), and counts leading losses. Returns
        0 if the most recent game was a win or there's no history.

        Within a multi-game-per-cycle diff we don't know the in-cycle order;
        we conservatively place wins BEFORE losses in the sequence, which
        means a (1W, 2L) diff at the head reports a 0-game loss streak even
        if losses could have come last. False-negatives over false-positives
        for the ping.
        """
        async with sqa.connect(self.bot.db_path) as connection:
            async with connection.execute(
                "SELECT wins, losses FROM league_history "
                "WHERE puuid IN ("
                "    SELECT leagueId FROM league_players WHERE puuid = ?"
                "    UNION"
                "    SELECT puuid    FROM league_players WHERE puuid = ?"
                ") "
                "AND wins IS NOT NULL AND losses IS NOT NULL "
                "ORDER BY id DESC LIMIT 15",
                (puuid, puuid),
            ) as cursor:
                rows = await cursor.fetchall()
        if len(rows) < 2:
            return 0
        sequence: list[str] = []
        for i in range(len(rows) - 1):
            newer_w, newer_l = rows[i]
            older_w, older_l = rows[i + 1]
            delta_w = max(newer_w - older_w, 0)
            delta_l = max(newer_l - older_l, 0)
            sequence.extend(["W"] * delta_w + ["L"] * delta_l)
        streak = 0
        for outcome in sequence:
            if outcome == "L":
                streak += 1
            else:
                break
        return streak

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
        async with sqa.connect(self.bot.db_path) as connection:
            async with connection.execute_fetchall(
                "SELECT league_username FROM league_players WHERE puuid = ?",
                (puuid,),
            ) as cursor:
                if cursor and len(cursor) > 0:
                    return cursor[0][0]
                else:
                    return "Unknown"

    async def check_name(self, puuid):
        async with sqa.connect(self.bot.db_path) as connection:
            async with connection.execute_fetchall(
                "SELECT puuid, league_username FROM league_players WHERE puuid = ?",
                (puuid,),
            ) as cursor:
                if not cursor or len(cursor) == 0:
                    return
                puuid, stored_name = cursor[0]

            name = (await self.bot.lolapi.get_account_by_puuId(puuid))["gameName"]

            if name != stored_name:
                self.bot.logging.info(f"updating {stored_name} to {name}")
                await connection.execute(
                    "UPDATE league_players SET league_username = ? WHERE puuid = ?",
                    (name, puuid),
                )
                await connection.commit()

    @tasks.loop(seconds=120)
    async def post_ranks(self):
        await self.bot.wait_until_ready()
        await self.fetch_ranks_from_riot()
        # self.ranked_dict = await self.fetch_ranks_from_db()
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

            output_list = []
            for index, posting in enumerate(sorted_results):
                updated_flag = (
                    " \U0001f6a9" if posting["summonerName"] in self.last_updated_by else ""
                )
                last_five = await self.get_last_five_games(posting["puuid"])
                last_five_line = f"Last 5: {last_five}\n" if last_five else ""
                if posting["tier"].title() == "Master":
                    post = (
                        str(index + 1)
                        + ". "
                        + posting["summonerName"]
                        + f" - <@{posting['user_id']}>"
                        + updated_flag
                        + "\n"
                        + "Rank: "
                        + posting["tier"].title()
                        + " "
                        + str(posting["leaguePoints"])
                        + "lp"
                        + "\n"
                        + "Played: "
                        + str(posting["GamesPlayed"])
                        + " with a "
                        + str("{:.2f}".format(posting["WinRate"]))
                        + "% winrate"
                        + "\n"
                        + last_five_line
                    )
                else:
                    post = (
                        str(index + 1)
                        + ". "
                        + posting["summonerName"]
                        + f" - <@{posting['user_id']}>"
                        + updated_flag
                        + "\n"
                        + "Rank: "
                        + posting["tier"].title()
                        + " "
                        + posting["rank"]
                        + " "
                        + str(posting["leaguePoints"])
                        + "lp"
                        + "\n"
                        + "Played: "
                        + str(posting["GamesPlayed"])
                        + " games with a "
                        + str("{:.2f}".format(posting["WinRate"]))
                        + "% winrate"
                        + "\n"
                        + last_five_line
                    )
                output_list.append(post)

            paste = self.bot.get_channel(919981835428179988)
            try:
                async for message in paste.history():
                    await message.delete()
            except discord.errors.Forbidden:
                self.bot.logging.warning("Missing permissions to delete messages, skipping cleanup")

            if len(output_list) != 0:
                to_send = "\n".join(output_list)
                await paste.send(
                    to_send,
                    silent=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        return

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
        async with sqa.connect(database=self.bot.db_path) as connection:
            try:
                self.bot.logging.info(f"updating table, logging {user_stats_dict}")
                last_values = await connection.execute_fetchall(
                    "SELECT * FROM league_history WHERE puuid = ? ORDER BY id DESC",
                    (user_stats_dict["puuid"],),
                )
            except Exception as e:
                self.bot.logging.error(f"Failed to update table with error: {e}")
                return
        try:
            if last_values[0][-2:] == (
                user_stats_dict["wins"],
                user_stats_dict["losses"],
            ):
                return
        except Exception as e:
            self.bot.logging.info(f"Exception as {e}")

        async with sqa.connect(self.bot.db_path) as connection:
            await connection.execute(
                "INSERT INTO league_history (puuid, lp, division, tier, wins, losses) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user_stats_dict["puuid"],
                    str(user_stats_dict["leaguePoints"]),
                    user_stats_dict["rank"],
                    user_stats_dict["tier"],
                    user_stats_dict["wins"],
                    user_stats_dict["losses"],
                ),
            )
            await connection.commit()
        return


async def setup(bot: MyDiscordBot):
    await bot.add_cog(FetchFromRiot(bot))
