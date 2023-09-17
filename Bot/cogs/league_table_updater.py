from discord.ext import commands, tasks
import aiosqlite as sqa
from utils.rank_sorting_class import Ranker  # pylint: disable=E0401


class FetchFromRiot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        self.post_ranks.start()  # pylint: disable=E1101
        self.previous_ranks = None
        self.ranked_dict = None

    async def fetch_users_rank(self, users):
        users_ranks = {}
        for user, name in users:
            user_rank = await self.bot.lolapi.get_league_position(user)
            fivev5 = list(
                filter(lambda x: x["queueType"] == "RANKED_SOLO_5x5",
                       user_rank))
            if len(fivev5) > 0:
                fivev5 = fivev5[0]
                fivev5["discord_name"] = name
                fivev5["sorted_rank"] = Ranker(fivev5["tier"], fivev5["rank"],
                                               fivev5["leaguePoints"])
                fivev5["GamesPlayed"] = fivev5["wins"] + fivev5["losses"]
                fivev5["WinRate"] = (fivev5["wins"] /
                                     fivev5["GamesPlayed"]) * 100

                users_ranks[fivev5["summonerName"]] = fivev5
            else:
                fivev5 = []

        return users_ranks

    async def fetch_ranks(self):
        self.bot.logging.info("fetching ranks")
        # fetch from db
        async with sqa.connect(self.bot.db_path) as connection:
            async with connection.execute_fetchall(
                    "SELECT puuid, IIF(nickname='', discord_tag, nickname) FROM (SELECT * FROM league_players LEFT JOIN users ON user_id = discord_user_id)"
            ) as cursor:
                # Fetch current ranks and store them in a dict with updated values
                self.ranked_dict = await self.fetch_users_rank(cursor)

        return

    @tasks.loop(seconds=30)
    async def post_ranks(self):
        await self.bot.wait_until_ready()
        await self.fetch_ranks()
        if self.ranked_dict != self.previous_ranks:
            self.previous_ranks = self.ranked_dict
            to_post = filter(lambda x: type(x) == type({}),
                             [data for data in self.ranked_dict.values()])
            sorted_results = sorted(to_post,
                                    key=lambda d: d["sorted_rank"],
                                    reverse=True)

            output_list = []
            for index, posting in enumerate(sorted_results):
                if posting["tier"].title() == "Master":
                    post = str(index + 1) + ". " + posting[
                        "summonerName"] + "\n" + "Rank: " + posting[
                            "tier"].title() + " " + str(
                                posting["leaguePoints"]
                            ) + "lp" + "\n" + "Played: " + str(
                                posting["GamesPlayed"]) + " with a " + str(
                                    "{:.2f}".format(posting["WinRate"]
                                                    )) + "% winrate" + "\n"
                else:
                    post = str(index + 1) + ". " + posting[
                        "summonerName"] + f" - {posting['discord_name']}" + "\n" + "Rank: " + posting[
                            "tier"].title(
                            ) + " " + posting["rank"] + " " + str(
                                posting["leaguePoints"]
                            ) + "lp" + "\n" + "Played: " + str(
                                posting["GamesPlayed"]
                            ) + " games with a " + str("{:.2f}".format(
                                posting["WinRate"])) + "% winrate" + "\n"
                output_list.append(post)

            paste = self.bot.get_channel(919981835428179988)
            async for message in paste.history():
                await message.delete()

            if len(output_list) != 0:
                to_send = '\n'.join(output_list)
                await paste.send(to_send)

        return


async def setup(bot: commands.Bot):
    await bot.add_cog(FetchFromRiot(bot))