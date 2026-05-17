import glob
import logging
import os

from discord.ext import commands, tasks

log = logging.getLogger(__name__)

COG_DIR = "./cogs"
POLL_SECONDS = 30


class AutoReload(commands.Cog):
    """Watches cog files for changes and hot-reloads any that change.

    Pairs with scripts/deploy.sh: cron pulls latest main, mtimes shift,
    this cog reloads the affected extensions in-process. No restart, no
    signals, no external IPC. The bot's Discord connection stays open.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._mtimes: dict[str, float] = {}
        self.watch.start()

    def cog_unload(self):
        self.watch.cancel()

    @tasks.loop(seconds=POLL_SECONDS)
    async def watch(self) -> None:
        for path in sorted(glob.glob(f"{COG_DIR}/*.py")):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            ext_name = f"cogs.{os.path.basename(path)[:-3]}"
            prev = self._mtimes.get(path)

            if prev is None:
                # First time we've seen this file. If the bot already has the
                # extension loaded (initial discovery in main.py), just record
                # mtime. Otherwise a new cog was added — load it.
                if ext_name not in self.bot.extensions:
                    try:
                        await self.bot.load_extension(ext_name)
                        log.info(f"AutoReload: loaded {ext_name}")
                    except Exception as exc:
                        log.error(f"AutoReload: failed to load {ext_name}: {exc}")
                self._mtimes[path] = mtime
            elif mtime != prev:
                try:
                    await self.bot.reload_extension(ext_name)
                    log.info(f"AutoReload: reloaded {ext_name}")
                except Exception as exc:
                    log.error(f"AutoReload: failed to reload {ext_name}: {exc}")
                self._mtimes[path] = mtime

    @watch.before_loop
    async def before_watch(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoReload(bot))
