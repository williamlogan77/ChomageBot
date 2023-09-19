import typing
from typing import Any
import discord
from discord.app_commands.models import Choice
from discord.ext import commands
from discord import app_commands
import aiosqlite as sqa
from discord.interactions import Interaction


class attachedDiscord(app_commands.Transformer):

    async def transform(self, interaction: discord.Interaction, value: Any):
        return value

    async def autocomplete(self, interaction: discord.Interaction, value):
        async with sqa.connect(interaction.client.db_path) as db:
            db.row_factory = lambda cursor, row: row[0]
            attached_accounts = await db.execute_fetchall(
                "SELECT league_username FROM league_players WHERE discord_user_id = ?",
                (interaction.namespace.user.id, ))
        return [
            app_commands.Choice(name=league_name, value=league_name)
            for league_name in attached_accounts
        ]


class LeagueUsers(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="add_player",
                          description="Add user to the table")
    @app_commands.describe(league_name='The league name',
                           user='The discord account attached')
    async def add_to_db(self, ctx: discord.Interaction, league_name: str,
                        user: discord.User):

        puuid = (await self.bot.lolapi.get_summoner_by_name(league_name))["id"]
        async with sqa.connect(self.bot.db_path) as db:  # type: ignore
            await db.execute(
                "REPLACE INTO league_players (discord_user_id, puuid, league_username) VALUES (?, ?, ?)",
                (user.id, puuid, league_name))
            await db.commit()
        self.bot.logging.info(
            f"put {user.id, user.name, puuid, league_name} into db for {ctx.user}"
        )
        await ctx.response.send_message(f"Added {league_name} into the db")
        return

    @app_commands.command(
        name="remove_player",
        description="Remove a player from the league history database")
    @app_commands.describe(user="Clear all accounts associated with this user",
                           league_name="Specify account to remove")
    async def remove_from_db(
            self,
            ctx: discord.Interaction,
            user: discord.User,
            league_name: app_commands.Transform[str, attachedDiscord] = ""):
        if league_name != "":
            names = league_name.split(" ")
            async with sqa.connect(self.bot.db_path) as db:
                for name in names:
                    await db.execute(
                        "DELETE FROM league_players WHERE league_username = ?",
                        (name, ))
                    await db.commit()
                    await ctx.response.send_message(f"{name} removed from list"
                                                    )

        else:
            async with sqa.connect(self.bot.db_path) as db:
                await db.execute(
                    "DELETE FROM league_players WHERE discord_user_id = ?",
                    (user.id, ))
                await db.commit()
                await ctx.response.send_message(
                    f"successfully removed all accounts awwssociated with {user.name}"
                )

        return

    @app_commands.command(
        name="show_players",
        description="Shows all player currently stored in the league database")
    async def show_all(self, ctx: discord.Interaction):
        async with sqa.connect(self.bot.db_path) as db:
            to_show = await db.execute_fetchall(
                "SELECT league_username FROM league_players")
        to_print = ""
        for name in to_show:
            to_print += str(name[0]) + "\n"

        await ctx.response.send_message(to_print)


async def setup(bot: commands.Bot):
    await bot.add_cog(LeagueUsers(bot))
