import asyncio
import glob
import os
import sqlite3 as sq
import pantheon
import aiosqlite as sqa
import discord
from discord.ext.commands import Bot
from dotenv import load_dotenv
import logging
from utils.db_utils import DButils
import sys

load_dotenv("../.env")


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
        self.lolapi = pantheon.Pantheon("euw1", os.environ.get("riot_key"), debug=True)
        self.logging: logging.Logger = None



    async def setup_hook(self) -> None:
        discord.utils.setup_logging()
        self.logging = logging.getLogger()
        

        for file in glob.glob("./cogs/*.py"):
            await self.load_extension(f"cogs.{file.split('/')[-1][:-3]}")

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

    if not os.path.isfile("./db/database.sqlite"):
        MyDiscordBot.info("Setting up database")
        with open("./db/database.sqlite", "x", encoding="utf-8") as f:
            pass
        with sq.connect("./db/database.sqlite") as connection:
            cursor = connection.cursor()
            with open("./db/setup.sql", "r", encoding="utf-8") as f:
                sql_code = f.read()
            cursor.executescript(sql_code)
    else:
        logger.info("Database exists, setup done.")





async def main(my_token: str) -> None:
    # my_logger = logging.getLogger()
    # setup_db(my_logger)
    dbpath = "./db/database.sqlite"
    bot = MyDiscordBot(
        command_prefix="!",
        intents=discord.Intents.all(),
        db_path=dbpath,
        serverid=667751882260742164,
    )
    async with bot:

        await bot.start(my_token)


if __name__ == "__main__":
    asyncio.run(main(os.environ.get("token")))  # type: ignore

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
