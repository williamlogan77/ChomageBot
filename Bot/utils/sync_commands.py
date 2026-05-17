"""Sync slash commands to Discord from a shell, no /refresh sync needed.

Background: discord.py's app-command tree is registered with Discord via
``bot.tree.sync()``. New or renamed commands don't appear in any client
until that's called. Normally we trigger it via the in-bot ``/refresh
sync`` slash command — but if that command itself is broken, missing,
or you simply can't reach Discord interactively, that doesn't help.

This script does the same job from outside Discord:

  1. Constructs MyDiscordBot the same way main.py does.
  2. Loads every cog (so the tree is fully populated).
  3. ``bot.login()`` — REST auth only, no Gateway connection. The
     running bot keeps its Gateway session; we don't kick it.
  4. ``bot.tree.sync(guild=<guild_id>)`` registers the current command
     set with Discord.
  5. Exits cleanly. Any background tasks the cogs queued in __init__
     get cancelled when the event loop closes; they never ran a real
     iteration because they all wait on ``bot.wait_until_ready()``.

Run on the container:

    cd /root/ChomageBot/Bot && python -m utils.sync_commands

Reads ``token`` and ``guild_id`` from ../.env (same as main.py).
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import sys

import discord
from dotenv import load_dotenv

# Make Bot/ importable (`from main import MyDiscordBot` etc.) when this
# script is run from the Bot/ working dir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import MyDiscordBot  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync_commands")


async def main() -> None:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
    load_dotenv(env_path)

    token = os.environ.get("token")
    guild_id_str = os.environ.get("guild_id", "0")
    if not token:
        raise SystemExit("token env var not set (check .env)")
    try:
        guild_id = int(guild_id_str)
    except ValueError as exc:
        raise SystemExit(f"guild_id is not an integer: {guild_id_str!r}") from exc
    if guild_id == 0:
        raise SystemExit("guild_id env var must be set in .env")

    bot = MyDiscordBot(
        command_prefix="!",
        intents=discord.Intents.all(),
        db_path="./db/database.sqlite",
        serverid=guild_id,
    )
    bot.logging = log  # cogs reference this; set it before loading them

    loaded = 0
    skipped = []
    for path in glob.glob("./cogs/*.py"):
        cog_name = os.path.basename(path)[:-3]
        try:
            await bot.load_extension(f"cogs.{cog_name}")
            loaded += 1
        except Exception as exc:
            skipped.append((cog_name, repr(exc)))
    log.info(f"Loaded {loaded} cog(s); skipped {len(skipped)}")
    for name, err in skipped:
        log.warning(f"  skipped {name}: {err}")

    log.info("REST login (no Gateway)...")
    await bot.login(token)

    guild = discord.Object(id=guild_id)
    log.info(f"Syncing commands to guild {guild_id}...")
    synced = await bot.tree.sync(guild=guild)
    log.info(f"Synced {len(synced)} guild command(s):")
    for cmd in synced:
        log.info(f"  /{cmd.name}")

    await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
