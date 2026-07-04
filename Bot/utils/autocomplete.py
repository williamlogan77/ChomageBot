from typing import Any

import discord
from discord import app_commands
from utils import db


class DiscordAttachedLeagueNames(app_commands.Transformer):
    async def transform(self, interaction: discord.Interaction, value: Any):
        return value

    async def autocomplete(self, interaction: discord.Interaction, value):
        rows = await db.fetchall(
            "SELECT league_username FROM league_players WHERE discord_user_id = %s",
            (interaction.namespace.user.id,),
        )
        return [app_commands.Choice(name=league_name, value=league_name) for (league_name,) in rows]
