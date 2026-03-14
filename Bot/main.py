import asyncio
import os
import discord
from dotenv import load_dotenv
from bot import MyDiscordBot
import logging

# Load .env from parent directory relative to this file's location
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(env_path)

###============================================================================
# Setup log level between debug and info for util calls

logging.VERBOSE = 15
logging.addLevelName(logging.VERBOSE, "VERBOSE")

def verbose(self, message, *args, **kwargs):
    if self.isEnabledFor(logging.VERBOSE):
        self._log(logging.VERBOSE, message, args, **kwargs)
logging.Logger.verbose = verbose

###============================================================================

async def main(my_token: str) -> None:
    server_id = int(os.environ.get("guild_id", "0"))
    if server_id == 0:
        raise ValueError("guild_id environment variable must be set in .env file")
    
    bot = MyDiscordBot(
        command_prefix="!",
        intents=discord.Intents.all(),
        serverid=server_id,
    )
    async with bot:
        await bot.start(my_token)

###============================================================================

if __name__ == "__main__":
    asyncio.run(main(os.environ.get("token")))  # type: ignore
