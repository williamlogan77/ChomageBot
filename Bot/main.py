import asyncio
import glob
import logging
import os
import sqlite3 as sq

import aiosqlite as sqa
import discord
import pantheon
from discord.ext.commands import Bot
from dotenv import load_dotenv
from utils.db_utils import DButils

# Load .env from parent directory relative to this file's location
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(env_path)


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
        # Log API key info (first/last few chars only for security)
        if riot_key:
            key_preview = f"{riot_key[:10]}...{riot_key[-4:]}" if len(riot_key) > 14 else "***"
            print(f"Riot API Key loaded: {key_preview}")
        else:
            print("WARNING: Riot API Key not found in environment variables!")

        self.lolapi = pantheon.Pantheon("euw1", riot_key, debug=True)
        self.logging: logging.Logger = None

    async def setup_hook(self) -> None:
        discord.utils.setup_logging()
        self.logging = logging.getLogger()

        for file in glob.glob("./cogs/*.py"):
            # Use os.path for cross-platform compatibility
            cog_name = os.path.basename(file)[:-3]
            await self.load_extension(f"cogs.{cog_name}")

    async def sync_discord(self) -> None:
        print("Syncing users")
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
        self.logging.info("Connected to discord, syncing users and channels")
        await self.sync_discord()
        self.logging.info("Bot is ready")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        return


def setup_db(logger: logging.Logger) -> None:
    """Apply ./db/setup.sql to the database. Safe to run on every boot —
    every statement uses CREATE TABLE/INDEX IF NOT EXISTS, so this is a
    no-op when the schema is already current.
    """
    db_path = "./db/database.sqlite"
    if not os.path.isfile(db_path):
        logger.info("Creating empty database file")
        open(db_path, "x", encoding="utf-8").close()
    with sq.connect(db_path) as connection:
        with open("./db/setup.sql", encoding="utf-8") as f:
            connection.executescript(f.read())
    logger.info("Database schema applied")


async def main(my_token: str) -> None:
    setup_db(logging.getLogger())
    dbpath = "./db/database.sqlite"
    server_id = int(os.environ.get("guild_id", "0"))
    if server_id == 0:
        raise ValueError("guild_id environment variable must be set in .env file")

    bot = MyDiscordBot(
        command_prefix="!",
        intents=discord.Intents.all(),
        db_path=dbpath,
        serverid=server_id,
    )
    async with bot:
        await bot.start(my_token)


if __name__ == "__main__":
    token = os.environ.get("token")
    if not token:
        raise ValueError("token env var must be set in .env file")
    asyncio.run(main(token))

#
# bot.start(token)

# setup(bot)

# if __name__ == "__main__":
#     parser = argparse.ArgumentParser(description="Chomage Bot")
#     parser.add_argument('guild_id', type=str, help='The ID of your server :)')
#     args = parser.parse_args()
#     # bot = bot(guild_id, )
#
#
# # python main.py "thisguild"
#
#     print(args)
