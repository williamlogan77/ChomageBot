import typing
import discord
from discord.ext import commands
from discord import app_commands
import random
import numpy as np
from typing import Any, List, Union


class TeamGenerator(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="select_teams", description="Create a number of teams of players"
    )
    @app_commands.describe(
        team_size="The size of the team",
    )
    async def generate_teams(
        self,
        ctx: discord.Interaction,
        team_size: typing.Optional[int] = 5,
    ):
        view = DropdownView(team_size=team_size)
        await ctx.response.send_message("Choose your team", view=view)

        return


class UserDropdown(discord.ui.UserSelect):
    def __init__(self, team_size):

        super().__init__(placeholder="Select a user", min_values=2, max_values=25)
        self.team_size = team_size

    async def callback(self, interaction: discord.Interaction) -> Any:
        members: List[Union[discord.Member, discord.User]] = self.values
        random.shuffle(members)
        for team_no in range(int(np.ceil(len(members) / self.team_size))):
            t1 = members[0 : self.team_size]
            [members.remove(x) for x in members]  # pylint: disable=W0106
            random.shuffle(members)
            # await interaction.response.send_message(f"Team number {team_no+1} is:")

            to_send = "Team number {team_no+1} is:" + "\n "
            for team_member in t1:
                to_send += f"<@{team_member.id}>" + "\n "

            await interaction.response.send_message(to_send)


class DropdownView(discord.ui.View):
    def __init__(self, team_size):
        super().__init__()
        self.add_item(UserDropdown(team_size=team_size))


async def setup(bot: commands.Bot):
    await bot.add_cog(TeamGenerator(bot))
