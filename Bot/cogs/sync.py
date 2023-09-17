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
        await ctx.response.defer()
        msg = await ctx.followup.send("Syncing cogs...",
                                      wait=True,
                                      ephemeral=True)
        user_roles = [x.name for x in ctx.user.roles]  # type: ignore
        if "Keeper of Chomage" in user_roles:
            print("updating cogs", flush=True)
            os.chdir("cogs/")
            for cog in glob.glob("*.py"):
                if not cog.startswith("sync"):
                    print("Reloading", cog, flush=True)
                    await self.bot.reload_extension(f"cogs.{cog[:-3]}")
            os.chdir("../")
            await self.bot.tree.sync()
            await msg.edit(content="Sucessfully synced and reloaded all cogs.")
            self.bot.logging.info("Synced and reloaded all cogs")
        else:
            msg.edit(
                content=
                "You need the 'Keeper of Chomage' Role to use this command")
            self.bot.logging.warning("Cogs have not reloaded")


async def setup(bot: commands.Bot):
    await bot.add_cog(Sync(bot))
