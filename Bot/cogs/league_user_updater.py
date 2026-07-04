import discord
from discord import app_commands
from discord.ext import commands
from main import MyDiscordBot
from utils import db
from utils.autocomplete import DiscordAttachedLeagueNames


class LeagueUsers(commands.Cog):
    def __init__(self, bot: MyDiscordBot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")

    @app_commands.command(name="add_player", description="Add user to the table")
    @app_commands.describe(
        league_name="The league name",
        tagline="The bit after # (don't include this)",
        user="The discord account attached",
    )
    async def add_to_db(
        self,
        ctx: discord.Interaction,
        league_name: str,
        tagline: str,
        user: discord.User,
    ):
        # Get PUUID from Account API - this is all we need now
        puuid = (await self.bot.lolapi.get_account_by_riotId(league_name, tagline))["puuid"]

        await db.execute(
            """INSERT INTO league_players (
                    discord_user_id,
                    leagueId,
                    puuid,
                    league_username,
                    tag
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (leagueId) DO UPDATE SET
                    discord_user_id = EXCLUDED.discord_user_id,
                    puuid = EXCLUDED.puuid,
                    league_username = EXCLUDED.league_username,
                    tag = EXCLUDED.tag""",
            (user.id, puuid, puuid, league_name, tagline),
        )
        self.bot.logging.info(
            f"put {user.id, user.name, puuid, league_name, tagline} into db for {ctx.user}"
        )
        await ctx.response.send_message(f"Added {league_name} into the db")
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
            names = [name for name in (part.strip() for part in league_name.split(";")) if name]
            for name in names:
                await db.execute("DELETE FROM league_players WHERE league_username = %s", (name,))
            # One response per interaction — a second send_message raises
            # InteractionResponded, which used to abort multi-name removals.
            await ctx.response.send_message(f"{', '.join(names)} removed from list")

        else:
            await db.execute("DELETE FROM league_players WHERE discord_user_id = %s", (user.id,))
            await ctx.response.send_message(
                f"successfully removed all accounts associated with {user.name}"
            )

        return

    @app_commands.command(
        name="show_players",
        description="Shows all player currently stored in the league database",
    )
    async def show_all(self, ctx: discord.Interaction):
        to_show = await db.fetchall("SELECT league_username, tag FROM league_players")
        to_print = ""
        for name in to_show:
            to_print += str(name[0]) + "#" + str(name[1]) + "\n"

        await ctx.response.send_message(to_print)


async def setup(bot: commands.Bot):
    await bot.add_cog(LeagueUsers(bot))
