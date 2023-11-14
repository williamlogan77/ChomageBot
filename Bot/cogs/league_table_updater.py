from discord.ext import commands, tasks
import aiosqlite as sqa
from utils.rank_sorting_class import Ranker  # pylint: disable=E0401,E0611
from main import MyDiscordBot
from pantheon.utils.exceptions import RateLimit, Timeout, ServerError
from discord import app_commands
import asyncio
import discord

# Want to fetch ranks to post from the database
# want to fetch from rito every 30s


class FetchFromRiot(commands.Cog):
    def __init__(self, bot: MyDiscordBot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        self.post_ranks.start()  # pylint: disable=E1101
        # self.fetch_ranks_from_riot.start() # pylint: disable=E1101
        self.previous_ranks = {}

        self.ranked_dict = None

    async def fetch_users_rank(self, users):
        users_ranks = {}
        looked_up_users = []
        for user, name in users:
            while user not in looked_up_users:
                try:
                    user_rank = await self.bot.lolapi.get_league_position(user)
                    looked_up_users.append(user)
                except (RateLimit, Timeout) as limited:
                    if isinstance(limited, RateLimit):
                        self.bot.logging.warning(
                            f"Rate limited on {user, name}, waiting {limited.timeToWait} seconds"
                        )
                        await asyncio.sleep(int(limited.timeToWait))
                    else:
                        print("Timed out", flush=True)
                        await asyncio.sleep(10)

            fivev5 = list(
                filter(lambda x: x["queueType"] == "RANKED_SOLO_5x5", user_rank)
            )
            if len(fivev5) > 0:
                fivev5 = fivev5[0]
                fivev5["discord_name"] = name
                fivev5["sorted_rank"] = Ranker(
                    fivev5["tier"], fivev5["rank"], fivev5["leaguePoints"]
                )
                fivev5["GamesPlayed"] = fivev5["wins"] + fivev5["losses"]
                fivev5["WinRate"] = (fivev5["wins"] / fivev5["GamesPlayed"]) * 100

                users_ranks[fivev5["summonerName"]] = fivev5
            else:
                fivev5 = []

        return users_ranks

    async def fetch_ranks_from_db(self):
        async with sqa.connect(self.bot.db_path) as connection:
            async with connection.execute_fetchall(
                "SELECT league_username, lp, division, tier FROM (SELECT DISTINCT puuid, MAX(timestamp), lp, division, tier FROM league_history GROUP BY puuid) as history, (SELECT * FROM league_players) as players WHERE history.puuid = players.puuid"
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

    @tasks.loop(seconds=30)
    async def fetch_ranks_from_riot(self):
        self.bot.logging.info("fetching ranks")
        # fetch from db
        # await self.fetch_ranks_from_db()
        async with sqa.connect(self.bot.db_path) as connection:
            async with connection.execute_fetchall(
                "SELECT puuid, IIF(nickname='', discord_tag, nickname) FROM (SELECT * FROM league_players LEFT JOIN users ON user_id = discord_user_id)"
            ) as cursor:
                # Fetch current ranks and store them in a dict with updated values
                try:
                    self.ranked_dict = await self.fetch_users_rank(cursor)
                except ServerError as exc:
                    self.bot.logging.error(
                        f"Error of: {exc}, trying again in 10 seconds"
                    )
                    asyncio.sleep(10)
                # print(self.ranked_dict, flush=True)

        return

    async def check_name(self, puuid, name):
        async with sqa.connect(self.bot.db_path) as connection:
            async with connection.execute_fetchall(
                "SELECT puuid, league_username FROM league_players WHERE puuid = ?",
                (puuid,),
            ) as cursor:
                puuid, stored_name = cursor[0]
            if name != stored_name:
                self.bot.logger.info(f"updating {stored_name} to {name}", flush=True)
                await connection.execute(
                    "UPDATE league_players SET league_username = ? WHERE puuid = ?",
                    (name, puuid),
                )
                await connection.commit()

    @tasks.loop(seconds=10)
    async def post_ranks(self):
        await self.bot.wait_until_ready()
        # await self.fetch_ranks()
        self.ranked_dict = await self.fetch_ranks_from_db()
        # if len(self.previous_ranks) == 0:
        #     self.previous_ranks = self.ranked_dict
        #     return

        if (self.ranked_dict != self.previous_ranks) or (not self.previous_ranks):
            for user in self.ranked_dict.keys():
                await self.check_name(
                    self.ranked_dict[user]["summonerId"],
                    self.ranked_dict[user]["summonerName"],
                )

                if self.previous_ranks and user in self.previous_ranks.keys():
                    if (
                        self.ranked_dict[user]["leaguePoints"]
                        != self.previous_ranks[user]["leaguePoints"]
                    ):
                        print(f"{user} updated", flush=True)
                        await self.update_table(user, self.ranked_dict[user])
                        # print(self.ranked_dict[user], flush=True)

            self.bot.logging.info("Posting ranks")
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
                        + f" - {posting['discord_name']}"
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

            paste = self.bot.get_channel(919981835428179988)
            async for message in paste.history():
                await message.delete()

            if len(output_list) != 0:
                to_send = "\n".join(output_list)
                await paste.send(to_send)

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
        await msg.edit(content="Stopped refreshing of ranks")

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

    # Needs updating to grab last match from the table
    async def update_table(self, user, user_stats_dict):
        async with sqa.connect(database=self.bot.db_path) as connection:
            last_values = await connection.execute_fetchall(
                "SELECT * FROM league_history ORDER BY id DESC WHERE puuid = ?",
                (user_stats_dict["summonerId"]),
            )
        if last_values[0][-2:] == (user_stats_dict["wins"], user_stats_dict["losses"]):
            return
        async with sqa.connect(self.bot.db_path) as connection:
            await connection.execute(
                "INSERT INTO league_history (puuid, lp, division, tier, wins, losses) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user_stats_dict["summonerId"],
                    str(user_stats_dict["leaguePoints"]),
                    user_stats_dict["rank"],
                    user_stats_dict["tier"],
                    user_stats_dict["wins"],
                    user_stats_dict["losses"],
                ),
            )
            await connection.commit()
        return


async def setup(bot: commands.Bot):
    await bot.add_cog(FetchFromRiot(bot))
