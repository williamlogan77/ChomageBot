import discord
from discord.ext import commands
from discord import app_commands
import random
import numpy as np


class TeamGenerator(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def generate_teams(
        self,
        ctx: discord.Interaction,
        members: commands.Greedy[discord.Member],
        team_size: int = 5,
    ):
        random.shuffle(members)
        for team_no in np.ceil(11 / team_size):
            t1 = members[0:5]
            [members.remove(x) for x in members]  # pylint: disable=W0106
            random.shuffle(members)
            await ctx.response.send_message(f"Team number {team_no} is:")
            for team_member in t1:
                await ctx.response.send_message(team_member)
        return


async def setup(bot: commands.Bot):
    await bot.add_cog(TeamGenerator(bot))
