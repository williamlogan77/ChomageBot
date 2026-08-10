"""Weekly awards ceremony + all-time trophy cabinet.

From Monday 06:00 Europe/London onward the bot posts five awards into
#general covering the calendar week that just ended (Monday 00:00 ->
Monday 00:00 London wall time), aggregated per Discord user across all
of their tracked league accounts. Winners land in the weekly_awards
table; an all-time trophy cabinet board (optional awards_channel_id env,
see utils/config.py) is wiped-and-reposted after each ceremony.

The ceremony is fully silent: silent=True + AllowedMentions.none() on
every send, winners mentioned as <@id> for the rendered name only —
nobody gets pinged. #general is never wiped — plain sends, chunked under
Discord's 2000-char cap via leaderboard.chunk_blocks.

Scheduling is a 15-min @tasks.loop, idempotent across restarts and hot
reloads: a tick only posts when the awarded week has no weekly_awards
rows yet AND the bot_config marker hasn't seen it (the marker covers the
pathological zero-winner week, which records no rows). Idempotency is
what gates a repost, NOT the calendar — the tick will happily post on a
Tuesday if Monday's attempt never landed, so a bot that was down (or an
exception mid-ceremony) costs a late post rather than the whole week.
Computation lives in utils/awards.py; this cog owns scheduling, posting
and persistence.
"""

import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands, tasks
from main import MyDiscordBot
from psycopg.types.json import Jsonb
from utils import awards, config, db, leaderboard
from utils.loop_restart import restart_loop_later

# #general — same channel as the loss-streak ping (STREAK_PING_CHANNEL in
# cogs/league_table_updater.py).
CEREMONY_CHANNEL = 667751882260742167

# How often the scheduler wakes up to check whether a ceremony is due.
CHECK_MINUTES = 15

# The ceremony fires on Mondays from this hour (Europe/London) onward.
CEREMONY_HOUR = 6

# bot_config key recording the last week_start a ceremony was posted for.
MARKER_KEY = "weekly_awards_last_posted"


class WeeklyAwards(commands.Cog):
    def __init__(self, bot: MyDiscordBot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        # Log the live env value on every (re)load: env vars are frozen at
        # container creation, so a .env edit followed by a plain restart
        # leaves the old value running — this line is how you catch it.
        cabinet_id = config.awards_channel_id()
        self.bot.logging.info(
            "Trophy cabinet channel (awards_channel_id env): "
            + (str(cabinet_id) if cabinet_id else "not set — cabinet posting disabled")
        )
        self.awards_tick.start()
        # Watchdog input, same contract as FetchFromRiot.post_ranks_last_fired
        # (cogs/heartbeat.py reloads us if this goes stale).
        self.awards_tick_last_fired: dt.datetime | None = None
        # Log the missing-cabinet-channel notice once per cog instance,
        # not once per ceremony (mirrors the Ranked 5s inert pattern).
        self._warned_no_cabinet = False
        # Week whose already-posted skip has been logged — once per week,
        # not every 15-minute tick all Monday.
        self._skip_logged_week: dt.date | None = None

    def cog_unload(self) -> None:
        # discord.py does NOT cancel @tasks.loop tasks on cog unload —
        # without this, every hot reload (auto_reload / heartbeat) leaves
        # the old instance's loop running next to the new one's.
        self.awards_tick.cancel()

    # ------------------------------------------------------------- channels

    async def _get_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException as exc:
                self.bot.logging.error(f"Awards channel {channel_id} unavailable: {exc!r}")
                return None
        return channel

    # ---------------------------------------------------------- persistence

    async def _already_posted(self, week_start: dt.date) -> bool:
        """True when this week's ceremony already ran (restart/reload safe).

        Primary check: any weekly_awards row for the week. The bot_config
        marker backs it up for a week where every award skipped — no rows
        get recorded, and without the marker the ceremony would repost
        every 15 minutes all Monday.
        """
        row = await db.fetchone(
            "SELECT 1 FROM weekly_awards WHERE week_start = %s LIMIT 1", (week_start,)
        )
        if row is not None:
            return True
        row = await db.fetchone("SELECT value FROM bot_config WHERE key = %s", (MARKER_KEY,))
        # ISO dates compare correctly as strings.
        return row is not None and row[0] >= week_start.isoformat()

    async def _record_winners(
        self,
        week_start: dt.date,
        results: dict,
        *,
        disabled: frozenset[str] = frozenset(),
        forced: frozenset[str] = frozenset(),
    ) -> None:
        """Persist winners. Disabled awards record nothing (they weren't
        posted); forced winners' detail carries a ``forced`` marker so
        the history says how the trophy happened."""
        rows = [
            (
                week_start,
                award,
                w.user_id,
                w.display_name,
                w.value,
                Jsonb({**w.detail, "forced": True} if award in forced else w.detail),
            )
            for award, winners in results.items()
            if award not in disabled
            for w in winners
        ]
        if rows:
            await db.executemany(
                "INSERT INTO weekly_awards "
                "(week_start, award, discord_user_id, display_name, value, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (week_start, award, discord_user_id) DO NOTHING",
                rows,
            )
        await db.execute(
            "INSERT INTO bot_config (key, value, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (MARKER_KEY, week_start.isoformat()),
        )
        self.bot.logging.info(
            f"Recorded {len(rows)} weekly award winner(s) for week of {week_start}; "
            f"{MARKER_KEY} -> {week_start.isoformat()}"
        )

    # ------------------------------------------------------------- ceremony

    async def _run_ceremony(self, week_start: dt.datetime, week_end: dt.datetime) -> None:
        channel = await self._get_channel(CEREMONY_CHANNEL)
        if channel is None:
            # Nothing recorded — the next tick retries the whole ceremony.
            return

        # Dashboard controls: enable/disable + taglines + default scope
        # (bot_config), forced winners + exclusions + per-week measures
        # (award_overrides) — see utils/awards.AwardAdjustments.
        adjustments = await awards.fetch_adjustments(week_start.date())
        inputs = await awards.fetch_inputs(week_start, week_end)
        results, season_reset, forced_applied, below_min, runners_up = awards.compute_results(
            inputs, adjustments
        )
        history = await awards.fetch_prior_winner_history(week_start.date())
        commentary = awards.build_commentaries(
            week_start.date(), results, runners_up, history, forced_applied, adjustments
        )
        measures = awards.measure_notes(adjustments)
        if below_min:
            self.bot.logging.info(
                "Below the qualification bar, skipped: "
                + ", ".join(
                    f"{award} (min {info['min']:g}, best "
                    f"{awards.qualifying_magnitude(award, info['winners'][0].value):g})"
                    for award, info in sorted(below_min.items())
                )
            )
        if adjustments.default_scope != awards.SCOPE_ALL or measures:
            self.bot.logging.info(
                f"Award measures: default scope {adjustments.default_scope!r}"
                + (
                    "; " + ", ".join(f"{a}: {m}" for a, m in sorted(measures.items()))
                    if measures
                    else ""
                )
            )
        if season_reset:
            self.bot.logging.info(
                "Season reset detected in awarded week — cross-reset LP deltas excluded"
            )
        if adjustments.disabled:
            self.bot.logging.info(
                f"Awards disabled via bot_config, skipped: {sorted(adjustments.disabled)}"
            )
        if adjustments.forced and not forced_applied == frozenset(adjustments.forced):
            dropped = sorted(set(adjustments.forced) - forced_applied)
            self.bot.logging.warning(
                f"Forced winner(s) no longer qualify, computed winners kept: {dropped}"
            )
        if adjustments.excluded:
            self.bot.logging.info(
                "Exclusions applied: "
                + ", ".join(
                    f"{award}: {sorted(ids)}" for award, ids in sorted(adjustments.excluded.items())
                )
            )
        blocks = awards.build_ceremony_blocks(
            week_start.date(),
            results,
            season_reset=season_reset,
            disabled=adjustments.disabled,
            taglines=adjustments.taglines,
            forced=forced_applied,
            measures=measures,
            below_min=below_min,
            commentary=commentary,
        )

        self.bot.logging.info(
            f"Posting weekly awards for week of {week_start.date()} "
            f"to #{getattr(channel, 'name', '?')} ({CEREMONY_CHANNEL})"
        )
        for message_text in leaderboard.chunk_blocks(blocks):
            await channel.send(
                message_text,
                silent=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        await self._record_winners(
            week_start.date(),
            results,
            disabled=adjustments.disabled,
            forced=forced_applied,
        )
        await self.refresh_cabinet()

    # -------------------------------------------------------------- cabinet

    async def refresh_cabinet(self) -> bool:
        """Re-render + wipe-and-post the trophy cabinet. False if skipped."""
        channel_id = config.awards_channel_id()
        if channel_id is None:
            if not self._warned_no_cabinet:
                self._warned_no_cabinet = True
                self.bot.logging.info(
                    "awards_channel_id not set — trophy cabinet posting disabled "
                    "(the weekly ceremony still runs)"
                )
            return False
        channel = await self._get_channel(channel_id)
        if channel is None:
            return False

        row = await db.fetchone("SELECT MAX(week_start) FROM weekly_awards")
        latest_week = row[0] if row else None
        latest_rows: list[tuple] = []
        if latest_week is not None:
            # Fresh display names: dashboard alias first (user_aliases),
            # then the users row; the recorded display_name is the
            # fallback for members who've since left.
            latest_rows = await db.fetchall(
                """SELECT wa.award, wa.discord_user_id,
                        COALESCE(
                            (SELECT NULLIF(a.alias, '') FROM user_aliases a
                             WHERE a.user_id = wa.discord_user_id),
                            NULLIF(
                                CASE
                                    WHEN COALESCE(u.nickname, '') = '' THEN u.discord_tag
                                    ELSE u.nickname
                                END,
                                ''),
                            wa.display_name),
                        wa.value, wa.detail
                    FROM weekly_awards wa
                        LEFT JOIN users u ON u.user_id = wa.discord_user_id
                    WHERE wa.week_start = %s
                    ORDER BY wa.id""",
                (latest_week,),
            )
        count_rows = await db.fetchall(
            """SELECT wa.award,
                    COALESCE(
                        (SELECT NULLIF(a.alias, '') FROM user_aliases a
                         WHERE a.user_id = wa.discord_user_id),
                        NULLIF(
                            CASE
                                WHEN COALESCE(u.nickname, '') = '' THEN u.discord_tag
                                ELSE u.nickname
                            END,
                            ''),
                        MAX(wa.display_name)),
                    COUNT(*)
                FROM weekly_awards wa
                    LEFT JOIN users u ON u.user_id = wa.discord_user_id
                GROUP BY wa.award, wa.discord_user_id, u.nickname, u.discord_tag"""
        )

        blocks = awards.build_cabinet_blocks(latest_week, latest_rows, count_rows)
        self.bot.logging.info(
            f"Posting weekly awards trophy cabinet to #{getattr(channel, 'name', '?')} "
            f"({channel_id}) — latest week {latest_week}: {len(latest_rows)} row(s), "
            f"all-time: {len(count_rows)} row(s)"
        )
        await leaderboard.wipe_and_post(channel, blocks, self.bot.logging)
        return True

    # ----------------------------------------------------------------- loop

    @tasks.loop(minutes=CHECK_MINUTES)
    async def awards_tick(self):
        await self.bot.wait_until_ready()
        # Watchdog input: set on EVERY tick (including the six non-Monday
        # days) so the heartbeat cog can tell "loop frozen" apart from
        # "no ceremony due".
        self.awards_tick_last_fired = dt.datetime.now()

        now_london = dt.datetime.now(awards.LONDON)
        # "Not before Monday CEREMONY_HOUR of the current week" — deliberately
        # a time-ordering check, not `weekday() == 0`. Under the weekday gate a
        # ceremony that failed for its Monday (bot down, or an exception inside
        # _run_ceremony) could never be retried: Tuesday's ticks bailed here,
        # and by the next Monday previous_week_bounds had moved on, so the week
        # was lost silently. Now a late tick any day of the week still posts
        # the week that ended, and idempotency comes from _already_posted
        # (rows + bot_config marker) rather than from the calendar.
        this_monday, _ = awards.week_bounds(now_london)
        if now_london < this_monday + dt.timedelta(hours=CEREMONY_HOUR):
            return
        week_start, week_end = awards.previous_week_bounds(now_london)
        if await self._already_posted(week_start.date()):
            if self._skip_logged_week != week_start.date():
                self._skip_logged_week = week_start.date()
                self.bot.logging.info(
                    f"Weekly awards for week of {week_start.date()} already posted — "
                    "ceremony skipped (the cabinet reposts only after a ceremony or "
                    "/refresh_awards_cabinet)"
                )
            return
        await self._run_ceremony(week_start, week_end)

    @awards_tick.error
    async def awards_tick_error(self, exc: BaseException) -> None:
        """Auto-restart awards_tick on unhandled error.

        Default @tasks.loop behaviour on exception is to log + stop the
        loop, which would silently kill every future ceremony. The restart
        must run detached — this callback executes inside the dying loop
        task, where is_running() is still True (see utils/loop_restart.py).
        """
        self.bot.logging.error(f"awards_tick errored: {exc!r}, restarting in 60s")
        restart_loop_later(
            self.awards_tick,
            name="awards_tick",
            log=self.bot.logging,
            still_active=lambda: self.bot.get_cog("WeeklyAwards") is self,
        )

    # ------------------------------------------------------------- commands

    @app_commands.command(
        name="awards_preview",
        description="Preview this week's award standings so far (records nothing)",
    )
    async def awards_preview(self, ctx: discord.Interaction):
        await ctx.response.defer(ephemeral=True)
        now_london = dt.datetime.now(awards.LONDON)
        week_start, _week_end = awards.week_bounds(now_london)
        # Same adjustments path as the real ceremony, so the preview
        # shows exactly what Monday will post (dashboard overrides,
        # exclusions and disabled awards included).
        adjustments = await awards.fetch_adjustments(week_start.date())
        inputs = await awards.fetch_inputs(week_start, now_london)
        results, season_reset, forced_applied, below_min, runners_up = awards.compute_results(
            inputs, adjustments
        )
        history = await awards.fetch_prior_winner_history(week_start.date())
        header = (
            f"\U0001f3c6 **Weekly Awards — preview** — week of "
            f"<t:{awards.week_epoch(week_start.date())}:d> so far (nothing recorded)"
        )
        blocks = awards.build_ceremony_blocks(
            week_start.date(),
            results,
            header=header,
            season_reset=season_reset,
            disabled=adjustments.disabled,
            taglines=adjustments.taglines,
            forced=forced_applied,
            measures=awards.measure_notes(adjustments),
            below_min=below_min,
            commentary=awards.build_commentaries(
                week_start.date(), results, runners_up, history, forced_applied, adjustments
            ),
        )
        if adjustments.disabled:
            blocks.append(
                "*Disabled for this week's ceremony (bot_config): "
                + ", ".join(
                    awards.AWARDS[a].title for a in awards.AWARD_ORDER if a in adjustments.disabled
                )
                + "*\n"
            )
        for message_text in leaderboard.chunk_blocks(blocks):
            await ctx.followup.send(
                message_text,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @app_commands.command(
        name="refresh_awards_cabinet",
        description="Re-render and repost the weekly awards trophy cabinet",
    )
    async def refresh_awards_cabinet(self, ctx: discord.Interaction):
        await ctx.response.defer(ephemeral=True)
        posted = await self.refresh_cabinet()
        if posted:
            await ctx.followup.send("Trophy cabinet refreshed", ephemeral=True)
        else:
            await ctx.followup.send(
                "Cabinet not posted — set awards_channel_id in .env "
                "(or the channel is unavailable)",
                ephemeral=True,
            )


async def setup(bot: MyDiscordBot):
    await bot.add_cog(WeeklyAwards(bot))
