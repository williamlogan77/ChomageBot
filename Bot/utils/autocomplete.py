from typing import Any
import discord
from discord import app_commands
import logging

log = logging.getLogger(__name__)

###============================================================================

class DiscordAttachedLeagueNames(app_commands.Transformer):
    # This class contains discord.Interaction.
    # interaction class contains property client (the bot this is used in).

    async def transform(self, interaction: discord.Interaction, value: Any):
        return value

    async def autocomplete(self, interaction: discord.Interaction, value):
        log.info(f"Autocompleting for {interaction.namespace.user.id}")
        league_names = await interaction.client.db_utils.get_username_from_discord_id(
            interaction.namespace.user.id)
        return [
            app_commands.Choice(name=league_name, value=league_name)
            for league_name in league_names
        ]