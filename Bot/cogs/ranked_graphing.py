from discord.ext import commands
import aiosqlite as sqa
from utils.rank_sorting_class import Ranker  # pylint: disable=E0401
from discord import app_commands
import discord
import aiosqlite as sqa
from utils.autocomplete import DiscordAttachedLeagueNames  # pylint: disable=E0401
import pickle
import matplotlib.pyplot as plt
import datetime as dt
import os


class LeagueGraphs(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")

    @app_commands.command(name="graph_user",
                          description="Plots a graph of users elo")
    async def generate_singular(
            self, ctx: discord.Interaction, user: discord.User,
            league_name: app_commands.Transform[str,
                                                DiscordAttachedLeagueNames]):
        async with sqa.connect(self.bot.db_path) as connection:
            summonerid = await connection.execute_fetchall(
                "SELECT * FROM league_players WHERE league_username = ?",
                (league_name, ))
            if summonerid is None:
                await ctx.response.send_message(
                    f"{league_name} does not exist in the database")
                return
            else:
                ctx.response.defer()
            msg = await ctx.followup.send("Refreshing ranks...",
                                          wait=True,
                                          ephemeral=True)

            with open("utils/my_fig.pickle", "rb") as f:
                fig = pickle.load(f)

            async with connection.execute(
                    "SELECT * FROM league_history WHERE puuid = ?",
                (summonerid, )) as cursor:
                x_to_plot = []
                y_to_plot = []
                async for point in cursor:
                    x_to_plot.append(
                        dt.datetime.strptime(point[2], '%Y-%m-%d %H:%M:%S'))
                    # lp, division, tier = point[3:6]
                    y_to_plot.append(Ranker(*point[3:6][::-1])._score)
                user = await connection.execute_fetchall(
                    "SELECT league_username FROM league_players WHERE puuid = ?",
                    (summonerid, ))
        plt.title(user[0][0])
        plt.scatter(x_to_plot, y_to_plot, marker="x", color="black")
        plt.plot(x_to_plot,
                 y_to_plot,
                 linewidth=2,
                 color="black",
                 linestyle=":",
                 alpha=0.2)
        plt.ylim((min(y_to_plot) - 100), (max(y_to_plot) + 100))
        plt.savefig("tmp/fig_to_send.jpg")

        await msg.edit(file="tmp/fig_to_send.jpg")
        os.remove("tmp/fig_to_send.jpg")


async def setup(bot: commands.Bot):
    await bot.add_cog(LeagueGraphs(bot))
