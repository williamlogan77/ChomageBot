import discord
from discord.ext import commands
from discord import app_commands
import glob
import os


class Sync(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="sync")
    async def sync(self, ctx: discord.Interaction):
        user_roles = [x.name for x in ctx.user.roles]  # type: ignore
        if "Keeper of Chomage" in user_roles:
            os.chdir("cogs/")
            for cog in glob.glob("*.py"):
                if not cog.startswith("sync"):
                    print(os.getcwd())
                    print(cog)
                    await self.bot.reload_extension(f"{cog[:-3]}")
                    print("done", cog)
            os.chdir("../")
            await self.bot.tree.sync()
            await ctx.response.send_message("sucessfully synced.")
            print("synced cogs")
        else:
            await ctx.response.send_message(
                "You need the 'Keeper of Chomage' Role to use this command")


async def setup(bot: commands.Bot):
    await bot.add_cog(Sync(bot))
