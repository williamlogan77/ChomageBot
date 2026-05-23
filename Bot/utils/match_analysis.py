"""Match-stats EDA helpers.

Pure functions for loading and visualising the bot's match_stats table.
Imported by:
  - the Discord cog (cogs/match_analysis.py) to render charts on demand,
  - the exploration notebook (notebooks/match_analysis.ipynb),
  - the batch runner (notebooks/run_analysis.py).

No matplotlib state is kept here — every plot function builds and returns
a fresh Figure that the caller is responsible for closing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

DEFAULT_DB = Path(__file__).resolve().parent.parent / "db" / "database.sqlite"

# Days of the week and their short labels, Monday=0 per pandas weekday().
DOW_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Bucket edges used for "time since previous game" analysis. The first
# bucket (< 10 min) captures dodges/remakes/back-to-back queues; the last
# captures "fresh session" after a long break.
GAP_BINS_MIN = [0, 10, 30, 60, 120, 360, 1440, 99999]
GAP_LABELS = ["<10m", "10-30m", "30-60m", "1-2h", "2-6h", "6-24h", ">24h"]

# Duration buckets in minutes — typical League games are 20-40 min,
# stomps end <20, scaling games run 35+.
DURATION_BINS_MIN = [0, 15, 20, 25, 30, 35, 40, 999]
DURATION_LABELS = ["<15", "15-20", "20-25", "25-30", "30-35", "35-40", "40+"]


# --- Visual style -----------------------------------------------------------

PALETTE = {
    "primary": "#4878d0",
    "win": "#5fbc7a",
    "loss": "#e15759",
    "neutral": "#b8c0c8",
    "accent_orange": "#f0934a",
    "accent_teal": "#4daf94",
    "accent_purple": "#a878d0",
    "text": "#222",
    "muted": "#666",
    "grid": "#e6eaee",
    "spine": "#cfd4d9",
}

# Hand-picked cycle for multi-series charts (progression, learning curves).
# Avoids matplotlib's default tab10 which clashes with our win/loss greens
# and reds when overlapping.
SERIES_CYCLE = [
    "#4878d0",
    "#ee854a",
    "#6acc64",
    "#d65f5f",
    "#956cb4",
    "#8c613c",
    "#dc7ec0",
    "#797979",
    "#d5bb67",
    "#82c6e2",
]


def _apply_style() -> None:
    """Sets matplotlib defaults once at module import.

    Pulled into a function so it's easy to re-apply after a notebook user
    runs ``plt.style.use(...)`` and wants the bot's look back.
    """
    try:
        plt.style.use("seaborn-v0_8-whitegrid")
    except OSError:
        plt.style.use("seaborn-whitegrid")
    plt.rcParams.update(
        {
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.edgecolor": PALETTE["spine"],
            "axes.linewidth": 0.8,
            "axes.labelcolor": PALETTE["text"],
            "axes.titleweight": "bold",
            "axes.titlesize": 14,
            "axes.titlepad": 12,
            "axes.labelsize": 11,
            "axes.labelpad": 8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.prop_cycle": plt.cycler(color=SERIES_CYCLE),
            "xtick.color": PALETTE["muted"],
            "ytick.color": PALETTE["muted"],
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "grid.color": PALETTE["grid"],
            "grid.linewidth": 0.7,
            "legend.frameon": False,
            "legend.fontsize": 10,
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "figure.titlesize": 16,
            "figure.titleweight": "bold",
            "lines.linewidth": 2.0,
            "boxplot.boxprops.color": PALETTE["spine"],
            "boxplot.whiskerprops.color": PALETTE["spine"],
            "boxplot.capprops.color": PALETTE["spine"],
            "boxplot.medianprops.color": PALETTE["text"],
            "boxplot.flierprops.markeredgecolor": PALETTE["muted"],
        }
    )


_apply_style()


def _polish_ax(ax) -> None:
    """Consistent finishing touches per axes — call after configuring titles
    and labels but before tight_layout."""
    ax.set_axisbelow(True)
    for spine in ("left", "bottom"):
        if spine in ax.spines:
            ax.spines[spine].set_color(PALETTE["spine"])


def _baseline(ax, y: float = 0.5) -> None:
    """A subtle horizontal 'break-even' reference at y."""
    ax.axhline(y, color=PALETTE["muted"], linewidth=0.8, linestyle=(0, (4, 4)), alpha=0.55)


def load_matches(db_path: Path = DEFAULT_DB) -> pd.DataFrame:
    """Load match_stats joined with league_players + users; derive features.

    Each row is one (Riot account, match). Players with multiple Riot
    accounts (e.g. thewhittalian's 3 alts) collapse via the ``person``
    column — the canonical "this is one real human" key. ``person`` is
    the user's Discord nickname if set, else discord_tag, else the
    league_username for orphan accounts with no users-table mapping.

    The plot helpers all filter on ``person`` so per-user analyses
    aggregate every Riot account they have. ``riot_account`` is still
    available for per-account drill-downs (e.g. champion learning curve
    is meaningful per account, not per person).
    """
    with sqlite3.connect(db_path) as con:
        df = pd.read_sql_query(
            """
            SELECT
                ms.match_id,
                ms.puuid,
                ms.game_start,
                ms.queue_id,
                ms.champion,
                ms.win,
                ms.kills,
                ms.deaths,
                ms.assists,
                ms.duration_sec,
                lp.league_username AS riot_account,
                lp.discord_user_id,
                COALESCE(
                    NULLIF(TRIM(u.nickname), ''),
                    NULLIF(u.discord_tag, ''),
                    lp.league_username
                ) AS person
            FROM match_stats ms
            JOIN league_players lp USING (puuid)
            LEFT JOIN users u ON u.user_id = lp.discord_user_id
            ORDER BY ms.game_start ASC
            """,
            con,
        )

    df["game_start"] = pd.to_datetime(df["game_start"])
    df["duration_min"] = df["duration_sec"] / 60.0
    df["kda"] = (df["kills"] + df["assists"]) / df["deaths"].clip(lower=1)
    df["kd"] = df["kills"] / df["deaths"].clip(lower=1)
    df["hour"] = df["game_start"].dt.hour
    df["dow"] = df["game_start"].dt.weekday
    df["date"] = df["game_start"].dt.date
    df["duration_bucket"] = pd.cut(
        df["duration_min"], bins=DURATION_BINS_MIN, labels=DURATION_LABELS, right=False
    )

    # Per-person time-series features (treats multi-account users as one
    # continuous game stream — sorted by game_start so the streak/gap
    # bookkeeping reflects "their session" not "this account's session").
    df = df.sort_values(["person", "game_start"]).reset_index(drop=True)

    prev_end = df.groupby("person")["game_start"].shift(1) + pd.to_timedelta(
        df.groupby("person")["duration_sec"].shift(1).fillna(0), unit="s"
    )
    df["gap_since_prev_min"] = (df["game_start"] - prev_end).dt.total_seconds() / 60.0
    df["gap_bucket"] = pd.cut(
        df["gap_since_prev_min"], bins=GAP_BINS_MIN, labels=GAP_LABELS, right=False
    )

    df["loss_streak_in"] = df.groupby("person")["win"].transform(_loss_streak_entering)
    # nth-on-champ is per-Riot-account — different accounts have different
    # mastery curves on the same champ.
    df["nth_on_champ"] = df.groupby(["riot_account", "champion"]).cumcount() + 1
    return df


def people_summary(df: pd.DataFrame) -> pd.DataFrame:
    """One row per Discord person: total games, account count, account names.

    Used by the cog to build the player dropdown. Sorted by game count
    descending so the busiest players show first.
    """
    grouped = df.groupby("person").agg(
        games=("match_id", "size"),
        accounts=("riot_account", lambda s: sorted(set(s))),
    )
    grouped["account_count"] = grouped["accounts"].map(len)
    return grouped.sort_values("games", ascending=False).reset_index()


def _loss_streak_entering(wins: pd.Series) -> pd.Series:
    """For each row in `wins` (chronological 1=win/0=loss), return the count
    of consecutive losses immediately PRECEDING that row.

    A win resets the streak; the first game's value is 0 (no prior games).
    """
    out = np.zeros(len(wins), dtype=int)
    streak = 0
    for i, w in enumerate(wins.values):
        out[i] = streak
        streak = 0 if w == 1 else streak + 1
    return pd.Series(out, index=wins.index)


# --- plotting helpers -------------------------------------------------------


def _bin_winrate(df: pd.DataFrame, bucket_col: str) -> pd.DataFrame:
    """Aggregate winrate + sample count per bucket. Skips empty buckets."""
    g = (
        df.dropna(subset=[bucket_col])
        .groupby(bucket_col, observed=True)["win"]
        .agg(["count", "mean"])
        .rename(columns={"count": "games", "mean": "winrate"})
    )
    return g.reset_index()


def _annotate_bars(ax, x, heights, counts, fmt: str = "{:.0%}") -> None:
    """Print value + sample count above each bar, in a discreet two-line label."""
    for xi, h, n in zip(x, heights, counts, strict=False):
        if pd.isna(h):
            continue
        ax.annotate(
            f"{fmt.format(h)}\nn={int(n)}",
            xy=(xi, h),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=9,
            color=PALETTE["text"],
        )


def _title(base: str, player: str | None) -> str:
    return base if player is None else f"{base}  ·  {player}"


def _filter_player(df: pd.DataFrame, player: str | None) -> pd.DataFrame:
    """Filter rows to one ``person`` (Discord-aggregated). ``player`` here
    is a person key — pass the canonical display name to filter, or None
    for the all-players aggregate."""
    return df if player is None else df[df["person"] == player]


def _empty_figure(message: str) -> plt.Figure:
    """A blank-but-styled figure used when a chart has no data to show."""
    fig, ax = plt.subplots(figsize=(10, 3))
    ax.text(0.5, 0.5, message, ha="center", va="center", color=PALETTE["muted"], fontsize=12)
    ax.set_axis_off()
    return fig


# --- 1. KDA vs outcome ------------------------------------------------------


def plot_kda_vs_outcome(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """KDA distribution split by outcome, plus the actual win-rate at each
    KDA bucket. The first answers "do my wins LOOK better?", the second
    answers "does carrying actually CAUSE the win?".
    """
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to plot")

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.6))

    cap = d["kda"].quantile(0.99)
    wins = d.loc[d["win"] == 1, "kda"].clip(upper=cap)
    losses = d.loc[d["win"] == 0, "kda"].clip(upper=cap)
    bp = axes[0].boxplot(
        [wins, losses],
        labels=["Win", "Loss"],
        vert=True,
        patch_artist=True,
        widths=0.55,
        medianprops={"color": PALETTE["text"], "linewidth": 1.4},
    )
    for patch, colour in zip(bp["boxes"], (PALETTE["win"], PALETTE["loss"]), strict=False):
        patch.set_facecolor(colour)
        patch.set_alpha(0.55)
        patch.set_edgecolor(colour)
    axes[0].set_ylabel("KDA  (K + A) / max(D, 1)")
    axes[0].set_title(_title("KDA distribution by outcome", player))
    _polish_ax(axes[0])

    d2 = d.copy()
    d2["kda_bucket"] = pd.cut(
        d2["kda"],
        bins=[0, 1, 2, 3, 4, 5, 10, 999],
        labels=["<1", "1-2", "2-3", "3-4", "4-5", "5-10", "10+"],
    )
    g = _bin_winrate(d2, "kda_bucket")
    axes[1].bar(range(len(g)), g["winrate"], color=PALETTE["primary"], width=0.7)
    axes[1].set_xticks(range(len(g)))
    axes[1].set_xticklabels(g["kda_bucket"])
    axes[1].set_ylim(0, 1.1)
    axes[1].set_ylabel("Win rate")
    axes[1].set_xlabel("KDA bucket")
    axes[1].set_title(_title("Win rate by KDA", player))
    _baseline(axes[1])
    _annotate_bars(axes[1], range(len(g)), g["winrate"], g["games"])
    _polish_ax(axes[1])

    fig.tight_layout()
    return fig


# --- 2. Game duration vs outcome -------------------------------------------


def plot_duration_vs_outcome(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Win rate per game-length bucket + win/loss volume stacked alongside.
    Short games on either side, longer games are the slugfests.
    """
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to plot")

    g = _bin_winrate(d, "duration_bucket")
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.6))

    axes[0].bar(range(len(g)), g["winrate"], color=PALETTE["accent_teal"], width=0.7)
    axes[0].set_xticks(range(len(g)))
    axes[0].set_xticklabels(g["duration_bucket"])
    axes[0].set_ylim(0, 1.1)
    axes[0].set_xlabel("Game duration (min)")
    axes[0].set_ylabel("Win rate")
    axes[0].set_title(_title("Win rate by game duration", player))
    _baseline(axes[0])
    _annotate_bars(axes[0], range(len(g)), g["winrate"], g["games"])
    _polish_ax(axes[0])

    pivot = (
        d.dropna(subset=["duration_bucket"])
        .groupby(["duration_bucket", "win"], observed=True)
        .size()
        .unstack(fill_value=0)
    )
    pivot = pivot.reindex(DURATION_LABELS).fillna(0)
    x = np.arange(len(pivot))
    axes[1].bar(x, pivot.get(0, 0), color=PALETTE["loss"], width=0.7, label="Loss")
    axes[1].bar(
        x, pivot.get(1, 0), bottom=pivot.get(0, 0), color=PALETTE["win"], width=0.7, label="Win"
    )
    axes[1].set_xticks(x)
    axes[1].set_xticklabels(pivot.index)
    axes[1].set_xlabel("Game duration (min)")
    axes[1].set_ylabel("Games")
    axes[1].set_title(_title("Game count by duration", player))
    axes[1].legend(loc="upper right")
    _polish_ax(axes[1])

    fig.tight_layout()
    return fig


# --- 3. Champion analysis --------------------------------------------------


def plot_champion_winrate(
    df: pd.DataFrame, player: str | None = None, min_games: int = 10, top: int = 12
) -> plt.Figure:
    """Champion winners (left) vs losers (right). Bars annotated with sample
    count so a 70%-on-3-games doesn't masquerade as a winner.
    """
    d = _filter_player(df, player)
    g = d.groupby("champion")["win"].agg(["count", "mean"])
    g = g[g["count"] >= min_games]
    if g.empty:
        return _empty_figure(f"No champions with ≥{min_games} games")

    winners = g.sort_values("mean", ascending=False).head(top).sort_values("mean", ascending=True)
    losers = g.sort_values("mean", ascending=True).head(top).sort_values("mean", ascending=False)

    fig_h = max(4.2, max(len(winners), len(losers)) * 0.38)
    fig, axes = plt.subplots(1, 2, figsize=(13, fig_h))

    for ax, frame, colour, title in (
        (axes[0], winners, PALETTE["win"], "Winners — highest win rate"),
        (axes[1], losers, PALETTE["loss"], "Losers — lowest win rate"),
    ):
        y = np.arange(len(frame))
        ax.barh(y, frame["mean"], color=colour, height=0.7)
        ax.axvline(0.5, color=PALETTE["muted"], linewidth=0.8, linestyle=(0, (4, 4)), alpha=0.55)
        ax.set_yticks(y)
        ax.set_yticklabels(frame.index, color=PALETTE["text"])
        ax.set_xlim(0, 1)
        ax.set_xlabel("Win rate")
        ax.set_title(_title(f"{title} (≥{min_games} games)", player))
        _polish_ax(ax)
        for yi, (wr, n) in enumerate(zip(frame["mean"], frame["count"], strict=False)):
            ax.annotate(
                f"{wr:.0%}  n={int(n)}",
                xy=(wr, yi),
                xytext=(6, 0),
                textcoords="offset points",
                va="center",
                fontsize=9,
                color=PALETTE["text"],
            )

    fig.tight_layout()
    return fig


def plot_champion_learning_curve(
    df: pd.DataFrame, player: str | None = None, top: int = 5, window: int = 5
) -> plt.Figure:
    """Rolling win rate by nth game on a champion, for that player's most-
    played champions. Rising = learning the champ pays off; flat/falling
    = no measurable improvement.
    """
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to plot")
    top_champs = d["champion"].value_counts().head(top).index.tolist()
    d = d[d["champion"].isin(top_champs)].sort_values(["champion", "nth_on_champ"])

    fig, ax = plt.subplots(figsize=(11, 5))
    for idx, champ in enumerate(top_champs):
        sub = d[d["champion"] == champ]
        if len(sub) < window:
            continue
        roll = sub["win"].rolling(window=window, min_periods=1).mean()
        ax.plot(
            sub["nth_on_champ"],
            roll,
            label=f"{champ} (n={len(sub)})",
            color=SERIES_CYCLE[idx % len(SERIES_CYCLE)],
            linewidth=2.0,
            alpha=0.9,
        )

    _baseline(ax)
    ax.set_xlabel("Nth game on champion")
    ax.set_ylabel(f"Rolling win rate (window={window})")
    ax.set_title(_title(f"Learning curves — top {top} champions", player))
    ax.set_ylim(0, 1.05)
    ax.legend(loc="lower right", fontsize=9, ncol=1)
    _polish_ax(ax)
    fig.tight_layout()
    return fig


# --- 4. Temporal patterns ---------------------------------------------------


def _temporal_dual_axis(
    counts: pd.Series,
    winrate_mask: pd.Series,
    winrate_values: pd.Series,
    fig_size: tuple[float, float],
    x_ticks,
    x_labels,
    xlabel: str,
    title: str,
) -> plt.Figure:
    """Shared layout for hour-of-day and day-of-week charts. Two axes:
    grey volume bars in front, coloured win-rate line in back."""
    fig, ax_vol = plt.subplots(figsize=fig_size)
    ax_wr = ax_vol.twinx()

    ax_vol.bar(counts.index, counts.values, color=PALETTE["neutral"], width=0.75, label="Games")
    ax_vol.set_xlabel(xlabel)
    ax_vol.set_ylabel("Games played", color=PALETTE["muted"])
    ax_vol.set_xticks(x_ticks)
    ax_vol.set_xticklabels(x_labels)
    ax_vol.set_axisbelow(True)

    ax_wr.plot(
        winrate_values.index[winrate_mask],
        winrate_values[winrate_mask],
        color=PALETTE["primary"],
        marker="o",
        markersize=5,
        linewidth=2.2,
        label="Win rate",
    )
    _baseline(ax_wr)
    ax_wr.set_ylim(0, 1.05)
    ax_wr.set_ylabel("Win rate", color=PALETTE["primary"])
    ax_wr.tick_params(axis="y", colors=PALETTE["primary"])
    ax_wr.grid(False)
    ax_wr.spines["right"].set_visible(False)
    ax_wr.spines["top"].set_visible(False)

    fig.suptitle(title)
    fig.tight_layout()
    return fig


def plot_hour_of_day(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Win rate + game volume by hour of day. Tilt-hour detector."""
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to plot")
    by_hour = d.groupby("hour")["win"].agg(["count", "mean"]).reindex(range(24), fill_value=0)
    return _temporal_dual_axis(
        counts=by_hour["count"],
        winrate_mask=by_hour["count"] > 0,
        winrate_values=by_hour["mean"],
        fig_size=(13, 4.6),
        x_ticks=range(0, 24),
        x_labels=[f"{h:02d}" for h in range(24)],
        xlabel="Hour of day (local)",
        title=_title("Hour of day — volume + win rate", player),
    )


def plot_day_of_week(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Same shape as hour-of-day but on a day axis. Weekend warrior detector."""
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to plot")
    by_dow = d.groupby("dow")["win"].agg(["count", "mean"]).reindex(range(7), fill_value=0)
    return _temporal_dual_axis(
        counts=by_dow["count"],
        winrate_mask=by_dow["count"] > 0,
        winrate_values=by_dow["mean"],
        fig_size=(9, 4.6),
        x_ticks=range(7),
        x_labels=DOW_LABELS,
        xlabel="Day of week",
        title=_title("Day of week — volume + win rate", player),
    )


def plot_hour_dow_heatmap(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Win-rate heatmap on the hour × day grid. Two-dimensional view that
    catches patterns like "only Friday-evening is bad". Cells with <3
    samples are blanked to avoid noise."""
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to plot")
    counts = d.pivot_table(index="dow", columns="hour", values="win", aggfunc="size").reindex(
        index=range(7), columns=range(24)
    )
    winrate = d.pivot_table(index="dow", columns="hour", values="win", aggfunc="mean").reindex(
        index=range(7), columns=range(24)
    )
    winrate = winrate.where(counts >= 3)

    fig, ax = plt.subplots(figsize=(13, 4.6))
    cmap = plt.get_cmap("RdYlGn").copy()
    cmap.set_bad(color="#f3f4f6")  # sparse cells = soft grey
    im = ax.imshow(winrate.values, aspect="auto", cmap=cmap, vmin=0.3, vmax=0.7, origin="lower")
    ax.set_yticks(range(7))
    ax.set_yticklabels(DOW_LABELS)
    ax.set_xticks(range(0, 24))
    ax.set_xticklabels([f"{h:02d}" for h in range(24)])
    ax.set_xlabel("Hour of day")
    ax.set_ylabel("Day of week")
    ax.set_title(_title("Win rate heatmap (cells with <3 games dimmed)", player))
    ax.grid(False)
    cbar = fig.colorbar(im, ax=ax, label="Win rate", shrink=0.8, pad=0.02)
    cbar.outline.set_visible(False)
    fig.tight_layout()
    return fig


# --- 5. Recent form & momentum ---------------------------------------------


def plot_streak_recovery(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Win rate of the NEXT game, bucketed by loss streak going into it. If
    tilt is real, longer entering streaks should produce lower win rates."""
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to plot")
    d = d.copy()
    d["streak_bucket"] = pd.cut(
        d["loss_streak_in"],
        bins=[-1, 0, 1, 2, 3, 5, 99],
        labels=["0 (post-W)", "1", "2", "3", "4-5", "6+"],
    )
    g = _bin_winrate(d, "streak_bucket")

    fig, ax = plt.subplots(figsize=(11, 4.6))
    ax.bar(range(len(g)), g["winrate"], color=PALETTE["accent_orange"], width=0.7)
    ax.set_xticks(range(len(g)))
    ax.set_xticklabels(g["streak_bucket"])
    ax.set_xlabel("Loss streak entering this game")
    ax.set_ylabel("Win rate of this game")
    ax.set_title(_title("Tilt check — win rate vs entering loss streak", player))
    ax.set_ylim(0, 1.1)
    _baseline(ax)
    _annotate_bars(ax, range(len(g)), g["winrate"], g["games"])
    _polish_ax(ax)
    fig.tight_layout()
    return fig


def plot_time_since_prev(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Win rate by time since the player's previous game. Tests back-to-back
    queue tilt vs fresh session hypotheses."""
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to plot")
    g = _bin_winrate(d.dropna(subset=["gap_bucket"]), "gap_bucket")

    fig, ax = plt.subplots(figsize=(11, 4.6))
    ax.bar(range(len(g)), g["winrate"], color=PALETTE["accent_teal"], width=0.7)
    ax.set_xticks(range(len(g)))
    ax.set_xticklabels(g["gap_bucket"])
    ax.set_xlabel("Time since previous game")
    ax.set_ylabel("Win rate")
    ax.set_title(_title("Win rate vs gap since previous game", player))
    ax.set_ylim(0, 1.1)
    _baseline(ax)
    _annotate_bars(ax, range(len(g)), g["winrate"], g["games"])
    _polish_ax(ax)
    fig.tight_layout()
    return fig


# --- 6. Overview -----------------------------------------------------------


def plot_cumulative_winrate(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Cumulative win rate over time plus the rolling-20 view — hot/cold
    streaks across the dataset at a glance."""
    d = _filter_player(df, player).sort_values("game_start").reset_index(drop=True)
    if d.empty:
        return _empty_figure("No games to plot")

    d["cumulative_wr"] = d["win"].expanding().mean()
    d["rolling_20_wr"] = d["win"].rolling(window=20, min_periods=5).mean()

    fig, ax = plt.subplots(figsize=(13, 4.6))
    ax.fill_between(
        d["game_start"],
        d["rolling_20_wr"],
        0.5,
        where=d["rolling_20_wr"] >= 0.5,
        interpolate=True,
        color=PALETTE["win"],
        alpha=0.18,
    )
    ax.fill_between(
        d["game_start"],
        d["rolling_20_wr"],
        0.5,
        where=d["rolling_20_wr"] < 0.5,
        interpolate=True,
        color=PALETTE["loss"],
        alpha=0.18,
    )
    ax.plot(
        d["game_start"],
        d["rolling_20_wr"],
        color=PALETTE["primary"],
        linewidth=2.0,
        label="Rolling 20",
    )
    ax.plot(
        d["game_start"],
        d["cumulative_wr"],
        color=PALETTE["text"],
        linewidth=1.5,
        alpha=0.7,
        label="Cumulative",
    )
    _baseline(ax)
    ax.set_ylim(0, 1.05)
    ax.set_xlabel("Date")
    ax.set_ylabel("Win rate")
    ax.set_title(_title("Win rate over time", player))
    ax.legend(loc="lower right")
    _polish_ax(ax)
    fig.tight_layout()
    return fig


def plot_player_progression(
    df: pd.DataFrame, player: str | None = None, window: int = 30, min_games: int = 50
) -> plt.Figure:
    """Lifetime trend — rolling win rate vs **percent of career**.

    Game-count normalisation: x-axis runs 0-100% of each player's tracked
    history, so a 1500-game grinder and a 200-game player can be compared
    on the same axis. Aggregate panel labels each line with their linear
    slope in pp / 100% of career so "improving" vs "declining" reads off
    the legend.

    Single-player view: that person's curve, a linear fit, total game
    count, and the trend direction in pp / 100 games.
    """
    fig, ax = plt.subplots(figsize=(13, 5.4))

    def _series(sub: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        sub = sub.sort_values("game_start").reset_index(drop=True)
        # Map 1..N to a 0-100 percent axis so series of different lengths
        # share the x-axis.
        n_games = len(sub)
        pct = (
            pd.Series(np.linspace(0, 100, n_games), index=sub.index)
            if n_games > 1
            else pd.Series([50.0], index=sub.index)
        )
        roll = sub["win"].rolling(window=window, min_periods=max(5, window // 3)).mean()
        return pct, roll

    def _slope(pct: pd.Series, roll: pd.Series) -> float | None:
        mask = roll.notna()
        if mask.sum() < 5:
            return None
        # Slope in win-rate per 1% of career.
        return float(np.polyfit(pct[mask], roll[mask], 1)[0])

    if player is None:
        people = df.groupby("person").size()
        people = people[people >= min_games].sort_values(ascending=False).index.tolist()
        if not people:
            return _empty_figure(f"No players with ≥{min_games} games")
        for idx, name in enumerate(people):
            sub = df[df["person"] == name]
            pct, roll = _series(sub)
            slope = _slope(pct, roll)
            # Total-career delta — slope per %  times 100% = pp across career.
            if slope is None:
                label = f"{name}  (n={len(sub)})"
            else:
                career_pp = slope * 100  # WR points across the full career
                label = f"{name}  ({career_pp * 100:+.1f}pp/career, n={len(sub)})"
            ax.plot(
                pct,
                roll,
                label=label,
                alpha=0.85,
                color=SERIES_CYCLE[idx % len(SERIES_CYCLE)],
                linewidth=1.8,
            )
        ax.legend(loc="lower right", fontsize=8.5, ncol=2)
    else:
        sub = df[df["person"] == player]
        if sub.empty:
            return _empty_figure(f"No games for {player}")
        pct, roll = _series(sub)
        ax.plot(pct, roll, label=f"Rolling-{window} WR", color=PALETTE["primary"], linewidth=2.4)
        slope = _slope(pct, roll)
        if slope is not None:
            mask = roll.notna()
            fit = np.poly1d(np.polyfit(pct[mask], roll[mask], 1))
            ax.plot(
                pct,
                fit(pct),
                color=PALETTE["text"],
                linestyle="--",
                linewidth=1.2,
                label="Linear fit",
            )
            direction = "improving" if slope > 0 else "declining"
            box_colour = PALETTE["win"] if slope > 0 else PALETTE["loss"]
            career_pp = slope * 100
            per_100_games = slope / len(sub) * 100 * 100  # pp per 100 absolute games
            ax.text(
                0.02,
                0.96,
                f"Trend: {direction} by {abs(career_pp) * 100:.1f} pp across {len(sub):,} tracked games"
                f"\n≈ {abs(per_100_games):.2f} pp per 100 games",
                transform=ax.transAxes,
                fontsize=10,
                va="top",
                color=PALETTE["text"],
                bbox={
                    "facecolor": "white",
                    "alpha": 0.95,
                    "edgecolor": box_colour,
                    "linewidth": 1.4,
                    "pad": 6,
                },
            )
        ax.legend(loc="lower right")

    _baseline(ax)
    ax.set_ylim(0, 1.05)
    ax.set_xlim(0, 100)
    ax.set_xlabel("Percent of career (oldest → newest)")
    ax.set_ylabel(f"Rolling win rate (window={window})")
    ax.set_title(_title("Lifetime progression — getting better or worse?", player))
    _polish_ax(ax)
    fig.tight_layout()
    return fig


def compute_duos(df: pd.DataFrame, min_games: int = 5) -> pd.DataFrame:
    """All ordered same-team duo pairs across the dataset, with games + WR.

    "Duo" = two distinct ``person`` values sharing the same ``match_id``
    AND the same ``win`` (same team — the assumed-duo signal in this
    private friend-group context). The output columns ``a`` and ``b`` are
    always sorted alphabetically so each pair appears once.
    """
    m = df[["match_id", "win", "person"]].drop_duplicates()
    pairs = m.merge(m, on=["match_id", "win"])
    pairs = pairs[pairs["person_x"] < pairs["person_y"]]
    if pairs.empty:
        return pd.DataFrame(columns=["a", "b", "games", "wins", "winrate"])
    agg = (
        pairs.groupby(["person_x", "person_y"])
        .agg(games=("win", "size"), wins=("win", "sum"))
        .reset_index()
        .rename(columns={"person_x": "a", "person_y": "b"})
    )
    agg["winrate"] = agg["wins"] / agg["games"]
    agg = agg[agg["games"] >= min_games]
    return agg.sort_values("games", ascending=False).reset_index(drop=True)


def plot_duo_winrate(
    df: pd.DataFrame, player: str | None = None, min_games: int = 10, top: int = 12
) -> plt.Figure:
    """Same-team duo analysis.

    Aggregate view: top ``top`` most-played duos, bars coloured by win rate.

    Per-player view: each partner the focal player has played with at
    least ``min_games`` times — bar = winrate together, annotated with
    sample count. A dotted line shows the player's overall solo win
    rate for reference.
    """
    duos = compute_duos(df, min_games=min_games)
    if duos.empty:
        return _empty_figure(f"No same-team duos with ≥{min_games} games")

    if player is None:
        d = duos.head(top).iloc[::-1]  # reverse so highest-volume sits at top of barh
        labels = d["a"] + "  +  " + d["b"]
        fig_h = max(4.5, len(d) * 0.4)
        fig, ax = plt.subplots(figsize=(13, fig_h))
        colours = [PALETTE["win"] if wr >= 0.5 else PALETTE["loss"] for wr in d["winrate"]]
        ax.barh(range(len(d)), d["games"], color=colours, height=0.7)
        ax.set_yticks(range(len(d)))
        ax.set_yticklabels(labels)
        ax.set_xlabel("Games played together (same team)")
        ax.set_title(_title(f"Top duos (≥{min_games} same-team games together)", player))
        _polish_ax(ax)
        for yi, (g, wr) in enumerate(zip(d["games"], d["winrate"], strict=False)):
            ax.annotate(
                f"{int(g)} games · {wr:.0%} WR",
                xy=(g, yi),
                xytext=(6, 0),
                textcoords="offset points",
                va="center",
                fontsize=9,
                color=PALETTE["text"],
            )
        fig.tight_layout()
        return fig

    # Per-player view — partners only.
    partner_rows = duos[(duos["a"] == player) | (duos["b"] == player)].copy()
    if partner_rows.empty:
        return _empty_figure(f"No same-team duos for {player} with ≥{min_games} games")
    partner_rows["partner"] = partner_rows.apply(
        lambda r: r["b"] if r["a"] == player else r["a"], axis=1
    )
    partner_rows = partner_rows.sort_values("games", ascending=False).head(top)
    partner_rows = partner_rows.sort_values("winrate", ascending=True)

    solo_wr = df[df["person"] == player]["win"].mean()

    fig_h = max(4.2, len(partner_rows) * 0.45)
    fig, ax = plt.subplots(figsize=(13, fig_h))
    colours = [
        PALETTE["win"] if wr >= solo_wr else PALETTE["loss"] for wr in partner_rows["winrate"]
    ]
    ax.barh(range(len(partner_rows)), partner_rows["winrate"], color=colours, height=0.7)
    ax.axvline(
        solo_wr,
        color=PALETTE["muted"],
        linestyle="--",
        linewidth=1.0,
        label=f"{player}'s overall WR ({solo_wr:.0%})",
    )
    ax.set_yticks(range(len(partner_rows)))
    ax.set_yticklabels(partner_rows["partner"])
    ax.set_xlabel("Win rate when on the same team")
    ax.set_xlim(0, 1)
    ax.set_title(_title(f"Duo win rate by partner (≥{min_games} games each)", player))
    ax.legend(loc="lower right")
    _polish_ax(ax)
    for yi, (wr, n) in enumerate(zip(partner_rows["winrate"], partner_rows["games"], strict=False)):
        ax.annotate(
            f"{wr:.0%} · n={int(n)}",
            xy=(wr, yi),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            fontsize=9,
            color=PALETTE["text"],
        )
    fig.tight_layout()
    return fig


def plot_activity_over_time(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Calendar-time view — games per month + the monthly win rate.

    Counters the apples-vs-oranges issue of the progression chart by
    plotting absolute date on the x-axis. For the aggregate view this
    shows when the friend group was actually active and whether month-
    over-month win rate has any drift. For one player it's their
    individual session pattern.
    """
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to plot")

    d = d.copy()
    d["month"] = d["game_start"].dt.to_period("M").dt.to_timestamp()
    by_month = d.groupby("month").agg(games=("win", "size"), winrate=("win", "mean")).reset_index()

    fig, ax_vol = plt.subplots(figsize=(13, 4.8))
    ax_wr = ax_vol.twinx()

    ax_vol.bar(
        by_month["month"],
        by_month["games"],
        width=24,  # days, since monthly
        color=PALETTE["neutral"],
        label="Games / month",
    )
    ax_vol.set_xlabel("Month")
    ax_vol.set_ylabel("Games / month", color=PALETTE["muted"])
    ax_vol.set_axisbelow(True)

    enough = by_month["games"] >= 5  # don't draw winrate on near-empty months
    ax_wr.plot(
        by_month.loc[enough, "month"],
        by_month.loc[enough, "winrate"],
        color=PALETTE["primary"],
        marker="o",
        markersize=5,
        linewidth=2.2,
        label="Win rate",
    )
    _baseline(ax_wr)
    ax_wr.set_ylim(0, 1.05)
    ax_wr.set_ylabel("Win rate", color=PALETTE["primary"])
    ax_wr.tick_params(axis="y", colors=PALETTE["primary"])
    ax_wr.grid(False)
    ax_wr.spines["right"].set_visible(False)
    ax_wr.spines["top"].set_visible(False)

    fig.suptitle(_title("Activity over time — games / month + win rate", player))
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def summary_table(df: pd.DataFrame) -> pd.DataFrame:
    """Compact per-person summary: games, win rate, mean KDA, mean duration,
    favourite champion."""
    rows = []
    for person, sub in df.groupby("person"):
        fav = sub["champion"].value_counts().idxmax()
        rows.append(
            {
                "person": person,
                "games": len(sub),
                "winrate": sub["win"].mean(),
                "avg_kda": sub["kda"].mean(),
                "avg_duration_min": sub["duration_min"].mean(),
                "favourite_champ": fav,
                "fav_champ_games": int((sub["champion"] == fav).sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("games", ascending=False).reset_index(drop=True)


# --- registry --------------------------------------------------------------

#: All aggregate plot functions, in the order the runner script + notebook
#: should display them. Each takes (df, player=None) -> Figure.
ALL_PLOTS = [
    ("01_activity_over_time", plot_activity_over_time),
    ("02_cumulative_winrate", plot_cumulative_winrate),
    ("03_player_progression", plot_player_progression),
    ("04_kda_vs_outcome", plot_kda_vs_outcome),
    ("05_duration_vs_outcome", plot_duration_vs_outcome),
    ("06_champion_winrate", plot_champion_winrate),
    ("07_champion_learning_curve", plot_champion_learning_curve),
    ("08_hour_of_day", plot_hour_of_day),
    ("09_day_of_week", plot_day_of_week),
    ("10_hour_dow_heatmap", plot_hour_dow_heatmap),
    ("11_streak_recovery", plot_streak_recovery),
    ("12_time_since_prev", plot_time_since_prev),
    ("13_duo_winrate", plot_duo_winrate),
]
