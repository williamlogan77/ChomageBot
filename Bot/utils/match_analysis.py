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

import math
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

# A "session" is a contiguous run of games for one person where each
# consecutive game starts within this many minutes of the previous one.
# 60 minutes covers queue-up + champ select + breaks between back-to-back
# games but excludes "popped on after dinner" type returns.
SESSION_GAP_MIN = 60

# Session-length buckets for the "do long grinds hurt?" view.
# Left-closed: [1,2)=1 game, [2,4)=2-3, [4,6)=4-5, [6,10)=6-9, [10,16)=10-15, [16,inf)=16+.
SESSION_LEN_BINS = [1, 2, 4, 6, 10, 16, 9999]
SESSION_LEN_LABELS = ["1 game", "2-3", "4-5", "6-9", "10-15", "16+"]


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


def _baseline(ax, y: float = 0.5, label: str | None = None) -> None:
    """A subtle horizontal 'break-even' reference at y. With ``label``
    the line is added to the axes legend so readers know what it means."""
    ax.axhline(
        y,
        color=PALETTE["muted"],
        linewidth=0.8,
        linestyle=(0, (4, 4)),
        alpha=0.55,
        label=label,
    )


def _subtitle(ax, text: str) -> None:
    """One-line italic caption immediately under the (already-set) title.

    Tells the reader what to look at — every plot should call this once
    so an audience that's never seen the chart can read it cold.
    """
    # Push the bold title up a touch so the caption has room.
    title = ax.get_title()
    if title:
        ax.set_title(title, pad=22)
    ax.text(
        0.0,
        1.01,
        text,
        transform=ax.transAxes,
        ha="left",
        va="bottom",
        fontsize=10,
        color=PALETTE["muted"],
        style="italic",
    )


def wilson_ci(wins: int, games: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% confidence interval for a binomial proportion.

    More accurate than the normal approximation for small n — and the
    "70% on 3 games" trap on champion winrate charts is exactly that
    small-n problem.
    """
    if games <= 0:
        return (0.0, 0.0)
    p = wins / games
    denom = 1 + z * z / games
    centre = p + z * z / (2 * games)
    margin = z * np.sqrt(p * (1 - p) / games + z * z / (4 * games * games))
    return (max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom))


def bayesian_shrunk_wr(
    wins: int, games: int, baseline_wr: float, prior_strength: float = 10.0
) -> float:
    """Beta-prior shrinkage of a small-sample WR back toward a baseline.

    Equivalent to a Beta(α, β) prior centred on ``baseline_wr`` with
    ``α + β = prior_strength`` pseudo-observations. Posterior mean is
    a weighted average of the observed WR and the baseline; when
    ``games == prior_strength`` the two weights are equal. Heavy-sample
    champions (games >> prior_strength) barely move.
    """
    if games <= 0:
        return baseline_wr
    obs_wr = wins / games
    return (games / (games + prior_strength)) * obs_wr + (
        prior_strength / (games + prior_strength)
    ) * baseline_wr


def chi2_pvalue(chi2: float, df: int) -> float:
    """Wilson-Hilferty cube-root chi² survival approximation, no scipy.

    Accurate to <0.01 of p once df ≥ 5 and chi² is in a reasonable range.
    Used for "is this bucketed-winrate pattern real or noise?" callouts.
    """
    import math

    if df <= 0 or chi2 <= 0:
        return 1.0
    z = ((chi2 / df) ** (1.0 / 3.0) - (1.0 - 2.0 / (9.0 * df))) / math.sqrt(2.0 / (9.0 * df))
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def logistic_fit(
    X: np.ndarray, y: np.ndarray, max_iter: int = 50, tol: float = 1e-6, l2: float = 0.5
) -> tuple[np.ndarray, np.ndarray, float]:
    """Logistic regression via IRLS (Iteratively Reweighted Least Squares).

    Newton-Raphson updates on the negative log-likelihood. A small L2
    ridge penalty (``l2``) regularises the design matrix when it's
    near-singular (lots of one-hot person dummies — common here) which
    keeps standard errors finite without distorting the headline
    coefficients.

    Args:
        X: (n, p) design matrix WITHOUT intercept column.
        y: (n,) outcomes in {0, 1}.
        l2: ridge penalty on coefficients except the intercept.

    Returns:
        beta:    (p+1,) coefficient vector, intercept first
        se:      (p+1,) Wald standard errors
        loglik:  scalar final log-likelihood
    """
    n, p = X.shape
    X1 = np.hstack([np.ones((n, 1)), X])
    beta = np.zeros(p + 1)
    # Ridge: don't penalise the intercept.
    R = l2 * np.eye(p + 1)
    R[0, 0] = 0.0

    for _ in range(max_iter):
        z = np.clip(X1 @ beta, -30.0, 30.0)
        mu = 1.0 / (1.0 + np.exp(-z))
        W = mu * (1.0 - mu) + 1e-9
        # Hessian H = X' diag(W) X + R, gradient g = X'(mu - y) + R beta
        H = X1.T @ (X1 * W[:, None]) + R
        g = X1.T @ (mu - y) + R @ beta
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            break
        new_beta = beta - step
        if np.max(np.abs(new_beta - beta)) < tol:
            beta = new_beta
            break
        beta = new_beta

    # Final Hessian inverse for Wald SEs.
    z_final = np.clip(X1 @ beta, -30.0, 30.0)
    mu = 1.0 / (1.0 + np.exp(-z_final))
    W = mu * (1.0 - mu) + 1e-9
    H = X1.T @ (X1 * W[:, None]) + R
    try:
        cov = np.linalg.inv(H)
        se = np.sqrt(np.clip(np.diag(cov), 0, None))
    except np.linalg.LinAlgError:
        se = np.full(p + 1, np.nan)

    eps = 1e-9
    loglik = float(np.sum(y * np.log(mu + eps) + (1 - y) * np.log(1 - mu + eps)))
    return beta, se, loglik


def wald_pvalue(coef: float, se: float) -> float:
    """Two-tailed Wald test p-value from coefficient + standard error.

    Uses the erfc/sqrt(2) normal-tail approximation — no scipy needed.
    """
    import math

    if se is None or not np.isfinite(se) or se <= 0:
        return 1.0
    z = abs(coef) / se
    return float(math.erfc(z / math.sqrt(2.0)))


def chi2_homogeneity(counts: np.ndarray, totals: np.ndarray) -> tuple[float, int, float]:
    """Chi-square test of "does P(win) differ across buckets?".

    Given each bucket's win count + total game count, computes the
    pooled overall WR, the expected wins under the null (no effect),
    and returns (chi² stat, degrees of freedom, p-value). Buckets with
    zero games are dropped.
    """
    counts = np.asarray(counts, dtype=float)
    totals = np.asarray(totals, dtype=float)
    mask = totals > 0
    counts = counts[mask]
    totals = totals[mask]
    if len(totals) < 2 or totals.sum() == 0:
        return (0.0, 0, 1.0)
    p_pool = counts.sum() / totals.sum()
    if p_pool <= 0 or p_pool >= 1:
        return (0.0, 0, 1.0)
    exp_win = totals * p_pool
    exp_loss = totals * (1.0 - p_pool)
    obs_loss = totals - counts
    # Standard 2-row contingency chi²; df = (rows-1)*(cols-1) = k-1 for two outcomes.
    with np.errstate(invalid="ignore", divide="ignore"):
        chi2 = float(
            np.sum((counts - exp_win) ** 2 / exp_win)
            + np.sum((obs_loss - exp_loss) ** 2 / exp_loss)
        )
    df = len(totals) - 1
    return (chi2, df, chi2_pvalue(chi2, df))


def _p_marker(p: float) -> str:
    """A short human-readable significance tag for a p-value."""
    if p < 0.001:
        return "p<0.001"
    if p < 0.01:
        return f"p={p:.3f}"
    if p < 0.05:
        return f"p={p:.3f}"
    return f"p={p:.2f}"


def _p_verdict(p: float) -> str:
    """Plain-English verdict to follow the p-value."""
    if p < 0.001:
        return "very likely real"
    if p < 0.01:
        return "likely real"
    if p < 0.05:
        return "probably real"
    if p < 0.1:
        return "suggestive"
    return "consistent with noise"


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

    # Session features: a "session" is a contiguous run of one person's
    # games where each consecutive game starts within SESSION_GAP_MIN of
    # the previous game ending. A long break starts a new session.
    new_session = df["gap_since_prev_min"].isna() | (df["gap_since_prev_min"] > SESSION_GAP_MIN)
    df["session_id"] = new_session.groupby(df["person"]).cumsum()
    df["session_game_idx"] = df.groupby(["person", "session_id"]).cumcount() + 1
    # Pre-compute session length so the same value lands on every row of
    # the session — used by the "WR by session length" panel later.
    session_len = df.groupby(["person", "session_id"])["session_game_idx"].transform("max")
    df["session_length"] = session_len
    return df


def load_rank_history(db_path: Path = DEFAULT_DB) -> pd.DataFrame:
    """Load league_history mapped to ``person`` keys + rank scores.

    league_history is keyed by either modern puuid OR the older encrypted
    summoner ID (league_players.leagueId). Both join paths matter — the
    UNION below picks the right Discord person regardless of which key
    the row was inserted under.

    Returns one row per (person, timestamp) with: tier, division, lp,
    a numeric ``rank_score`` (Iron IV = 0, Master = 7000+) for plotting,
    and the cumulative ``wins`` / ``losses`` for that timestamp.
    """
    from utils.rank_sorting_class import Ranker

    with sqlite3.connect(db_path) as con:
        df = pd.read_sql_query(
            """
            SELECT
                lh.timestamp,
                lh.lp,
                lh.division,
                lh.tier,
                lh.wins,
                lh.losses,
                COALESCE(
                    NULLIF(TRIM(u.nickname), ''),
                    NULLIF(u.discord_tag, ''),
                    lp.league_username
                ) AS person
            FROM league_history lh
            JOIN league_players lp
              ON lp.puuid = lh.puuid OR lp.leagueId = lh.puuid
            LEFT JOIN users u ON u.user_id = lp.discord_user_id
            WHERE lh.tier IS NOT NULL AND lh.division IS NOT NULL AND lh.lp IS NOT NULL
            ORDER BY lh.timestamp ASC
            """,
            con,
        )
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    def _score(tier: str, div: str, lp: int) -> float | None:
        try:
            return float(Ranker(tier, div, lp)._score)
        except (KeyError, ValueError, AttributeError):
            return None

    df["rank_score"] = [
        _score(t, d, p) for t, d, p in zip(df["tier"], df["division"], df["lp"], strict=False)
    ]
    df = df.dropna(subset=["rank_score"]).reset_index(drop=True)
    return df


# Pretty labels for the rank-score y-axis. One label per tier+division
# combo, positioned at the centre of that band on the Ranker._score scale.
_RANK_TICK_LABELS: list[tuple[float, str]] = [
    (0, "Iron IV"),
    (200, "Iron II"),
    (401, "Bronze IV"),
    (601, "Bronze II"),
    (802, "Silver IV"),
    (1002, "Silver II"),
    (1203, "Gold IV"),
    (1403, "Gold II"),
    (1604, "Plat IV"),
    (1804, "Plat II"),
    (2005, "Emerald IV"),
    (2205, "Emerald II"),
    (2406, "Diamond IV"),
    (2606, "Diamond II"),
    (2807, "Master"),
]


def compute_lp_events(db_path: Path = DEFAULT_DB) -> pd.DataFrame:
    """Per-game LP events derived from league_history diffs.

    For each (person, consecutive snapshot pair) where exactly one game
    fired between them (wins delta + losses delta == 1), we record:

      - outcome:    "win" or "loss"
      - delta_score: change in Ranker._score (signed)
      - timestamp:   when the new snapshot was recorded

    Using ``rank_score`` instead of raw LP keeps the math correct across
    tier crossings: a win at Gold I 95LP → Plat IV 5LP is a positive
    delta on the continuous scale even though raw LP went from 95 to 5.
    """
    ranks = load_rank_history(db_path)
    if ranks.empty:
        return pd.DataFrame(columns=["person", "timestamp", "outcome", "delta_score"])
    ranks = ranks.sort_values(["person", "timestamp"]).reset_index(drop=True)
    grp = ranks.groupby("person")
    ranks["prev_score"] = grp["rank_score"].shift(1)
    ranks["prev_wins"] = grp["wins"].shift(1)
    ranks["prev_losses"] = grp["losses"].shift(1)
    ranks["dw"] = ranks["wins"] - ranks["prev_wins"]
    ranks["dl"] = ranks["losses"] - ranks["prev_losses"]
    ranks["delta_score"] = ranks["rank_score"] - ranks["prev_score"]
    # Single-game events only. dw or dl can occasionally be NaN on the
    # first row per person; drop those.
    one_game = ranks.dropna(subset=["dw", "dl", "delta_score"])
    one_game = one_game[(one_game["dw"] + one_game["dl"]) == 1]
    one_game = one_game.copy()
    one_game["outcome"] = np.where(one_game["dw"] == 1, "win", "loss")
    return one_game[["person", "timestamp", "outcome", "delta_score"]].reset_index(drop=True)


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
    """Pooled (game-weighted) win rate + count per bucket. Skips empties.

    Used for per-person plots where there's only one person in ``df`` —
    pooled and macro give the same answer, and pooled is simpler.
    """
    g = (
        df.dropna(subset=[bucket_col])
        .groupby(bucket_col, observed=True)["win"]
        .agg(["count", "mean"])
        .rename(columns={"count": "games", "mean": "winrate"})
    )
    return g.reset_index()


def _bucket_winrate(df: pd.DataFrame, bucket_col: str, min_per_person: int = 5) -> pd.DataFrame:
    """Macro-averaged win rate per bucket — busy players don't dominate.

    When ``df`` spans >1 person we compute each person's WR per bucket
    (over their own games in that bucket), then take the mean across
    people who have ``≥ min_per_person`` games in that bucket. Langers's
    2000 games at hour 22 stop drowning out everyone else's hour-22
    signal. For a single-person df it returns pooled WR (same answer).

    Output columns: ``bucket_col, winrate, games, n_people, ci_lo, ci_hi``.
    ``games`` is the pooled count (used for chi² tests). ``ci_lo/hi``
    are ±1σ across people for the macro path (NaN otherwise) — the
    visible spread of who's winning where.
    """
    d = df.dropna(subset=[bucket_col])
    if d.empty:
        return pd.DataFrame(columns=[bucket_col, "winrate", "games", "n_people", "ci_lo", "ci_hi"])

    is_macro = d["person"].nunique() > 1
    if not is_macro:
        g = (
            d.groupby(bucket_col, observed=True)["win"]
            .agg(["count", "mean"])
            .rename(columns={"count": "games", "mean": "winrate"})
        )
        g["n_people"] = 1
        g["ci_lo"] = np.nan
        g["ci_hi"] = np.nan
        return g.reset_index()

    per_person = (
        d.groupby([bucket_col, "person"], observed=True)
        .agg(person_games=("win", "size"), person_wins=("win", "sum"))
        .reset_index()
    )
    per_person = per_person[per_person["person_games"] >= min_per_person]
    per_person["person_wr"] = per_person["person_wins"] / per_person["person_games"]
    agg = (
        per_person.groupby(bucket_col, observed=True)
        .agg(
            winrate=("person_wr", "mean"),
            std=("person_wr", "std"),
            n_people=("person", "nunique"),
            games=("person_games", "sum"),
        )
        .reset_index()
    )
    agg["ci_lo"] = (agg["winrate"] - agg["std"].fillna(0)).clip(lower=0)
    agg["ci_hi"] = (agg["winrate"] + agg["std"].fillna(0)).clip(upper=1)
    return agg.drop(columns=["std"])


def _macro_label(macro_count: int) -> str:
    """Subtitle suffix telling the reader the aggregation mode."""
    if macro_count <= 1:
        return "pooled across this player's games"
    return f"macro-averaged across {macro_count} players (each weighted equally)"


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
    _subtitle(
        axes[0],
        f"Win median {wins.median():.1f} vs loss median {losses.median():.1f} — the gap is the carry effect.",
    )
    _polish_ax(axes[0])

    d2 = d.copy()
    d2["kda_bucket"] = pd.cut(
        d2["kda"],
        bins=[0, 1, 2, 3, 4, 5, 10, 999],
        labels=["<1", "1-2", "2-3", "3-4", "4-5", "5-10", "10+"],
    )
    g = _bucket_winrate(d2, "kda_bucket")
    macro_n = int(g["n_people"].max()) if not g.empty else 1
    axes[1].bar(range(len(g)), g["winrate"], color=PALETTE["primary"], width=0.7)
    if macro_n > 1:
        axes[1].errorbar(
            range(len(g)),
            g["winrate"],
            yerr=[g["winrate"] - g["ci_lo"], g["ci_hi"] - g["winrate"]],
            fmt="none",
            ecolor=PALETTE["text"],
            capsize=3,
            linewidth=1.0,
            alpha=0.6,
        )
    axes[1].set_xticks(range(len(g)))
    axes[1].set_xticklabels(g["kda_bucket"])
    axes[1].set_ylim(0, 1.1)
    axes[1].set_ylabel("Win rate")
    axes[1].set_xlabel("KDA bucket")
    axes[1].set_title(_title("Win rate by KDA", player))
    _subtitle(
        axes[1],
        f"Higher buckets should win more. {_macro_label(macro_n)}; whiskers = spread across people.",
    )
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

    g = _bucket_winrate(d, "duration_bucket")
    macro_n = int(g["n_people"].max()) if not g.empty else 1
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.6))

    axes[0].bar(range(len(g)), g["winrate"], color=PALETTE["accent_teal"], width=0.7)
    if macro_n > 1:
        axes[0].errorbar(
            range(len(g)),
            g["winrate"],
            yerr=[g["winrate"] - g["ci_lo"], g["ci_hi"] - g["winrate"]],
            fmt="none",
            ecolor=PALETTE["text"],
            capsize=3,
            linewidth=1.0,
            alpha=0.6,
        )
    axes[0].set_xticks(range(len(g)))
    axes[0].set_xticklabels(g["duration_bucket"])
    axes[0].set_ylim(0, 1.1)
    axes[0].set_xlabel("Game duration (min)")
    axes[0].set_ylabel("Win rate")
    axes[0].set_title(_title("Win rate by game duration", player))
    _subtitle(
        axes[0],
        f"Stomps vs scaling. {_macro_label(macro_n)}; whiskers = spread across people.",
    )
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
    _subtitle(axes[1], "How many games at each length, split win (green) vs loss (red).")
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

    fig_h = max(4.2, max(len(winners), len(losers)) * 0.42)
    fig, axes = plt.subplots(1, 2, figsize=(13, fig_h))

    for ax, frame, colour, title in (
        (axes[0], winners, PALETTE["win"], "Winners — highest win rate"),
        (axes[1], losers, PALETTE["loss"], "Losers — lowest win rate"),
    ):
        y = np.arange(len(frame))
        # Wilson 95% CI for each champion's winrate — the whiskers tell
        # you when "70% on 3 games" is too thin to trust.
        cis = [
            wilson_ci(int(round(p * n)), int(n))
            for p, n in zip(frame["mean"], frame["count"], strict=False)
        ]
        lo = np.array([c[0] for c in cis])
        hi = np.array([c[1] for c in cis])
        means = frame["mean"].to_numpy()
        # Highlight bars whose CI doesn't overlap 50% — those are the
        # statistically reliable "real" winners/losers.
        reliable = (lo > 0.5) if colour == PALETTE["win"] else (hi < 0.5)
        bar_colours = [colour if r else PALETTE["neutral"] for r in reliable]
        ax.barh(y, means, color=bar_colours, height=0.7)
        ax.errorbar(
            means,
            y,
            xerr=[means - lo, hi - means],
            fmt="none",
            ecolor=PALETTE["text"],
            capsize=3,
            linewidth=1.0,
            alpha=0.7,
        )
        ax.axvline(0.5, color=PALETTE["muted"], linewidth=0.8, linestyle=(0, (4, 4)), alpha=0.55)
        ax.set_yticks(y)
        ax.set_yticklabels(frame.index, color=PALETTE["text"])
        ax.set_xlim(0, 1)
        ax.set_xlabel("Win rate")
        ax.set_title(_title(f"{title} (≥{min_games} games)", player))
        _subtitle(
            ax,
            "Whiskers = Wilson 95% CI; faded bars are too thin to call statistically.",
        )
        _polish_ax(ax)
        for yi, (wr, n) in enumerate(zip(means, frame["count"], strict=False)):
            ax.annotate(
                f"{wr:.0%}  n={int(n)}",
                xy=(min(wr + 0.02, 0.98), yi),
                xytext=(6, 0),
                textcoords="offset points",
                va="center",
                fontsize=9,
                color=PALETTE["text"],
            )

    fig.tight_layout()
    return fig


def plot_champion_picks(
    df: pd.DataFrame,
    player: str | None = None,
    min_games: int = 5,
    top: int = 14,
    prior_strength: float = 10.0,
) -> plt.Figure:
    """Per-player "should I play X more, or drop it?" — Bayesian-shrunk WR
    delta vs the player's personal baseline.

    For every champion played at least ``min_games`` times, the raw WR
    is pulled toward the player's overall WR via a Beta prior worth
    ``prior_strength`` games. A new 5-game champion at 100% WR gets
    shrunk roughly halfway back; a 200-game pocket pick barely moves.
    The chart shows the resulting WR delta (shrunk_wr − baseline_wr)
    in percentage points — positive means actually a better-than-you
    pick, negative means drop candidate.

    Solid bars = raw Wilson 95% CI excludes the baseline (statistically
    confident lift/drag). Faded grey = the shrunk delta is real but the
    raw evidence is still thin.
    """
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to analyse")

    if player is None:
        baseline_wr = float(d["win"].mean())
        baseline_label = f"group baseline ({baseline_wr:.0%})"
    else:
        baseline_wr = float(d["win"].mean())
        baseline_label = f"{player}'s baseline ({baseline_wr:.0%})"

    champ_stats = d.groupby("champion")["win"].agg(["count", "sum", "mean"])
    champ_stats = champ_stats[champ_stats["count"] >= min_games]
    if champ_stats.empty:
        return _empty_figure(f"No champions with ≥{min_games} games")

    champ_stats = champ_stats.rename(columns={"count": "n", "sum": "wins", "mean": "raw_wr"})
    champ_stats["shrunk_wr"] = [
        bayesian_shrunk_wr(int(w), int(n), baseline_wr, prior_strength)
        for w, n in zip(champ_stats["wins"], champ_stats["n"], strict=False)
    ]
    champ_stats["delta_pp"] = (champ_stats["shrunk_wr"] - baseline_wr) * 100
    cis = [
        wilson_ci(int(w), int(n))
        for w, n in zip(champ_stats["wins"], champ_stats["n"], strict=False)
    ]
    champ_stats["ci_lo"] = [c[0] for c in cis]
    champ_stats["ci_hi"] = [c[1] for c in cis]
    champ_stats["confident"] = (champ_stats["ci_lo"] > baseline_wr) | (
        champ_stats["ci_hi"] < baseline_wr
    )

    # Show the top "best-pick" champions AND the bottom "drop candidates"
    # to make the contrast obvious; same chart, sorted ascending so
    # worst is at the bottom and best at the top.
    by_delta = champ_stats.sort_values("delta_pp", ascending=False)
    picks = pd.concat([by_delta.head(top // 2), by_delta.tail(top - top // 2)])
    picks = picks.drop_duplicates().sort_values("delta_pp", ascending=True)

    fig_h = max(4.6, len(picks) * 0.42)
    fig, ax = plt.subplots(figsize=(13, fig_h))
    y = np.arange(len(picks))

    colours = []
    for _, r in picks.iterrows():
        if not r["confident"]:
            colours.append(PALETTE["neutral"])
        elif r["delta_pp"] > 0:
            colours.append(PALETTE["win"])
        else:
            colours.append(PALETTE["loss"])
    ax.barh(y, picks["delta_pp"], color=colours, height=0.7)
    ax.axvline(0, color=PALETTE["text"], linewidth=0.8)
    ax.set_yticks(y)
    ax.set_yticklabels(picks.index)
    ax.set_xlabel(f"Shrunk WR delta vs {baseline_label} (pp)")
    ax.set_title(
        _title(f"Champion picks — Bayesian-shrunk (prior = {int(prior_strength)} games)", player)
    )
    _subtitle(
        ax,
        "Solid bars = Wilson 95% CI excludes baseline (real signal). "
        "Faded grey = shrunk lift looks positive but raw sample still thin.",
    )
    _polish_ax(ax)

    # Wide solid bars (|delta_pp| > 5) get the label tucked inside in
    # white; narrow bars and faded-grey (low-confidence) bars stay
    # outside to remain readable. Threshold matches plot_feature_impact.
    for yi, (_, r) in enumerate(picks.iterrows()):
        label = (
            f"{r['delta_pp']:+.1f}pp  ·  raw {r['raw_wr']:.0%} → shrunk {r['shrunk_wr']:.0%}  "
            f"·  n={int(r['n'])}"
        )
        is_grey = not r["confident"]
        if abs(r["delta_pp"]) > 5 and not is_grey:
            if r["delta_pp"] >= 0:
                ax.annotate(
                    label,
                    xy=(r["delta_pp"], yi),
                    xytext=(-6, 0),
                    textcoords="offset points",
                    va="center",
                    ha="right",
                    fontsize=9,
                    color="white",
                )
            else:
                ax.annotate(
                    label,
                    xy=(r["delta_pp"], yi),
                    xytext=(6, 0),
                    textcoords="offset points",
                    va="center",
                    ha="left",
                    fontsize=9,
                    color="white",
                )
        else:
            sign_pad = 1 if r["delta_pp"] >= 0 else -1
            ax.annotate(
                label,
                xy=(r["delta_pp"], yi),
                xytext=(8 * sign_pad, 0),
                textcoords="offset points",
                va="center",
                ha="left" if r["delta_pp"] >= 0 else "right",
                fontsize=9,
                color=PALETTE["text"],
            )

    max_abs = max(8.0, float(picks["delta_pp"].abs().max()) + 6.0)
    ax.set_xlim(-max_abs * 1.15, max_abs * 1.15)
    fig.tight_layout()
    return fig


def plot_player_comparison(
    df: pd.DataFrame,
    player: str | None = None,
    min_games: int = 30,
    min_subset: int = 10,
    rolling_window: int = 30,
) -> plt.Figure:
    """Every tracked player on every key metric, side-by-side as a heatmap.

    Cell colour is the column-wise z-score so "above the group" reads green
    and "below the group" reads red regardless of the metric's units.
    Annotations are raw values so the reader sees both the rank and the
    magnitude. When ``player`` is supplied the global heatmap is unchanged
    but that player's row gets a thick primary border so the eye snaps to
    them inside the group context.
    """
    metrics = [
        "Overall WR",
        "Avg KDA",
        "Prime-hr WR (19-23)",
        "Weekend WR",
        "Tilt WR (after 2L+)",
        "Top champ WR",
        "Career trend (pp)",
        "LP / 100 games",
    ]

    people_games = df.groupby("person").size()
    people = people_games[people_games >= min_games].index.tolist()
    if not people:
        return _empty_figure(f"No players with ≥{min_games} games")

    # Overall WR per person — also used to sort the rows.
    overall_wr = df.groupby("person")["win"].mean()
    people = sorted(people, key=lambda p: overall_wr.get(p, 0.0), reverse=True)

    # LP/100 games per person, derived from rank-history deltas.
    try:
        lp_events = compute_lp_events(DEFAULT_DB)
    except Exception:
        lp_events = pd.DataFrame(columns=["person", "delta_score"])
    if not lp_events.empty:
        lp_grouped = lp_events.groupby("person").agg(
            lp_total=("delta_score", "sum"), lp_n=("delta_score", "size")
        )
    else:
        lp_grouped = pd.DataFrame(columns=["lp_total", "lp_n"])

    rows = []
    for person in people:
        sub = df[df["person"] == person]
        n_games = len(sub)

        # Overall WR — gate to NaN only if (somehow) below min_subset.
        wr_overall = float(sub["win"].mean()) if n_games >= min_subset else np.nan

        # Avg KDA — same gate.
        avg_kda = float(sub["kda"].mean()) if n_games >= min_subset else np.nan

        prime = sub[sub["hour"].isin([19, 20, 21, 22, 23])]
        wr_prime = float(prime["win"].mean()) if len(prime) >= min_subset else np.nan

        weekend = sub[sub["dow"].isin([5, 6])]
        wr_weekend = float(weekend["win"].mean()) if len(weekend) >= min_subset else np.nan

        tilt = sub[sub["loss_streak_in"] >= 2]
        wr_tilt = float(tilt["win"].mean()) if len(tilt) >= min_subset else np.nan

        # Top champ: that person's single most-played champion, if ≥10 games.
        champ_counts = sub.groupby("champion")["win"].agg(["count", "mean"])
        champ_counts = champ_counts[champ_counts["count"] >= min_subset]
        if not champ_counts.empty:
            top = champ_counts.sort_values("count", ascending=False).iloc[0]
            wr_top_champ = float(top["mean"])
        else:
            wr_top_champ = np.nan

        # Career trend — slope of rolling-30 WR vs % of career, in pp across
        # the full career. Same convention as plot_player_progression.
        sub_sorted = sub.sort_values("game_start").reset_index(drop=True)
        n = len(sub_sorted)
        if n > 1:
            pct = pd.Series(np.linspace(0, 100, n), index=sub_sorted.index)
            roll = (
                sub_sorted["win"]
                .rolling(window=rolling_window, min_periods=max(5, rolling_window // 3))
                .mean()
            )
            mask = roll.notna()
            if mask.sum() >= 5:
                slope = float(np.polyfit(pct[mask], roll[mask], 1)[0])
                career_trend_pp = slope * 100 * 100
            else:
                career_trend_pp = np.nan
        else:
            career_trend_pp = np.nan

        # LP/100 games — sum of delta_score / event count * 100.
        if person in lp_grouped.index:
            n_events = int(lp_grouped.loc[person, "lp_n"])
            if n_events >= min_subset:
                lp_per_100 = float(lp_grouped.loc[person, "lp_total"]) / n_events * 100.0
            else:
                lp_per_100 = np.nan
        else:
            lp_per_100 = np.nan

        rows.append(
            [
                wr_overall,
                avg_kda,
                wr_prime,
                wr_weekend,
                wr_tilt,
                wr_top_champ,
                career_trend_pp,
                lp_per_100,
            ]
        )

    values = np.array(rows, dtype=float)

    # Column-wise z-score for colour. NaNs are ignored in the mean/std and
    # left as NaN in the z-grid so the masked colormap renders them grey.
    col_mean = np.nanmean(values, axis=0)
    col_std = np.nanstd(values, axis=0)
    safe_std = np.where(col_std > 0, col_std, 1.0)
    z = (values - col_mean) / safe_std
    z = np.where(col_std > 0, z, 0.0)
    z[np.isnan(values)] = np.nan

    fig, ax = plt.subplots(figsize=(13, max(4, len(metrics) * 0.5 + 2)))
    cmap = plt.get_cmap("RdYlGn").copy()
    cmap.set_bad("#f3f4f6")
    masked_z = np.ma.masked_invalid(z)
    im = ax.imshow(masked_z, aspect="auto", cmap=cmap, vmin=-1.5, vmax=1.5)

    y_labels = [f"{p}  ({int(people_games[p])} games)" for p in people]
    ax.set_yticks(np.arange(len(people)))
    ax.set_yticklabels(y_labels)
    ax.set_xticks(np.arange(len(metrics)))
    ax.set_xticklabels(metrics, rotation=30, ha="right")

    wr_cols = {0, 2, 3, 4, 5}
    kda_col = 1
    trend_col = 6
    lp_col = 7
    for i in range(len(people)):
        for j in range(len(metrics)):
            v = values[i, j]
            if np.isnan(v):
                text = "—"
                color = PALETTE["muted"]
            elif j in wr_cols:
                text = f"{v:.0%}"
                color = PALETTE["text"]
            elif j == kda_col:
                text = f"{v:.2f}"
                color = PALETTE["text"]
            elif j == trend_col:
                text = f"{v:+.1f}"
                color = PALETTE["text"]
            elif j == lp_col:
                text = f"{v:+.0f}"
                color = PALETTE["text"]
            else:
                text = f"{v:.2f}"
                color = PALETTE["text"]
            ax.text(j, i, text, ha="center", va="center", fontsize=8.5, color=color)

    if player is not None and player in people:
        idx = people.index(player)
        for y_edge in (idx - 0.5, idx + 0.5):
            ax.axhline(
                y_edge,
                color=PALETTE["primary"],
                linewidth=2.4,
                xmin=0,
                xmax=1,
            )

    ax.set_title(_title("Player comparison — every metric, side by side", player))
    _subtitle(
        ax,
        "Cell colour = z-score within column (green = above group avg, red = below). "
        "Annotation = raw value.",
    )
    cbar = fig.colorbar(im, ax=ax, label="Z-score", shrink=0.6, pad=0.02)
    cbar.outline.set_visible(False)
    _polish_ax(ax)
    ax.grid(False)
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
    _subtitle(ax, "Up = grinding the champ pays off. Flat = practice isn't improving you.")
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
    subtitle: str | None = None,
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
    if subtitle:
        # Tuck the italic caption under the suptitle.
        fig.text(
            0.5,
            0.92,
            subtitle,
            ha="center",
            va="top",
            fontsize=10,
            color=PALETTE["muted"],
            style="italic",
        )
    fig.tight_layout(rect=(0, 0, 1, 0.93 if subtitle else 1.0))
    return fig


def plot_hour_of_day(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Win rate + game volume by hour of day. Tilt-hour detector."""
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to plot")
    by_hour = d.groupby("hour")["win"].agg(["count", "mean"]).reindex(range(24), fill_value=0)
    macro = _bucket_winrate(
        d.assign(_hr=d["hour"]).rename(columns={"_hr": "hour_bucket"}), "hour_bucket"
    )
    macro = macro.set_index("hour_bucket").reindex(range(24))
    macro_n = int(macro["n_people"].fillna(0).max() or 0)
    plot_wr = macro["winrate"] if macro_n > 1 else by_hour["mean"]
    plot_mask = macro["winrate"].notna() if macro_n > 1 else by_hour["count"] > 0
    wins = (by_hour["count"] * by_hour["mean"]).round().astype(int).to_numpy()
    _chi2, _dof, pval = chi2_homogeneity(wins, by_hour["count"].astype(int).to_numpy())
    return _temporal_dual_axis(
        counts=by_hour["count"],
        winrate_mask=plot_mask,
        winrate_values=plot_wr,
        fig_size=(13, 4.6),
        x_ticks=range(0, 24),
        x_labels=[f"{h:02d}" for h in range(24)],
        xlabel="Hour of day (local)",
        title=_title("Hour of day — volume + win rate", player),
        subtitle=(
            f"Grey bars = games at that hour. Blue line = win rate ({_macro_label(macro_n)}). "
            f"χ² for any hour effect: {_p_marker(pval)} ({_p_verdict(pval)})."
        ),
    )


def plot_day_of_week(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Same shape as hour-of-day but on a day axis. Weekend warrior detector."""
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to plot")
    by_dow = d.groupby("dow")["win"].agg(["count", "mean"]).reindex(range(7), fill_value=0)
    macro = _bucket_winrate(
        d.assign(_d=d["dow"]).rename(columns={"_d": "dow_bucket"}), "dow_bucket"
    )
    macro = macro.set_index("dow_bucket").reindex(range(7))
    macro_n = int(macro["n_people"].fillna(0).max() or 0)
    plot_wr = macro["winrate"] if macro_n > 1 else by_dow["mean"]
    plot_mask = macro["winrate"].notna() if macro_n > 1 else by_dow["count"] > 0
    wins = (by_dow["count"] * by_dow["mean"]).round().astype(int).to_numpy()
    _chi2, _dof, pval = chi2_homogeneity(wins, by_dow["count"].astype(int).to_numpy())
    return _temporal_dual_axis(
        counts=by_dow["count"],
        winrate_mask=plot_mask,
        winrate_values=plot_wr,
        fig_size=(9, 4.6),
        x_ticks=range(7),
        x_labels=DOW_LABELS,
        xlabel="Day of week",
        title=_title("Day of week — volume + win rate", player),
        subtitle=(
            f"Grey bars = games per day. Blue line = win rate ({_macro_label(macro_n)}). "
            f"χ² for any day effect: {_p_marker(pval)} ({_p_verdict(pval)})."
        ),
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
    is_macro = d["person"].nunique() > 1
    if is_macro:
        # Per-person cell WR (with at least 3 of THEIR games in the cell),
        # then mean across people for each cell.
        per_person = (
            d.groupby(["dow", "hour", "person"])
            .agg(person_games=("win", "size"), person_wins=("win", "sum"))
            .reset_index()
        )
        per_person = per_person[per_person["person_games"] >= 3]
        per_person["person_wr"] = per_person["person_wins"] / per_person["person_games"]
        winrate = per_person.pivot_table(
            index="dow", columns="hour", values="person_wr", aggfunc="mean"
        ).reindex(index=range(7), columns=range(24))
        contrib_people = per_person.pivot_table(
            index="dow", columns="hour", values="person", aggfunc="nunique"
        ).reindex(index=range(7), columns=range(24))
        # Require at least 2 people to draw a cell — single-person cells
        # would re-introduce the dominance problem.
        winrate = winrate.where(contrib_people.fillna(0) >= 2)
        subtitle = (
            "Greener = better at that hour/day. Each cell averages across "
            "≥2 people (each weighted equally); ≥3 games per cell per person."
        )
    else:
        winrate = d.pivot_table(index="dow", columns="hour", values="win", aggfunc="mean").reindex(
            index=range(7), columns=range(24)
        )
        winrate = winrate.where(counts >= 3)
        subtitle = (
            "Greener = better at that hour/day. Cells with <3 of this player's games are dimmed."
        )

    fig, ax = plt.subplots(figsize=(13, 4.6))
    cmap = plt.get_cmap("RdYlGn").copy()
    cmap.set_bad(color="#f3f4f6")
    im = ax.imshow(winrate.values, aspect="auto", cmap=cmap, vmin=0.3, vmax=0.7, origin="lower")
    ax.set_yticks(range(7))
    ax.set_yticklabels(DOW_LABELS)
    ax.set_xticks(range(0, 24))
    ax.set_xticklabels([f"{h:02d}" for h in range(24)])
    ax.set_xlabel("Hour of day")
    ax.set_ylabel("Day of week")
    ax.set_title(_title("Win rate heatmap", player))
    _subtitle(ax, subtitle)
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
    g = _bucket_winrate(d, "streak_bucket")
    macro_n = int(g["n_people"].max()) if not g.empty else 1

    # Chi² uses raw pooled counts (the actual outcomes) — the right
    # statistical test even when we *display* macro-averaged WRs.
    pooled = _bin_winrate(d, "streak_bucket").set_index("streak_bucket").reindex(g["streak_bucket"])
    wins = (pooled["games"] * pooled["winrate"]).round().astype(int).to_numpy()
    totals = pooled["games"].astype(int).to_numpy()
    _chi2, _dof, pval = chi2_homogeneity(wins, totals)

    fig, ax = plt.subplots(figsize=(11, 4.6))
    ax.bar(range(len(g)), g["winrate"], color=PALETTE["accent_orange"], width=0.7)
    if macro_n > 1:
        ax.errorbar(
            range(len(g)),
            g["winrate"],
            yerr=[g["winrate"] - g["ci_lo"], g["ci_hi"] - g["winrate"]],
            fmt="none",
            ecolor=PALETTE["text"],
            capsize=3,
            linewidth=1.0,
            alpha=0.6,
        )
    ax.set_xticks(range(len(g)))
    ax.set_xticklabels(g["streak_bucket"])
    ax.set_xlabel("Loss streak entering this game")
    ax.set_ylabel("Win rate of this game")
    ax.set_title(_title("Tilt check — win rate vs entering loss streak", player))
    _subtitle(
        ax,
        f"Downward step = tilt is real. χ² across buckets: {_p_marker(pval)} ({_p_verdict(pval)})  ·  {_macro_label(macro_n)}.",
    )
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
    d_gap = d.dropna(subset=["gap_bucket"])
    g = _bucket_winrate(d_gap, "gap_bucket")
    macro_n = int(g["n_people"].max()) if not g.empty else 1
    pooled = _bin_winrate(d_gap, "gap_bucket").set_index("gap_bucket").reindex(g["gap_bucket"])
    wins = (pooled["games"] * pooled["winrate"]).round().astype(int).to_numpy()
    totals = pooled["games"].astype(int).to_numpy()
    _chi2, _dof, pval = chi2_homogeneity(wins, totals)

    fig, ax = plt.subplots(figsize=(11, 4.6))
    ax.bar(range(len(g)), g["winrate"], color=PALETTE["accent_teal"], width=0.7)
    if macro_n > 1:
        ax.errorbar(
            range(len(g)),
            g["winrate"],
            yerr=[g["winrate"] - g["ci_lo"], g["ci_hi"] - g["winrate"]],
            fmt="none",
            ecolor=PALETTE["text"],
            capsize=3,
            linewidth=1.0,
            alpha=0.6,
        )
    ax.set_xticks(range(len(g)))
    ax.set_xticklabels(g["gap_bucket"])
    ax.set_xlabel("Time since previous game")
    ax.set_ylabel("Win rate")
    ax.set_title(_title("Win rate vs gap since previous game", player))
    _subtitle(
        ax,
        f"Back-to-back vs fresh session. χ² across buckets: {_p_marker(pval)} ({_p_verdict(pval)})  ·  {_macro_label(macro_n)}.",
    )
    ax.set_ylim(0, 1.1)
    _baseline(ax)
    _annotate_bars(ax, range(len(g)), g["winrate"], g["games"])
    _polish_ax(ax)
    fig.tight_layout()
    return fig


def plot_session_analysis(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Three-panel session view: how long are your sessions, do you tilt
    within them, and do long grinds pay off?

    A "session" is a contiguous run of games where each next game starts
    within ``SESSION_GAP_MIN`` minutes of the previous one ending. Longer
    breaks start a new session — the typical "I queued, played 5 games,
    went to dinner" pattern groups into one session.

    Left panel    Session length distribution. Most sessions are short;
                  the tail tells you how grindy the heaviest sessions get.
    Middle panel  Rolling WR by Nth game IN the session — does fatigue
                  hit? A downward slope = in-session tilt is real.
    Right panel   WR by session length bucket. Compares "1-and-done"
                  sessions vs marathon sessions to see if grinding
                  produces better outcomes overall.
    """
    d = _filter_player(df, player)
    if d.empty or "session_id" not in d.columns:
        return _empty_figure("No games to analyse")

    # Per-session aggregates (one row per session).
    sessions = (
        d.groupby(["person", "session_id"])
        .agg(length=("session_game_idx", "max"), wins=("win", "sum"))
        .reset_index()
    )
    sessions["session_wr"] = sessions["wins"] / sessions["length"]

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8))

    # --- Panel 1: session length distribution ---
    ax = axes[0]
    counts, bins, _ = ax.hist(
        sessions["length"],
        bins=np.arange(1, max(2, int(sessions["length"].max()) + 2)) - 0.5,
        color=PALETTE["primary"],
        edgecolor="white",
        linewidth=0.6,
    )
    median_len = float(sessions["length"].median())
    p90_len = float(sessions["length"].quantile(0.90))
    ax.axvline(
        median_len,
        color=PALETTE["text"],
        linewidth=1.0,
        linestyle="--",
        label=f"median {median_len:.0f}",
    )
    ax.axvline(
        p90_len,
        color=PALETTE["accent_orange"],
        linewidth=1.0,
        linestyle=":",
        label=f"90th pct {p90_len:.0f}",
    )
    ax.set_xlabel("Session length (games)")
    ax.set_ylabel("Number of sessions")
    ax.set_title(_title("Session length distribution", player))
    _subtitle(
        ax,
        f"{len(sessions)} sessions · gap threshold = {SESSION_GAP_MIN} min "
        f"· {int(sessions['length'].sum())} games total.",
    )
    ax.legend(loc="upper right")
    _polish_ax(ax)

    # --- Panel 2: WR by Nth game in session ---
    ax = axes[1]
    # Cap at "10+" so the long tail doesn't pull noise into a long x-axis.
    d2 = d.copy()
    d2["nth_capped"] = d2["session_game_idx"].clip(upper=10)
    g = _bucket_winrate(d2, "nth_capped")
    macro_n = int(g["n_people"].max()) if not g.empty else 1
    x = g["nth_capped"].astype(int).to_numpy()
    ax.plot(x, g["winrate"], color=PALETTE["primary"], marker="o", linewidth=2.2, markersize=6)
    if macro_n > 1:
        ax.fill_between(
            x,
            g["ci_lo"],
            g["ci_hi"],
            color=PALETTE["primary"],
            alpha=0.15,
            label="±1σ across people",
        )
        ax.legend(loc="lower right")
    _baseline(ax)
    ax.set_xlabel("Nth game in current session  (10+ collapsed)")
    ax.set_ylabel("Win rate")
    ax.set_ylim(0, 1.05)
    ax.set_xticks(range(1, 11))
    ax.set_xticklabels([str(i) for i in range(1, 10)] + ["10+"])
    ax.set_title(_title("Win rate by Nth game in session", player))

    # Chi² on raw pooled counts for the "in-session tilt is real?" line.
    pooled = _bin_winrate(d2, "nth_capped").set_index("nth_capped").reindex(x)
    wins = (pooled["games"] * pooled["winrate"]).round().astype(int).to_numpy()
    totals = pooled["games"].astype(int).to_numpy()
    _chi2, _dof, pval = chi2_homogeneity(wins, totals)
    _subtitle(
        ax,
        f"In-session tilt check: χ² across game positions {_p_marker(pval)} ({_p_verdict(pval)}).",
    )
    _polish_ax(ax)

    # --- Panel 3: WR by session length bucket ---
    ax = axes[2]
    d3 = d.copy()
    d3["session_length_bucket"] = pd.cut(
        d3["session_length"], bins=SESSION_LEN_BINS, labels=SESSION_LEN_LABELS, right=False
    )
    g3 = _bucket_winrate(d3, "session_length_bucket")
    macro_n3 = int(g3["n_people"].max()) if not g3.empty else 1
    xs = range(len(g3))
    ax.bar(xs, g3["winrate"], color=PALETTE["accent_teal"], width=0.7)
    if macro_n3 > 1:
        ax.errorbar(
            list(xs),
            g3["winrate"],
            yerr=[g3["winrate"] - g3["ci_lo"], g3["ci_hi"] - g3["winrate"]],
            fmt="none",
            ecolor=PALETTE["text"],
            capsize=3,
            linewidth=1.0,
            alpha=0.6,
        )
    ax.set_xticks(list(xs))
    ax.set_xticklabels(g3["session_length_bucket"], rotation=0)
    ax.set_xlabel("Session length bucket")
    ax.set_ylabel("Win rate")
    ax.set_ylim(0, 1.1)
    ax.set_title(_title("Win rate by session length", player))
    _baseline(ax)
    _annotate_bars(ax, list(xs), g3["winrate"], g3["games"])
    _polish_ax(ax)

    pooled3 = (
        _bin_winrate(d3, "session_length_bucket")
        .set_index("session_length_bucket")
        .reindex(g3["session_length_bucket"])
    )
    wins3 = (pooled3["games"] * pooled3["winrate"]).round().astype(int).to_numpy()
    totals3 = pooled3["games"].astype(int).to_numpy()
    _chi2, _dof, pval3 = chi2_homogeneity(wins3, totals3)
    _subtitle(
        ax,
        f"Long-grind check: χ² across lengths {_p_marker(pval3)} ({_p_verdict(pval3)}).",
    )

    fig.tight_layout()
    return fig


# --- 6. Overview -----------------------------------------------------------


def plot_lp_economics(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Per-game LP gain-vs-loss imbalance — the climbing-vs-treading-water view.

    A player with avg +22 per win and avg -20 per loss climbs even at
    50% WR. A player with avg +18 per win and avg -25 per loss can win
    62% and still lose LP. The MMR system bakes those rates from the
    skill gap to current rank; this chart reveals it directly.

    Aggregate view: diverging bar per person, left side = avg LP lost
    per loss (red), right side = avg LP gained per win (green), sorted
    by the net LP per 100 games. Annotation shows "+W / -L → net/100"
    so you can read climber vs treading directly.

    Single-person view: histograms of LP gains (green) + LP losses (red,
    signed negative for natural visual layout). Vertical lines mark
    each mean. A status box reports the net rate and a plain-English
    verdict (climbing / treading / falling).
    """
    try:
        events = compute_lp_events(DEFAULT_DB)
    except Exception as exc:
        return _empty_figure(f"Could not load LP events: {exc!r}")
    if events.empty:
        return _empty_figure("No LP events in league_history yet")

    if player is None:
        # Top-N most-tracked people. Match-stats game count, not LP-event
        # count, so the people shown line up with the rest of the panel.
        top_people = df.groupby("person").size().sort_values(ascending=False).head(8).index.tolist()
        agg_rows = []
        for person, sub in events.groupby("person"):
            if person not in top_people:
                continue
            wins = sub[sub["outcome"] == "win"]["delta_score"]
            losses = sub[sub["outcome"] == "loss"]["delta_score"]
            if len(wins) < 5 or len(losses) < 5:
                continue
            n = len(sub)
            net_per_100 = sub["delta_score"].sum() / n * 100
            agg_rows.append(
                {
                    "person": person,
                    "avg_win": float(wins.mean()),
                    "avg_loss": float(losses.mean()),
                    "n": n,
                    "net_per_100": net_per_100,
                }
            )
        if not agg_rows:
            return _empty_figure("Not enough LP events for the top players")
        agg = (
            pd.DataFrame(agg_rows).sort_values("net_per_100", ascending=True).reset_index(drop=True)
        )

        fig_h = max(4.6, len(agg) * 0.55)
        fig, ax = plt.subplots(figsize=(13, fig_h))
        y = np.arange(len(agg))
        ax.barh(y, agg["avg_loss"], color=PALETTE["loss"], height=0.7, label="Avg LP per loss")
        ax.barh(y, agg["avg_win"], color=PALETTE["win"], height=0.7, label="Avg LP per win")
        ax.axvline(0, color=PALETTE["text"], linewidth=0.8)
        ax.set_yticks(y)
        ax.set_yticklabels(agg["person"])
        ax.set_xlabel("LP delta per game (Ranker score, signed)")
        ax.set_title(_title("LP economics — gain per win vs loss per loss", player))
        _subtitle(
            ax,
            "Right green bar > left red bar = climbing even at 50% WR. "
            "Sort: net LP per 100 games (top = best climbers).",
        )
        # Place annotations inside the green (win) bar when it's wide
        # enough (≥12 LP); otherwise nudge them outside. White-on-green
        # reads cleanly and reclaims the right-side whitespace.
        for yi, row in agg.iterrows():
            label = (
                f"+{row['avg_win']:.1f} W  ·  {row['avg_loss']:.1f} L  ·  "
                f"net {row['net_per_100']:+.0f}/100  ·  n={int(row['n'])}"
            )
            if row["avg_win"] >= 12:
                ax.annotate(
                    label,
                    xy=(row["avg_win"], yi),
                    xytext=(-6, 0),
                    textcoords="offset points",
                    va="center",
                    ha="right",
                    fontsize=9,
                    color="white",
                )
            else:
                tag_colour = PALETTE["win"] if row["net_per_100"] > 0 else PALETTE["loss"]
                ax.annotate(
                    label,
                    xy=(row["avg_win"], yi),
                    xytext=(8, 0),
                    textcoords="offset points",
                    va="center",
                    ha="left",
                    fontsize=9,
                    color=tag_colour,
                )
        max_abs = max(35.0, float(max(agg["avg_win"].max(), -agg["avg_loss"].min()) + 8))
        # With annotations now mostly inside the bars, drop the extra
        # right-side padding so the chart isn't 30% whitespace.
        ax.set_xlim(-max_abs, max_abs * 1.15)
        ax.legend(loc="lower right")
        _polish_ax(ax)
        fig.tight_layout()
        return fig

    # --- Single-person view ---
    sub = events[events["person"] == player]
    if sub.empty:
        return _empty_figure(f"No LP events recorded for {player}")
    wins = sub.loc[sub["outcome"] == "win", "delta_score"].astype(float)
    losses = sub.loc[sub["outcome"] == "loss", "delta_score"].astype(float)
    if wins.empty or losses.empty:
        return _empty_figure(f"{player} has no recorded LP events for both win and loss")

    avg_w, avg_l = float(wins.mean()), float(losses.mean())
    n = len(sub)
    net_per_100 = float(sub["delta_score"].sum() / n * 100)
    if net_per_100 > 50:
        verdict = "climbing fast — MMR > rank"
        v_colour = PALETTE["win"]
    elif net_per_100 > 5:
        verdict = "climbing"
        v_colour = PALETTE["win"]
    elif net_per_100 < -50:
        verdict = "falling fast — MMR < rank"
        v_colour = PALETTE["loss"]
    elif net_per_100 < -5:
        verdict = "falling"
        v_colour = PALETTE["loss"]
    else:
        verdict = "treading water — at fair MMR"
        v_colour = PALETTE["neutral"]

    fig, ax = plt.subplots(figsize=(13, 5.0))
    # Symmetric bin range so the two distributions sit visually opposite.
    edge = max(float(wins.max()), -float(losses.min()), 30.0) + 4
    bins = np.arange(-edge, edge + 1, 2)
    ax.hist(
        losses,
        bins=bins,
        color=PALETTE["loss"],
        alpha=0.75,
        edgecolor="white",
        linewidth=0.6,
        label=f"Losses (avg {avg_l:+.1f})",
    )
    ax.hist(
        wins,
        bins=bins,
        color=PALETTE["win"],
        alpha=0.75,
        edgecolor="white",
        linewidth=0.6,
        label=f"Wins (avg {avg_w:+.1f})",
    )
    ax.axvline(avg_w, color=PALETTE["win"], linestyle="--", linewidth=1.0)
    ax.axvline(avg_l, color=PALETTE["loss"], linestyle="--", linewidth=1.0)
    ax.axvline(0, color=PALETTE["text"], linewidth=0.8)
    ax.set_xlabel("LP delta per game")
    ax.set_ylabel("Number of games")
    ax.set_title(_title("LP economics — per-game gain & loss distribution", player))
    _subtitle(
        ax,
        f"Dashed lines = means. {n} tracked LP events. "
        f"Net {net_per_100:+.0f} LP per 100 games → {verdict}.",
    )
    ax.legend(loc="upper right")
    _polish_ax(ax)

    ax.text(
        0.02,
        0.96,
        f"Avg + per win:   {avg_w:+5.1f}\n"
        f"Avg − per loss:  {avg_l:+5.1f}\n"
        f"Net per 100:     {net_per_100:+5.0f}\n"
        f"Verdict: {verdict}",
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=10,
        family="DejaVu Sans Mono",
        bbox={
            "facecolor": "white",
            "alpha": 0.95,
            "edgecolor": v_colour,
            "linewidth": 1.4,
            "pad": 6,
        },
    )
    fig.tight_layout()
    return fig


def plot_rank_trajectory(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Rank score over time, drawn from league_history.

    Single-person view: their rank line plus a rolling-20 match-stats
    WR overlay on a twin axis, so you can read "WR climbed, did the
    rank actually follow?" off one chart. Annotates start LP, current
    LP, and net delta across the tracked window.

    Aggregate view: top 6 most-active people as separate lines, no WR
    overlay (would be unreadable with that many series). Picks the
    busiest people by match_stats game count so the lines actually move.
    """
    try:
        ranks = load_rank_history(DEFAULT_DB)
    except Exception as exc:
        return _empty_figure(f"Could not load rank history: {exc!r}")
    if ranks.empty:
        return _empty_figure("No rank history available")

    if player is None:
        # Pick top-N most-active people in match_stats so the rank chart
        # lines up with the same people the rest of the panel describes.
        top_people = df.groupby("person").size().sort_values(ascending=False).head(6).index.tolist()
        ranks = ranks[ranks["person"].isin(top_people)]
        if ranks.empty:
            return _empty_figure("No rank history for the tracked top players")

        fig, ax = plt.subplots(figsize=(13, 5.4))
        for idx, name in enumerate(top_people):
            sub = ranks[ranks["person"] == name].sort_values("timestamp")
            if sub.empty:
                continue
            ax.plot(
                sub["timestamp"],
                sub["rank_score"],
                color=SERIES_CYCLE[idx % len(SERIES_CYCLE)],
                linewidth=1.6,
                alpha=0.85,
                label=name,
            )
        _rank_axis(ax)
        ax.set_xlabel("Date")
        ax.set_ylabel("Rank")
        ax.set_title(_title("Rank trajectory — top 6 most-active people", player))
        _subtitle(
            ax,
            "Higher = better rank. Each line is one Discord person; "
            f"{len(ranks):,} LP snapshots across {ranks['person'].nunique()} people.",
        )
        ax.legend(loc="lower left", ncol=2, fontsize=9)
        _polish_ax(ax)
        fig.autofmt_xdate()
        fig.tight_layout()
        return fig

    # --- Single-person view: rank line + WR overlay ---
    sub = ranks[ranks["person"] == player].sort_values("timestamp").reset_index(drop=True)
    if sub.empty:
        return _empty_figure(f"No rank history for {player}")

    fig, ax_rank = plt.subplots(figsize=(13, 5.4))
    ax_rank.plot(
        sub["timestamp"],
        sub["rank_score"],
        color=PALETTE["primary"],
        linewidth=2.0,
        label="Rank",
    )
    _rank_axis(ax_rank)
    ax_rank.set_xlabel("Date")
    ax_rank.set_ylabel("Rank", color=PALETTE["primary"])
    ax_rank.tick_params(axis="y", colors=PALETTE["primary"])

    # WR overlay on twin axis — sourced from match_stats, not history.
    matches = df[df["person"] == player].sort_values("game_start").reset_index(drop=True)
    if not matches.empty:
        matches = matches.copy()
        matches["rolling_20_wr"] = matches["win"].rolling(window=20, min_periods=5).mean()
        ax_wr = ax_rank.twinx()
        ax_wr.plot(
            matches["game_start"],
            matches["rolling_20_wr"],
            color=PALETTE["accent_orange"],
            linewidth=1.6,
            alpha=0.7,
            linestyle="--",
            label="Rolling-20 WR (matches)",
        )
        ax_wr.axhline(0.5, color=PALETTE["muted"], linewidth=0.7, linestyle=":", alpha=0.6)
        ax_wr.set_ylim(0, 1.05)
        ax_wr.set_ylabel("Match-stats WR", color=PALETTE["accent_orange"])
        ax_wr.tick_params(axis="y", colors=PALETTE["accent_orange"])
        ax_wr.grid(False)
        ax_wr.spines["right"].set_visible(False)
        ax_wr.spines["top"].set_visible(False)
        # Combined legend.
        lines1, labels1 = ax_rank.get_legend_handles_labels()
        lines2, labels2 = ax_wr.get_legend_handles_labels()
        ax_rank.legend(lines1 + lines2, labels1 + labels2, loc="lower left")
    else:
        ax_rank.legend(loc="lower left")

    # Headline annotation: net change across the tracked window.
    first = sub.iloc[0]
    last = sub.iloc[-1]
    delta = last["rank_score"] - first["rank_score"]
    direction = "climbed" if delta > 0 else ("dropped" if delta < 0 else "flat")
    box_colour = (
        PALETTE["win"] if delta > 0 else (PALETTE["loss"] if delta < 0 else PALETTE["neutral"])
    )
    ax_rank.text(
        0.02,
        0.96,
        f"Start: {first['tier'].title()} {first['division']} {int(first['lp'])}LP\n"
        f"Now:   {last['tier'].title()} {last['division']} {int(last['lp'])}LP\n"
        f"Net:   {direction} {abs(int(delta))} pts",
        transform=ax_rank.transAxes,
        va="top",
        ha="left",
        fontsize=10,
        family="DejaVu Sans Mono",
        bbox={
            "facecolor": "white",
            "alpha": 0.95,
            "edgecolor": box_colour,
            "linewidth": 1.4,
            "pad": 6,
        },
    )

    ax_rank.set_title(_title("Rank trajectory + rolling WR", player))
    _subtitle(
        ax_rank,
        f"Solid blue = rank ({len(sub):,} LP snapshots). Dashed orange = match-stats rolling-20 WR.",
    )
    _polish_ax(ax_rank)
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def _rank_axis(ax) -> None:
    """Apply rank-score y-axis labels (Iron IV → Master) and grid lines."""
    pad_top = 200
    bottom = max(0, ax.get_ylim()[0] - 50)
    top = ax.get_ylim()[1] + pad_top
    ax.set_ylim(bottom, top)
    ticks = [score for score, _ in _RANK_TICK_LABELS if bottom <= score <= top]
    labels = [label for score, label in _RANK_TICK_LABELS if bottom <= score <= top]
    ax.set_yticks(ticks)
    ax.set_yticklabels(labels)


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
    _subtitle(
        ax,
        "Green fill = above 50% form, red fill = below. Black line = lifetime average.",
    )
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
    _subtitle(
        ax,
        "X is % of career so short and long histories share the axis. Slope in legend = direction.",
    )
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


def compute_head_to_head(df: pd.DataFrame, min_games: int = 3) -> pd.DataFrame:
    """All opposite-team encounters between tracked players.

    Mirror of :func:`compute_duos` but joins on match_id ALONE (without
    win), then keeps the rows where the two players had different win
    values — i.e. they were on opposite teams of the same game.

    Returns columns: ``a``, ``b``, ``games`` (encounters), ``a_wins``
    (times person ``a`` beat ``b``), ``a_winrate``. Pair ordering is
    alphabetical (``a < b``) so each pair appears once; the WR is from
    ``a``'s perspective.
    """
    m = df[["match_id", "win", "person"]].drop_duplicates()
    pairs = m.merge(m, on="match_id", suffixes=("_x", "_y"))
    pairs = pairs[(pairs["person_x"] < pairs["person_y"]) & (pairs["win_x"] != pairs["win_y"])]
    if pairs.empty:
        return pd.DataFrame(columns=["a", "b", "games", "a_wins", "a_winrate"])
    agg = (
        pairs.groupby(["person_x", "person_y"])
        .agg(games=("win_x", "size"), a_wins=("win_x", "sum"))
        .reset_index()
        .rename(columns={"person_x": "a", "person_y": "b"})
    )
    agg["a_winrate"] = agg["a_wins"] / agg["games"]
    agg = agg[agg["games"] >= min_games]
    return agg.sort_values("games", ascending=False).reset_index(drop=True)


def plot_duo_winrate(
    df: pd.DataFrame,
    player: str | None = None,
    min_games: int = 10,
    h2h_min_games: int = 4,
    top: int = 10,
) -> plt.Figure:
    """Two-panel social view: same-team duos (left) + head-to-head (right).

    Same-team detection joins on ``(match_id, win)`` — two tracked
    players with the same outcome in the same match were on one side.
    Head-to-head joins on ``match_id`` and keeps rows where ``win``
    differs — opposite teams in the same game.

    Aggregate view: left = top duos by games together; right = top
    head-to-head encounters by frequency, bars centred on 50% so the
    favoured side reads at a glance.

    Per-person view: left = partners with their WR vs that person's
    solo baseline; right = opponents with that person's record against
    each (dashed line = even 50%).
    """
    duos = compute_duos(df, min_games=min_games)
    h2h = compute_head_to_head(df, min_games=h2h_min_games)
    if duos.empty and h2h.empty:
        return _empty_figure("No multi-tracked-player matches yet")

    fig, axes = plt.subplots(1, 2, figsize=(16, max(4.6, top * 0.45)))

    if player is None:
        # --- Left: top duos by games together ---
        ax = axes[0]
        d = duos.head(top).iloc[::-1]
        if d.empty:
            ax.text(
                0.5,
                0.5,
                f"No duos with ≥{min_games} games",
                ha="center",
                va="center",
                color=PALETTE["muted"],
                transform=ax.transAxes,
            )
            ax.set_axis_off()
        else:
            labels = d["a"] + "  +  " + d["b"]
            colours = [PALETTE["win"] if wr >= 0.5 else PALETTE["loss"] for wr in d["winrate"]]
            ax.barh(range(len(d)), d["games"], color=colours, height=0.7)
            ax.set_yticks(range(len(d)))
            ax.set_yticklabels(labels)
            ax.set_xlabel("Games played together (same team)")
            ax.set_title(_title(f"Top duos (≥{min_games} together)", player))
            _subtitle(ax, "Bar length = games together. Green/red = WR above/below 50%.")
            _polish_ax(ax)
            for yi, (g, wr) in enumerate(zip(d["games"], d["winrate"], strict=False)):
                ax.annotate(
                    f"{int(g)} · {wr:.0%}",
                    xy=(g, yi),
                    xytext=(6, 0),
                    textcoords="offset points",
                    va="center",
                    fontsize=9,
                    color=PALETTE["text"],
                )

        # --- Right: top head-to-heads by frequency, centred on 50% ---
        ax = axes[1]
        h = h2h.head(top).iloc[::-1]
        if h.empty:
            ax.text(
                0.5,
                0.5,
                f"No head-to-heads with ≥{h2h_min_games} games",
                ha="center",
                va="center",
                color=PALETTE["muted"],
                transform=ax.transAxes,
            )
            ax.set_axis_off()
        else:
            # Always orient with the favoured side on the right.
            h = h.copy()
            h["left"] = np.where(h["a_winrate"] >= 0.5, h["b"], h["a"])
            h["right"] = np.where(h["a_winrate"] >= 0.5, h["a"], h["b"])
            h["right_wr"] = np.where(h["a_winrate"] >= 0.5, h["a_winrate"], 1 - h["a_winrate"])
            h["delta_pp"] = (h["right_wr"] - 0.5) * 100  # always positive
            labels = h["left"] + "  vs  " + h["right"]
            ax.barh(range(len(h)), h["delta_pp"], color=PALETTE["primary"], height=0.7)
            ax.axvline(0, color=PALETTE["text"], linewidth=0.8)
            ax.set_yticks(range(len(h)))
            ax.set_yticklabels(labels)
            ax.set_xlabel("Favoured side's WR over 50%  (percentage points)")
            ax.set_title(_title(f"Top head-to-heads (≥{h2h_min_games} encounters)", player))
            _subtitle(
                ax,
                "Right name = winner of the matchup. Bar length = how lopsided.",
            )
            _polish_ax(ax)
            for yi, row in h.reset_index(drop=True).iterrows():
                ax.annotate(
                    f"{int(row['games'])} games · {row['right_wr']:.0%}",
                    xy=(row["delta_pp"], yi),
                    xytext=(6, 0),
                    textcoords="offset points",
                    va="center",
                    fontsize=9,
                    color=PALETTE["text"],
                )
        fig.tight_layout()
        return fig

    # --- Per-person view ---
    solo_wr = df[df["person"] == player]["win"].mean() if not df.empty else 0.5

    # Left: this player's partners.
    ax = axes[0]
    partner_rows = duos[(duos["a"] == player) | (duos["b"] == player)].copy()
    if partner_rows.empty:
        ax.text(
            0.5,
            0.5,
            f"No duos for {player} with ≥{min_games} games",
            ha="center",
            va="center",
            color=PALETTE["muted"],
            transform=ax.transAxes,
        )
        ax.set_axis_off()
    else:
        partner_rows["partner"] = partner_rows.apply(
            lambda r: r["b"] if r["a"] == player else r["a"], axis=1
        )
        partner_rows = (
            partner_rows.sort_values("games", ascending=False)
            .head(top)
            .sort_values("winrate", ascending=True)
        )
        colours = [
            PALETTE["win"] if wr >= solo_wr else PALETTE["loss"] for wr in partner_rows["winrate"]
        ]
        ax.barh(range(len(partner_rows)), partner_rows["winrate"], color=colours, height=0.7)
        ax.axvline(
            solo_wr,
            color=PALETTE["muted"],
            linestyle="--",
            linewidth=1.0,
            label=f"solo baseline ({solo_wr:.0%})",
        )
        ax.set_yticks(range(len(partner_rows)))
        ax.set_yticklabels(partner_rows["partner"])
        ax.set_xlabel("WR same-team with partner")
        ax.set_xlim(0, 1)
        ax.set_title(_title("Duo WR by partner", player))
        _subtitle(ax, "Green = partner lifts you above baseline; red = drags below.")
        ax.legend(loc="lower right")
        _polish_ax(ax)
        for yi, (wr, n) in enumerate(
            zip(partner_rows["winrate"], partner_rows["games"], strict=False)
        ):
            ax.annotate(
                f"{wr:.0%} · n={int(n)}",
                xy=(wr, yi),
                xytext=(6, 0),
                textcoords="offset points",
                va="center",
                fontsize=9,
                color=PALETTE["text"],
            )

    # Right: this player's head-to-head record vs each opponent.
    ax = axes[1]
    h_player = h2h[(h2h["a"] == player) | (h2h["b"] == player)].copy()
    if h_player.empty:
        ax.text(
            0.5,
            0.5,
            f"No head-to-heads for {player} with ≥{h2h_min_games} games",
            ha="center",
            va="center",
            color=PALETTE["muted"],
            transform=ax.transAxes,
        )
        ax.set_axis_off()
    else:
        # Normalise: ``own_wr`` = focal player's win-rate vs this opponent.
        h_player["opponent"] = h_player.apply(
            lambda r: r["b"] if r["a"] == player else r["a"], axis=1
        )
        h_player["own_wr"] = np.where(
            h_player["a"] == player, h_player["a_winrate"], 1 - h_player["a_winrate"]
        )
        h_player = (
            h_player.sort_values("games", ascending=False)
            .head(top)
            .sort_values("own_wr", ascending=True)
        )
        colours = [PALETTE["win"] if wr >= 0.5 else PALETTE["loss"] for wr in h_player["own_wr"]]
        ax.barh(range(len(h_player)), h_player["own_wr"], color=colours, height=0.7)
        ax.axvline(0.5, color=PALETTE["muted"], linestyle="--", linewidth=1.0, label="even (50%)")
        ax.set_yticks(range(len(h_player)))
        ax.set_yticklabels(h_player["opponent"])
        ax.set_xlabel(f"{player}'s WR vs opponent")
        ax.set_xlim(0, 1)
        ax.set_title(_title("Head-to-head by opponent", player))
        _subtitle(ax, "Above 50% = you beat them more often. Bar colour = direction.")
        ax.legend(loc="lower right")
        _polish_ax(ax)
        for yi, (wr, n) in enumerate(zip(h_player["own_wr"], h_player["games"], strict=False)):
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
    fig.text(
        0.5,
        0.92,
        "Grey bars = games that month. Blue line = winrate that month (months with <5 games skipped).",
        ha="center",
        va="top",
        fontsize=10,
        color=PALETTE["muted"],
        style="italic",
    )
    fig.autofmt_xdate()
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return fig


def compute_feature_impacts(
    df: pd.DataFrame, player: str | None = None, min_games: int = 25
) -> pd.DataFrame:
    """Marginal effect of each candidate factor on win probability.

    For each feature ``f`` we split the games into "f is true" and "f is
    false" buckets and compute ``P(win | f) - P(win | ¬f)`` as the
    effect size. Sign tells the direction, magnitude how much it matters.
    Each split also gets a chi-square p-value and Wilson 95% CIs on
    each side so the caller can see which effects are statistically
    backed.

    Returns a DataFrame sorted by absolute effect size, with columns:
    feature, effect_pp, n_yes, n_no, wr_yes, wr_no, ci_yes, ci_no, p.
    """
    d = _filter_player(df, player)
    if d.empty:
        return pd.DataFrame()

    overall_wr = d["win"].mean()
    if "duo_partner_in_match" not in d.columns:
        # Cheap on-the-fly column: was there ANOTHER tracked person on
        # this same match's same team? Used as the "duo" feature.
        same_team_pairs = d[["match_id", "win", "person"]].merge(
            df[["match_id", "win", "person"]], on=["match_id", "win"]
        )
        same_team_pairs = same_team_pairs[
            same_team_pairs["person_x"] != same_team_pairs["person_y"]
        ]
        with_duo = set(same_team_pairs[["match_id", "person_x"]].itertuples(index=False, name=None))
        d = d.copy()
        d["had_tracked_duo"] = [
            (mid, p) in with_duo for mid, p in zip(d["match_id"], d["person"], strict=False)
        ]
    else:
        d = d.copy()

    # Bucket helpers — boolean masks describe one side of each split.
    kda_75 = d["kda"].quantile(0.75)
    kda_25 = d["kda"].quantile(0.25)
    most_played_champ = d["champion"].value_counts().idxmax() if not d.empty else None

    features: list[tuple[str, pd.Series]] = [
        ("High KDA (top 25%)", d["kda"] >= kda_75),
        ("Low KDA (bottom 25%)", d["kda"] <= kda_25),
        ("Short game (<25min)", d["duration_min"] < 25),
        ("Long game (≥35min)", d["duration_min"] >= 35),
        ("Late night (00-04h)", d["hour"].between(0, 4)),
        ("Evening peak (18-23h)", d["hour"].between(18, 23)),
        ("Weekend", d["dow"] >= 5),
        ("After loss streak ≥3", d["loss_streak_in"] >= 3),
        ("Back-to-back (<10m gap)", d["gap_since_prev_min"] < 10),
        ("Long break (>6h gap)", d["gap_since_prev_min"] > 360),
        ("Same-team tracked partner", d["had_tracked_duo"]),
    ]
    if most_played_champ is not None:
        features.append((f"Picked {most_played_champ}", d["champion"] == most_played_champ))

    is_macro = d["person"].nunique() > 1

    rows = []
    for name, mask in features:
        mask = mask.fillna(False).astype(bool)
        yes = d[mask]
        no = d[~mask]
        n_yes, n_no = len(yes), len(no)
        if n_yes < min_games or n_no < min_games:
            continue

        # χ² stays on raw pooled counts (the correct statistical test).
        wins_yes_pooled = int(yes["win"].sum())
        wins_no_pooled = int(no["win"].sum())
        _chi2, _df, pval = chi2_homogeneity(
            np.array([wins_yes_pooled, wins_no_pooled], dtype=float),
            np.array([n_yes, n_no], dtype=float),
        )

        if is_macro:
            # Per-person effect: each person's WR-with-feature minus
            # WR-without, averaged across people who have ≥10 games on
            # both sides. Heavy players stop dominating the headline.
            per_person_yes = yes.groupby("person")["win"].agg(["count", "mean"])
            per_person_no = no.groupby("person")["win"].agg(["count", "mean"])
            joined = per_person_yes.join(per_person_no, lsuffix="_yes", rsuffix="_no", how="inner")
            joined = joined[(joined["count_yes"] >= 10) & (joined["count_no"] >= 10)]
            if joined.empty:
                continue
            effect_pp = float((joined["mean_yes"] - joined["mean_no"]).mean() * 100)
            wr_yes = float(joined["mean_yes"].mean())
            wr_no = float(joined["mean_no"].mean())
            n_people_eff = int(len(joined))
        else:
            wr_yes = yes["win"].mean()
            wr_no = no["win"].mean()
            effect_pp = (wr_yes - wr_no) * 100
            n_people_eff = 1

        ci_yes = wilson_ci(wins_yes_pooled, n_yes)
        ci_no = wilson_ci(wins_no_pooled, n_no)
        rows.append(
            {
                "feature": name,
                "effect_pp": effect_pp,
                "n_yes": n_yes,
                "n_no": n_no,
                "wr_yes": wr_yes,
                "wr_no": wr_no,
                "ci_yes": ci_yes,
                "ci_no": ci_no,
                "p": pval,
                "overall_wr": overall_wr,
                "n_people": n_people_eff,
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.iloc[out["effect_pp"].abs().sort_values(ascending=False).index].reset_index(
        drop=True
    )


def plot_feature_impact(
    df: pd.DataFrame, player: str | None = None, min_games: int = 25, top: int = 10
) -> plt.Figure:
    """Ranked "what predicts a win?" chart.

    For every candidate factor (high KDA, weekend, after loss streak,
    same-team partner, picked your main, …) we compute the percentage-
    point shift in win rate when the factor is true vs false. Bars are
    sorted by absolute shift; statistically significant ones (p<0.05)
    are drawn solid, non-significant ones faded to grey.
    """
    impacts = compute_feature_impacts(df, player=player, min_games=min_games)
    if impacts.empty:
        return _empty_figure(f"Not enough games to compare factors (need ≥{min_games} each side)")

    impacts = impacts.head(top).iloc[::-1]  # reverse so largest sits on top
    y = np.arange(len(impacts))

    colours = []
    for _, r in impacts.iterrows():
        sig = r["p"] < 0.05
        if not sig:
            colours.append(PALETTE["neutral"])
        elif r["effect_pp"] > 0:
            colours.append(PALETTE["win"])
        else:
            colours.append(PALETTE["loss"])

    fig_h = max(4.6, len(impacts) * 0.45)
    fig, ax = plt.subplots(figsize=(13, fig_h))
    ax.barh(y, impacts["effect_pp"], color=colours, height=0.7)
    ax.axvline(0, color=PALETTE["text"], linewidth=0.8)
    ax.set_yticks(y)
    ax.set_yticklabels(impacts["feature"])
    ax.set_xlabel("Win rate when factor true MINUS when false (percentage points)")
    ax.set_title(_title("Feature impact — what actually moves your win rate?", player))
    n_people = int(impacts["n_people"].max()) if "n_people" in impacts.columns else 1
    _subtitle(
        ax,
        f"Solid bars = p<0.05 (likely real). Faded grey = noise. {_macro_label(n_people)}.",
    )
    _polish_ax(ax)

    # Annotate each row with detail: "+8.4pp · 56% vs 47% · n=420/2100 · p=0.001".
    # Wide solid bars (|effect_pp| > 5) get the label tucked inside; narrow
    # bars and faded-grey (non-significant) bars stay outside to remain
    # readable against the chart background.
    for yi, (_, r) in enumerate(impacts.iterrows()):
        label = (
            f"{r['effect_pp']:+.1f}pp  ·  {r['wr_yes']:.0%} vs {r['wr_no']:.0%}  "
            f"·  n={int(r['n_yes'])} vs {int(r['n_no'])}  ·  {_p_marker(r['p'])}"
        )
        is_grey = not (r["p"] < 0.05)
        if abs(r["effect_pp"]) > 5 and not is_grey:
            if r["effect_pp"] >= 0:
                ax.annotate(
                    label,
                    xy=(r["effect_pp"], yi),
                    xytext=(-6, 0),
                    textcoords="offset points",
                    va="center",
                    ha="right",
                    fontsize=9,
                    color="white",
                )
            else:
                ax.annotate(
                    label,
                    xy=(r["effect_pp"], yi),
                    xytext=(6, 0),
                    textcoords="offset points",
                    va="center",
                    ha="left",
                    fontsize=9,
                    color="white",
                )
        else:
            sign_pad = 1 if r["effect_pp"] >= 0 else -1
            ax.annotate(
                label,
                xy=(r["effect_pp"], yi),
                xytext=(6 * sign_pad, 0),
                textcoords="offset points",
                va="center",
                ha="left" if r["effect_pp"] >= 0 else "right",
                fontsize=9,
                color=PALETTE["text"],
            )

    # Symmetric x-axis around zero so positive/negative bars are comparable.
    max_abs = max(8, float(impacts["effect_pp"].abs().max()) + 4)
    ax.set_xlim(-max_abs, max_abs)
    fig.tight_layout()
    return fig


def _draw_card(
    ax,
    x: float,
    y: float,
    w: float,
    h: float,
    accent: str,
) -> None:
    """Render a flat 'dashboard tile' at axes-fraction (x, y, w, h).

    Three layers: subtle shadow, white rounded card, a left accent
    stripe coloured ``accent`` so the eye picks up direction (green =
    positive, red = negative, blue = neutral) without reading the
    number.
    """
    from matplotlib.patches import FancyBboxPatch

    shadow = FancyBboxPatch(
        (x + 0.003, y - 0.006),
        w,
        h,
        boxstyle="round,pad=0,rounding_size=0.012",
        facecolor="black",
        edgecolor="none",
        alpha=0.06,
        zorder=1,
        mutation_aspect=h / w,
    )
    ax.add_patch(shadow)
    body = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0,rounding_size=0.012",
        facecolor="white",
        edgecolor=PALETTE["spine"],
        linewidth=0.9,
        zorder=2,
        mutation_aspect=h / w,
    )
    ax.add_patch(body)
    stripe_w = w * 0.035
    stripe = FancyBboxPatch(
        (x, y),
        stripe_w,
        h,
        boxstyle="round,pad=0,rounding_size=0.012",
        facecolor=accent,
        edgecolor="none",
        zorder=3,
        mutation_aspect=h / stripe_w,
    )
    ax.add_patch(stripe)


def _card_text(
    ax,
    x: float,
    y: float,
    w: float,
    h: float,
    label: str,
    value: str,
    sublabel: str = "",
    value_color: str = "#222",
) -> None:
    """Lay out label / value / sublabel inside an already-drawn card."""
    pad_x = w * 0.07
    text_x = x + pad_x
    # Top label, small uppercase grey.
    ax.text(
        text_x,
        y + h - h * 0.20,
        label.upper(),
        ha="left",
        va="center",
        fontsize=10,
        color=PALETTE["muted"],
        fontweight="bold",
        zorder=4,
    )
    # Big value, vertically centred-ish.
    ax.text(
        text_x,
        y + h * 0.50,
        value,
        ha="left",
        va="center",
        fontsize=18,
        color=value_color,
        fontweight="bold",
        zorder=4,
    )
    if sublabel:
        ax.text(
            text_x,
            y + h * 0.16,
            sublabel,
            ha="left",
            va="center",
            fontsize=9,
            color=PALETTE["muted"],
            zorder=4,
        )


def _build_logistic_design(
    df: pd.DataFrame, player: str | None
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Build the design matrix for win-probability logistic regression.

    Continuous features are standardised (z-score) so their coefficients
    are comparable: each one is "log-odds shift per +1 standard deviation".
    Binary features are 0/1. When ``player is None`` we add person fixed
    effects (one-hot dummies, drop one as baseline) so the headline
    coefficients are net of "who's playing" — the biggest source of
    confounding given the 200:1 game-count spread.
    """
    d = _filter_player(df, player).copy()
    if d.empty:
        return np.empty((0, 0)), np.empty(0), []

    same_team = d[["match_id", "win", "person"]].merge(
        df[["match_id", "win", "person"]], on=["match_id", "win"]
    )
    same_team = same_team[same_team["person_x"] != same_team["person_y"]]
    with_duo = set(same_team[["match_id", "person_x"]].itertuples(index=False, name=None))
    d["had_tracked_duo"] = [
        (mid, p) in with_duo for mid, p in zip(d["match_id"], d["person"], strict=False)
    ]

    # Continuous features (standardised). Log1p the gap because it's
    # heavily right-skewed. Clip below at 0 so the first-game NaNs (no
    # previous match) don't poison the log.
    gap_filled = d["gap_since_prev_min"].fillna(d["gap_since_prev_min"].median()).fillna(60.0)
    d["log_gap_min"] = np.log1p(gap_filled.clip(lower=0.0))

    cont_features = {
        "KDA (z)": d["kda"],
        "Duration min (z)": d["duration_min"],
        "Loss streak entering (z)": d["loss_streak_in"],
        "Log gap since prev (z)": d["log_gap_min"],
    }
    bin_features = {
        "Late night (00-04h)": d["hour"].between(0, 4).astype(int),
        "Evening peak (18-23h)": d["hour"].between(18, 23).astype(int),
        "Weekend": (d["dow"] >= 5).astype(int),
        "Short game (<25 min)": (d["duration_min"] < 25).astype(int),
        "Same-team tracked partner": d["had_tracked_duo"].astype(int),
    }

    most_played = d["champion"].value_counts().head(3).index.tolist()
    for champ in most_played:
        bin_features[f"Played {champ}"] = (d["champion"] == champ).astype(int)

    cols: list[str] = []
    parts: list[np.ndarray] = []
    for name, series in cont_features.items():
        vals = pd.to_numeric(series, errors="coerce").fillna(series.median()).to_numpy(dtype=float)
        std = vals.std()
        if std < 1e-9:
            continue
        parts.append(((vals - vals.mean()) / std)[:, None])
        cols.append(name)
    for name, series in bin_features.items():
        vals = series.fillna(0).to_numpy(dtype=float)
        if vals.sum() < 20 or vals.sum() > len(vals) - 20:
            continue
        parts.append(vals[:, None])
        cols.append(name)

    # Person fixed effects (only when aggregating; baseline = most-played).
    if player is None and d["person"].nunique() > 1:
        people = d["person"].value_counts().index.tolist()
        baseline = people[0]
        for p in people[1:]:
            vals = (d["person"] == p).astype(float).to_numpy()
            if vals.sum() < 20:
                continue
            parts.append(vals[:, None])
            cols.append(f"[person] {p} vs {baseline}")

    if not parts:
        return np.empty((0, 0)), np.empty(0), []
    X = np.hstack(parts)
    y = d["win"].to_numpy(dtype=float)
    return X, y, cols


def plot_logistic_coefficients(
    df: pd.DataFrame, player: str | None = None, top: int = 12
) -> plt.Figure:
    """Logistic regression of P(win) on the candidate factors.

    Replaces the marginal "feature impact" chi-square with a proper
    multivariate model — each coefficient is the log-odds shift after
    controlling for the other features (and for "who's playing" via
    person fixed effects when in aggregate mode). Significance is the
    Wald z-test (|coef|/SE) against zero.

    Bars: log-odds units. Solid green/red if p<0.05, faded grey if not.
    Annotation: standardised coef, odds ratio, and p-value.
    """
    X, y, cols = _build_logistic_design(df, player)
    if X.size == 0 or len(np.unique(y)) < 2:
        return _empty_figure("Not enough data to fit logistic regression")

    beta, se, loglik_full = logistic_fit(X, y, l2=0.5)
    # Drop intercept + person-fixed-effect coefficients from the chart —
    # the user cares about the factor coefficients, not "is person X
    # better than baseline" (which is just their WR gap).
    rows = []
    for idx, name in enumerate(cols):
        if name.startswith("[person]"):
            continue
        coef = beta[idx + 1]  # +1 to skip intercept
        se_i = se[idx + 1]
        p = wald_pvalue(coef, se_i)
        rows.append({"name": name, "coef": coef, "se": se_i, "p": p, "or": float(np.exp(coef))})
    if not rows:
        return _empty_figure("No usable factors after filtering")

    # McFadden pseudo-R² vs intercept-only null. Analytical form: under the
    # null the MLE is p_bar = y.mean(), so loglik_null collapses to
    # n_w*log(p_bar) + n_l*log(1 - p_bar). _build_logistic_design already
    # filters degenerate y, but guard p_bar ∈ {0,1} anyway.
    p_bar = float(y.mean())
    if 0.0 < p_bar < 1.0:
        n_w = float(y.sum())
        n_l = float(len(y) - n_w)
        loglik_null = n_w * math.log(p_bar) + n_l * math.log(1.0 - p_bar)
        mcfadden_r2 = 1.0 - (loglik_full / loglik_null) if loglik_null != 0 else 0.0
    else:
        mcfadden_r2 = 0.0

    coefs_df = (
        pd.DataFrame(rows)
        .iloc[lambda f: f["coef"].abs().sort_values(ascending=False).index]
        .head(top)
        .iloc[::-1]
        .reset_index(drop=True)
    )

    fig_h = max(4.6, len(coefs_df) * 0.45)
    fig, ax = plt.subplots(figsize=(13, fig_h))
    y_pos = np.arange(len(coefs_df))

    sig_mask = coefs_df["p"] < 0.05
    colours = [
        (PALETTE["win"] if c > 0 else PALETTE["loss"]) if s else PALETTE["neutral"]
        for c, s in zip(coefs_df["coef"], sig_mask, strict=False)
    ]
    ax.barh(y_pos, coefs_df["coef"], color=colours, height=0.7)
    ax.errorbar(
        coefs_df["coef"],
        y_pos,
        xerr=1.96 * coefs_df["se"],
        fmt="none",
        ecolor=PALETTE["text"],
        capsize=3,
        linewidth=1.0,
        alpha=0.6,
    )
    ax.axvline(0, color=PALETTE["text"], linewidth=0.8)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(coefs_df["name"])
    ax.set_xlabel("Log-odds (95% CI whiskers)")

    n_people = df["person"].nunique() if player is None else 1
    title_macro = (
        f"controlling for person ({n_people} people)" if n_people > 1 else "single-person fit"
    )
    ax.set_title(_title(f"Logistic regression — {title_macro}", player))
    if mcfadden_r2 < 0.05:
        _subtitle(
            ax,
            f"McFadden pseudo-R² = {mcfadden_r2:.2%} — model explains essentially none "
            "of the variance; outcomes are dominated by factors not in the model "
            "(teammate quality, draft, etc.). "
            "Solid bars = p<0.05 (real signal). Faded grey = no evidence. "
            "Continuous features are per +1σ; binary features are vs the off state.",
        )
    else:
        _subtitle(
            ax,
            f"McFadden pseudo-R² = {mcfadden_r2:.2%}. "
            "Solid bars = p<0.05 (real signal). Faded grey = no evidence. "
            "Continuous features are per +1σ; binary features are vs the off state.",
        )
    _polish_ax(ax)

    # Annotation placement: solid (significant) coloured bars wider than
    # 0.5 log-odds get the label tucked inside the bar in white. Narrower
    # bars and faded-grey (non-significant) bars keep the label outside
    # to stay readable.
    for yi, r in coefs_df.iterrows():
        odds_ratio_pct = (r["or"] - 1) * 100
        label = (
            f"{r['coef']:+.2f}  ·  OR {r['or']:.2f} "
            f"({odds_ratio_pct:+.0f}%)  ·  {_p_marker(r['p'])}"
        )
        is_grey = not (r["p"] < 0.05)
        if abs(r["coef"]) > 0.5 and not is_grey:
            if r["coef"] >= 0:
                ax.annotate(
                    label,
                    xy=(r["coef"], yi),
                    xytext=(-6, 0),
                    textcoords="offset points",
                    va="center",
                    ha="right",
                    fontsize=9,
                    color="white",
                )
            else:
                ax.annotate(
                    label,
                    xy=(r["coef"], yi),
                    xytext=(6, 0),
                    textcoords="offset points",
                    va="center",
                    ha="left",
                    fontsize=9,
                    color="white",
                )
        else:
            sign_pad = 1 if r["coef"] >= 0 else -1
            ax.annotate(
                label,
                xy=(r["coef"], yi),
                xytext=(8 * sign_pad, 0),
                textcoords="offset points",
                va="center",
                ha="left" if r["coef"] >= 0 else "right",
                fontsize=9,
                color=PALETTE["text"],
            )

    max_abs = max(0.4, float(coefs_df["coef"].abs().max() + 1.96 * coefs_df["se"].max()) * 1.05)
    ax.set_xlim(-max_abs, max_abs)
    fig.tight_layout()
    return fig


def plot_stats_summary(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Dashboard-style headline card view.

    Nine tile cards: games, win rate (with Wilson CI), avg KDA, most-
    played champion, best day, worst hour, current streak, career trend
    (across-career pp), and a duo card (most-played duo aggregate /
    best partner per-person). Each tile has a coloured left stripe
    flagging direction so the eye reads green/red without parsing the
    number.
    """
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to summarise")

    sorted_d = d.sort_values("game_start")
    games = len(d)
    wins = int(d["win"].sum())
    wr = wins / games
    wr_lo, wr_hi = wilson_ci(wins, games)
    avg_kda = d["kda"].mean()
    avg_kd = d["kd"].mean()
    avg_dur = d["duration_min"].mean()

    champ_stats = d.groupby("champion")["win"].agg(["count", "mean"])
    most_played = champ_stats.sort_values("count", ascending=False).iloc[0]
    mp_name = most_played.name
    mp_n = int(most_played["count"])
    mp_wr = float(most_played["mean"])

    dow_stats = d.groupby("dow")["win"].agg(["count", "mean"])
    reliable_dow = dow_stats[dow_stats["count"] >= 10]
    best_dow_idx = reliable_dow["mean"].idxmax() if not reliable_dow.empty else None
    hour_stats = d.groupby("hour")["win"].agg(["count", "mean"])
    reliable_hour = hour_stats[hour_stats["count"] >= 10]
    worst_hour_idx = reliable_hour["mean"].idxmin() if not reliable_hour.empty else None

    outcomes = sorted_d["win"].tolist()
    if outcomes:
        last = outcomes[-1]
        streak = 0
        for o in reversed(outcomes):
            if o == last:
                streak += 1
            else:
                break
        streak_kind = "W" if last == 1 else "L"
    else:
        streak = 0
        streak_kind = "—"

    trend_pp_career: float | None = None
    if games >= 30:
        roll = sorted_d["win"].rolling(window=30, min_periods=10).mean().reset_index(drop=True)
        pct = pd.Series(np.linspace(0, 100, games))
        mask = roll.notna()
        if mask.sum() >= 5:
            slope_per_pct = float(np.polyfit(pct[mask], roll[mask], 1)[0])
            trend_pp_career = slope_per_pct * 100

    duos = compute_duos(df, min_games=5)
    duo_label = "Most-played duo" if player is None else "Best duo partner"
    duo_value = "—"
    duo_sublabel = "needs ≥5 same-team games"
    duo_accent = PALETTE["neutral"]
    duo_value_color = PALETTE["text"]
    if not duos.empty:
        if player is None:
            row = duos.sort_values("games", ascending=False).iloc[0]
            duo_value = f"{row['a']} + {row['b']}"
            duo_sublabel = f"{int(row['games'])} games · {row['winrate']:.0%} WR"
            duo_accent = PALETTE["win"] if row["winrate"] >= 0.5 else PALETTE["loss"]
        else:
            partners = duos[(duos["a"] == player) | (duos["b"] == player)].copy()
            if not partners.empty:
                partners["partner"] = partners.apply(
                    lambda r: r["b"] if r["a"] == player else r["a"], axis=1
                )
                row = partners.sort_values("winrate", ascending=False).iloc[0]
                duo_value = str(row["partner"])
                lift = row["winrate"] - wr
                duo_sublabel = f"{row['winrate']:.0%} WR · {int(row['games'])} games · {lift * 100:+.1f}pp vs solo"
                duo_accent = PALETTE["win"] if lift > 0 else PALETTE["loss"]
                duo_value_color = duo_accent

    # --- Render ---
    fig = plt.figure(figsize=(13, 7.0))
    ax = fig.add_subplot(111)
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    title_who = player or "all players"
    fig.suptitle(f"Stats summary — {title_who}", fontsize=18, fontweight="bold", y=0.965)

    n_people = df["person"].nunique() if player is None else 1
    if player is None:
        sub_text = (
            f"{games:,} games across {n_people} tracked people. "
            "Each card shows the aggregate headline; values are macro-averaged where applicable."
        )
    else:
        riot_accounts = d["riot_account"].nunique()
        acct_note = f" across {riot_accounts} Riot accounts" if riot_accounts > 1 else ""
        sub_text = f"{games:,} games{acct_note}. Win rates have Wilson 95% CIs."
    fig.text(
        0.5,
        0.905,
        sub_text,
        ha="center",
        fontsize=10,
        color=PALETTE["muted"],
        style="italic",
    )

    # 3x3 grid layout in axes coords. Outer margins + gap between cards.
    margin_x, margin_y = 0.035, 0.04
    gap_x, gap_y = 0.022, 0.035
    n_cols, n_rows = 3, 3
    grid_top = 0.86  # leave room for suptitle + subtitle
    grid_bottom = margin_y
    card_w = (1 - 2 * margin_x - (n_cols - 1) * gap_x) / n_cols
    card_h = (grid_top - grid_bottom - (n_rows - 1) * gap_y) / n_rows

    def cell(col: int, row: int) -> tuple[float, float]:
        # row 0 = top
        x = margin_x + col * (card_w + gap_x)
        y = grid_top - card_h - row * (card_h + gap_y)
        return x, y

    def tile(col, row, label, value, sublabel, accent, value_color=None):
        x, y = cell(col, row)
        _draw_card(ax, x, y, card_w, card_h, accent=accent)
        _card_text(
            ax,
            x,
            y,
            card_w,
            card_h,
            label=label,
            value=value,
            sublabel=sublabel,
            value_color=value_color or accent,
        )

    # Row 0
    tile(
        0,
        0,
        "Games",
        f"{games:,}",
        f"{wins} W · {games - wins} L  ·  avg {avg_dur:.1f} min",
        accent=PALETTE["primary"],
        value_color=PALETTE["text"],
    )
    wr_accent = (
        PALETTE["win"] if wr > 0.5 else (PALETTE["loss"] if wr < 0.5 else PALETTE["primary"])
    )
    tile(
        1,
        0,
        "Win rate",
        f"{wr:.1%}",
        f"95% CI {wr_lo:.0%}–{wr_hi:.0%}",
        accent=wr_accent,
    )
    tile(
        2,
        0,
        "Avg KDA",
        f"{avg_kda:.2f}",
        f"K/D ratio {avg_kd:.2f}",
        accent=PALETTE["primary"],
        value_color=PALETTE["text"],
    )

    # Row 1
    champ_accent = PALETTE["win"] if mp_wr >= 0.5 else PALETTE["loss"]
    tile(
        0,
        1,
        "Most-played champion",
        mp_name,
        f"{mp_n} games · {mp_wr:.0%} WR",
        accent=champ_accent,
    )
    if best_dow_idx is not None:
        wr_d = float(reliable_dow.loc[best_dow_idx, "mean"])
        tile(
            1,
            1,
            "Best day",
            DOW_LABELS[best_dow_idx],
            f"{wr_d:.0%} WR  ·  {(wr_d - wr) * 100:+.1f}pp vs baseline",
            accent=PALETTE["win"],
        )
    else:
        tile(
            1,
            1,
            "Best day",
            "—",
            "not enough data",
            accent=PALETTE["neutral"],
            value_color=PALETTE["muted"],
        )
    if worst_hour_idx is not None:
        wr_h = float(reliable_hour.loc[worst_hour_idx, "mean"])
        tile(
            2,
            1,
            "Worst hour",
            f"{int(worst_hour_idx):02d}:00",
            f"{wr_h:.0%} WR  ·  {(wr_h - wr) * 100:+.1f}pp vs baseline",
            accent=PALETTE["loss"],
        )
    else:
        tile(
            2,
            1,
            "Worst hour",
            "—",
            "not enough data",
            accent=PALETTE["neutral"],
            value_color=PALETTE["muted"],
        )

    # Row 2
    if streak_kind == "W":
        streak_accent = PALETTE["win"]
    elif streak_kind == "L":
        streak_accent = PALETTE["loss"]
    else:
        streak_accent = PALETTE["neutral"]
    tile(
        0,
        2,
        "Current streak",
        f"{streak}{streak_kind}",
        "most recent run of same-outcome games",
        accent=streak_accent,
    )
    if trend_pp_career is not None:
        trend_accent = PALETTE["win"] if trend_pp_career > 0 else PALETTE["loss"]
        direction = "improving" if trend_pp_career > 0 else "declining"
        tile(
            1,
            2,
            "Career trend",
            f"{trend_pp_career * 100:+.1f}pp",
            f"across career  ·  {direction}",
            accent=trend_accent,
        )
    else:
        tile(
            1,
            2,
            "Career trend",
            "—",
            "needs ≥30 games",
            accent=PALETTE["neutral"],
            value_color=PALETTE["muted"],
        )
    tile(
        2,
        2,
        duo_label,
        duo_value,
        duo_sublabel,
        accent=duo_accent,
        value_color=duo_value_color,
    )

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
    ("01_stats_summary", plot_stats_summary),
    ("02_player_comparison", plot_player_comparison),
    ("03_logistic_coefficients", plot_logistic_coefficients),
    ("04_feature_impact", plot_feature_impact),
    ("05_activity_over_time", plot_activity_over_time),
    ("06_rank_trajectory", plot_rank_trajectory),
    ("07_lp_economics", plot_lp_economics),
    ("08_cumulative_winrate", plot_cumulative_winrate),
    ("09_player_progression", plot_player_progression),
    ("10_kda_vs_outcome", plot_kda_vs_outcome),
    ("11_duration_vs_outcome", plot_duration_vs_outcome),
    ("12_champion_winrate", plot_champion_winrate),
    ("13_champion_picks", plot_champion_picks),
    ("14_champion_learning_curve", plot_champion_learning_curve),
    ("15_hour_of_day", plot_hour_of_day),
    ("16_day_of_week", plot_day_of_week),
    ("17_hour_dow_heatmap", plot_hour_dow_heatmap),
    ("18_streak_recovery", plot_streak_recovery),
    ("19_time_since_prev", plot_time_since_prev),
    ("20_session_analysis", plot_session_analysis),
    ("21_duo_winrate", plot_duo_winrate),
]
