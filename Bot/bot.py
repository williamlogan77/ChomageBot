from discord.ext.commands import Bot
import discord
import logging
import glob
import os
from utils.api_utils import APIutils
from utils.db_utils import DButils

from dotenv import load_dotenv

# Load .env from parent directory relative to this file's location
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(env_path)

log = logging.getLogger(__name__)

class MyDiscordBot(Bot):
    def __init__(
        self,
        command_prefix: str,
        intents: discord.Intents,
        serverid: int,
    ) -> None:
        super().__init__(command_prefix, intents=intents)
        discord.utils.setup_logging(root=True)
        self.serverid = serverid
        self.riot_key = os.environ.get("riot_key")
        if self.riot_key:
            log.info(f"Riot API Key loaded")
        else:
            log.warn("WARNING: Riot API Key not found in environment variables!")
        self.apiutils = APIutils(self.riot_key)
        self.dbutils = DButils("./db/databasecopy.sqlite")
        self.db_path ="./db/databasecopy.sqlite"       #Env var pls

###============================================================================

    async def setup_hook(self) -> None:        
        log.info("Running setup hook")
        # Configure root logger. All other classes will use this logger

        for file in glob.glob("./cogs/*.py"):
            # Use os.path for cross-platform compatibility
            cog_name = os.path.basename(file)[:-3]
            await self.load_extension(f"cogs.{cog_name}")
        await self.tree.sync()

    async def sync_discord(self) -> None:
        log.info("Syncing users")
        guild = self.get_guild(self.serverid)
        # We wait for on ready then we can get the guild without an API call
        await self.dbutils.add_members_to_db(guild.members)
        log.info("Syncing channels")
        await self.dbutils.add_channels_to_db(guild.channels)
        return

    async def on_ready(self) -> None:
        log.info("Connected to discord")
        await self.sync_discord()
        log.info("Bot is ready")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        return
