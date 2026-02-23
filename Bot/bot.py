from discord.ext.commands import Bot
import logging

log = logging.getLogger(__name__)

class MyDiscordBot(Bot):
    def __init__(
        self,
        command_prefix: str,
        intents: discord.Intents,
        db_path: str,
        serverid: int,
    ) -> None:
        super().__init__(command_prefix, intents=intents)
        self.dbutils = DButils(db_path=db_path)
        self.db_path = db_path
        self.guildid = serverid
        
        riot_key = os.environ.get("riot_key")
        if riot_key:
            log.info(f"Riot API Key loaded")
        else:
            log.warn("WARNING: Riot API Key not found in environment variables!")

    async def setup_hook(self) -> None:
        discord.utils.setup_logging(root=True)
        # Configure root logger. All other classes will use this logger

        for file in glob.glob("./cogs/*.py"):
            # Use os.path for cross-platform compatibility
            cog_name = os.path.basename(file)[:-3]
            await self.load_extension(f"cogs.{cog_name}")

    async def sync_discord(self) -> None:
        log.info("Syncing users")
        guild = await self.fetch_guild(self.guildid)
        async with sqa.connect(self.db_path) as db:
            async for member in guild.fetch_members():
                nickname = member.nick if member.nick is not None else ""
                await db.execute(
                    "REPLACE INTO users (user_id, nickname, discord_tag) VALUES (?, ?, ?)",
                    (member.id, nickname, member.name),
                )
                await db.commit()

            channels = await guild.fetch_channels()

            for channel in channels:
                if str(channel.type) == "category":
                    continue
                await db.execute(
                    "REPLACE INTO discord_channels (channel_id, name, type) VALUES (?, ?, ?)",
                    (int(channel.id), str(channel.name), str(channel.type)),
                )
            await db.commit()
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



#
#
#def setup_db(logger: logging.Logger) -> None:
#
#    if not os.path.isfile("./db/database.sqlite"):
#        MyDiscordBot.info("Setting up database")
#        with open("./db/database.sqlite", "x", encoding="utf-8") as f:
#            pass
#        with sq.connect("./db/database.sqlite") as connection:
#            cursor = connection.cursor()
#            with open("./db/setup.sql", "r", encoding="utf-8") as f:
#                sql_code = f.read()
#            cursor.executescript(sql_code)
#    else:
#        logger.info("Database exists, setup done.")
#

