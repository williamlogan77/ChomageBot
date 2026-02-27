from discord.ext.commands import Bot
import logging
import glob

log = logging.getLogger(__name__)

class MyDiscordBot(Bot):
    def __init__(
        self,
        command_prefix: str,
        intents: discord.Intents,
        serverid: int,
    ) -> None:
        super().__init__(command_prefix, intents=intents)
        self.serverid = serverid
        self.riot_key = os.environ.get("riot_key")
        if riot_key:
            log.info(f"Riot API Key loaded")
        else:
            log.warn("WARNING: Riot API Key not found in environment variables!")
        self.apiutils = APIutils(riot_key)
        self.dbutils = DButils()

###============================================================================

    async def setup_hook(self) -> None:
        discord.utils.setup_logging(root=True)
        # Configure root logger. All other classes will use this logger

        for file in glob.glob("./cogs/*.py"):
            # Use os.path for cross-platform compatibility
            cog_name = os.path.basename(file)[:-3]
            await self.load_extension(f"cogs.{cog_name}")

    async def sync_discord(self) -> None:
        log.info("Syncing users")
        guild = await self.fetch_guild(self.serverid)
        members_iterator = guild.fetch_members()    # Returns an async iterator
        dbutils.add_members_to_db(members_iterator)
        log.info("Syncing channels")
        channels = await guild.fetch_channels() # Returns an list
        dbutils.add_channels_to_db(channels)
        return

    async def on_connect(self) -> None:
        await self.wait_until_ready()
        log.info("Connected to discord, syncing users and channels")
        await self.sync_discord()
        log.info("Bot is ready")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        return
