"""Interactive match-stats explorer.

Slash command ``/match_stats`` opens an ephemeral view with a player
dropdown and one button per chart category. Clicking a button regenerates
the relevant chart for the currently-selected player (or the aggregate)
and edits the message in place.

The analysis is in ``utils.match_analysis`` — same module used by the
exploration notebook and the batch runner, so a tweak there shows up in
all three surfaces.
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

# Maximum size of one Discord file upload. We render at 110 DPI which lands
# every chart well under this, but the budget exists if a future chart grows.
PNG_DPI = 110

# View timeout — after this, buttons stop working and the user runs /match_stats
# again. Long enough for genuine exploration, short enough that we're not
# holding pandas DataFrames in memory indefinitely.
VIEW_TIMEOUT_SECONDS = 900

# Discord select menus cap at 25 options. We always reserve slot 0 for the
# aggregate view, leaving 24 player slots.
SELECT_PLAYER_LIMIT = 24

CHART_DEFS = [
    # (label, plot_fn, embed_title)
    ("Progression", analysis.plot_player_progression, "Lifetime progression"),
    ("Cumulative WR", analysis.plot_cumulative_winrate, "Cumulative win rate"),
    ("KDA", analysis.plot_kda_vs_outcome, "KDA vs outcome"),
    ("Duration", analysis.plot_duration_vs_outcome, "Game duration vs outcome"),
    ("Champs W/L", analysis.plot_champion_winrate, "Champion winners vs losers"),
    ("Champ curves", analysis.plot_champion_learning_curve, "Champion learning curves"),
    ("Hour", analysis.plot_hour_of_day, "Hour-of-day"),
    ("Day", analysis.plot_day_of_week, "Day-of-week"),
    ("Heatmap", analysis.plot_hour_dow_heatmap, "Hour × day heatmap"),
    ("Tilt", analysis.plot_streak_recovery, "Win rate vs entering loss streak"),
]


def _figure_to_file(fig, name: str = "chart.png") -> discord.File:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=PNG_DPI, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return discord.File(buf, filename=name)


class _StatsView(discord.ui.View):
    """The interactive component group. Held in memory per /match_stats call."""

    def __init__(self, df, invoker_id: int):
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.df = df
        self.invoker_id = invoker_id
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

        # Buttons go in rows 1 and 2 (5 each).
        for idx, (label, _, _) in enumerate(CHART_DEFS):
            row = 1 + (idx // 5)
            button = discord.ui.Button(label=label, row=row, style=discord.ButtonStyle.secondary)
            button.callback = self._make_chart_callback(idx)
            self.add_item(button)

    # Lock the view to whoever ran the command — otherwise clicks from
    # other users would race against each other.
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "This explorer was opened by someone else. Run /match_stats yourself.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        # Disable everything once we time out so the buttons read as inactive.
        for item in self.children:
            item.disabled = True

    async def _on_player_select(self, interaction: discord.Interaction) -> None:
        chosen = self._select.values[0]
        self.player = None if chosen == "__all__" else chosen
        # Move the "default" marker so the dropdown shows the new choice
        # next time it's opened.
        for opt in self._select.options:
            opt.default = opt.value == chosen
        await interaction.response.edit_message(view=self)

    def _make_chart_callback(self, idx: int):
        label, fn, title = CHART_DEFS[idx]

        async def _cb(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            try:
                # Matplotlib can stall the event loop on bigger charts;
                # push the render to a worker thread.
                fig = await asyncio.to_thread(fn, self.df, self.player)
            except Exception as exc:
                log.error(f"chart {label!r} failed: {exc!r}")
                await interaction.followup.send(f"Chart failed: {exc!r}", ephemeral=True)
                return

            who = self.player or "all players"
            embed = discord.Embed(title=f"{title} — {who}")
            embed.set_image(url="attachment://chart.png")
            file = _figure_to_file(fig)
            await interaction.edit_original_response(embed=embed, attachments=[file], view=self)

        return _cb


class MatchAnalysis(commands.Cog):
    """Slash command surface for the match-stats explorer."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")

    @app_commands.command(
        name="match_stats",
        description="Open the interactive match-stats explorer (ephemeral)",
    )
    @app_commands.guild_only()
    async def match_stats(self, ctx: discord.Interaction) -> None:
        await ctx.response.defer(ephemeral=True)
        try:
            df = await asyncio.to_thread(analysis.load_matches, self.bot.db_path)
        except Exception as exc:
            self.bot.logging.error(f"match_stats load failed: {exc!r}")
            await ctx.followup.send(f"Failed to load match data: {exc!r}", ephemeral=True)
            return

        if df.empty:
            await ctx.followup.send("No match data yet — run /backfill_all first.", ephemeral=True)
            return

        view = _StatsView(df, invoker_id=ctx.user.id)
        embed = discord.Embed(
            title="Match-stats explorer",
            description=(
                f"{len(df):,} games across {df['player'].nunique()} players "
                f"({df['game_start'].min().date()} → {df['game_start'].max().date()}).\n\n"
                "Use the dropdown to filter to one player, then click any chart button. "
                "The view expires after 15 minutes — re-run /match_stats to reopen."
            ),
        )
        await ctx.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MatchAnalysis(bot))
