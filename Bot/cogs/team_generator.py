import typing
import random
from typing import Any, List, Union
import discord
from discord.ext import commands
from discord import app_commands
import numpy as np


class TeamGenerator(commands.Cog):
    """Cog containing slash commands for team selection"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")

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
        """Slash command to generate teams

        Args:
            ctx (discord.Interaction): The discord interaction object (or
            context) - in this case this is the command sent
            team_size (typing.Optional[int], optional): The size of the team to choose.
            Defaults to 5.
        """
        view = DropdownView(team_size=team_size)
        await ctx.response.send_message("Choose your team", view=view)

        return


class UserDropdown(discord.ui.UserSelect):
    """Generate the dropdown component for the selection"""

    def __init__(self, team_size):

        super().__init__(placeholder="Select a user", min_values=2, max_values=25)
        self.team_size = team_size

    async def callback(self, interaction: discord.Interaction) -> Any:
        members: List[Union[discord.Member, discord.User]] = self.values
        random.shuffle(members)
        to_send = ""
        for team_no in range(int(np.ceil(len(members) / self.team_size))):
            t1 = members[0 : self.team_size]
            _ = [members.remove(t) for t in t1]

            random.shuffle(members)

            to_send += f"Team number {team_no+1} is:" + "\n "
            for team_member in t1:
                to_send += f"<@{team_member.id}>" + "\n "
            to_send += "\n "

        await interaction.response.send_message(to_send)


class DropdownView(discord.ui.View):
    """The overall view object"""

    def __init__(self, team_size):
        super().__init__()
        self.add_item(UserDropdown(team_size=team_size))


async def setup(bot: commands.Bot):
    """Setup function as needed by discord.py"""
    await bot.add_cog(TeamGenerator(bot))
