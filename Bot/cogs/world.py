import discord
from discord.ext import commands
from discord import app_commands


class ping(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="ping")
    async def pong(self, interaction: discord.Interaction):
        await interaction.response.send_message("ping")


async def setup(bot: commands.Bot):
    await bot.add_cog(ping(bot))
