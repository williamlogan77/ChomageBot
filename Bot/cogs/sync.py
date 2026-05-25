import glob
import os

import discord
from discord.app_commands import command
from discord.ext.commands import GroupCog
from main import MyDiscordBot


class Refresh(
    GroupCog,
    group_name="refresh",
    group_description="Controls the refreshing and syncing",
):
    """The cog containing commands related to admin tools"""

    def __init__(self, bot: MyDiscordBot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")

    @command(name="sync")
    async def sync(self, ctx: discord.Interaction):
        """Sync slash commands.

        Guild-scope sync first (instant — appears immediately in this
        server) followed by a global sync (canonical, propagates to
        other servers / DMs over the next ~hour). Mirrors the pattern
        in ``Bot/utils/sync_commands.py`` so admins don't have to wait
        for global propagation after updating a command's decorator.
        """
        await ctx.response.defer()
        msg = await ctx.followup.send("Syncing commands...", wait=True, ephemeral=True)
        self.bot.logging.info("Syncing commands")
        try:
            guild = ctx.guild
            if guild is not None:
                self.bot.tree.copy_global_to(guild=guild)
                guild_synced = await self.bot.tree.sync(guild=guild)
                self.bot.logging.info(
                    f"Synced {len(guild_synced)} guild-scope command(s) to {guild.id}"
                )
            global_synced = await self.bot.tree.sync()
            self.bot.logging.info(f"Synced {len(global_synced)} global command(s)")
            await msg.edit(
                content=(
                    f"Synced {len(guild_synced) if guild else 0} guild + "
                    f"{len(global_synced)} global commands."
                )
            )
        except Exception as e:
            self.bot.logging.error(f"Failed to sync due to: {e}")
            await msg.edit(content=f"Unsuccessful sync: {e!r}")

    @command(name="reload_cogs")
    async def reload_cogs(self, ctx: discord.Interaction):
        """Command to reload all cogs"""
        await ctx.response.defer()
        msg = await ctx.followup.send("Reloading cogs...", wait=True, ephemeral=True)

        self.bot.logging.info("updating cogs")

        os.chdir("cogs/")
        cogs = glob.glob("*.py")
        loaded = 0
        for cog in cogs:
            if not cog.startswith("sync"):
                try:
                    self.bot.logging.info(f"Unloading {cog}")
                    await self.bot.unload_extension(f"cogs.{cog[:-3]}")
                    self.bot.logging.info(f"Loading {cog}")
                    await self.bot.load_extension(f"cogs.{cog[:-3]}")
                    loaded += 1
                except Exception as e:
                    self.bot.logging.error(f"Unable to reload {cog}, error of: {e}")

        os.chdir("../")

        await msg.edit(content="Sucessfully reloaded all cogs.")
        self.bot.logging.info(f"Synced and reloaded {loaded}/{len(cogs)} cogs")


async def setup(bot: MyDiscordBot):
    """Setup function as needed by discord.py"""
    await bot.add_cog(Refresh(bot))
