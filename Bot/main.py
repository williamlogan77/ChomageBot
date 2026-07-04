import asyncio
import glob
import logging
import os
import sys

import discord
import pantheon
from discord.ext.commands import Bot
from dotenv import load_dotenv
from utils import db

# Load .env from parent directory relative to this file's location
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(env_path)


class MyDiscordBot(Bot):
    def __init__(
        self,
        command_prefix: str,
        intents: discord.Intents,
        serverid: int,
    ) -> None:
        super().__init__(command_prefix, intents=intents)
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
        # Drain the Discord API iterators BEFORE touching the pool so no
        # pooled connection (max 5) sits pinned across paginated network
        # fetches, then upsert each table in one executemany batch.
        members = [member async for member in guild.fetch_members()]
        channels = await guild.fetch_channels()

        await db.executemany(
            "INSERT INTO users (user_id, nickname, discord_tag) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "nickname = EXCLUDED.nickname, discord_tag = EXCLUDED.discord_tag",
            [
                (member.id, member.nick if member.nick is not None else "", member.name)
                for member in members
            ],
        )
        await db.executemany(
            "INSERT INTO discord_channels (channel_id, name, type) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (channel_id) DO UPDATE SET "
            "name = EXCLUDED.name, type = EXCLUDED.type",
            [
                (int(channel.id), str(channel.name), str(channel.type))
                for channel in channels
                if str(channel.type) != "category"
            ],
        )
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


async def main(my_token: str) -> None:
    # Apply ./db/setup.postgres.sql on every boot. Safe — every statement
    # uses CREATE TABLE/INDEX IF NOT EXISTS, so this is a no-op when the
    # schema is already current.
    await db.apply_schema("./db/setup.postgres.sql")
    print("Database schema applied")

    server_id = int(os.environ.get("guild_id", "0"))
    if server_id == 0:
        raise ValueError("guild_id environment variable must be set in .env file")

    bot = MyDiscordBot(
        command_prefix="!",
        intents=discord.Intents.all(),
        serverid=server_id,
    )
    try:
        async with bot:
            await bot.start(my_token)
    finally:
        await db.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        # psycopg's async pool can't run on Windows' default ProactorEventLoop.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
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
