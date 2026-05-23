"""Match-stats EDA helpers.

Pure functions for loading and visualising the bot's match_stats table.
Imported by both the exploration notebook (match_analysis.ipynb) and the
batch runner (run_analysis.py). No matplotlib state is kept here — every
plot function builds and returns a fresh Figure.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

DEFAULT_DB = Path(__file__).resolve().parent.parent / "Bot" / "db" / "database.sqlite"

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


def load_matches(db_path: Path = DEFAULT_DB) -> pd.DataFrame:
    """Load match_stats joined with league_players and derive feature columns.

    Returns a DataFrame where each row is one (player, match). Note that a
    game where two tracked players were on the same team appears twice —
    that is intentional for per-player analyses; pass through
    drop_duplicates(['match_id']) for an aggregate game-count view.
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
                lp.league_username AS player
            FROM match_stats ms
            JOIN league_players lp USING (puuid)
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

    # Per-player time-series features: time since previous game, current
    # loss-streak coming INTO this game, nth game on the champion.
    df = df.sort_values(["player", "game_start"]).reset_index(drop=True)

    # Time since previous game (this player's previous match)
    prev_end = df.groupby("player")["game_start"].shift(1) + pd.to_timedelta(
        df.groupby("player")["duration_sec"].shift(1).fillna(0), unit="s"
    )
    df["gap_since_prev_min"] = (df["game_start"] - prev_end).dt.total_seconds() / 60.0
    df["gap_bucket"] = pd.cut(
        df["gap_since_prev_min"], bins=GAP_BINS_MIN, labels=GAP_LABELS, right=False
    )

    # Loss streak ENTERING this game: how many consecutive losses immediately
    # before this row, per player. Resets on a win.
    df["loss_streak_in"] = df.groupby("player")["win"].transform(_loss_streak_entering)

    # Nth game on this champion (for the learning-curve view).
    df["nth_on_champ"] = df.groupby(["player", "champion"]).cumcount() + 1

    return df


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


def _annotate_bars(ax, x, heights, counts, fmt="{:.0%}"):
    """Print value + sample count above each bar."""
    for xi, h, n in zip(x, heights, counts, strict=False):
        if pd.isna(h):
            continue
        ax.text(
            xi,
            h + 0.01,
            f"{fmt.format(h)}\n(n={int(n)})",
            ha="center",
            va="bottom",
            fontsize=8,
        )


def _title(base: str, player: str | None) -> str:
    return base if player is None else f"{base} — {player}"


def _filter_player(df: pd.DataFrame, player: str | None) -> pd.DataFrame:
    return df if player is None else df[df["player"] == player]


# --- 1. KDA vs outcome ------------------------------------------------------


def plot_kda_vs_outcome(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Box-plot of KDA, split by win/loss. Clipped at 99th percentile so a
    few stomp-games don't squish the box.
    """
    d = _filter_player(df, player)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))

    cap = d["kda"].quantile(0.99)
    wins = d.loc[d["win"] == 1, "kda"].clip(upper=cap)
    losses = d.loc[d["win"] == 0, "kda"].clip(upper=cap)
    axes[0].boxplot([wins, losses], labels=["Win", "Loss"], vert=True)
    axes[0].set_ylabel("KDA  (K+A) / max(D,1)")
    axes[0].set_title(_title("KDA distribution by outcome", player))
    axes[0].grid(axis="y", alpha=0.3)

    # Winrate binned by KDA — does carrying actually correlate with the W?
    d2 = d.copy()
    d2["kda_bucket"] = pd.cut(
        d2["kda"],
        bins=[0, 1, 2, 3, 4, 5, 10, 999],
        labels=["<1", "1-2", "2-3", "3-4", "4-5", "5-10", "10+"],
    )
    g = _bin_winrate(d2, "kda_bucket")
    axes[1].bar(range(len(g)), g["winrate"], color="#4c8bf5")
    axes[1].set_xticks(range(len(g)))
    axes[1].set_xticklabels(g["kda_bucket"])
    axes[1].set_ylim(0, 1.05)
    axes[1].set_ylabel("Win rate")
    axes[1].set_xlabel("KDA bucket")
    axes[1].set_title(_title("Win rate by KDA", player))
    axes[1].grid(axis="y", alpha=0.3)
    _annotate_bars(axes[1], range(len(g)), g["winrate"], g["games"])

    fig.tight_layout()
    return fig


# --- 2. Game duration vs outcome -------------------------------------------


def plot_duration_vs_outcome(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Win rate per game-length bucket. Short stomps on either side, longer
    games are the slugfests.
    """
    d = _filter_player(df, player)
    g = _bin_winrate(d, "duration_bucket")

    fig, axes = plt.subplots(1, 2, figsize=(12, 4))

    axes[0].bar(range(len(g)), g["winrate"], color="#3aa3a3")
    axes[0].set_xticks(range(len(g)))
    axes[0].set_xticklabels(g["duration_bucket"])
    axes[0].set_ylim(0, 1.05)
    axes[0].set_xlabel("Game duration (min)")
    axes[0].set_ylabel("Win rate")
    axes[0].set_title(_title("Win rate by game duration", player))
    axes[0].grid(axis="y", alpha=0.3)
    _annotate_bars(axes[0], range(len(g)), g["winrate"], g["games"])

    # Volume of games per bucket, coloured by win/loss split.
    pivot = (
        d.dropna(subset=["duration_bucket"])
        .groupby(["duration_bucket", "win"], observed=True)
        .size()
        .unstack(fill_value=0)
    )
    pivot = pivot.reindex(DURATION_LABELS).fillna(0)
    x = np.arange(len(pivot))
    axes[1].bar(x, pivot.get(0, 0), color="#d9534f", label="Loss")
    axes[1].bar(x, pivot.get(1, 0), bottom=pivot.get(0, 0), color="#5cb85c", label="Win")
    axes[1].set_xticks(x)
    axes[1].set_xticklabels(pivot.index)
    axes[1].set_xlabel("Game duration (min)")
    axes[1].set_ylabel("Games")
    axes[1].set_title(_title("Game count by duration", player))
    axes[1].legend()
    axes[1].grid(axis="y", alpha=0.3)

    fig.tight_layout()
    return fig


# --- 3. Champion analysis --------------------------------------------------


def plot_champion_winrate(
    df: pd.DataFrame, player: str | None = None, min_games: int = 10, top: int = 20
) -> plt.Figure:
    """Per-champion winrate, sorted, with sample-count bars alongside."""
    d = _filter_player(df, player)
    g = d.groupby("champion")["win"].agg(["count", "mean"])
    g = g[g["count"] >= min_games].sort_values("mean", ascending=True).tail(top)

    fig, axes = plt.subplots(1, 2, figsize=(13, max(4, len(g) * 0.3)))
    y = np.arange(len(g))

    bars = axes[0].barh(
        y, g["mean"], color=["#5cb85c" if v >= 0.5 else "#d9534f" for v in g["mean"]]
    )
    axes[0].axvline(0.5, color="black", linewidth=0.7, linestyle=":")
    axes[0].set_yticks(y)
    axes[0].set_yticklabels(g.index)
    axes[0].set_xlim(0, 1)
    axes[0].set_xlabel("Win rate")
    axes[0].set_title(_title(f"Champion win rate (≥{min_games} games)", player))
    axes[0].grid(axis="x", alpha=0.3)
    for bar, n in zip(bars, g["count"], strict=False):
        axes[0].text(
            bar.get_width() + 0.01,
            bar.get_y() + bar.get_height() / 2,
            f"{bar.get_width():.0%} (n={int(n)})",
            va="center",
            fontsize=8,
        )

    axes[1].barh(y, g["count"], color="#4c8bf5")
    axes[1].set_yticks(y)
    axes[1].set_yticklabels(g.index)
    axes[1].set_xlabel("Games played")
    axes[1].set_title(_title("Champion volume", player))
    axes[1].grid(axis="x", alpha=0.3)

    fig.tight_layout()
    return fig


def plot_champion_learning_curve(
    df: pd.DataFrame, player: str | None = None, top: int = 5, window: int = 5
) -> plt.Figure:
    """Rolling win rate by nth game on a champion, for that player's most-
    played champions. A rising curve = learning the champ pays off; flat
    or falling = no improvement.
    """
    d = _filter_player(df, player)
    top_champs = d["champion"].value_counts().head(top).index.tolist()
    d = d[d["champion"].isin(top_champs)].sort_values(["champion", "nth_on_champ"])

    fig, ax = plt.subplots(figsize=(10, 5))
    for champ in top_champs:
        sub = d[d["champion"] == champ]
        if len(sub) < window:
            continue
        roll = sub["win"].rolling(window=window, min_periods=1).mean()
        ax.plot(sub["nth_on_champ"], roll, label=f"{champ} (n={len(sub)})")

    ax.axhline(0.5, color="black", linewidth=0.7, linestyle=":")
    ax.set_xlabel("Nth game on champion")
    ax.set_ylabel(f"Rolling win rate (window={window})")
    ax.set_title(_title(f"Learning curves — top {top} champions", player))
    ax.set_ylim(0, 1.05)
    ax.legend(loc="lower right", fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig


# --- 4. Temporal patterns ---------------------------------------------------


def plot_hour_of_day(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Win rate + game volume by hour of day. Tilt-hour detector."""
    d = _filter_player(df, player)
    by_hour = d.groupby("hour")["win"].agg(["count", "mean"]).reindex(range(24), fill_value=0)

    fig, ax1 = plt.subplots(figsize=(12, 4.5))
    ax2 = ax1.twinx()

    ax1.bar(by_hour.index, by_hour["count"], color="#cfd8dc", label="Games")
    ax1.set_xlabel("Hour of day (local)")
    ax1.set_ylabel("Games played")
    ax1.set_xticks(range(0, 24))
    ax1.grid(axis="y", alpha=0.3)

    # Only draw the winrate line where we actually have samples.
    mask = by_hour["count"] > 0
    ax2.plot(
        by_hour.index[mask], by_hour["mean"][mask], color="#d9534f", marker="o", label="Win rate"
    )
    ax2.axhline(0.5, color="black", linewidth=0.7, linestyle=":")
    ax2.set_ylim(0, 1.05)
    ax2.set_ylabel("Win rate")

    fig.suptitle(_title("Hour of day — volume + win rate", player))
    fig.tight_layout()
    return fig


def plot_day_of_week(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Same shape as hour-of-day but on day axis."""
    d = _filter_player(df, player)
    by_dow = d.groupby("dow")["win"].agg(["count", "mean"]).reindex(range(7), fill_value=0)

    fig, ax1 = plt.subplots(figsize=(9, 4.5))
    ax2 = ax1.twinx()

    ax1.bar(by_dow.index, by_dow["count"], color="#cfd8dc")
    ax1.set_xticks(range(7))
    ax1.set_xticklabels(DOW_LABELS)
    ax1.set_xlabel("Day of week")
    ax1.set_ylabel("Games played")
    ax1.grid(axis="y", alpha=0.3)

    mask = by_dow["count"] > 0
    ax2.plot(by_dow.index[mask], by_dow["mean"][mask], color="#d9534f", marker="o")
    ax2.axhline(0.5, color="black", linewidth=0.7, linestyle=":")
    ax2.set_ylim(0, 1.05)
    ax2.set_ylabel("Win rate")

    fig.suptitle(_title("Day of week — volume + win rate", player))
    fig.tight_layout()
    return fig


def plot_hour_dow_heatmap(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Win-rate heatmap on the hour × day grid. Need both dimensions to spot
    things like "Friday-evening tilt only" that one-dimensional charts miss.
    Cells with <3 samples are blanked.
    """
    d = _filter_player(df, player)
    counts = d.pivot_table(index="dow", columns="hour", values="win", aggfunc="size").reindex(
        index=range(7), columns=range(24)
    )
    winrate = d.pivot_table(index="dow", columns="hour", values="win", aggfunc="mean").reindex(
        index=range(7), columns=range(24)
    )
    winrate = winrate.where(counts >= 3)  # mask sparse cells

    fig, ax = plt.subplots(figsize=(13, 4.5))
    im = ax.imshow(winrate.values, aspect="auto", cmap="RdYlGn", vmin=0.3, vmax=0.7, origin="lower")
    ax.set_yticks(range(7))
    ax.set_yticklabels(DOW_LABELS)
    ax.set_xticks(range(0, 24))
    ax.set_xlabel("Hour of day")
    ax.set_ylabel("Day of week")
    ax.set_title(_title("Win rate heatmap (cells with <3 games blanked)", player))
    fig.colorbar(im, ax=ax, label="Win rate")
    fig.tight_layout()
    return fig


# --- 5. Recent form & momentum ---------------------------------------------


def plot_streak_recovery(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Win rate of the NEXT game, bucketed by loss streak going into it.

    If "tilt" is real, longer entering loss streaks should produce lower
    win rates on the next game. If the chart is flat, there's no tilt
    signal — each game is independent.
    """
    d = _filter_player(df, player)
    d = d.copy()
    d["streak_bucket"] = pd.cut(
        d["loss_streak_in"],
        bins=[-1, 0, 1, 2, 3, 5, 99],
        labels=["0 (post-W)", "1", "2", "3", "4-5", "6+"],
    )
    g = _bin_winrate(d, "streak_bucket")

    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.bar(range(len(g)), g["winrate"], color="#f0ad4e")
    ax.set_xticks(range(len(g)))
    ax.set_xticklabels(g["streak_bucket"])
    ax.set_xlabel("Loss streak entering this game")
    ax.set_ylabel("Win rate of this game")
    ax.set_title(_title("Tilt check — win rate vs entering loss streak", player))
    ax.set_ylim(0, 1.05)
    ax.axhline(0.5, color="black", linewidth=0.7, linestyle=":")
    ax.grid(axis="y", alpha=0.3)
    _annotate_bars(ax, range(len(g)), g["winrate"], g["games"])
    fig.tight_layout()
    return fig


def plot_time_since_prev(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Win rate by time since the player's previous game. Tests "back-to-back
    queue tilt" vs "fresh session" hypotheses.
    """
    d = _filter_player(df, player)
    g = _bin_winrate(d.dropna(subset=["gap_bucket"]), "gap_bucket")

    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.bar(range(len(g)), g["winrate"], color="#5bc0de")
    ax.set_xticks(range(len(g)))
    ax.set_xticklabels(g["gap_bucket"])
    ax.set_xlabel("Time since previous game")
    ax.set_ylabel("Win rate")
    ax.set_title(_title("Win rate vs gap since previous game", player))
    ax.set_ylim(0, 1.05)
    ax.axhline(0.5, color="black", linewidth=0.7, linestyle=":")
    ax.grid(axis="y", alpha=0.3)
    _annotate_bars(ax, range(len(g)), g["winrate"], g["games"])
    fig.tight_layout()
    return fig


# --- 6. Overview -----------------------------------------------------------


def plot_cumulative_winrate(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Cumulative win rate over time (and rolling-20 win rate) — quick way
    to see hot/cold streaks across the dataset.
    """
    d = _filter_player(df, player).sort_values("game_start").reset_index(drop=True)
    if d.empty:
        fig, ax = plt.subplots()
        ax.text(0.5, 0.5, "No data", ha="center")
        return fig

    d["cumulative_wr"] = d["win"].expanding().mean()
    d["rolling_20_wr"] = d["win"].rolling(window=20, min_periods=5).mean()

    fig, ax = plt.subplots(figsize=(12, 4.5))
    ax.plot(d["game_start"], d["cumulative_wr"], label="Cumulative", color="black", alpha=0.8)
    ax.plot(d["game_start"], d["rolling_20_wr"], label="Rolling 20", color="#d9534f", alpha=0.7)
    ax.axhline(0.5, color="black", linewidth=0.7, linestyle=":")
    ax.set_ylim(0, 1.05)
    ax.set_xlabel("Date")
    ax.set_ylabel("Win rate")
    ax.set_title(_title("Win rate over time", player))
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig


def summary_table(df: pd.DataFrame) -> pd.DataFrame:
    """Compact per-player summary: games, win rate, mean KDA, mean duration,
    favourite champion."""
    rows = []
    for player, sub in df.groupby("player"):
        fav = sub["champion"].value_counts().idxmax()
        rows.append(
            {
                "player": player,
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
    ("01_cumulative_winrate", plot_cumulative_winrate),
    ("02_kda_vs_outcome", plot_kda_vs_outcome),
    ("03_duration_vs_outcome", plot_duration_vs_outcome),
    ("04_champion_winrate", plot_champion_winrate),
    ("05_champion_learning_curve", plot_champion_learning_curve),
    ("06_hour_of_day", plot_hour_of_day),
    ("07_day_of_week", plot_day_of_week),
    ("08_hour_dow_heatmap", plot_hour_dow_heatmap),
    ("09_streak_recovery", plot_streak_recovery),
    ("10_time_since_prev", plot_time_since_prev),
]
