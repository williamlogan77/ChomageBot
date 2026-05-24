"""Persistent match-stats control panel + ephemeral chart explorer.

A single message sits in the dedicated match-stats channel. Two ways to
enter the explorer from the panel:

  - Pick a player from the dropdown → ephemeral preset to that person.
  - Click a chart button → ephemeral preset to "all players" + that chart.

Either way an ``_ExplorerView`` opens for the clicker only. Inside the
ephemeral they get the same player dropdown + chart-switcher buttons so
they can keep pivoting without spamming the channel.

The panel survives bot restarts: every component has a stable
``custom_id`` and the view is re-registered on cog load. Use
``/match_stats_panel`` (admin) to (re-)post it; the dropdown's options
are baked into the message at post time, so re-run after adding new
tracked players.

Analysis lives in ``utils.match_analysis``, shared with anywhere else
that wants to render these charts.
"""

from __future__ import annotations

import asyncio
import io
import logging

import discord
import matplotlib

matplotlib.use("Agg")  # headless render — no display backend needed
import matplotlib.pyplot as plt
from discord import app_commands
from discord.ext import commands, tasks
from utils import match_analysis as analysis

log = logging.getLogger(__name__)

PNG_DPI = 110

# The panel lives in exactly this channel — /match_stats_panel always
# targets it, regardless of where the slash command was invoked.
PANEL_CHANNEL_ID = 1507729687529652234

EXPLORER_TIMEOUT_SECONDS = 900

# Discord select menus cap at 25 options total. Reserve slot 0 for the
# "All players" aggregate, leaving 24 person slots.
SELECT_PERSON_LIMIT = 24

# Stable prefix for every persistent component's custom_id. Changing
# this orphans existing panel messages — clicks won't dispatch to the
# new view until /match_stats_panel re-posts.
PANEL_CUSTOM_ID_PREFIX = "ms:panel"
PANEL_SELECT_ID = f"{PANEL_CUSTOM_ID_PREFIX}:person"

# Marker used by the sticky-pin loop to recognise an existing panel
# message in channel history. Must match the title set in
# ``_build_panel_embed``.
PANEL_EMBED_TITLE = "📊 ChomageBot — Match Stats"

# How often the sticky-pin loop checks whether the panel is still the
# most-recent message in PANEL_CHANNEL_ID. Cheap (one history call),
# generous enough to avoid API noise.
STICKY_CHECK_MINUTES = 5

# How many recent messages to scan on cog load when trying to recover
# the panel's message ID after a restart.
STICKY_RECOVERY_HISTORY_LIMIT = 50

# Sentinel value used by the persistence stub so the dropdown is valid
# even before /match_stats_panel has been run with real data.
ALL_VALUE = "__all__"
PLACEHOLDER_VALUE = "__placeholder__"

# (label, emoji, plot_fn, embed_title) — single source of truth for which
# charts exist + how they appear in the UI. Add an entry here and a
# matching entry in ``analysis.ALL_PLOTS`` to expose a new chart.
CHART_DEFS = [
    ("Summary", "📑", analysis.plot_stats_summary, "Stats summary"),
    (
        "Logit",
        "🧠",
        analysis.plot_logistic_coefficients,
        "Logistic regression — controlled coefficients",
    ),
    (
        "Compare",
        "🆚",
        analysis.plot_player_comparison,
        "Player comparison — every metric side by side",
    ),
    ("Activity", "📅", analysis.plot_activity_over_time, "Activity over time"),
    ("Rank", "🏅", analysis.plot_rank_trajectory, "Rank trajectory (with WR overlay)"),
    ("LP", "💰", analysis.plot_lp_economics, "LP economics — gain per win vs loss per loss"),
    ("Cumulative WR", "📊", analysis.plot_cumulative_winrate, "Cumulative win rate"),
    ("Progression", "📈", analysis.plot_player_progression, "Lifetime progression"),
    ("KDA", "⚔️", analysis.plot_kda_vs_outcome, "KDA vs outcome"),
    ("Duration", "⏱️", analysis.plot_duration_vs_outcome, "Game duration vs outcome"),
    ("Champs W/L", "🏆", analysis.plot_champion_winrate, "Champion winners vs losers"),
    ("Picks", "✨", analysis.plot_champion_picks, "Champion picks — Bayesian-shrunk WR delta"),
    ("Champ curves", "🎯", analysis.plot_champion_learning_curve, "Champion learning curves"),
    ("Hour", "🕐", analysis.plot_hour_of_day, "Hour of day"),
    ("Day", "🗓️", analysis.plot_day_of_week, "Day of week"),
    ("Heatmap", "🔥", analysis.plot_hour_dow_heatmap, "Hour × day heatmap"),
    ("Tilt", "😤", analysis.plot_streak_recovery, "Win rate vs entering loss streak"),
    ("Gap", "⏰", analysis.plot_time_since_prev, "Win rate vs time since previous game"),
    ("Sessions", "🎮", analysis.plot_session_analysis, "Session-grouping analysis"),
    (
        "Duos / H2H",
        "🤝",
        analysis.plot_duo_winrate,
        "Duos (same team) + head-to-head (opposite teams)",
    ),
]


def _figure_to_file(fig, name: str = "chart.png") -> discord.File:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=PNG_DPI, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return discord.File(buf, filename=name)


def _build_panel_embed() -> discord.Embed:
    """The static description sitting above the panel buttons."""
    embed = discord.Embed(
        title=PANEL_EMBED_TITLE,
        description=(
            "Browse what factors influence wins vs. losses across every "
            "tracked Ranked Solo/Duo game.\n\n"
            "**Use the dropdown** to focus on one person (their Riot accounts "
            "are aggregated), or **click a chart button** to open the all-"
            "players view. A private explorer opens for you — only you see it.\n\n"
            "Inside the explorer you can pivot between charts and players "
            "without leaving the message."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="What's in here",
        value=(
            "📑 **Summary** — headline numbers at a glance (start here)\n"
            "🆚 **Compare** — every player on every metric, side by side\n"
            "🧠 **Logit** — multivariate logistic regression of win drivers\n"
            "📅 **Activity** — games / month + WR by month\n"
            "📊 **Cumulative WR** · 📈 **Progression** (% of career)\n"
            "⚔️ **KDA** · ⏱️ **Duration** · 🏆 **Champs W/L** (Wilson CI) · 🎯 **Champ curves**\n"
            "🕐 **Hour** · 🗓️ **Day** · 🔥 **Heatmap**  (χ² p-values on temporal effects)\n"
            "😤 **Tilt** · ⏰ **Gap** since previous game\n"
            "🤝 **Duos** — same-team partnerships and their winrate"
        ),
        inline=False,
    )
    embed.set_footer(
        text="Data refreshes as games are recorded. Re-run /match_stats_panel after adding new tracked players."
    )
    return embed


def _build_person_options(person_rows) -> list[discord.SelectOption]:
    """Translate the ``people_summary`` rows into select options. Each
    option's value is the canonical ``person`` key; the label includes
    a (N accounts) suffix when a person has multiple Riot accounts so
    the user can see the aggregation at a glance.
    """
    options = [
        discord.SelectOption(
            label="All players (aggregate)", value=ALL_VALUE, emoji="🌐", default=True
        )
    ]
    for row in person_rows[:SELECT_PERSON_LIMIT]:
        suffix = f"  ·  {row['account_count']} accounts" if row["account_count"] > 1 else ""
        label = f"{row['person']}{suffix}"
        # SelectOption labels max 100 chars — truncate just in case.
        if len(label) > 100:
            label = label[:97] + "…"
        options.append(
            discord.SelectOption(
                label=label,
                value=str(row["person"]),
                description=f"{int(row['games']):,} games",
            )
        )
    return options


# --- Explorer view (ephemeral) ---------------------------------------------


class _ExplorerView(discord.ui.View):
    """Per-user, ephemeral explorer. Holds df + selection in memory only."""

    def __init__(self, df, invoker_id: int, chart_idx: int, person: str | None):
        super().__init__(timeout=EXPLORER_TIMEOUT_SECONDS)
        self.df = df
        self.invoker_id = invoker_id
        self.chart_idx = chart_idx
        self.person: str | None = person

        person_rows = analysis.people_summary(df).to_dict("records")
        options = _build_person_options(person_rows)
        for opt in options:
            opt.default = (person is None and opt.value == ALL_VALUE) or (
                person is not None and opt.value == person
            )

        select = discord.ui.Select(placeholder="Pick a player…", options=options, row=0)
        select.callback = self._on_person_select
        self.add_item(select)
        self._select = select

        for idx, (label, emoji, _, _) in enumerate(CHART_DEFS):
            button = discord.ui.Button(label=label, emoji=emoji, row=1 + (idx // 5))
            button.callback = self._make_chart_callback(idx)
            self.add_item(button)
        self._refresh_highlights()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "That explorer belongs to someone else. Use the panel to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

    async def _on_person_select(self, interaction: discord.Interaction) -> None:
        chosen = self._select.values[0]
        self.person = None if chosen == ALL_VALUE else chosen
        for opt in self._select.options:
            opt.default = opt.value == chosen
        await self._rerender(interaction)

    def _make_chart_callback(self, idx: int):
        async def _cb(interaction: discord.Interaction) -> None:
            self.chart_idx = idx
            await self._rerender(interaction)

        return _cb

    def _refresh_highlights(self) -> None:
        """Style the active chart's button primary so the user can see
        which chart they're viewing."""
        btn_count = 0
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.style = (
                    discord.ButtonStyle.primary
                    if btn_count == self.chart_idx
                    else discord.ButtonStyle.secondary
                )
                btn_count += 1

    async def _rerender(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self._refresh_highlights()

        label, emoji, fn, title = CHART_DEFS[self.chart_idx]
        try:
            # Push matplotlib to a worker thread — bigger charts can take
            # a second or two and would otherwise stall the gateway.
            fig = await asyncio.to_thread(fn, self.df, self.person)
        except Exception as exc:
            log.error(f"chart {label!r} render failed: {exc!r}")
            await interaction.followup.send(f"Chart failed: {exc!r}", ephemeral=True)
            return

        who = self.person or "all players"
        embed = discord.Embed(title=f"{emoji} {title} — {who}")
        embed.set_image(url="attachment://chart.png")
        file = _figure_to_file(fig)
        await interaction.edit_original_response(embed=embed, attachments=[file], view=self)


# --- Persistent panel ------------------------------------------------------


class MatchStatsPanel(discord.ui.View):
    """Persistent control panel. ``person_options`` is set at post-time
    from a DB query; the persistence-registration instance can pass None
    (its options don't matter for callback dispatch — discord.py routes
    by custom_id)."""

    def __init__(self, person_rows=None):
        super().__init__(timeout=None)

        if person_rows is not None:
            options = _build_person_options(person_rows)
        else:
            # Persistence stub — at least one option is required for the
            # select to validate.
            options = [
                discord.SelectOption(
                    label="All players (aggregate)", value=ALL_VALUE, default=True
                ),
                discord.SelectOption(label="—", value=PLACEHOLDER_VALUE),
            ]

        select = discord.ui.Select(
            placeholder="Pick a player to explore…",
            options=options,
            row=0,
            custom_id=PANEL_SELECT_ID,
        )
        select.callback = self._on_select
        self.add_item(select)

        for idx, (label, emoji, _, _) in enumerate(CHART_DEFS):
            row = 1 + (idx // 5)
            button = discord.ui.Button(
                label=label,
                emoji=emoji,
                row=row,
                style=discord.ButtonStyle.secondary,
                custom_id=f"{PANEL_CUSTOM_ID_PREFIX}:c{idx}",
            )
            button.callback = self._make_chart_callback(idx)
            self.add_item(button)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("MatchAnalysis")
        if cog is None:
            await interaction.response.send_message(
                "Match-stats cog isn't loaded — bot may be restarting. Try again in a moment.",
                ephemeral=True,
            )
            return
        # discord.py passes the chosen value(s) via interaction.data["values"].
        chosen = interaction.data["values"][0]
        if chosen == PLACEHOLDER_VALUE:
            await interaction.response.send_message(
                "This panel is the persistence stub — re-run /match_stats_panel to install the live one.",
                ephemeral=True,
            )
            return
        person = None if chosen == ALL_VALUE else chosen
        # Default chart for a person pick = Activity (calendar-time view).
        await cog.open_explorer(interaction, chart_idx=0, person=person)

    def _make_chart_callback(self, idx: int):
        async def _cb(interaction: discord.Interaction) -> None:
            cog = interaction.client.get_cog("MatchAnalysis")
            if cog is None:
                await interaction.response.send_message(
                    "Match-stats cog isn't loaded — bot may be restarting. Try again in a moment.",
                    ephemeral=True,
                )
                return
            # Chart-button click defaults to the aggregate view — the user
            # can pick a person inside the ephemeral.
            await cog.open_explorer(interaction, chart_idx=idx, person=None)

        return _cb


# --- Cog -------------------------------------------------------------------


class MatchAnalysis(commands.Cog):
    """Slash command for posting the panel + the open_explorer entry
    point used by the panel's components."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        # Persistence stub registration — actual options don't matter for
        # dispatch; discord.py routes by custom_id.
        self._panel_view = MatchStatsPanel()
        bot.add_view(self._panel_view)
        # Tracks the most recent panel message so the sticky loop knows
        # which message to delete when re-posting. None until either the
        # admin slash command posts one, or recovery finds an existing
        # panel in channel history.
        self._panel_message_id: int | None = None
        self.sticky_panel.start()

    def cog_unload(self) -> None:
        self.sticky_panel.cancel()
        self._panel_view.stop()

    @app_commands.command(
        name="match_stats_panel",
        description="(Re-)post the persistent match-stats control panel in the dedicated channel",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    async def match_stats_panel(self, ctx: discord.Interaction) -> None:
        await ctx.response.defer(ephemeral=True)

        channel = self.bot.get_channel(PANEL_CHANNEL_ID) or await self.bot.fetch_channel(
            PANEL_CHANNEL_ID
        )
        if channel is None:
            await ctx.followup.send(
                f"I can't find the configured panel channel (id={PANEL_CHANNEL_ID}). "
                "Check the channel exists and the bot can see it.",
                ephemeral=True,
            )
            return

        try:
            msg, person_count = await self._post_panel(channel)
        except discord.Forbidden:
            await ctx.followup.send(
                f"I don't have permission to post in <#{PANEL_CHANNEL_ID}>.", ephemeral=True
            )
            return
        except Exception as exc:
            self.bot.logging.error(f"match_stats_panel failed: {exc!r}")
            await ctx.followup.send(f"Failed to post panel: {exc!r}", ephemeral=True)
            return

        await ctx.followup.send(
            f"Panel posted in <#{channel.id}> → [jump]({msg.jump_url}).\n"
            f"Dropdown lists {person_count} players. "
            "If there's a previous panel above this one, delete it manually — "
            "both will work but you probably don't want two.",
            ephemeral=True,
        )

    async def _post_panel(self, channel: discord.abc.Messageable) -> tuple[discord.Message, int]:
        """Load player rows, build the view, post the panel. Shared by the
        admin slash command and the sticky-pin loop. Updates
        ``self._panel_message_id`` so the next sticky check knows which
        message represents the live panel.

        Returns ``(message, person_count)``. Raises ``discord.Forbidden``
        if the bot lacks send permission; other exceptions propagate.
        """
        df = await asyncio.to_thread(analysis.load_matches, self.bot.db_path)
        person_rows = analysis.people_summary(df).to_dict("records") if not df.empty else []
        view = MatchStatsPanel(person_rows=person_rows)
        msg = await channel.send(
            embed=_build_panel_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self._panel_message_id = msg.id
        return msg, len(person_rows)

    def _is_panel_message(self, message: discord.Message) -> bool:
        """True if this message looks like our match-stats panel.

        Identification is by author + embed title — robust across both
        the persistence-stub view and the live view, and doesn't depend
        on ``message.components`` introspection which is fiddly across
        discord.py versions.
        """
        if message.author.id != self.bot.user.id:
            return False
        if not message.embeds:
            return False
        return message.embeds[0].title == PANEL_EMBED_TITLE

    @tasks.loop(minutes=STICKY_CHECK_MINUTES)
    async def sticky_panel(self) -> None:
        """Re-post the panel if it's been bumped off being the most-recent
        message in PANEL_CHANNEL_ID.

        Cold start (no panel ever posted via the admin command, no panel
        found in history) is a no-op — we don't auto-create panels the
        admin never asked for.
        """
        channel = self.bot.get_channel(PANEL_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(PANEL_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden):
                return

        # Recover the panel's message ID after a restart by scanning the
        # last N messages for our embed signature. Only the *first* panel
        # we see (newest-first iteration) matters — older ones are stale.
        if self._panel_message_id is None:
            async for old in channel.history(limit=STICKY_RECOVERY_HISTORY_LIMIT):
                if self._is_panel_message(old):
                    self._panel_message_id = old.id
                    self.bot.logging.info(
                        f"Sticky panel: recovered existing panel message id={old.id}"
                    )
                    break
            if self._panel_message_id is None:
                # No panel has ever been posted (or it scrolled off). Wait
                # for an admin to invoke /match_stats_panel before doing
                # anything.
                return

        # Topmost message check: if the latest message in the channel is
        # the panel, we're sticky and there's nothing to do.
        latest = None
        async for m in channel.history(limit=1):
            latest = m
            break
        if latest is not None and latest.id == self._panel_message_id:
            self.bot.logging.info("Sticky panel: still topmost, no action")
            return

        bumper = f"{latest.id} from {latest.author}" if latest is not None else "(empty channel)"
        self.bot.logging.info(f"Sticky panel: bumped by message {bumper}, re-posting")

        # Delete the previous panel before re-posting so the channel
        # doesn't accumulate stale panels. If the message is already gone
        # (manual delete, etc.) discord.NotFound is fine.
        old_id = self._panel_message_id
        try:
            old_msg = await channel.fetch_message(old_id)
            await old_msg.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            self.bot.logging.warning(
                f"Sticky panel: missing permission to delete old panel {old_id}; "
                "re-posting anyway"
            )
        except Exception as exc:
            self.bot.logging.error(f"Sticky panel: failed to delete old panel {old_id}: {exc!r}")

        try:
            await self._post_panel(channel)
        except discord.Forbidden:
            self.bot.logging.warning(
                f"Sticky panel: missing permission to post in <#{PANEL_CHANNEL_ID}>"
            )
        # Any other exception bubbles into sticky_panel_error → restart.

    @sticky_panel.before_loop
    async def before_sticky_panel(self) -> None:
        await self.bot.wait_until_ready()

    @sticky_panel.error
    async def sticky_panel_error(self, exc: BaseException) -> None:
        """Auto-restart sticky_panel on unhandled error.

        Default @tasks.loop behaviour on exception is log + stop. Mirror
        the post_ranks / stream_matches recovery pattern so a transient
        failure (rate-limit blip, Gateway hiccup) doesn't permanently
        disable sticky behaviour.
        """
        self.bot.logging.error(f"sticky_panel errored: {exc!r}, restarting in 60s")
        await asyncio.sleep(60)
        if not self.sticky_panel.is_running():
            self.sticky_panel.start()

    async def open_explorer(
        self, interaction: discord.Interaction, chart_idx: int, person: str | None
    ) -> None:
        """Entry point called by panel components: load data, render the
        requested chart for the requested person, and send an ephemeral
        explorer to the clicker."""
        await interaction.response.defer(ephemeral=True)
        try:
            df = await asyncio.to_thread(analysis.load_matches, self.bot.db_path)
        except Exception as exc:
            self.bot.logging.error(f"match_stats data load failed: {exc!r}")
            await interaction.followup.send(f"Failed to load match data: {exc!r}", ephemeral=True)
            return

        if df.empty:
            await interaction.followup.send(
                "No match data yet — run /backfill_all first.", ephemeral=True
            )
            return

        label, emoji, fn, title = CHART_DEFS[chart_idx]
        try:
            fig = await asyncio.to_thread(fn, df, person)
        except Exception as exc:
            self.bot.logging.error(f"initial chart {label!r} render failed: {exc!r}")
            await interaction.followup.send(f"Chart failed: {exc!r}", ephemeral=True)
            return

        who = person or "all players"
        embed = discord.Embed(title=f"{emoji} {title} — {who}")
        embed.set_footer(
            text=(
                f"{len(df):,} games · {df['person'].nunique()} people "
                f"({df['riot_account'].nunique()} Riot accounts) · "
                f"{df['game_start'].min().date()} → {df['game_start'].max().date()}"
            )
        )
        embed.set_image(url="attachment://chart.png")
        file = _figure_to_file(fig)
        view = _ExplorerView(df, invoker_id=interaction.user.id, chart_idx=chart_idx, person=person)
        await interaction.followup.send(embed=embed, file=file, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MatchAnalysis(bot))
