import discord
from discord.ext.commands import GroupCog
from discord.app_commands import command
import glob
import os

from Bot.main import MyDiscordBot


class Refresh(
    GroupCog,
    group_name="Refresh",
    group_description="Controls the refreshing and syncing",
):
    def __init__(self, bot: MyDiscordBot):
        self.bot = bot

    @command(name="sync")
    async def sync(self, ctx: discord.Interaction):
        await ctx.response.defer()
        msg = await ctx.followup.send("Syncing commands...", wait=True, ephemeral=True)
        self.bot.logging.info("Syncing commands")
        try:
            await self.bot.tree.sync()
            msg.edit(content="Succesfully synced")
        except Exception as e:
            self.bot.logging.error(f"Failed to sync due to: {e}")
            msg.edit(content="Unsuccesful sync")

    @command(name="reload_cogs")
    async def reload_cogs(self, ctx: discord.Interaction):
        await ctx.response.defer()
        msg = await ctx.followup.send("Reloading cogs...", wait=True, ephemeral=True)

        self.bot.logging.info("updating cogs")

        os.chdir("cogs/")
        loaded = 0
        for idx, cog in enumerate(glob.glob("*.py")):
            if not cog.startswith("sync"):
                try:
                    self.bot.logging.info(f"Reloading {cog}")
                    await self.bot.reload_extension(f"cogs.{cog[:-3]}")
                    loaded += 1
                except Exception as e:
                    self.bot.logging.error(f"Unable to load {cog}, error of: {e}")

        os.chdir("../")

        await msg.edit(content="Sucessfully reloaded all cogs.")
        self.bot.logging.info(f"Synced and reloaded {loaded}/{idx} cogs")


async def setup(bot: MyDiscordBot):
    await bot.add_cog(Refresh(bot))
