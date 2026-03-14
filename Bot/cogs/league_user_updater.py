import discord
from discord.ext import commands
from discord import app_commands
from utils.autocomplete import DiscordAttachedLeagueNames  # pylint: disable=E0401
from main import MyDiscordBot # pylint: disable=E0401

import logging

log = logging.getLogger(__name__)

class LeagueUsers(commands.Cog):

    def __init__(self, bot: MyDiscordBot):
        self.bot = bot
        log.info(f"{__name__} loaded")

    @app_commands.command(name="add_player", description="Add user to the table")
    @app_commands.describe(
        league_name="The league name",
        tag_line="The bit after # (don't include this)",
        user="The discord account attached",
    )
    async def add_player(
        self,
        ctx: discord.Interaction,
        league_name: str,
        tag_line: str,
        user: discord.User,
    ):
        # Get PUUID from Account API - this is all we need now
        try:
            response = await self.bot.api_utils.get_account_by_riotid(league_name, tag_line)
            puuid = response["puuid"]
            await self.bot.db_utils.add_player(user.id, puuid, league_name, tag_line)
            await ctx.response.send_message(f"Added {league_name} into the db")
        except Exception as e:
            log.error(f"Failed to fetch rank for {league_name}: {type(e).__name__} - {e}")
        return

    @app_commands.command(
        name="remove_player",
        description="Remove a player from the league history database",
    )
    @app_commands.describe(
        user="Clear all accounts associated with this user",
        league_name="Specify account to remove, seperated with a ,",
    )
    async def remove_from_db(
        self,
        ctx: discord.Interaction,
        user: discord.User,
        league_name: app_commands.Transform[str, DiscordAttachedLeagueNames] = "",
    ):
        if league_name:
            for name in league_name.split(","):  # Who splits on a ;?
                # Whats even the point, the autofill makes it hard to supply more than 1 value
                await self.bot.db_utils.delete_player_by_username(name)
            await ctx.response.send_message(f"{name} removed from list")
        else:
            await self.bot.db_utils.delete_player_by_discord_id(user.id)
            await ctx.response.send_message(
                f"successfully removed all accounts associated with {user.name}"
            )

        return

    @app_commands.command(
        name="show_players",
        description="Shows all player currently stored in the league database",
    )
    async def show_all(self, ctx: discord.Interaction):
        usernames = await self.bot.db_utils.get_usernames_and_tags()
        message = "\n".join(f"{name[0]} #{name[1]}" for name in usernames)
        if not message:
            message = "No players to rank"
        await ctx.response.send_message(message)


async def setup(bot: commands.Bot):
    await bot.add_cog(LeagueUsers(bot))
