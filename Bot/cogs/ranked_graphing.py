from discord.ext import commands
import aiosqlite as sqa
from utils.rank_sorting_class import Ranker  # pylint: disable=E0401
from discord import app_commands
import discord
from utils.autocomplete import DiscordAttachedLeagueNames  # pylint: disable=E0401
from utils.add_a_cheg import ChegClass  # pylint: disable=E0401
import pickle
import matplotlib.pyplot as plt
import datetime as dt
import os
from main import MyDiscordBot  # pylint: disable=E0401


class LeagueGraphs(commands.Cog):
    def __init__(self, bot: MyDiscordBot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")

    @app_commands.command(
        name="graph_user_granular",
        description="Plots a graph of users elo with granular movements",
    )
    async def generate_singular(
        self,
        ctx: discord.Interaction,
        user: discord.User,
        league_name: app_commands.Transform[str, DiscordAttachedLeagueNames],
    ):
        async with sqa.connect(self.bot.db_path) as connection:
            summonerid = await connection.execute_fetchall(
                "SELECT puuid FROM league_players WHERE league_username = ?",
                (league_name,),
            )
            if summonerid is None:
                await ctx.response.send_message(
                    f"{league_name} does not exist in the database"
                )
                return
            else:
                await ctx.response.defer()
            # msg = await ctx.followup.send("Refreshing ranks...",
            #                               wait=True,
            #                               ephemeral=True)

        with open("utils/my_fig.pickle", "rb") as f:
            fig = pickle.load(f)

            # plt._backend_mod.new_figure_manager_given_figure(1, fig)
        async with sqa.connect(self.bot.db_path) as connection:
            # need to change date here for relevant split - figure out logic for it
            async with connection.execute(
                "SELECT * FROM league_history WHERE timestamp > '2024-05-15' AND leagueId = ?", (summonerid[0][0],)
            ) as cursor:
                x_to_plot = []
                y_to_plot = []
                async for point in cursor:
                    x_to_plot.append(
                        dt.datetime.strptime(point[2], "%Y-%m-%d %H:%M:%S")
                    )
                    # lp, division, tier = point[3:6]
                    y_to_plot.append(Ranker(*point[3:6][::-1])._score)
        async with sqa.connect(self.bot.db_path) as connection:
            user = await connection.execute_fetchall(
                "SELECT league_username FROM league_players WHERE puuid = ?",
                (summonerid[0][0],),
            )
        plt.title(user[0][0])

        for idx in range(len(y_to_plot) - 1):
            try:
                if y_to_plot[idx] > y_to_plot[idx + 1]:
                    tmp_col = "red"
                else:
                    tmp_col = "green"
            except IndexError:
                tmp_col = "black"
            plt.scatter(x_to_plot[idx], y_to_plot[idx], marker="x", color=tmp_col)
        plt.plot(
            x_to_plot, y_to_plot, linewidth=2, color="black", linestyle=":", alpha=0.2
        )
        plt.ylim((min(y_to_plot) - 50), (max(y_to_plot) + 50))
        plt.tight_layout()
        plt.savefig("tmp/fig_to_send.jpg")

        ChegClass.add_my_cheg()

        await ctx.followup.send(file=discord.File(open("tmp/to_send_cheg.jpg", "rb")))
        os.remove("tmp/fig_to_send.jpg")
        os.remove("tmp/to_send_cheg.jpg")

    @app_commands.command(
        name="graph_user_average",
        description="Plots a graph of users elo as an average over days",
    )
    async def generate_multiple(
        self,
        ctx: discord.Interaction,
        user: discord.User,
        league_name: app_commands.Transform[str, DiscordAttachedLeagueNames],
    ):
        async with sqa.connect(self.bot.db_path) as connection:
            summonerid = await connection.execute_fetchall(
                "SELECT leagueId FROM league_players WHERE league_username = ?",
                (league_name,),
            )
        if summonerid is None:
            await ctx.response.send_message(
                f"{league_name} does not exist in the database"
            )
            return
        else:
            await ctx.response.defer()
        # msg = await ctx.followup.send("Refreshing ranks...",
        #                               wait=True,
        #                               ephemeral=True)

        with open("utils/my_fig.pickle", "rb") as f:
            fig = pickle.load(f)

        # plt._backend_mod.new_figure_manager_given_figure(1, fig)
        async with sqa.connect(self.bot.db_path) as connection:
            async with connection.execute(
                "SELECT * FROM league_history WHERE timestamp > '2024-05-15' AND leagueId = ?", (summonerid[0][0],)
            ) as cursor:
                score_dict = {}
                async for point in cursor:
                    key = dt.datetime.strptime(point[2][:10], "%Y-%m-%d")
                    value = Ranker(*point[3:6][::-1])._score
                    if key not in score_dict.keys():
                        score_dict[key] = []
                    score_dict[key].append(value)

        async with sqa.connect(self.bot.db_path) as connection:
            user = await connection.execute_fetchall(
                "SELECT league_username FROM league_players WHERE leagueId = ?",
                (summonerid[0][0],),
            )

        plt.title(user[0][0])

        x_to_plot = []
        y_to_plot = []
        for key, value in score_dict.items():
            x_to_plot.append(key)
            y_to_plot.append(sum(value) / len(value))
        plt.plot(
            x_to_plot, y_to_plot, linewidth=2, color="black", linestyle="-.", alpha=1
        )
        plt.ylim((min(y_to_plot) - 50), (max(y_to_plot) + 50))
        plt.xticks(rotation=45, ha="center")
        plt.tight_layout()
        plt.savefig("tmp/fig_to_send.jpg")

        ChegClass.add_my_cheg()

        await ctx.followup.send(file=discord.File(open("tmp/to_send_cheg.jpg", "rb")))
        os.remove("tmp/fig_to_send.jpg")
        os.remove("tmp/to_send_cheg.jpg")


async def setup(bot: commands.Bot):
    await bot.add_cog(LeagueGraphs(bot))
