"""Liveness watchdog for the bot's @tasks.loop tasks.

Discord.py's ``@tasks.loop`` has a known edge case: after a long Gateway
disconnect that resolves via ``RESUMED``, the loop's internal scheduler
sometimes never fires the next iteration. The task reports ``is_running()
== True`` so an external observer doesn't notice — the leaderboard goes
stale, the stream stops catching new games, and only a manual reload
brings them back.

This cog watches the *last_fired* timestamps that the two main loops
update at the end of each successful iteration. If either timestamp is
older than a generous threshold (way longer than the loop's interval),
we assume the loop is frozen and reload the parent cog so a fresh
instance starts a fresh @tasks.loop task.

The cog reload is the same hard-reset that ``touch <cog>.py`` triggers
via auto_reload — discord.py cancels every @tasks.loop bound to the
cog, then ``__init__`` calls ``.start()`` again on the new instance.
"""

import datetime as dt
import logging

import discord
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

# How often we wake up to check.
CHECK_INTERVAL_MINUTES = 5

# A 120s loop should fire ~5x per 10 min; if no fire in 10 min the
# task is conclusively stuck rather than just slow.
POST_RANKS_STALE_AFTER = dt.timedelta(minutes=10)

# A 5-min loop with up-to-21 players × match-fetch budget can legitimately
# take several minutes when the rate limiter is contended; 30 min is a
# very safe stale threshold that still catches the disconnect-freeze case.
STREAM_STALE_AFTER = dt.timedelta(minutes=30)


class Heartbeat(commands.Cog):
    """Restart frozen @tasks.loop tasks by reloading their parent cog."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        self.watchdog.start()

    def cog_unload(self) -> None:
        self.watchdog.cancel()

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def watchdog(self) -> None:
        now = dt.datetime.now()
        await self._check_one(
            cog_name="FetchFromRiot",
            extension="cogs.league_table_updater",
            attr="post_ranks_last_fired",
            stale_after=POST_RANKS_STALE_AFTER,
            now=now,
        )
        await self._check_one(
            cog_name="Backfill",
            extension="cogs.backfill",
            attr="_stream_last_ran",
            stale_after=STREAM_STALE_AFTER,
            now=now,
        )

    async def _check_one(
        self,
        cog_name: str,
        extension: str,
        attr: str,
        stale_after: dt.timedelta,
        now: dt.datetime,
    ) -> None:
        cog = self.bot.get_cog(cog_name)
        if cog is None:
            return  # cog not loaded; nothing to watchdog
        last = getattr(cog, attr, None)
        if last is None:
            # The loop hasn't completed an iteration yet — could be a
            # fresh start, give it one full stale-window before forcing
            # a reload.
            return
        elapsed = now - last
        if elapsed <= stale_after:
            return  # healthy

        self.bot.logging.warning(
            f"Heartbeat: {cog_name}.{attr} stale ({elapsed} since last fire); "
            f"reloading {extension}"
        )
        try:
            await self.bot.reload_extension(extension)
            self.bot.logging.info(f"Heartbeat: reloaded {extension}")
        except Exception as exc:
            self.bot.logging.error(f"Heartbeat: reload of {extension} failed: {exc!r}")

    @watchdog.before_loop
    async def before_watchdog(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Log every slash-command invocation for debug visibility.

        Fires for ALL interactions; we filter to application-commands so
        button/modal/select interactions aren't logged here. The cog's own
        commands and other cogs' commands all flow through the same
        dispatcher, so this single listener covers the whole bot.
        """
        if interaction.type != discord.InteractionType.application_command:
            return
        cmd = interaction.command.qualified_name if interaction.command else "?"
        channel = getattr(interaction.channel, "name", interaction.channel_id)
        self.bot.logging.info(
            f"/{cmd} invoked by {interaction.user} (id={interaction.user.id}) " f"in #{channel}"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Heartbeat(bot))
