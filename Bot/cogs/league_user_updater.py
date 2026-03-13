import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite as sqa
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
            response = await self.bot.apiutils.get_account_by_riotid(league_name, tag_line)
            puuid = response["puuid"]
            async with sqa.connect(self.bot.db_path) as db:  # type: ignore
                await db.execute(
                    """REPLACE INTO league_players (
                            discord_user_id,
                            puuid,
                            league_username,
                            tag
                        )
                        VALUES (?, ?, ?, ?)""",  #leagueId removed
                    (user.id, puuid, league_name, tag_line),
                )
                await db.commit()
            log.info(
                f"put {user.id, puuid, league_name, tag_line} into db for {ctx.user}"
            )
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
        league_name="Specify account to remove, seperated with a ;",
    )
    async def remove_from_db(
        self,
        ctx: discord.Interaction,
        user: discord.User,
        league_name: app_commands.Transform[str, DiscordAttachedLeagueNames] = "",
    ):
        if league_name != "":
            names = league_name.split(";")
            async with sqa.connect(self.bot.db_path) as db:
                for name in names:
                    await db.execute(
                        "DELETE FROM league_players WHERE league_username = ?", (name,)
                    )
                    await db.commit()
                    await ctx.response.send_message(f"{name} removed from list")

        else:
            async with sqa.connect(self.bot.db_path) as db:
                await db.execute(
                    "DELETE FROM league_players WHERE discord_user_id = ?", (user.id,)
                )
                await db.commit()
                await ctx.response.send_message(
                    f"successfully removed all accounts associated with {user.name}"
                )

        return

    @app_commands.command(
        name="show_players",
        description="Shows all player currently stored in the league database",
    )
    async def show_all(self, ctx: discord.Interaction):
        async with sqa.connect(self.bot.db_path) as db:
            to_show = await db.execute_fetchall(
                "SELECT league_username, tag FROM league_players"
            )
        message = ""
        for name in to_show:
            message += str(name[0]) + "#" + str(name[1]) + "\n"
        if not message:
            message = "No players to rank"
        await ctx.response.send_message(message)


async def setup(bot: commands.Bot):
    await bot.add_cog(LeagueUsers(bot))
