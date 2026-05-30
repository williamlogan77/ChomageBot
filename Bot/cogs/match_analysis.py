"""Persistent match-stats control panel + public chart explorer.

A single message sits in the dedicated match-stats channel. Two ways to
enter the explorer from the panel:

  - Pick a player (or one of their Riot accounts) from the dropdown →
    chart preset to that selection.
  - Click a chart button → chart preset to "all players" + that chart.

Either way an ``_ExplorerView`` chart posts publicly so everyone in the
channel can see it. The in-message dropdown + chart-switcher buttons
are locked to the original clicker via ``interaction_check`` so randos
can't hijack someone else's view.

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
import datetime as dt
import io
import logging
import re
import sqlite3
import time

import discord
import matplotlib
import pandas as pd

matplotlib.use("Agg")  # headless render — no display backend needed
import matplotlib.pyplot as plt
from discord import app_commands
from discord.ext import commands, tasks
from utils import match_analysis as analysis

log = logging.getLogger(__name__)

PNG_DPI = 110

# 5-minute TTL cache around analysis.load_matches. Every chart click used
# to do a full SQLite read (~6500 rows into pandas) — the cache collapses
# bursts of clicks within the TTL down to a single read.
_DF_CACHE_TTL_SECONDS = 300
_df_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_df_cache_lock = asyncio.Lock()


async def _load_matches_cached(db_path: str) -> pd.DataFrame:
    """5-minute TTL cache around ``analysis.load_matches``.

    Lock-around-read-and-write is intentional: under a thundering-herd of
    simultaneous chart clicks (e.g. after a sticky-pin repost) we want
    one reader to land and the others to wait for the result rather than
    all doing parallel SQLite reads.

    The cache holds the DataFrame by reference — callers MUST NOT mutate
    it. Current plot helpers only slice (no in-place ops), so this is
    safe today; any future mutation site must ``df.copy()`` first.
    """
    async with _df_cache_lock:
        entry = _df_cache.get(db_path)
        now = time.monotonic()
        if entry is not None:
            cached_at, df = entry
            if now - cached_at < _DF_CACHE_TTL_SECONDS:
                log.info(f"DF cache hit, age={now - cached_at:.1f}s")
                return df
        log.info("DF cache miss, loading…")
        df = await asyncio.to_thread(analysis.load_matches, db_path)
        _df_cache[db_path] = (now, df)
        return df


# The panel lives in exactly this channel — /match_stats_panel always
# targets it, regardless of where the slash command was invoked.
PANEL_CHANNEL_ID = 1507729687529652234

EXPLORER_TIMEOUT_SECONDS = 900

# Stable prefix for every persistent component's custom_id. Changing
# this orphans existing panel messages — clicks won't dispatch to the
# new view until /match_stats_panel re-posts.
PANEL_CUSTOM_ID_PREFIX = "ms:panel"
PANEL_SELECT_ID = f"{PANEL_CUSTOM_ID_PREFIX}:person"
# Second board dropdown: surfaces the overflow analytics charts (the ones
# that otherwise only live behind the explorer's "More ▾"). Picking a chart
# posts it; picking the sentinel opens the full paginated More menu.
PANEL_MORE_SELECT_ID = f"{PANEL_CUSTOM_ID_PREFIX}:more"

# Marker used by the sticky-pin loop to recognise an existing panel
# message in channel history. Must match the title set in
# ``_build_panel_embed``.
PANEL_EMBED_TITLE = "📊 ChomageBot — Match Stats"

# Role name (case-sensitive) gating the admin slash commands. We use a
# role check rather than ``default_permissions`` because the friend
# group's admins don't have the built-in ``manage_guild`` perm — Discord
# would hide the commands from them.
CHOMAGE_KEEPER_ROLE = "Keeper of Chomage"


def _is_chomage_keeper(interaction: discord.Interaction) -> bool:
    """app_commands.check predicate: True iff invoker has the keeper role."""
    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    return any(role.name == CHOMAGE_KEEPER_ROLE for role in user.roles)


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
# Sentinel option in the board's More-analytics dropdown that opens the full
# paginated More menu (the dropdown itself can only hold Discord's 25-option
# max, so the rest live behind this entry).
MORE_SEE_ALL_VALUE = "__see_all__"

# Board layout: row 0 = player select, rows 1-3 = chart buttons, row 4 = the
# More-analytics select. That leaves room for 15 chart buttons (3 rows × 5).
# The 5 charts bumped off the board stay reachable inside the explorer.
BOARD_CHART_BUTTONS = 15
# Discord caps a select at 25 options; reserve one for the "see all" entry.
BOARD_MORE_OPTION_CAP = 24

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
    ("Actions", "📋", analysis.plot_actions_card, "Prescriptive insights per player"),
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

# Charts only reachable via the explorer's "More ▾" select. Stems must
# exist in ``analysis.ALL_PLOTS``. Order shown here is the order in the
# dropdown.
MORE_CHART_DEFS = [
    ("21_duo_winrate", "Duos / H2H", "🤝", "Same-team partnerships + head-to-head"),
    ("30_role_winrate", "Per-role WR", "🛡️", "WR per role (TOP/JG/MID/ADC/SUP) with Wilson CIs"),
    (
        "31_player_role_matrix",
        "Role matrix",
        "🧭",
        "Player×role heatmap vs personal baseline (aggregate only)",
    ),
    ("32_tilt_by_gap", "Tilt analysis", "😤", "After-loss vs after-win WR by inter-game gap"),
    (
        "33_session_position_wr",
        "Session position WR",
        "🎮",
        "WR by game-number within a play session",
    ),
    (
        "34_improvement_slope",
        "Improvement slope",
        "📈",
        "Logistic regression of WR on game-index per player",
    ),
    ("35_stat_sheet", "Stat sheet", "📊", "6-metric percentile rank vs friend group"),
    ("36_game_pace", "Game pace", "⏱️", "Stomper vs scaler — duration of wins vs losses"),
    (
        "37_shrunk_champ_rankings",
        "Shrunk champ rankings",
        "🏆",
        "Top 15 champs by Bayesian-shrunk WR",
    ),
    (
        "38_champ_pool_concentration",
        "Pool concentration",
        "🎯",
        "Shannon entropy, effective N, Pareto",
    ),
    ("39_champion_mastery", "Mastery curve", "🎓", "WR by play-count bucket on each champion"),
    ("40_champion_rust", "Champion rust", "🪦", "WR by days since last played that champion"),
    ("41_dow_hour_heatmap", "DOW × hour heatmap", "🔥", "WR by day-of-week × time-of-day"),
    ("04_feature_impact", "Feature impact", "🧪", "Pre-game factor WR shifts with BH-FDR q-values"),
    ("08_cumulative_winrate", "Cumulative WR", "📉", "Lifetime WR + rolling-20 hot/cold streaks"),
    (
        "22_model_calibration",
        "Calibration plot",
        "🎯",
        "Out-of-sample logit AUC + reliability curve",
    ),
    ("24_per_player_predictability", "Predictability", "🔮", "Per-player out-of-sample logit AUC"),
    ("25_tier_winrate", "Tier WR", "🏔️", "WR per tier when game was played (Wilson CIs)"),
    ("26_match_highlights", "Match highlights", "🌟", "Six record-holding matches as a card grid"),
    (
        "27_recent_sessions",
        "Recent sessions",
        "📜",
        "Newest 10 sessions with W/L sequence (per-person)",
    ),
    (
        "28_playstyle_clusters",
        "Playstyle clusters",
        "🧬",
        "k-means + PCA archetypes across the friend group",
    ),
    (
        "29_champion_freshness",
        "Champion freshness",
        "🌱",
        "Days since last played per champion (per-person)",
    ),
    (
        "42_same_champ_behavior",
        "Same-champ behavior",
        "🔁",
        "Do players ride hot champs (after win) or comfort-pick (after loss)?",
    ),
    (
        "43_ride_payoff",
        "Ride payoff",
        "💰",
        "Does riding hot champs actually win? Per-cohort WR test.",
    ),
    (
        "44_win_autocorrelation",
        "Win autocorrelation",
        "📡",
        "Lag-k correlation of win/loss series - momentum or mean-reversion?",
    ),
    (
        "45_champ_swap_recs",
        "Swap cheat sheet",
        "💱",
        "Buy/sell champion recs based on shrunken WR + play frequency",
    ),
    (
        "46_per_account_breakdown",
        "Per-account WR",
        "🎭",
        "Smurf/alt detection - does each Riot account have the same WR?",
    ),
    (
        "47_champion_meta_shifts",
        "Meta shifts",
        "🌊",
        "Quarterly champion WR changes — patch / meta detection (chi-square + BH-FDR)",
    ),
    (
        "48_insights_card",
        "Insights card",
        "🧾",
        "TL;DR — your role, pace, BUY/SELL champs, golden hour in one chart",
    ),
    (
        "49_recent_form",
        "Recent form",
        "🔥",
        "Last 30/60/90d WR vs lifetime — who's hot/cold?",
    ),
    (
        "50_session_stamina",
        "Session stamina",
        "🏃",
        "WR by total session length — does the marathon penalty exist?",
    ),
    (
        "51_patch_meta_shifts",
        "Patch meta shifts",
        "🩹",
        "Champion WR by Riot patch (vs iter 57's quarter proxy)",
    ),
    (
        "52_kda_dominance",
        "KDA dominance",
        "🥊",
        "Within shared games, who out-KDAs whom? Pair-wise % dominance.",
    ),
    (
        "53_rank_recovery",
        "Rank recovery",
        "📐",
        "Time to climb back after a tier demotion",
    ),
    (
        "54_last_game_of_day",
        "Last game of day",
        "🌙",
        "WR of last game on multi-game days — play-until-win or quit-while-ahead?",
    ),
    (
        "55_longest_streaks",
        "Longest streaks",
        "⛓️",
        "Longest historical win/loss streaks vs coin-flip expectation",
    ),
    (
        "56_mahalanobis_outliers",
        "Career outliers",
        "🚨",
        "Most anomalous games via multivariate Mahalanobis distance",
    ),
    (
        "57_monthly_champion_shifts",
        "Monthly champ shifts",
        "📅",
        "Per-champion monthly WR shifts (finer than quarterly meta chart)",
    ),
]


# Matches the canonical-index custom_ids on both the panel buttons
# (ms:panel:cN) and the explorer buttons (ms:expl:cN). The person select
# (ms:panel:person…) deliberately doesn't match — it isn't a chart.
_CHART_CUSTOM_ID_RE = re.compile(r"^ms:(?:panel|expl):c(\d+)$")

# The chart pinned to the first slot regardless of popularity. Index 0 is
# also the default chart opened on a person-pick, so keeping it first keeps
# the panel's visual lead-in and the default view aligned.
_PINNED_CHART_IDX = 0


def _chart_display_order(db_path: str) -> list[int]:
    """Canonical CHART_DEFS indices ordered most-clicked-first for display.

    Reads click counts from ``command_usage`` (both panel and in-explorer
    chart buttons, which share the canonical ``cN`` index), and returns a
    permutation of ``range(len(CHART_DEFS))``:

      - ``_PINNED_CHART_IDX`` (Summary) always lands first.
      - remaining charts sort by descending click count.
      - canonical index breaks ties — so with no usage data the result is
        the canonical order, and the layout only diverges as real clicks
        accumulate.

    Never raises: any DB error (missing table pre-migration, locked file)
    falls back to canonical order. Returns CANONICAL indices, not slots —
    the caller keeps ``custom_id``/callbacks bound to these so dispatch,
    the usage decoder, and historical rows all stay valid.
    """
    n = len(CHART_DEFS)
    canonical = list(range(n))
    counts: dict[int, int] = {}
    try:
        with sqlite3.connect(db_path) as con:
            rows = con.execute(
                "SELECT command_name, COUNT(*) FROM command_usage "
                "WHERE command_name LIKE 'ms:panel:c%' OR command_name LIKE 'ms:expl:c%' "
                "GROUP BY command_name"
            ).fetchall()
        for name, cnt in rows:
            m = _CHART_CUSTOM_ID_RE.match(name or "")
            if m:
                idx = int(m.group(1))
                if 0 <= idx < n:
                    counts[idx] = counts.get(idx, 0) + int(cnt)
    except Exception as exc:
        log.info(f"chart display-order: usage read failed ({exc!r}), using canonical order")
        return canonical

    return sorted(
        canonical,
        key=lambda i: (i != _PINNED_CHART_IDX, -counts.get(i, 0), i),
    )


def _figure_to_file(fig, name: str = "chart.png") -> discord.File:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=PNG_DPI, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return discord.File(buf, filename=name)


def _ordinal_suffix(n: int) -> str:
    """English ordinal suffix — 11/12/13 are "th"; 1/2/3 take st/nd/rd."""
    if 10 <= (n % 100) <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _build_me_text_embed(df: pd.DataFrame, sub: pd.DataFrame, person_name: str) -> discord.Embed:
    """Compact text-only insights summary for /me_text.

    Mirrors the gating logic of ``analysis.plot_insights_card`` (>=50 games
    overall, per-section thresholds) but emits Discord embed fields rather
    than a matplotlib card. Sections with insufficient data are skipped.
    """
    n_games = int(len(sub))
    wr = float(sub["win"].mean())

    primary_hex = analysis.PALETTE["primary"].lstrip("#")
    embed = discord.Embed(
        title=f"📊 {person_name}",
        color=discord.Color(int(primary_hex, 16)),
    )

    # Headline: WR + games + percentile vs friend group via stat-sheet frame.
    frame = analysis._stat_sheet_frame(df)
    pct_label: str | None = None
    if person_name in set(frame["person"]):
        wr_values = frame["wr"].astype(float)
        ranks = wr_values.rank(method="average")
        focal_idx = int(frame.index[frame["person"] == person_name][0])
        if len(wr_values) > 1:
            pct = (ranks.iloc[focal_idx] - 1) / (len(wr_values) - 1) * 100.0
            pct_int = int(round(pct))
            pct_label = f"{pct_int}{_ordinal_suffix(pct_int)} pct vs group"
    headline = f"{wr:.1%} WR, {n_games} games"
    if pct_label is not None:
        headline = f"{headline}, {pct_label}"
    embed.description = headline

    # Role: best + worst (each requires >=10 games in that role).
    role_d = sub[sub["role"].isin(analysis.ROLE_ORDER)]
    if not role_d.empty:
        role_agg = (
            role_d.groupby("role").agg(games=("win", "size"), wins=("win", "sum")).reset_index()
        )
        role_agg = role_agg[role_agg["games"] >= analysis._INSIGHTS_ROLE_MIN_GAMES]
        if not role_agg.empty:
            role_agg["wr"] = role_agg["wins"] / role_agg["games"]
            role_agg = role_agg.sort_values("wr", ascending=False)
            best = role_agg.iloc[0]
            worst = role_agg.iloc[-1]
            lines = [
                f"Best: {best['role']} {best['wr']:.1%} (n={int(best['games'])})",
            ]
            if len(role_agg) > 1:
                lines.append(f"Worst: {worst['role']} {worst['wr']:.1%} (n={int(worst['games'])})")
            embed.add_field(name="🛡️ Role", value="\n".join(lines), inline=False)

    # Pace: Welch's t-test on duration_min, wins vs losses.
    wins_dur = sub.loc[sub["win"] == 1, "duration_min"].to_numpy()
    losses_dur = sub.loc[sub["win"] == 0, "duration_min"].to_numpy()
    if len(wins_dur) >= 2 and len(losses_dur) >= 2:
        diff, p_pace = analysis._welch_t(wins_dur, losses_dur)
        if diff < -1.0:
            verdict = "Stomper"
        elif diff > 1.0:
            verdict = "Scaler"
        else:
            verdict = "Neutral"
        embed.add_field(
            name="⏱️ Pace",
            value=f"{verdict} | wins {diff:+.1f}min vs losses (p={p_pace:.3f})",
            inline=False,
        )

    # Top picks — Bayesian-shrunk BUY/SELL recs. Needs >=100 games total.
    if n_games >= analysis._INSIGHTS_SWAP_MIN_GAMES:
        baseline_wr = wr
        prior_n = 30.0
        champ = sub.groupby("champion")["win"].agg(["count", "sum"])
        champ = champ.rename(columns={"count": "games", "sum": "wins"})
        champ = champ[champ["games"] >= 3]
        if not champ.empty:
            champ["shrunk_wr"] = [
                analysis.bayesian_shrunk_wr(int(w), int(n), baseline_wr, prior_n)
                for w, n in zip(champ["wins"], champ["games"], strict=False)
            ]
            champ["play_freq"] = champ["games"] / n_games
            champ["vs_baseline"] = champ["shrunk_wr"] - baseline_wr

            buy = champ[(champ["shrunk_wr"] > baseline_wr + 0.02) & (champ["games"] >= 5)].copy()
            buy["score"] = buy["vs_baseline"] * (1.0 - buy["play_freq"])
            buy = buy.sort_values("score", ascending=False).head(3)

            sell = champ[(champ["shrunk_wr"] < baseline_wr - 0.02) & (champ["games"] >= 20)].copy()
            sell["score"] = (-sell["vs_baseline"]) * sell["play_freq"]
            sell = sell.sort_values("score", ascending=False).head(3)

            def _fmt_champ_row(name: str, row) -> str:
                return f"{name} ({row['shrunk_wr']:.0%}, n={int(row['games'])})"

            lines: list[str] = []
            if not buy.empty:
                buy_strs = ", ".join(_fmt_champ_row(c, r) for c, r in buy.iterrows())
                lines.append(f"BUY: {buy_strs}")
            if not sell.empty:
                sell_strs = ", ".join(_fmt_champ_row(c, r) for c, r in sell.iterrows())
                lines.append(f"SELL: {sell_strs}")
            if lines:
                embed.add_field(
                    name="🏆 Shrunken champ rankings",
                    value="\n".join(lines),
                    inline=False,
                )

    # Golden hour — best/worst (dow, hour_bucket) cell with >=15 games each.
    if not sub.empty:
        gh = sub.copy()
        gh["hour_bucket"] = gh["hour"].map(analysis._hour_bucket)
        cells = (
            gh.groupby(["dow", "hour_bucket"], observed=True)
            .agg(games=("win", "size"), wins=("win", "sum"))
            .reset_index()
        )
        cells = cells[cells["games"] >= analysis._INSIGHTS_CELL_MIN_GAMES]
        if not cells.empty:
            cells["wr"] = cells["wins"] / cells["games"]
            best_cell = cells.sort_values("wr", ascending=False).iloc[0]
            worst_cell = cells.sort_values("wr", ascending=True).iloc[0]
            best_day = analysis.DOW_LABELS[int(best_cell["dow"])]
            best_part = str(best_cell["hour_bucket"]).split(" ")[0]
            worst_day = analysis.DOW_LABELS[int(worst_cell["dow"])]
            worst_part = str(worst_cell["hour_bucket"]).split(" ")[0]
            lines = [
                f"BEST: {best_day} {best_part} {best_cell['wr']:.0%} "
                f"(n={int(best_cell['games'])})",
            ]
            if (best_cell["dow"], best_cell["hour_bucket"]) != (
                worst_cell["dow"],
                worst_cell["hour_bucket"],
            ):
                lines.append(
                    f"WORST: {worst_day} {worst_part} {worst_cell['wr']:.0%} "
                    f"(n={int(worst_cell['games'])})"
                )
            embed.add_field(name="🕐 Golden hour", value="\n".join(lines), inline=False)

    # Recent form — 30d vs lifetime, anchored to global latest game_start.
    game_start_all = pd.to_datetime(df["game_start"])
    now = game_start_all.max()
    if pd.notna(now):
        sub_ts = pd.to_datetime(sub["game_start"])
        recent_mask = sub_ts >= (now - pd.Timedelta(days=30))
        n_30 = int(recent_mask.sum())
        if n_30 >= 8:
            wr_30 = float(sub.loc[recent_mask, "win"].mean())
            delta_pp = (wr_30 - wr) * 100.0
            embed.add_field(
                name="🔥 Recent form",
                value=(f"30d: {wr_30:.0%} (n={n_30}) vs lifetime {wr:.0%} " f"→ {delta_pp:+.1f}pp"),
                inline=False,
            )

    # Hot-champ behavior: P(same|win) vs P(same|loss); need >=30 each.
    hc = sub.sort_values("game_start").reset_index(drop=True)
    hc = hc.assign(
        prev_champion=hc["champion"].shift(1),
        prev_win=hc["win"].shift(1),
    )
    hc = hc.dropna(subset=["prev_win", "prev_champion"])
    if not hc.empty:
        hc = hc.copy()
        hc["prev_win"] = hc["prev_win"].astype(int)
        hc["same_champion"] = (hc["champion"] == hc["prev_champion"]).astype(int)
        win_rows = hc[hc["prev_win"] == 1]
        loss_rows = hc[hc["prev_win"] == 0]
        if len(win_rows) >= 30 and len(loss_rows) >= 30:
            p_w = float(win_rows["same_champion"].mean())
            p_l = float(loss_rows["same_champion"].mean())
            delta = p_w - p_l
            delta_pp = delta * 100.0
            if delta_pp > 15.0:
                interp = "strong ride-hot"
            elif delta_pp >= 0.0:
                interp = "neutral"
            else:
                interp = "comfort-pick after loss"
            embed.add_field(
                name="🔁 Hot-champ behavior",
                value=(
                    f"P(same|win) = {p_w:.0%}  P(same|loss) = {p_l:.0%}  "
                    f"delta = {delta:+.1%}\n{interp}"
                ),
                inline=False,
            )

    last_update = pd.to_datetime(df["game_start"]).max()
    embed.set_footer(
        text=(
            f"{len(df):,} games · {df['person'].nunique()} people · "
            f"last update {last_update:%Y-%m-%d}"
        )
    )
    return embed


def _build_me_todo_embed(df: pd.DataFrame, sub: pd.DataFrame, person_name: str) -> discord.Embed:
    """Prescriptive 3-5 bullet to-do list for /me_todo.

    Bullets fire in priority order: drop-a-SELL, avoid-cold-cell,
    lean-into-hot-cell, role-swap, comfort-pick-after-loss, recent-form,
    pace-style. Each bullet has its own n-floor + magnitude gate. If
    fewer than 3 valid actionable bullets, fall back to descriptive
    stats so the user gets SOMETHING.
    """
    n_games = int(len(sub))
    wr = float(sub["win"].mean())
    bullets: list[str] = []

    # Champion shrunken WR table — used by SELL and BUY bullets together.
    # Computed once because the SELL bullet needs the top BUY for the
    # "try {top_buy} instead" half of the template.
    top_buy_name: str | None = None
    top_buy_wr: float | None = None
    sell_top = None
    if n_games >= analysis._INSIGHTS_SWAP_MIN_GAMES:
        baseline_wr = wr
        prior_n = 30.0
        champ = sub.groupby("champion")["win"].agg(["count", "sum"])
        champ = champ.rename(columns={"count": "games", "sum": "wins"})
        champ = champ[champ["games"] >= 3]
        if not champ.empty:
            champ["shrunk_wr"] = [
                analysis.bayesian_shrunk_wr(int(w), int(n), baseline_wr, prior_n)
                for w, n in zip(champ["wins"], champ["games"], strict=False)
            ]
            champ["play_freq"] = champ["games"] / n_games
            champ["vs_baseline"] = champ["shrunk_wr"] - baseline_wr

            buy = champ[(champ["shrunk_wr"] > baseline_wr + 0.02) & (champ["games"] >= 5)].copy()
            if not buy.empty:
                buy["score"] = buy["vs_baseline"] * (1.0 - buy["play_freq"])
                buy = buy.sort_values("score", ascending=False)
                top_buy_name = str(buy.index[0])
                top_buy_wr = float(buy.iloc[0]["shrunk_wr"])

            # Bullet 1 — SELL gate: tighter than _me_text (4pp + n>=20).
            sell = champ[(champ["vs_baseline"] <= -0.04) & (champ["games"] >= 20)].copy()
            if not sell.empty:
                sell["score"] = (-sell["vs_baseline"]) * sell["play_freq"]
                sell = sell.sort_values("score", ascending=False)
                sell_top = sell.iloc[0]
                sell_name = str(sell.index[0])
                sell_wr = float(sell_top["shrunk_wr"])
                sell_n = int(sell_top["games"])
                sell_delta_pp = float(sell_top["vs_baseline"]) * 100.0
                bullet = (
                    f"Stop locking {sell_name} — {sell_wr:.0%} WR on {sell_n} games "
                    f"({sell_delta_pp:+.1f}pp below your baseline)."
                )
                if top_buy_name is not None and top_buy_wr is not None:
                    bullet += f" Try {top_buy_name} instead ({top_buy_wr:.0%})."
                bullets.append(bullet)

    # Bullets 2 + 3 — temporal cells. Same data prep as _me_text but with
    # a 10pp delta gate on top of the n>=15 floor.
    if not sub.empty:
        gh = sub.copy()
        gh["hour_bucket"] = gh["hour"].map(analysis._hour_bucket)
        cells = (
            gh.groupby(["dow", "hour_bucket"], observed=True)
            .agg(games=("win", "size"), wins=("win", "sum"))
            .reset_index()
        )
        cells = cells[cells["games"] >= analysis._INSIGHTS_CELL_MIN_GAMES]
        if not cells.empty:
            cells["wr"] = cells["wins"] / cells["games"]
            cells["delta_pp"] = (cells["wr"] - wr) * 100.0

            worst_cell = cells.sort_values("wr", ascending=True).iloc[0]
            if worst_cell["delta_pp"] <= -10.0:
                worst_day = analysis.DOW_LABELS[int(worst_cell["dow"])]
                worst_part = str(worst_cell["hour_bucket"]).split(" ")[0]
                bullets.append(
                    f"Skip {worst_day} {worst_part} games — your WR there is "
                    f"{worst_cell['wr']:.0%} on {int(worst_cell['games'])} games "
                    f"({worst_cell['delta_pp']:+.1f}pp below baseline)."
                )

            best_cell = cells.sort_values("wr", ascending=False).iloc[0]
            if best_cell["delta_pp"] >= 10.0:
                best_day = analysis.DOW_LABELS[int(best_cell["dow"])]
                best_part = str(best_cell["hour_bucket"]).split(" ")[0]
                bullets.append(
                    f"Queue {best_day} {best_part} — your WR there is "
                    f"{best_cell['wr']:.0%} (+{best_cell['delta_pp']:.1f}pp above baseline)."
                )

    # Bullet 4 — role swap. Tighter n-floor than _me_text (n>=30 each)
    # and a 5pp gap requirement.
    role_d = sub[sub["role"].isin(analysis.ROLE_ORDER)]
    if not role_d.empty:
        role_agg = (
            role_d.groupby("role").agg(games=("win", "size"), wins=("win", "sum")).reset_index()
        )
        role_agg = role_agg[role_agg["games"] >= 30]
        if len(role_agg) >= 2:
            role_agg["wr"] = role_agg["wins"] / role_agg["games"]
            role_agg = role_agg.sort_values("wr", ascending=False)
            best_role = role_agg.iloc[0]
            worst_role = role_agg.iloc[-1]
            gap_pp = (best_role["wr"] - worst_role["wr"]) * 100.0
            if gap_pp >= 5.0:
                bullets.append(
                    f"Play more {best_role['role']} ({best_role['wr']:.0%} on "
                    f"{int(best_role['games'])}), avoid {worst_role['role']} "
                    f"({worst_role['wr']:.0%} on {int(worst_role['games'])})."
                )

    # Bullet 5 — comfort pick after loss. WR(stay) − WR(switch) given the
    # previous game was a loss; fires when staying is +3pp or more.
    hc = sub.sort_values("game_start").reset_index(drop=True)
    hc = hc.assign(
        prev_champion=hc["champion"].shift(1),
        prev_win=hc["win"].shift(1),
    )
    hc = hc.dropna(subset=["prev_win", "prev_champion"])
    if not hc.empty:
        hc = hc.copy()
        hc["prev_win"] = hc["prev_win"].astype(int)
        hc["same_champion"] = (hc["champion"] == hc["prev_champion"]).astype(int)
        after_loss = hc[hc["prev_win"] == 0]
        stay = after_loss[after_loss["same_champion"] == 1]
        switch = after_loss[after_loss["same_champion"] == 0]
        if len(stay) >= 15 and len(switch) >= 15:
            payoff_pp = (float(stay["win"].mean()) - float(switch["win"].mean())) * 100.0
            if payoff_pp >= 3.0:
                bullets.append(
                    "After a loss: stay on your champion. Switching costs you "
                    f"~{payoff_pp:.1f}pp."
                )

    # Bullet 6 — recent form. 30d delta of >=5pp either direction,
    # keeping the n_30 >= 8 floor used by _me_text.
    game_start_all = pd.to_datetime(df["game_start"])
    now = game_start_all.max()
    if pd.notna(now):
        sub_ts = pd.to_datetime(sub["game_start"])
        recent_mask = sub_ts >= (now - pd.Timedelta(days=30))
        n_30 = int(recent_mask.sum())
        if n_30 >= 8:
            wr_30 = float(sub.loc[recent_mask, "win"].mean())
            delta_pp = (wr_30 - wr) * 100.0
            if delta_pp >= 5.0:
                bullets.append(
                    f"You're on form — recent 30d at {wr_30:.0%} "
                    f"(+{delta_pp:.1f}pp vs lifetime)."
                )
            elif delta_pp <= -5.0:
                bullets.append(
                    f"Recent form dip: 30d at {wr_30:.0%} ({delta_pp:+.1f}pp "
                    "below lifetime). Take a break?"
                )

    # Bullet 7 — pace style. Welch's t on duration_min, wins vs losses;
    # gate on |diff| >= 1.5min and p < 0.10.
    wins_dur = sub.loc[sub["win"] == 1, "duration_min"].to_numpy()
    losses_dur = sub.loc[sub["win"] == 0, "duration_min"].to_numpy()
    if len(wins_dur) >= 2 and len(losses_dur) >= 2:
        diff, p_pace = analysis._welch_t(wins_dur, losses_dur)
        if abs(diff) >= 1.5 and p_pace < 0.10:
            if diff < 0:
                bullets.append(
                    f"You're an early-game stomper (wins {abs(diff):.1f}min shorter "
                    f"than losses, p={p_pace:.3f}). Push tempo."
                )
            else:
                bullets.append(
                    f"You're a late scaler (wins {diff:.1f}min longer than "
                    f"losses, p={p_pace:.3f}). Play for scaling comps."
                )

    bullets = bullets[:5]

    # Descriptive fallback so the user always gets something to read.
    if len(bullets) < 3:
        bullets.append(f"Lifetime: {wr:.0%} WR across {n_games} games.")
        # Top-played champion by raw count.
        top_played = sub["champion"].value_counts()
        if not top_played.empty:
            top_name = str(top_played.index[0])
            top_n = int(top_played.iloc[0])
            top_wr = float(sub.loc[sub["champion"] == top_name, "win"].mean())
            bullets.append(f"Most-played: {top_name} ({top_n} games, {top_wr:.0%} WR).")
        if top_buy_name is not None and top_buy_wr is not None:
            bullets.append(f"Consider playing more {top_buy_name} ({top_buy_wr:.0%} shrunk WR).")
        bullets = bullets[:5]

    primary_hex = analysis.PALETTE["primary"].lstrip("#")
    embed = discord.Embed(
        title=f"📋 {person_name} — what to change",
        color=discord.Color(int(primary_hex, 16)),
        description="\n".join(f"• {b}" for b in bullets),
    )

    # Footer mirrors /me_text's percentile-vs-group line.
    frame = analysis._stat_sheet_frame(df)
    pct_label: str | None = None
    if person_name in set(frame["person"]):
        wr_values = frame["wr"].astype(float)
        ranks = wr_values.rank(method="average")
        focal_idx = int(frame.index[frame["person"] == person_name][0])
        if len(wr_values) > 1:
            pct = (ranks.iloc[focal_idx] - 1) / (len(wr_values) - 1) * 100.0
            pct_int = int(round(pct))
            pct_label = f"{pct_int}{_ordinal_suffix(pct_int)} pct vs group"
    footer_parts = [f"{n_games} games"]
    if pct_label is not None:
        footer_parts.append(pct_label)
    embed.set_footer(text=" · ".join(footer_parts))
    return embed


def _build_panel_embed() -> discord.Embed:
    """The static description sitting above the panel buttons."""
    embed = discord.Embed(
        title=PANEL_EMBED_TITLE,
        description=(
            "Browse what factors influence wins vs. losses across every "
            "tracked Ranked Solo/Duo game.\n\n"
            "**Use the dropdown** to focus on one person (Riot accounts roll "
            "up) or drill into a single Riot account, or **click a chart "
            "button** to open the all-players view. The chart posts publicly "
            "in the channel — only the person who opened it can pivot the "
            "view from the in-message buttons.\n\n"
            "Inside the chart you can pivot between charts and players "
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
            "📋 **Actions** · 📈 **Progression** (% of career)\n"
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


def _build_person_options(df) -> list[discord.SelectOption]:
    """Translate the matches dataframe into select options.

    Layout: slot 0 = "All players" aggregate. Then for each Discord person
    (most-active first) a rollup row valued ``person:<name>``, and if that
    person has multiple Riot accounts the individual accounts follow
    indented underneath as ``account:<riot_account>``. Single-account
    people skip the drill-down (the rollup IS the account view).

    Discord caps select options at 25 — we fill in priority order and stop.
    """
    options = [
        discord.SelectOption(
            label="All players (aggregate)", value=ALL_VALUE, emoji="🌐", default=True
        )
    ]
    if df is None or len(df) == 0:
        return options

    person_totals = df.groupby("person").size().sort_values(ascending=False)
    account_totals = df.groupby(["person", "riot_account"]).size()
    used = 1  # slot 0 already taken
    for person, p_games in person_totals.items():
        if used >= 25:
            break
        accounts = account_totals.loc[person].sort_values(ascending=False)
        n_accts = len(accounts)

        if n_accts > 1:
            label = f"{person}  ·  {n_accts} accts"
            description = f"{int(p_games):,} games (all accounts)"
        else:
            label = str(person)
            description = f"{int(p_games):,} games"
        options.append(
            discord.SelectOption(
                label=label[:100],
                value=f"person:{person}"[:100],
                emoji="👤",
                description=description[:100],
            )
        )
        used += 1

        # Drill-down: list individual Riot accounts only for multi-account people.
        if n_accts > 1:
            for acct, a_games in accounts.items():
                if used >= 25:
                    break
                options.append(
                    discord.SelectOption(
                        label=f"  ↳ {acct}"[:100],
                        value=f"account:{acct}"[:100],
                        description=f"{int(a_games):,} games · in {person}"[:100],
                    )
                )
                used += 1
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

        options = _build_person_options(df)
        for opt in options:
            opt.default = (person is None and opt.value == ALL_VALUE) or (
                person is not None and opt.value == person
            )

        select = discord.ui.Select(placeholder="Pick a player…", options=options, row=0)
        select.callback = self._on_person_select
        self.add_item(select)
        self._select = select

        # Reserve the final button slot (row 4, position 4) for the "More ▾"
        # entry point — five rows × five components is Discord's hard cap,
        # and the More-select must live in its own row inside the follow-up
        # view. Drop the last CHART_DEFS button from this view; it (and the
        # iter 38+ charts) are reachable via the More-select instead.
        max_buttons = 19
        for idx, (label, emoji, _, _) in enumerate(CHART_DEFS[:max_buttons]):
            # Canonical-index custom_id so usage_logger can attribute each
            # click to a specific chart. Without it these ephemeral-view
            # buttons get auto-generated ids and their clicks are invisible
            # — which is exactly the signal the panel's dynamic ordering
            # needs. The id is unique within the view (distinct idx) and
            # safe across concurrent explorers (non-persistent views route
            # by message id, so identical ids don't collide).
            button = discord.ui.Button(
                label=label,
                emoji=emoji,
                row=1 + (idx // 5),
                custom_id=f"ms:expl:c{idx}",
            )
            button.callback = self._make_chart_callback(idx)
            self.add_item(button)

        more_button = discord.ui.Button(
            label="More ▾",
            emoji="➕",
            row=1 + (max_buttons // 5),
            style=discord.ButtonStyle.secondary,
        )
        more_button.callback = self._on_more_clicked
        self.add_item(more_button)
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
        self.person = None if chosen in (ALL_VALUE, "all") else chosen
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
        which chart they're viewing. The trailing "More ▾" button is
        skipped — it isn't a chart, so it stays secondary."""
        chart_btn_count = 0
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            if child.label == "More ▾":
                continue
            child.style = (
                discord.ButtonStyle.primary
                if chart_btn_count == self.chart_idx
                else discord.ButtonStyle.secondary
            )
            chart_btn_count += 1

    async def _on_more_clicked(self, interaction: discord.Interaction) -> None:
        view = _MoreAnalyticsView(df=self.df, invoker_id=self.invoker_id, person=self.person)
        await interaction.response.send_message(
            "Pick another chart — it'll post in the channel.",
            view=view,
            ephemeral=True,
        )

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

        who = analysis._display_label(self.person) or "all players"
        embed = discord.Embed(title=f"{emoji} {title} — {who}")
        embed.set_image(url="attachment://chart.png")
        file = _figure_to_file(fig)
        await interaction.edit_original_response(embed=embed, attachments=[file], view=self)


# --- More-analytics select view (ephemeral) --------------------------------


class _MoreAnalyticsView(discord.ui.View):
    """Per-user ephemeral wrapper around the chart-overflow selects. Lists
    every chart that didn't fit on the main explorer's button grid.
    Picking an option posts the chosen chart as a public follow-up so the
    channel sees the result — the select itself stays ephemeral.

    Discord caps each ``discord.ui.Select`` at 25 options. When
    MORE_CHART_DEFS grows past that, options are paginated across two
    selects rather than silently truncated.
    """

    # Per-select cap. Discord allows 25; keep one slot in reserve so we
    # don't accidentally tip over when adding emoji/description metadata.
    _MAX_OPTIONS_PER_SELECT = 24

    def __init__(self, df, invoker_id: int, person: str | None):
        super().__init__(timeout=EXPLORER_TIMEOUT_SECONDS)
        self.df = df
        self.invoker_id = invoker_id
        self.person = person

        plot_by_stem = dict(analysis.ALL_PLOTS)
        options: list[discord.SelectOption] = []
        self._option_meta: dict[str, tuple[str, str]] = {}
        for stem, label, emoji, description in MORE_CHART_DEFS:
            if stem not in plot_by_stem:
                # Defensive: skip rather than crash the whole view if the
                # analysis lib drops a chart. Log so we notice during dev.
                log.warning(f"MORE_CHART_DEFS stem {stem!r} missing from ALL_PLOTS")
                continue
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=stem[:100],
                    description=description[:100],
                    emoji=emoji,
                )
            )
            self._option_meta[stem] = (label, emoji)

        # Single-select layout preserved verbatim when we're under the
        # Discord cap. Paginated layout kicks in only when we'd otherwise
        # truncate.
        self._selects: list[discord.ui.Select] = []
        if len(options) <= self._MAX_OPTIONS_PER_SELECT:
            select = discord.ui.Select(placeholder="More analytics ↓", options=options, row=0)
            select.callback = self._on_select
            self.add_item(select)
            self._selects.append(select)
        else:
            # Balanced split — ceil(n/2) on page 1, remainder on page 2.
            half = (len(options) + 1) // 2
            chunks = [options[:half], options[half:]]
            total = len(chunks)
            for idx, chunk in enumerate(chunks):
                select = discord.ui.Select(
                    placeholder=f"More analytics ({idx + 1}/{total}) ↓",
                    options=chunk,
                    row=idx,
                )
                select.callback = self._on_select
                self.add_item(select)
                self._selects.append(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "That menu belongs to someone else.", ephemeral=True
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        # Multiple selects share this callback — resolve which one fired
        # via the interaction's custom_id rather than capturing per-select.
        stem = interaction.data["values"][0]
        plot_by_stem = dict(analysis.ALL_PLOTS)
        fn = plot_by_stem.get(stem)
        if fn is None:
            await interaction.response.send_message(
                f"Chart `{stem}` not found in the analysis lib.", ephemeral=True
            )
            return
        await interaction.response.defer()
        try:
            fig = await asyncio.to_thread(fn, self.df, self.person)
        except Exception as exc:
            log.error(f"more-chart {stem!r} render failed: {exc!r}")
            await interaction.followup.send(f"Chart failed: {exc!r}", ephemeral=True)
            return

        label, emoji = self._option_meta.get(stem, (stem, "📊"))
        who = analysis._display_label(self.person) or "all players"
        embed = discord.Embed(title=f"{emoji} {label} — {who}")
        embed.set_image(url="attachment://chart.png")
        file = _figure_to_file(fig)
        # Ephemeral — only the clicker sees the chart, channel stays clean.
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)


# --- Persistent panel ------------------------------------------------------


class MatchStatsPanel(discord.ui.View):
    """Persistent control panel. ``df`` is set at post-time from a DB
    query; the persistence-registration instance can pass None (its
    options don't matter for callback dispatch — discord.py routes by
    custom_id)."""

    def __init__(self, df=None, display_order: list[int] | None = None):
        super().__init__(timeout=None)

        if df is not None:
            options = _build_person_options(df)
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

        # `display_order` is a permutation of canonical CHART_DEFS indices
        # controlling VISUAL placement only (most-clicked first, Summary
        # pinned). custom_id + callback stay bound to the canonical index
        # `idx`, so dispatch, the usage decoder, and historical cN rows are
        # unaffected by reordering. None (e.g. the persistence stub) renders
        # canonical order — dispatch doesn't care about order anyway.
        if display_order is None:
            display_order = list(range(len(CHART_DEFS)))
        # Rows 1-3: the top BOARD_CHART_BUTTONS charts as buttons. The rest
        # are reachable via the explorer (opened by any board click) and the
        # More-analytics select below.
        for slot, idx in enumerate(display_order[:BOARD_CHART_BUTTONS]):
            label, emoji, _, _ = CHART_DEFS[idx]
            row = 1 + (slot // 5)
            button = discord.ui.Button(
                label=label,
                emoji=emoji,
                row=row,
                style=discord.ButtonStyle.secondary,
                custom_id=f"{PANEL_CUSTOM_ID_PREFIX}:c{idx}",
            )
            button.callback = self._make_chart_callback(idx)
            self.add_item(button)

        # Row 4: the overflow-analytics dropdown. Options are static
        # (MORE_CHART_DEFS), so the persistence stub and the live panel build
        # the same set — dispatch routes by custom_id regardless.
        more_select = discord.ui.Select(
            placeholder="More analytics ↓",
            options=self._build_board_more_options(),
            row=4,
            custom_id=PANEL_MORE_SELECT_ID,
        )
        more_select.callback = self._on_more_select
        self.add_item(more_select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("MatchAnalysis")
        if cog is None:
            await interaction.response.send_message(
                "Match-stats cog isn't loaded — bot may be restarting. Try again in a moment.",
                ephemeral=True,
            )
            return
        # discord.py passes the chosen value(s) via interaction.data["values"].
        # Values are now prefixed: "all" / "__all__" / "person:..." / "account:...".
        chosen = interaction.data["values"][0]
        if chosen == PLACEHOLDER_VALUE:
            await interaction.response.send_message(
                "This panel is the persistence stub — re-run /match_stats_panel to install the live one.",
                ephemeral=True,
            )
            return
        person = None if chosen in (ALL_VALUE, "all") else chosen
        # Default chart for a person pick = Summary card.
        await cog.open_explorer(interaction, chart_idx=0, person=person)

    @staticmethod
    def _build_board_more_options() -> list[discord.SelectOption]:
        """Options for the board's More-analytics dropdown: the first
        ``BOARD_MORE_OPTION_CAP`` overflow charts (skipping any stem missing
        from ALL_PLOTS) plus a trailing 'open full list' entry. Static — no
        df needed — so the persistence stub renders the same set."""
        plot_by_stem = dict(analysis.ALL_PLOTS)
        options: list[discord.SelectOption] = []
        for stem, label, emoji, description in MORE_CHART_DEFS:
            if stem not in plot_by_stem:
                continue
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=stem[:100],
                    description=description[:100],
                    emoji=emoji,
                )
            )
            if len(options) >= BOARD_MORE_OPTION_CAP:
                break
        options.append(
            discord.SelectOption(
                label="Open full analytics list…",
                value=MORE_SEE_ALL_VALUE,
                emoji="➕",
                description="Every chart, paginated",
            )
        )
        return options

    async def _on_more_select(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("MatchAnalysis")
        if cog is None:
            await interaction.response.send_message(
                "Match-stats cog isn't loaded — bot may be restarting. Try again in a moment.",
                ephemeral=True,
            )
            return
        chosen = interaction.data["values"][0]
        if chosen == MORE_SEE_ALL_VALUE:
            # Board click defaults to the all-players view; the user can
            # refocus inside the explorer.
            await cog.open_more_menu(interaction, person=None)
        else:
            await cog.open_more_chart(interaction, stem=chosen, person=None)

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


class _PanelButtonDispatch(discord.ui.View):
    """Dispatch-only persistent view for chart-button custom_ids that the
    live board can surface via usage ordering but the 15-button panel stub
    does NOT register.

    The board shows the usage-ranked top ``BOARD_CHART_BUTTONS`` charts, so a
    high-canonical-index chart (e.g. c17) can appear as a live button. The
    persistence stub (``MatchStatsPanel()``) only builds the canonical first
    15 (c0..c14), so without this view a click on c15..c19 would have no
    registered handler after a restart and silently do nothing. Never sent as
    a message — only registered via ``add_view`` so discord.py routes these
    custom_ids. Callbacks mirror ``MatchStatsPanel._make_chart_callback``.
    """

    def __init__(self, indices: range):
        super().__init__(timeout=None)
        for idx in indices:
            label, emoji, _, _ = CHART_DEFS[idx]
            button = discord.ui.Button(
                label=label,
                emoji=emoji,
                custom_id=f"{PANEL_CUSTOM_ID_PREFIX}:c{idx}",
            )
            button.callback = self._make_cb(idx)
            self.add_item(button)

    def _make_cb(self, idx: int):
        async def _cb(interaction: discord.Interaction) -> None:
            cog = interaction.client.get_cog("MatchAnalysis")
            if cog is None:
                await interaction.response.send_message(
                    "Match-stats cog isn't loaded — bot may be restarting. Try again in a moment.",
                    ephemeral=True,
                )
                return
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
        # The 15-button stub above registers c0..c14 (+ the player and
        # More-analytics selects). Usage ordering can place c15..c19 onto the
        # live board, so register those custom_ids too — otherwise their
        # clicks have no handler after a restart and silently no-op.
        overflow = range(BOARD_CHART_BUTTONS, len(CHART_DEFS))
        self._overflow_view = _PanelButtonDispatch(overflow) if len(overflow) else None
        if self._overflow_view is not None:
            bot.add_view(self._overflow_view)
        # Tracks the most-recent panel message so the sticky loop knows
        # which message to delete when re-posting. None until either the
        # admin slash command posts one, or recovery finds an existing
        # panel in channel history.
        self._panel_message_id: int | None = None
        # Heartbeat watchdog reads this; set at the end of each successful
        # sticky_panel tick (topmost-check passed or re-post completed).
        self._sticky_panel_last_fired: dt.datetime | None = None
        self.sticky_panel.start()

    def cog_unload(self) -> None:
        self.sticky_panel.cancel()
        self._panel_view.stop()
        if self._overflow_view is not None:
            self._overflow_view.stop()

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Make `@app_commands.check` failures user-visible.

        Without this, denied admin invocations surface as a generic
        "interaction failed" — confusing for non-keepers who didn't know
        the command existed.
        """
        if isinstance(error, app_commands.CheckFailure):
            msg = f"That command is restricted to the **{CHOMAGE_KEEPER_ROLE}** role."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        # Let the rest fall through to default handling (raises into the
        # bot's global error handler, if any).
        raise error

    @app_commands.command(
        name="match_stats_panel",
        description="(Re-)post the persistent match-stats control panel in the dedicated channel",
    )
    @app_commands.guild_only()
    @app_commands.check(_is_chomage_keeper)
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

        # Clear the channel before posting so the new panel is the only
        # message visible. purge handles 14-day-young messages in bulk;
        # older ones fall back to per-message deletes automatically.
        purged = 0
        try:
            deleted = await channel.purge(limit=None)
            purged = len(deleted)
            self.bot.logging.info(
                f"match_stats_panel: purged {purged} message(s) from <#{channel.id}> before posting"
            )
        except discord.Forbidden:
            await ctx.followup.send(
                f"I don't have permission to delete messages in <#{PANEL_CHANNEL_ID}>. "
                "Grant Manage Messages and re-run.",
                ephemeral=True,
            )
            return
        except Exception as exc:
            self.bot.logging.warning(f"match_stats_panel purge failed: {exc!r}, continuing anyway")

        # Any panel message id we had cached is now gone — let the loop
        # re-detect from the fresh post.
        self._panel_message_id = None

        try:
            msg, df = await self._post_panel(channel)
        except discord.Forbidden:
            await ctx.followup.send(
                f"I don't have permission to post in <#{PANEL_CHANNEL_ID}>.", ephemeral=True
            )
            return
        except Exception as exc:
            self.bot.logging.error(f"match_stats_panel failed: {exc!r}")
            await ctx.followup.send(f"Failed to post panel: {exc!r}", ephemeral=True)
            return

        n_people = int(df["person"].nunique()) if not df.empty else 0
        n_accounts = int(df["riot_account"].nunique()) if not df.empty else 0
        await ctx.followup.send(
            f"Panel posted in <#{channel.id}> → [jump]({msg.jump_url}).\n"
            f"Cleared {purged} prior message(s). "
            f"Dropdown lists {n_people} people ({n_accounts} Riot accounts total).",
            ephemeral=True,
        )

    @app_commands.command(
        name="refresh_db_cache",
        description="Clear the 5-minute chart-data cache (admin only)",
    )
    @app_commands.guild_only()
    @app_commands.check(_is_chomage_keeper)
    async def refresh_db_cache(self, interaction: discord.Interaction) -> None:
        """Force the next chart render to re-read match_stats from disk
        instead of using the in-memory cache. Useful after running a backfill
        or migration so the bot's view is immediately fresh.
        """
        n_entries = len(_df_cache)
        _df_cache.clear()
        log.info(f"DF cache cleared by {interaction.user} ({n_entries} entries)")
        await interaction.response.send_message(
            f"Chart-data cache cleared ({n_entries} entries). Next chart click reloads from DB.",
            ephemeral=True,
        )

    @app_commands.command(name="whois", description="Bot operational status (admin)")
    @app_commands.guild_only()
    @app_commands.check(_is_chomage_keeper)
    async def whois(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        import os

        db_path = self.bot.db_path
        try:
            db_size_mb = os.path.getsize(db_path) / (1024 * 1024)
            db_modified = dt.datetime.fromtimestamp(os.path.getmtime(db_path))
        except FileNotFoundError:
            db_size_mb = None
            db_modified = None

        cache_entries = len(_df_cache)
        cache_age_s = None
        if cache_entries > 0:
            newest_at = max(t for t, _ in _df_cache.values())
            cache_age_s = time.monotonic() - newest_at

        # FetchFromRiot is the actual cog class name in league_table_updater
        # (not "PostRanks"); confirmed against heartbeat.py watchdog.
        backfill_cog = self.bot.get_cog("Backfill")
        stream_last = getattr(backfill_cog, "_stream_last_ran", None) if backfill_cog else None

        post_ranks_cog = self.bot.get_cog("FetchFromRiot")
        post_last = (
            getattr(post_ranks_cog, "post_ranks_last_fired", None) if post_ranks_cog else None
        )

        sticky_last = self._sticky_panel_last_fired

        def fmt_ts(t):
            if t is None:
                return "never"
            if isinstance(t, int | float):
                t = dt.datetime.fromtimestamp(t)
            try:
                return t.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return repr(t)

        lines = []
        if db_size_mb is not None:
            lines.append(f"`db file       ` {db_size_mb:.1f} MB · modified {fmt_ts(db_modified)}")
        else:
            lines.append(f"`db file       ` MISSING ({db_path})")

        try:
            df = await _load_matches_cached(self.bot.db_path)
            lines.append(f"`match_stats   ` {len(df):,} rows · {df['person'].nunique()} people")
        except Exception as exc:
            lines.append(f"`match_stats   ` load failed: {exc!r}")

        lines.append(f"`stream_matches` last {fmt_ts(stream_last)}")
        lines.append(f"`post_ranks    ` last {fmt_ts(post_last)}")
        lines.append(f"`sticky_panel  ` last {fmt_ts(sticky_last)}")

        if cache_age_s is not None:
            lines.append(f"`df cache      ` {cache_entries} entries · age {cache_age_s:.0f}s")
        else:
            lines.append("`df cache      ` empty")

        embed = discord.Embed(
            title="📋 Bot operational status",
            description="```\n" + "\n".join(lines) + "\n```",
            color=discord.Color(int(analysis.PALETTE["primary"].lstrip("#"), 16)),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _post_panel(self, channel: discord.abc.Messageable) -> tuple[discord.Message, object]:
        """Load matches, build the view, post the panel. Shared by the
        admin slash command and the sticky-pin loop. Updates
        ``self._panel_message_id`` so the next sticky check knows which
        message represents the live panel.

        Returns ``(message, df)``. Raises ``discord.Forbidden`` if the
        bot lacks send permission; other exceptions propagate.
        """
        df = await _load_matches_cached(self.bot.db_path)
        # Order the chart buttons most-clicked-first (Summary pinned),
        # recomputed on every (re)post from the live usage log. Off the
        # event loop — it's a quick read but still touches disk.
        display_order = await asyncio.to_thread(_chart_display_order, self.bot.db_path)
        view = MatchStatsPanel(
            df=df if not df.empty else None,
            display_order=display_order,
        )
        msg = await channel.send(
            embed=_build_panel_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self._panel_message_id = msg.id
        return msg, df

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
        """Keep the panel at the top of PANEL_CHANNEL_ID by DELETING any
        non-panel messages that landed after it. The panel message itself
        is preserved — no re-post — so its id, reactions and jump-url
        stay stable.

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
        # last N messages for our embed signature. Only the first panel
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
            self._sticky_panel_last_fired = dt.datetime.now()
            return

        # Panel is no longer the topmost message. Delete every message
        # AFTER it (chronologically newer) until the panel is on top
        # again. We don't repost the panel itself — keeps its id, embed,
        # and reactions stable.
        bumper = f"{latest.id} from {latest.author}" if latest is not None else "(empty channel)"
        self.bot.logging.info(f"Sticky panel: bumped by message {bumper}, clearing newer messages")

        deleted = 0
        # iterate newest-first; stop when we hit the panel or have cleared 50.
        async for m in channel.history(limit=50):
            if m.id == self._panel_message_id:
                break
            try:
                await m.delete()
                deleted += 1
            except discord.NotFound:
                pass
            except discord.Forbidden:
                self.bot.logging.warning(
                    f"Sticky panel: missing permission to delete message {m.id}; stopping"
                )
                break
            except Exception as exc:
                self.bot.logging.error(f"Sticky panel: failed to delete message {m.id}: {exc!r}")
        self.bot.logging.info(f"Sticky panel: cleared {deleted} non-panel message(s)")
        self._sticky_panel_last_fired = dt.datetime.now()

    @sticky_panel.before_loop
    async def before_sticky_panel(self) -> None:
        await self.bot.wait_until_ready()

    @sticky_panel.error
    async def sticky_panel_error(self, exc: BaseException) -> None:
        """Auto-restart sticky_panel on unhandled error.

        Default @tasks.loop behaviour on exception is log + stop. Mirror
        the stream_matches recovery pattern so a transient failure
        (rate-limit blip, Gateway hiccup) doesn't permanently disable
        sticky behaviour.
        """
        self.bot.logging.error(f"sticky_panel errored: {exc!r}, restarting in 60s")
        await asyncio.sleep(60)
        if not self.sticky_panel.is_running():
            self.sticky_panel.start()

    @app_commands.command(
        name="chart_index",
        description="List all available match-analysis charts with descriptions",
    )
    @app_commands.guild_only()
    async def chart_index(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        # Reverse map fn → stem so panel buttons (which key by fn, not stem)
        # can be cross-checked against ALL_PLOTS for orphan detection.
        stem_by_fn = {fn: stem for stem, fn in analysis.ALL_PLOTS}

        panel_lines: list[str] = []
        seen_stems: set[str] = set()
        for _label, emoji, fn, title in CHART_DEFS:
            stem = stem_by_fn.get(fn)
            if stem is None:
                log.warning(f"CHART_DEFS entry {title!r} fn missing from ALL_PLOTS")
                continue
            seen_stems.add(stem)
            panel_lines.append(f"{emoji} **{title}**")

        plot_by_stem = dict(analysis.ALL_PLOTS)
        more_lines: list[str] = []
        for stem, label, emoji, description in MORE_CHART_DEFS:
            if stem not in plot_by_stem:
                log.warning(f"MORE_CHART_DEFS stem {stem!r} missing from ALL_PLOTS")
                continue
            seen_stems.add(stem)
            more_lines.append(f"{emoji} **{label}** — {description}")

        orphan_stems = [s for s, _ in analysis.ALL_PLOTS if s not in seen_stems]

        cmds_embed = discord.Embed(
            title="⚡ Slash commands",
            description="\n".join(
                [
                    "`/match_stats_panel` — Post the persistent control panel (admin)",
                    "`/me [user]` — Visual insights card",
                    "`/me_text [user]` — Compact text summary",
                    "`/me_todo [user]` — Prescriptive bullet advice",
                    "`/champ <name>` — Group-wide stats for one champion",
                    "`/compare <a> <b>` — Side-by-side player comparison",
                    "`/streak [user]` — Current active win/loss streak",
                    "`/leaderboard_week [days]` — Past-N-days WR ranking",
                    "`/chart_index` — This catalog",
                    "`/refresh_db_cache` — Force cache invalidation (admin)",
                    "`/whois` — Bot operational status (admin)",
                ]
            ),
            color=discord.Color(int(analysis.PALETTE["primary"].lstrip("#"), 16)),
        )

        panel_embed = discord.Embed(
            title="📊 Panel buttons",
            description=(
                "Click any of these directly on the match-stats panel.\n\n" + "\n".join(panel_lines)
            ),
            color=discord.Color.blurple(),
        )
        panel_embed.set_footer(
            text=(
                "Tip: chart button → all-players view; dropdown → focus on one person. "
                "Inside the chart you can pivot without leaving the message."
            )
        )

        more_embed = discord.Embed(
            title='🔬 More analytics (click "More ▾" in the explorer)',
            description="\n".join(more_lines),
            color=discord.Color.dark_teal(),
        )

        embeds = [cmds_embed, panel_embed, more_embed]
        if orphan_stems:
            orphan_lines = [f"`{stem}`" for stem in orphan_stems]
            orphan_embed = discord.Embed(
                title="🔧 Other charts",
                description=(
                    "Not bound to any menu — reachable only via the notebook "
                    "or batch runner in `notebooks/`.\n\n" + "\n".join(orphan_lines)
                ),
                color=discord.Color.greyple(),
            )
            embeds.append(orphan_embed)

        await interaction.followup.send(embeds=embeds, ephemeral=True)

    @app_commands.command(
        name="me",
        description="Post your (or someone's) insights card",
    )
    @app_commands.guild_only()
    @app_commands.describe(user="(optional) look up another player's card instead of yours")
    async def me(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        target = user or interaction.user

        try:
            df = await _load_matches_cached(self.bot.db_path)
        except Exception as exc:
            self.bot.logging.error(f"/me data load failed: {exc!r}")
            await interaction.followup.send(f"Failed to load match data: {exc!r}", ephemeral=True)
            return

        if df.empty:
            await interaction.followup.send(
                "No match data yet — run /backfill_all first.", ephemeral=True
            )
            return

        # discord_user_id is stored as TEXT in SQLite for legacy reasons;
        # cast both sides to str so we don't miss matches on dtype mismatch.
        matches = df[df["discord_user_id"].astype(str) == str(target.id)]
        if matches.empty:
            await interaction.followup.send(
                f"No League account linked for {target.mention}. "
                "Link one with `/add_player` first.",
                ephemeral=True,
            )
            return

        person_name = str(matches["person"].iloc[0])
        player_key = f"person:{person_name}"

        try:
            fig = await asyncio.to_thread(analysis.plot_insights_card, df, player_key)
        except Exception as exc:
            self.bot.logging.error(f"/me insights-card render failed: {exc!r}")
            await interaction.followup.send(f"Chart failed: {exc!r}", ephemeral=True)
            return

        embed = discord.Embed(title=f"🧾 Insights card — {person_name}")
        embed.set_footer(
            text=(
                f"{len(df):,} games · {df['person'].nunique()} people "
                f"({df['riot_account'].nunique()} Riot accounts) · "
                f"{df['game_start'].min().date()} → {df['game_start'].max().date()}"
            )
        )
        embed.set_image(url="attachment://chart.png")
        file = _figure_to_file(fig)
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)

    @app_commands.command(
        name="me_text",
        description="Compact text summary (yours or someone's)",
    )
    @app_commands.guild_only()
    @app_commands.describe(user="(optional) look up another player's summary instead of yours")
    async def me_text(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        target = user or interaction.user

        try:
            df = await _load_matches_cached(self.bot.db_path)
        except Exception as exc:
            self.bot.logging.error(f"/me_text data load failed: {exc!r}")
            await interaction.followup.send(f"Failed to load match data: {exc!r}", ephemeral=True)
            return

        if df.empty:
            await interaction.followup.send(
                "No match data yet — run /backfill_all first.", ephemeral=True
            )
            return

        matches = df[df["discord_user_id"].astype(str) == str(target.id)]
        if matches.empty:
            await interaction.followup.send(
                f"No League account linked for {target.mention}. "
                "Link one with `/add_player` first.",
                ephemeral=True,
            )
            return

        person_name = str(matches["person"].iloc[0])
        sub = df[df["person"] == person_name]
        n_games = int(len(sub))
        if n_games < analysis._INSIGHTS_MIN_GAMES:
            await interaction.followup.send(
                f"Need >={analysis._INSIGHTS_MIN_GAMES} games for a summary "
                f"(you have {n_games}).",
                ephemeral=True,
            )
            return

        embed = _build_me_text_embed(df, sub, person_name)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="me_todo",
        description="Prescriptive bullets (yours or someone's)",
    )
    @app_commands.guild_only()
    @app_commands.describe(user="(optional) look up another player's to-do list instead of yours")
    async def me_todo(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        target = user or interaction.user

        try:
            df = await _load_matches_cached(self.bot.db_path)
        except Exception as exc:
            self.bot.logging.error(f"/me_todo data load failed: {exc!r}")
            await interaction.followup.send(f"Failed to load match data: {exc!r}", ephemeral=True)
            return

        if df.empty:
            await interaction.followup.send(
                "No match data yet — run /backfill_all first.", ephemeral=True
            )
            return

        matches = df[df["discord_user_id"].astype(str) == str(target.id)]
        if matches.empty:
            await interaction.followup.send(
                f"No League account linked for {target.mention}. "
                "Link one with `/add_player` first.",
                ephemeral=True,
            )
            return

        person_name = str(matches["person"].iloc[0])
        sub = df[df["person"] == person_name]
        n_games = int(len(sub))
        if n_games < analysis._INSIGHTS_MIN_GAMES:
            await interaction.followup.send(
                f"Need >={analysis._INSIGHTS_MIN_GAMES} games for a to-do list "
                f"(you have {n_games}).",
                ephemeral=True,
            )
            return

        embed = _build_me_todo_embed(df, sub, person_name)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="streak", description="Show your current active win/loss streak")
    @app_commands.guild_only()
    @app_commands.describe(user="(optional) look up another player's current streak")
    async def streak(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
    ) -> None:
        target = user or interaction.user
        await interaction.response.defer(ephemeral=True)

        df = await _load_matches_cached(self.bot.db_path)
        matched = df[df["discord_user_id"].astype(str) == str(target.id)]
        if matched.empty:
            await interaction.followup.send(
                f"No League account linked for {target.mention}. Use /add_player.",
                ephemeral=True,
            )
            return

        person_name = str(matched["person"].iloc[0])
        sub = matched.sort_values("game_start")
        last_outcome = int(sub["win"].iloc[-1])
        streak = 0
        for w in reversed(sub["win"].values):
            if int(w) == last_outcome:
                streak += 1
            else:
                break

        last_game = sub["game_start"].iloc[-1]
        outcome_word = "win" if last_outcome == 1 else "loss"
        emoji = "🔥" if last_outcome == 1 else "🥶"
        streak_word = f"{streak}-game {outcome_word} streak"

        embed = discord.Embed(
            title=f"{emoji} {person_name}",
            description=f"Currently on a **{streak_word}**.\nLast game: {last_game:%Y-%m-%d %H:%M}",
            color=discord.Color.green() if last_outcome == 1 else discord.Color.red(),
        )
        embed.set_footer(text=f"{len(sub):,} total games · WR {sub['win'].mean():.0%}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="leaderboard_week",
        description="Past 7 days WR per player (use days arg for other windows)",
    )
    @app_commands.guild_only()
    @app_commands.describe(days="Number of days to include (1-90, default 7)")
    async def leaderboard_week(self, interaction: discord.Interaction, days: int = 7) -> None:
        await interaction.response.defer(ephemeral=True)
        days = max(1, min(days, 90))

        try:
            df = await _load_matches_cached(self.bot.db_path)
        except Exception as exc:
            self.bot.logging.error(f"/leaderboard_week data load failed: {exc!r}")
            await interaction.followup.send(f"Failed to load match data: {exc!r}", ephemeral=True)
            return

        if df.empty:
            await interaction.followup.send(
                "No match data yet — run /backfill_all first.", ephemeral=True
            )
            return

        now = df["game_start"].max()
        cutoff = now - pd.Timedelta(days=days)
        recent = df[df["game_start"] >= cutoff]

        if recent.empty:
            await interaction.followup.send(f"No games in the last {days} days.", ephemeral=True)
            return

        rows = []
        for person, group in recent.groupby("person"):
            n = len(group)
            if n < 3:
                continue
            wins = int(group["win"].sum())
            wr = wins / n
            wilson_lo, wilson_hi = analysis.wilson_ci(wins, n)
            rows.append(
                {
                    "person": person,
                    "n": n,
                    "wr": wr,
                    "wilson_lo": wilson_lo,
                    "wilson_hi": wilson_hi,
                }
            )

        if not rows:
            await interaction.followup.send(
                f"No qualifying players (need ≥3 games) in the last {days} days.",
                ephemeral=True,
            )
            return

        rows.sort(key=lambda r: r["wr"], reverse=True)

        medal = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows):
            prefix = medal[i] if i < 3 else f"{i + 1}."
            lines.append(
                f"{prefix} **{r['person']}** — {r['wr']:.0%}  "
                f"({r['n']} games, CI {r['wilson_lo']:.0%}–{r['wilson_hi']:.0%})"
            )

        embed = discord.Embed(
            title=f"📈 Past {days}-day leaderboard",
            description="\n".join(lines),
            color=discord.Color(int(analysis.PALETTE["primary"].lstrip("#"), 16)),
        )
        embed.set_footer(
            text=(
                f"{len(recent):,} games · {len(rows)} qualifying players · "
                f"{cutoff:%Y-%m-%d} → {now:%Y-%m-%d}"
            )
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _champ_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest up to 25 champions matching the user's typed substring."""
        try:
            df = await _load_matches_cached(self.bot.db_path)
        except Exception:
            return []

        if df.empty:
            return []

        # Sort by play count desc so the most-played champs surface first
        # when the input is empty.
        counts = df.groupby("champion").size().sort_values(ascending=False)

        current_lc = current.lower()
        matches = [champion for champion in counts.index if current_lc in str(champion).lower()]
        return [app_commands.Choice(name=champion, value=champion) for champion in matches[:25]]

    @app_commands.command(
        name="champ",
        description="Group-wide stats for a single champion",
    )
    @app_commands.guild_only()
    @app_commands.describe(name="Champion name (Riot internal name, e.g. 'Khazix', 'MissFortune')")
    @app_commands.autocomplete(name=_champ_autocomplete)
    async def champ(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            df = await _load_matches_cached(self.bot.db_path)
        except Exception as exc:
            self.bot.logging.error(f"/champ data load failed: {exc!r}")
            await interaction.followup.send(f"Failed to load match data: {exc!r}", ephemeral=True)
            return

        if df.empty:
            await interaction.followup.send(
                "No match data yet — run /backfill_all first.", ephemeral=True
            )
            return

        sel = df[df["champion"].str.lower() == name.lower()]
        if sel.empty:
            all_champs = df["champion"].unique()
            close = [c for c in all_champs if name.lower() in c.lower()][:5]
            suggest = f"\nDid you mean: {', '.join(close)}" if close else ""
            await interaction.followup.send(
                f"No data for champion `{name}`.{suggest}", ephemeral=True
            )
            return

        champ_name = str(sel["champion"].iloc[0])
        total_picks = len(sel)
        wins = int(sel["win"].sum())
        raw_wr = wins / total_picks
        group_baseline = float(df["win"].mean())
        shrunk_wr = analysis.bayesian_shrunk_wr(
            wins, total_picks, baseline_wr=group_baseline, prior_strength=50
        )

        # Top player on this champ (shrunken WR per player, n>=5). Group
        # baseline used as the prior so a 5/5 player doesn't outrank a
        # 30/50 player on noise alone.
        per_player = sel.groupby("person").agg(n=("win", "size"), wins=("win", "sum")).reset_index()
        per_player = per_player[per_player["n"] >= 5]
        if len(per_player):
            per_player["raw_wr"] = per_player["wins"] / per_player["n"]
            per_player["shrunk_wr"] = per_player.apply(
                lambda r: analysis.bayesian_shrunk_wr(
                    int(r["wins"]),
                    int(r["n"]),
                    baseline_wr=group_baseline,
                    prior_strength=30,
                ),
                axis=1,
            )
            per_player = per_player.sort_values("shrunk_wr", ascending=False)
            top = per_player.iloc[0]
            top_player_line = (
                f"**{top['person']}** — {top['raw_wr']:.0%} on {int(top['n'])} games "
                f"(shrunk {top['shrunk_wr']:.0%})"
            )
        else:
            top_player_line = "No player has ≥5 games on this champion"

        now = df["game_start"].max()
        last_30d = sel[sel["game_start"] >= now - pd.Timedelta(days=30)]
        last_90d = sel[sel["game_start"] >= now - pd.Timedelta(days=90)]

        embed = discord.Embed(
            title=f"🏹 {champ_name}",
            color=discord.Color(int(analysis.PALETTE["primary"].lstrip("#"), 16)),
        )
        embed.add_field(
            name="Group totals",
            value=(
                f"{total_picks} picks · raw {raw_wr:.0%} · shrunk {shrunk_wr:.0%} "
                f"(group baseline {group_baseline:.0%})"
            ),
            inline=False,
        )
        embed.add_field(name="Top player", value=top_player_line, inline=False)
        embed.add_field(
            name="Recent",
            value=f"Last 30d: {len(last_30d)} picks · Last 90d: {len(last_90d)} picks",
            inline=False,
        )

        # Banner picks: players with >100 games on this champ. Only show
        # the field when at least one qualifies — no "none" noise.
        banner_rows = sel.groupby("person").size().reset_index(name="n")
        banner_rows = banner_rows[banner_rows["n"] > 100].sort_values("n", ascending=False)
        if not banner_rows.empty:
            banner_lines = []
            for _, br in banner_rows.iterrows():
                p_wins = int(sel[sel["person"] == br["person"]]["win"].sum())
                p_wr = p_wins / int(br["n"])
                banner_lines.append(f"**{br['person']}** — {int(br['n'])} games ({p_wr:.0%})")
            embed.add_field(name="🚩 Banner picks", value="\n".join(banner_lines), inline=False)

        # Role from the positions actually played in our data (no hardcoded
        # champ->role map) — show the most common one + its share.
        role_counts = sel.loc[sel["role"].isin(analysis.ROLE_ORDER), "role"].value_counts()
        if not role_counts.empty:
            top_role = role_counts.index[0]
            role = f"{top_role} ({role_counts.iloc[0] / role_counts.sum():.0%})"
        else:
            role = "unknown"
        embed.set_footer(
            text=f"Role: {role} · Played by {sel['person'].nunique()} different people"
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="compare",
        description="Side-by-side stat comparison between two players",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        user_a="First player to compare",
        user_b="Second player to compare",
    )
    async def compare(
        self,
        interaction: discord.Interaction,
        user_a: discord.User,
        user_b: discord.User,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if user_a.id == user_b.id:
            await interaction.followup.send("Pick two different users.", ephemeral=True)
            return

        try:
            df = await _load_matches_cached(self.bot.db_path)
        except Exception as exc:
            self.bot.logging.error(f"/compare data load failed: {exc!r}")
            await interaction.followup.send(f"Failed to load match data: {exc!r}", ephemeral=True)
            return

        if df.empty:
            await interaction.followup.send(
                "No match data yet — run /backfill_all first.", ephemeral=True
            )
            return

        rows: dict[str, pd.DataFrame] = {}
        for key, u in [("a", user_a), ("b", user_b)]:
            matched = df[df["discord_user_id"].astype(str) == str(u.id)]
            if matched.empty:
                await interaction.followup.send(
                    f"No League account linked for {u.mention}. "
                    "Link one with `/add_player` first.",
                    ephemeral=True,
                )
                return
            rows[key] = matched

        now = df["game_start"].max()
        cutoff = now - pd.Timedelta(days=30)

        metrics: dict[str, dict] = {}
        for key, sub in rows.items():
            n = len(sub)
            wins = int(sub["win"].sum())
            wr = wins / n
            kda = (sub["kills"] + sub["assists"]) / sub["deaths"].clip(lower=1)
            champ_counts = sub["champion"].value_counts()
            entry = {
                "name": str(sub["person"].iloc[0]),
                "n": n,
                "wr": wr,
                "kda_mean": float(kda.mean()),
                "kills_mean": float(sub["kills"].mean()),
                "deaths_mean": float(sub["deaths"].mean()),
                "assists_mean": float(sub["assists"].mean()),
                "top_champ": champ_counts.index[0] if len(champ_counts) else "—",
                "top_champ_n": int(champ_counts.iloc[0]) if len(champ_counts) else 0,
            }
            if "role" in sub.columns:
                roles = sub[sub["role"] != "UNKNOWN"]["role"].value_counts()
                entry["top_role"] = roles.index[0] if len(roles) else "—"
            else:
                entry["top_role"] = "—"

            recent = sub[sub["game_start"] >= cutoff]
            if len(recent) >= 5:
                entry["wr_30d"] = float(recent["win"].mean())
                entry["n_30d"] = len(recent)
            else:
                entry["wr_30d"] = None
                entry["n_30d"] = len(recent)

            wins_seq = sub.sort_values("game_start")["win"].values
            max_win = max_loss = cur_w = cur_l = 0
            for w in wins_seq:
                if w == 1:
                    cur_w += 1
                    cur_l = 0
                    max_win = max(max_win, cur_w)
                else:
                    cur_l += 1
                    cur_w = 0
                    max_loss = max(max_loss, cur_l)
            entry["max_win_streak"] = max_win
            entry["max_loss_streak"] = max_loss

            metrics[key] = entry

        a = metrics["a"]
        b = metrics["b"]

        embed = discord.Embed(
            title=f"⚔️ {a['name']} vs {b['name']}",
            color=discord.Color(int(analysis.PALETTE["primary"].lstrip("#"), 16)),
        )

        def fmt_pair(label, a_val, b_val, fmt="{:.0%}"):
            a_s = fmt.format(a_val) if a_val is not None else "—"
            b_s = fmt.format(b_val) if b_val is not None else "—"
            return f"`{label:<12}` {a_s:>10}  vs  {b_s:>10}"

        lines = [
            fmt_pair("WR", a["wr"], b["wr"]),
            fmt_pair("Games", a["n"], b["n"], "{:,}"),
            fmt_pair("KDA", a["kda_mean"], b["kda_mean"], "{:.2f}"),
            fmt_pair("Kills/g", a["kills_mean"], b["kills_mean"], "{:.1f}"),
            fmt_pair("Deaths/g", a["deaths_mean"], b["deaths_mean"], "{:.1f}"),
            fmt_pair("Assists/g", a["assists_mean"], b["assists_mean"], "{:.1f}"),
            f"`Top role   ` {a['top_role']:>10}  vs  {b['top_role']:>10}",
            f"`Top champ  ` {a['top_champ']:>10}  vs  {b['top_champ']:>10}",
            f"`(plays)    ` {a['top_champ_n']:>10}  vs  {b['top_champ_n']:>10}",
            fmt_pair("30d WR", a["wr_30d"], b["wr_30d"]),
            fmt_pair("Max W str", a["max_win_streak"], b["max_win_streak"], "{:,}"),
            fmt_pair("Max L str", a["max_loss_streak"], b["max_loss_streak"], "{:,}"),
        ]
        embed.description = "```\n" + "\n".join(lines) + "\n```"
        embed.set_footer(text=f"{a['n']:,} + {b['n']:,} = {a['n'] + b['n']:,} total games compared")

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def open_explorer(
        self, interaction: discord.Interaction, chart_idx: int, person: str | None
    ) -> None:
        """Entry point called by panel components: load data, render the
        requested chart for the requested person, post the chart as an
        EPHEMERAL message (only the clicker sees it). The chart-switcher
        buttons inside are restricted to the original clicker via
        interaction_check."""
        await interaction.response.defer(ephemeral=True)
        try:
            df = await _load_matches_cached(self.bot.db_path)
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

        who = analysis._display_label(person) or "all players"
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

    async def open_more_chart(
        self, interaction: discord.Interaction, stem: str, person: str | None
    ) -> None:
        """Render one overflow-analytics chart (by ALL_PLOTS stem) as an
        ephemeral post. Used by the board's More-analytics dropdown."""
        await interaction.response.defer(ephemeral=True)
        try:
            df = await _load_matches_cached(self.bot.db_path)
        except Exception as exc:
            self.bot.logging.error(f"more-chart data load failed: {exc!r}")
            await interaction.followup.send(f"Failed to load match data: {exc!r}", ephemeral=True)
            return
        if df.empty:
            await interaction.followup.send(
                "No match data yet — run /backfill_all first.", ephemeral=True
            )
            return

        fn = dict(analysis.ALL_PLOTS).get(stem)
        if fn is None:
            await interaction.followup.send(
                f"Chart `{stem}` not found in the analysis lib.", ephemeral=True
            )
            return
        try:
            fig = await asyncio.to_thread(fn, df, person)
        except Exception as exc:
            self.bot.logging.error(f"more-chart {stem!r} render failed: {exc!r}")
            await interaction.followup.send(f"Chart failed: {exc!r}", ephemeral=True)
            return

        label, emoji = next(
            ((lbl, emo) for s, lbl, emo, _ in MORE_CHART_DEFS if s == stem), (stem, "📊")
        )
        who = analysis._display_label(person) or "all players"
        embed = discord.Embed(title=f"{emoji} {label} — {who}")
        embed.set_image(url="attachment://chart.png")
        file = _figure_to_file(fig)
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)

    async def open_more_menu(self, interaction: discord.Interaction, person: str | None) -> None:
        """Open the full paginated More-analytics menu as an ephemeral —
        the board dropdown's 'open full list' escape hatch."""
        # Defer FIRST: _load_matches_cached can hit the DB / cache lock, which
        # may exceed Discord's 3s initial-ACK window. After defer we must use
        # followup.send (which carries views fine).
        await interaction.response.defer(ephemeral=True)
        try:
            df = await _load_matches_cached(self.bot.db_path)
        except Exception as exc:
            self.bot.logging.error(f"more-menu data load failed: {exc!r}")
            await interaction.followup.send(f"Failed to load match data: {exc!r}", ephemeral=True)
            return
        if df.empty:
            await interaction.followup.send(
                "No match data yet — run /backfill_all first.", ephemeral=True
            )
            return
        view = _MoreAnalyticsView(df=df, invoker_id=interaction.user.id, person=person)
        await interaction.followup.send(
            "Pick an analysis — it'll post here.", view=view, ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MatchAnalysis(bot))
