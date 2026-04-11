from discord.ext import commands, tasks
import aiosqlite as sqa
from utils.rank_sorting_class import Ranker  # pylint: disable=E0401,E0611
from main import MyDiscordBot  # pylint: disable=E0401
from discord import app_commands
import asyncio
import discord
import aiohttp
import os

import logging

# Want to fetch ranks to post from the database
# want to fetch from rito every 30s

log = logging.getLogger(__name__)

class FetchFromRiot(commands.Cog):
    def __init__(self, bot: MyDiscordBot):
        self.bot = bot
        self.post_ranks.start()  # pylint: disable=E1101
        # self.fetch_ranks_from_riot.start() # pylint: disable=E1101
        self.previous_ranks = {}
        self.min_games_played = 0

        self.ranked_dict: dict = None # type: ignore

    async def fetch_users_rank(self, users):
        users_ranks = {}
        looked_up_users = []
        
        for puuid, name, user_id in users:
            try:
                response = await self.bot.apiutils.get_league_queues_entries(puuid)
            except Exception as e:
                log.error(f"Failed to fetch rank for {name}: {type(e).__name__} - {e}")
                break
            if response is None:
                continue

            fivev5 = list(
                filter(lambda x: x["queueType"] == "RANKED_SOLO_5x5", response)
            )
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
        # log.info("fetching ranks")
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
                    #CHANGE
                    log.error(
                        f"Error of: {exc}, trying again in 60 seconds"
                    )
                    await asyncio.sleep(60)
        return

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

            try:
                response = await self.bot.apiutils.get_account_by_puuid(puuid)
            except Exception as e:
                log.error(f"Failed to fetch rank for {name}: {type(e).__name__} - {e}")
            name = response["gameName"]

            if name != stored_name:
                log.info(f"updating {stored_name} to {name}")
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
            for user in self.ranked_dict.keys():
                await self.check_name(self.ranked_dict[user]["puuid"])

                if self.previous_ranks and user in self.previous_ranks.keys():
                    if (
                        self.ranked_dict[user]["leaguePoints"]
                        != self.previous_ranks[user]["leaguePoints"]
                    ):
                        print(f"{user} updated", flush=True)
                        await self.update_table(user, self.ranked_dict[user])

            log.info("Posting ranks")
            self.previous_ranks = self.ranked_dict
            to_post = filter(
                lambda x: type(x) == type({}),
                [data for data in self.ranked_dict.values()],
            )
            # Sort by rank
            sorted_results = sorted(
                to_post, key=lambda d: d["sorted_rank"], reverse=True
            )

            # Sort by winrate
            # sorted_results = sorted(to_post,
            #                         key=lambda d: d["WinRate"],
            #                         reverse=True)

            output_list = []
            for index, posting in enumerate(sorted_results):
                if posting["tier"].title() == "Master":
                    post = (
                        str(index + 1)
                        + ". "
                        + posting["summonerName"]
                        + f" - <@{posting['user_id']}>"
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
                    )
                else:
                    post = (
                        str(index + 1)
                        + ". "
                        + posting["summonerName"]
                        + f" - <@{posting['user_id']}>"
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
                    )
                output_list.append(post)

            paste = self.bot.get_channel(919981835428179988)    #wow this is dodgy
            #Really we need a coomand to add a changle to the bot whhich it prints too. Maybe a defautlt value tooo?
            try:
                async for message in paste.history():
                    await message.delete()
            except discord.errors.Forbidden:
                log.warning("Missing permissions to delete messages, skipping cleanup")

            if len(output_list) != 0:
                to_send = "\n".join(output_list)
                await paste.send(to_send, silent=True)

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
        log.info(
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
        self.post_ranks.stop()  # pylint: disable=E1101
        await asyncio.sleep(30)
        self.post_ranks.cancel()  # pylint: disable=E1101
        await msg.edit(content="Stopped refreshing of ranks")
        log.info("Stopped the refreshing of ranks posting")

    @app_commands.command(
        name="start_rank_refresh",
        description="Restarts the league ranking table refreshing",
    )
    async def start_ranks(self, ctx: discord.Interaction):
        await ctx.response.defer()
        msg = await ctx.followup.send("Starting...", wait=True, ephemeral=True)
        if self.post_ranks.is_running():  # pylint: disable=E1101
            await msg.edit(content="Already running, cannot start")
        else:
            self.post_ranks.start()  # pylint: disable=E1101
            await msg.edit(content="Started refreshing of ranks")
            log.info("Started rank refresh")

    # Needs updating to grab last match from the table
    async def update_table(self, user, user_stats_dict):
        async with sqa.connect(database=self.bot.db_path) as connection:
            try:
                log.info(f"updating table, logging {user_stats_dict}")
                last_values = await connection.execute_fetchall(
                    "SELECT * FROM league_history WHERE puuid = ? ORDER BY id DESC",
                    (user_stats_dict["puuid"],),
                )
            except Exception as e:
                log.error(f"Failed to update table with error: {e}")
                return
        try:
            if last_values[0][-2:] == (
                user_stats_dict["wins"],
                user_stats_dict["losses"],
            ):
                return
        except Exception as e:
            log.info(f"Exception as {e}")

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
