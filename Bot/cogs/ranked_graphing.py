from discord.ext import commands, tasks
import aiosqlite as sqa
from utils.rank_sorting_class import Ranker  # pylint: disable=E0401
from pantheon.utils.exceptions import RateLimit
from discord import app_commands
import asyncio
import discord
import aiosqlite as sqa
from utils.autocomplete import DiscordAttachedLeagueNames  # pylint: disable=E0401


class LeagueGraphs(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")

    # @app_commands.command(name="graph_user")
    async def generate_singular(
            self, ctx: discord.Interaction,
            league_name: app_commands.Transform[str,
                                                DiscordAttachedLeagueNames]):
        async with sqa.connect(self.bot.db_path) as connection:
            summonerid = await connection.execute_fetchall(
                "SELECT * FROM league_players WHERE league_username = ?",
                (league_name, ))
            if summonerid is None:
                await ctx.response.send_message(
                    f"{league_name} does not exist in the database")

            await connection.execute(
                "SELECT * FROM league_history WHERE puuid = ?", (summonerid, ))


async def setup(bot: commands.Bot):
    await bot.add_cog(LeagueGraphs(bot))
