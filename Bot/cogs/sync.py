import discord
from discord.ext import commands
from discord import app_commands
from discord.utils import get


class Sync(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="sync")
    async def sync(self, interaction: discord.Interaction):
        user_roles = [x.name for x in interaction.user.roles]
        if "Keeper of Chomage" in user_roles:
            await self.bot.tree.sync()
            await interaction.response.send_message("sucessfully synced.")
        else:
            await interaction.response.send_message(
                "You need the 'Keeper of Chomage' Role to use this command")


async def setup(bot: commands.Bot):
    await bot.add_cog(Sync(bot))
