import datetime as dt
import os
import pickle

import discord
import matplotlib.pyplot as plt
from discord import app_commands
from discord.ext import commands
from main import MyDiscordBot
from utils import db, seasons
from utils.add_a_cheg import ChegClass
from utils.autocomplete import DiscordAttachedLeagueNames
from utils.rank_sorting_class import Ranker

# FALLBACK start of the ranked tracking window, used only while the
# seasons table is empty. Graphs normally read the latest auto-detected
# ladder reset from utils/seasons.py — no more manual updates on a new
# season. (Value: the 2026 ranked year opener, 2026-01-08 noon.)
FALLBACK_SPLIT_START = dt.datetime(2026, 1, 8, 12, 0, 0, tzinfo=dt.UTC)


async def _split_start() -> dt.datetime:
    """Start of the current ladder: latest detected reset, or the fallback."""
    return await seasons.current_season_start() or FALLBACK_SPLIT_START


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
        summonerid = await db.fetchall(
            "SELECT leagueId FROM league_players WHERE league_username = %s",
            (league_name,),
        )
        if not summonerid:
            await ctx.response.send_message(f"{league_name} does not exist in the database")
            return
        else:
            await ctx.response.defer()
        # msg = await ctx.followup.send("Refreshing ranks...",
        #                               wait=True,
        #                               ephemeral=True)

        with open("utils/my_fig.pickle", "rb") as f:
            # Load registers the figure with matplotlib's pyplot manager;
            # the returned value isn't otherwise used.
            pickle.load(f)

            # plt._backend_mod.new_figure_manager_given_figure(1, fig)
        # Older history rows are keyed by the legacy 47-char summoner ID
        # (league_players.leagueId), newer rows by the real 78-char puuid.
        # Match on either via a UNION subquery so legacy-tracked players
        # (e.g. DARWIN DARWIN N) actually see their post-migration games.
        # Solo queue only — Ranked 5s snapshots share league_history.
        split_start = await _split_start()
        points = await db.fetchall(
            "SELECT * FROM league_history WHERE timestamp >= %s "
            "AND queue = 'RANKED_SOLO_5x5' AND puuid IN ("
            "    SELECT leagueId FROM league_players WHERE league_username = %s"
            "    UNION"
            "    SELECT puuid FROM league_players WHERE league_username = %s"
            ") ORDER BY timestamp ASC",
            (split_start, league_name, league_name),
        )
        x_to_plot = []
        y_to_plot = []
        for point in points:
            # timestamp comes back as a tz-aware datetime already.
            x_to_plot.append(point[2])
            # lp, division, tier = point[3:6]
            y_to_plot.append(Ranker(*point[3:6][::-1])._score)
        if not y_to_plot:
            await ctx.followup.send(
                f"No ranked games for {league_name} since the current split "
                f"started ({split_start:%Y-%m-%d})."
            )
            return

        user = await db.fetchall(
            "SELECT league_username FROM league_players WHERE leagueId = %s",
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
        plt.plot(x_to_plot, y_to_plot, linewidth=2, color="black", linestyle=":", alpha=0.2)
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
        summonerid = await db.fetchall(
            "SELECT leagueId FROM league_players WHERE league_username = %s",
            (league_name,),
        )
        if not summonerid:
            await ctx.response.send_message(f"{league_name} does not exist in the database")
            return
        else:
            await ctx.response.defer()
        # msg = await ctx.followup.send("Refreshing ranks...",
        #                               wait=True,
        #                               ephemeral=True)

        with open("utils/my_fig.pickle", "rb") as f:
            # Load registers the figure with matplotlib's pyplot manager;
            # the returned value isn't otherwise used.
            pickle.load(f)

        # plt._backend_mod.new_figure_manager_given_figure(1, fig)
        # See generate_singular: match both legacy leagueId and modern
        # puuid via UNION so legacy-tracked players surface correctly.
        split_start = await _split_start()
        points = await db.fetchall(
            "SELECT * FROM league_history WHERE timestamp >= %s "
            "AND queue = 'RANKED_SOLO_5x5' AND puuid IN ("
            "    SELECT leagueId FROM league_players WHERE league_username = %s"
            "    UNION"
            "    SELECT puuid FROM league_players WHERE league_username = %s"
            ") ORDER BY timestamp ASC",
            (split_start, league_name, league_name),
        )
        score_dict = {}
        for point in points:
            # timestamp is a tz-aware datetime — bucket by calendar day.
            key = point[2].date()
            value = Ranker(*point[3:6][::-1])._score
            if key not in score_dict.keys():
                score_dict[key] = []
            score_dict[key].append(value)

        user = await db.fetchall(
            "SELECT league_username FROM league_players WHERE leagueId = %s",
            (summonerid[0][0],),
        )

        if not score_dict:
            await ctx.followup.send(
                f"No ranked games for {league_name} since the current split "
                f"started ({split_start:%Y-%m-%d})."
            )
            return

        plt.title(user[0][0])

        x_to_plot = []
        y_to_plot = []
        for key, value in score_dict.items():
            x_to_plot.append(key)
            y_to_plot.append(sum(value) / len(value))
        plt.plot(x_to_plot, y_to_plot, linewidth=2, color="black", linestyle="-.", alpha=1)
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
