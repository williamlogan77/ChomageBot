from discord.ext import commands, tasks
import aiosqlite as sqa
from utils.rank_sorting_class import Ranker  # pylint: disable=E0401
from pantheon.utils.exceptions import RateLimit
from discord import app_commands
import asyncio
import discord
import aiosqlite as sqa


class LeagueGraphs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")


    async def generate_singular(self, user: discord.User):
        async with sqa.connect(self.bot.db_path) as connection:
            connection.execute("SELECT * FROM ")

async def setup(bot: commands.Bot):
    await bot.add_cog(LeagueGraphs(bot))