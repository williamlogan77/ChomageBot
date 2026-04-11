from typing import Any
import discord
from discord import app_commands
import aiosqlite as sqa


class DiscordAttachedLeagueNames(app_commands.Transformer):
    # This class contains discord.Interaction.
    # interaction class contains property client (the bot this is used in).

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