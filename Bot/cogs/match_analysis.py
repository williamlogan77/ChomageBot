"""Persistent match-stats control panel + ephemeral chart explorer.

A single message sits in a dedicated channel: 10 chart buttons in front of
an embedded description. Clicking any button opens an ephemeral
``_ExplorerView`` for the clicker — only they see it — with that chart
preloaded and a player select + chart-switcher buttons inside, so they can
keep exploring without spamming the channel.

The panel survives bot restarts because every button uses a stable
``custom_id`` and the view is re-registered on cog load. To install it in
a channel, run ``/match_stats_panel`` (admin) in that channel.

Analysis lives in ``utils.match_analysis``, shared with the exploration
notebook and the batch runner.
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
from discord.ext import commands
from utils import match_analysis as analysis

log = logging.getLogger(__name__)

PNG_DPI = 110

# Ephemeral explorer goes inactive after this many seconds. The persistent
# panel itself has no timeout — it's the entry point.
EXPLORER_TIMEOUT_SECONDS = 900

# Discord select menus cap at 25 options. Reserve slot 0 for "All players".
SELECT_PLAYER_LIMIT = 24

# Stable prefix for every panel button's custom_id. Changing this orphans
# any panel messages already posted — they'll still display but clicks won't
# dispatch to the new view.
PANEL_CUSTOM_ID_PREFIX = "ms:panel"

# (label, emoji, plot_fn, embed_title) — single source of truth for which
# charts exist + how they appear in the UI. Add an entry here to expose a
# new chart in both the panel and the ephemeral explorer.
CHART_DEFS = [
    ("Progression", "📈", analysis.plot_player_progression, "Lifetime progression"),
    ("Cumulative WR", "📊", analysis.plot_cumulative_winrate, "Cumulative win rate"),
    ("KDA", "⚔️", analysis.plot_kda_vs_outcome, "KDA vs outcome"),
    ("Duration", "⏱️", analysis.plot_duration_vs_outcome, "Game duration vs outcome"),
    ("Champs W/L", "🏆", analysis.plot_champion_winrate, "Champion winners vs losers"),
    ("Champ curves", "🎯", analysis.plot_champion_learning_curve, "Champion learning curves"),
    ("Hour", "🕐", analysis.plot_hour_of_day, "Hour of day"),
    ("Day", "📅", analysis.plot_day_of_week, "Day of week"),
    ("Heatmap", "🔥", analysis.plot_hour_dow_heatmap, "Hour × day heatmap"),
    ("Tilt", "😤", analysis.plot_streak_recovery, "Win rate vs entering loss streak"),
]


def _figure_to_file(fig, name: str = "chart.png") -> discord.File:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=PNG_DPI, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return discord.File(buf, filename=name)


def _build_panel_embed() -> discord.Embed:
    """The static description shown above the persistent panel buttons."""
    embed = discord.Embed(
        title="📊 ChomageBot — Match Stats",
        description=(
            "Browse what factors influence wins vs. losses across every "
            "tracked Ranked Solo/Duo game.\n\n"
            "**Click any chart below.** A private explorer opens with that "
            "chart preloaded — you can filter by player or pivot to another "
            "chart without leaving it. Your view is ephemeral; only you see it."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="What's in here",
        value=(
            "📈 **Progression** — do you get better or worse with reps?\n"
            "📊 **Cumulative WR** — hot/cold streaks over time\n"
            "⚔️ **KDA** · ⏱️ **Duration** — does carrying matter? do stomps swing?\n"
            "🏆 **Champs W/L** — best vs worst picks\n"
            "🎯 **Champ curves** — does grinding a champ pay off?\n"
            "🕐 **Hour** · 📅 **Day** · 🔥 **Heatmap** — when do you play well?\n"
            "😤 **Tilt** — does a loss streak predict the next L?"
        ),
        inline=False,
    )
    embed.set_footer(text="Data refreshes automatically — every game played is recorded.")
    return embed


class _ExplorerView(discord.ui.View):
    """Ephemeral, per-user explorer. Holds the loaded df + current selection
    in memory only — discarded when the user dismisses the ephemeral or
    the view times out."""

    def __init__(self, df, invoker_id: int, chart_idx: int):
        super().__init__(timeout=EXPLORER_TIMEOUT_SECONDS)
        self.df = df
        self.invoker_id = invoker_id
        self.chart_idx = chart_idx
        self.player: str | None = None  # None = aggregate

        players = sorted(df["player"].unique().tolist())[:SELECT_PLAYER_LIMIT]
        select = discord.ui.Select(
            placeholder="Player filter…",
            options=[
                discord.SelectOption(
                    label="All players (aggregate)", value="__all__", default=True
                ),
                *[discord.SelectOption(label=p, value=p) for p in players],
            ],
            row=0,
        )
        select.callback = self._on_player_select
        self.add_item(select)
        self._select = select

        # Chart-switcher buttons. No custom_id — view is ephemeral, scoped
        # to this user's session, so persistence doesn't apply.
        for idx, (label, emoji, _, _) in enumerate(CHART_DEFS):
            button = discord.ui.Button(label=label, emoji=emoji, row=1 + (idx // 5))
            button.callback = self._make_chart_callback(idx)
            self.add_item(button)
        self._refresh_highlights()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "That explorer belongs to someone else. Click a button on the panel "
                "to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

    async def _on_player_select(self, interaction: discord.Interaction) -> None:
        chosen = self._select.values[0]
        self.player = None if chosen == "__all__" else chosen
        # Keep the dropdown showing the new pick when it's reopened.
        for opt in self._select.options:
            opt.default = opt.value == chosen
        await self._rerender(interaction)

    def _make_chart_callback(self, idx: int):
        async def _cb(interaction: discord.Interaction) -> None:
            self.chart_idx = idx
            await self._rerender(interaction)

        return _cb

    def _refresh_highlights(self) -> None:
        """Style the active chart's button so the user can see which chart
        they're looking at."""
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
            fig = await asyncio.to_thread(fn, self.df, self.player)
        except Exception as exc:
            log.error(f"chart {label!r} render failed: {exc!r}")
            await interaction.followup.send(f"Chart failed: {exc!r}", ephemeral=True)
            return

        who = self.player or "all players"
        embed = discord.Embed(title=f"{emoji} {title} — {who}")
        embed.set_image(url="attachment://chart.png")
        file = _figure_to_file(fig)
        await interaction.edit_original_response(embed=embed, attachments=[file], view=self)


class MatchStatsPanel(discord.ui.View):
    """Persistent control panel. Stays in a channel across bot restarts —
    every button has a stable ``custom_id`` and the view is re-registered
    on cog load."""

    def __init__(self):
        super().__init__(timeout=None)
        for idx, (label, emoji, _, _) in enumerate(CHART_DEFS):
            button = discord.ui.Button(
                label=label,
                emoji=emoji,
                row=idx // 5,
                style=discord.ButtonStyle.secondary,
                custom_id=f"{PANEL_CUSTOM_ID_PREFIX}:{idx}",
            )
            button.callback = self._make_callback(idx)
            self.add_item(button)

    def _make_callback(self, idx: int):
        async def _cb(interaction: discord.Interaction) -> None:
            cog = interaction.client.get_cog("MatchAnalysis")
            if cog is None:
                await interaction.response.send_message(
                    "Match-stats cog isn't loaded — bot may be restarting. Try again in a moment.",
                    ephemeral=True,
                )
                return
            await cog.open_explorer(interaction, idx)

        return _cb


class MatchAnalysis(commands.Cog):
    """Hosts the persistent-panel admin command and the per-click explorer
    entry point used by the panel buttons."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        # Register the persistent view so its buttons keep working across
        # restarts. discord.py dispatches by custom_id, so a freshly-loaded
        # cog can keep handling clicks on a panel message posted months ago.
        self._panel_view = MatchStatsPanel()
        bot.add_view(self._panel_view)

    def cog_unload(self) -> None:
        self._panel_view.stop()

    @app_commands.command(
        name="match_stats_panel",
        description="Post the persistent match-stats control panel in this channel (admin)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    async def match_stats_panel(self, ctx: discord.Interaction) -> None:
        await ctx.response.defer(ephemeral=True)
        try:
            channel = ctx.channel
            view = MatchStatsPanel()
            msg = await channel.send(embed=_build_panel_embed(), view=view)
        except discord.Forbidden:
            await ctx.followup.send(
                "I don't have permission to post in this channel.", ephemeral=True
            )
            return
        except Exception as exc:
            self.bot.logging.error(f"match_stats_panel post failed: {exc!r}")
            await ctx.followup.send(f"Failed to post panel: {exc!r}", ephemeral=True)
            return

        await ctx.followup.send(
            f"Panel posted in <#{channel.id}> → [jump]({msg.jump_url}).\n"
            "If you had a previous panel here, delete it manually — both will work, "
            "but you probably don't want two.",
            ephemeral=True,
        )

    async def open_explorer(self, interaction: discord.Interaction, chart_idx: int) -> None:
        """Panel-button click handler: load data, render the requested chart,
        and send an ephemeral explorer to the clicker."""
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
            fig = await asyncio.to_thread(fn, df, None)  # aggregate by default
        except Exception as exc:
            self.bot.logging.error(f"initial chart {label!r} render failed: {exc!r}")
            await interaction.followup.send(f"Chart failed: {exc!r}", ephemeral=True)
            return

        embed = discord.Embed(title=f"{emoji} {title} — all players")
        embed.set_footer(
            text=(
                f"{len(df):,} games · {df['player'].nunique()} players · "
                f"{df['game_start'].min().date()} → {df['game_start'].max().date()}"
            )
        )
        embed.set_image(url="attachment://chart.png")
        file = _figure_to_file(fig)
        view = _ExplorerView(df, invoker_id=interaction.user.id, chart_idx=chart_idx)
        await interaction.followup.send(embed=embed, file=file, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MatchAnalysis(bot))
