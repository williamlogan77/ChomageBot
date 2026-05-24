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

# Maximum gap between consecutive league_history snapshots (per person)
# for which we trust the diff as a single ranked game. The bot's
# post_ranks loop normally polls every 120s, so back-to-back snapshots
# are minutes apart; anything beyond 2 hours straddles a bot outage
# (see the late-2024 -> early-2026 gap caused by the silent task-loop
# freeze fixed in ecfff72) and the snapshot pair would otherwise
# fabricate a single "match" worth hundreds of LP.
MAX_INTER_SNAPSHOT_MIN = 120

# Threshold (in hours) for breaking the rank-trajectory line and
# shading the gap region. league_history only inserts a row on LP
# change, so quiet weeks would otherwise read as "gaps". Set high
# enough that normal player inactivity (vacations, exam weeks, taking
# a break from ranked) does NOT flag — only true bot outages do.
# 30 days isolates the 2024-11 -> 2026-01 task-loop freeze cleanly.
RANK_GAP_HOURS = 24 * 30


# Canonical role display order — used for x-axis ordering of role charts.
ROLE_ORDER = ["TOP", "JUNGLE", "MID", "ADC", "SUPPORT"]


# Heuristic primary-role mapping; some champs flex (e.g. Sett: top/support — we say TOP).
# Keys match the Riot internal `dataName` (Chogath, Leblanc, FiddleSticks, MonkeyKing, ...)
# as stored in match_stats.champion. Champions not present here resolve to "UNKNOWN" and
# are skipped by the per-role winrate chart.
CHAMPION_ROLES: dict[str, str] = {
    # TOP
    "Aatrox": "TOP",
    "Ambessa": "TOP",
    "Camille": "TOP",
    "Chogath": "TOP",
    "Darius": "TOP",
    "DrMundo": "TOP",
    "Fiora": "TOP",
    "Gangplank": "TOP",
    "Garen": "TOP",
    "Gnar": "TOP",
    "Gwen": "TOP",
    "Illaoi": "TOP",
    "Irelia": "TOP",
    "Jax": "TOP",
    "Jayce": "TOP",
    "Kayle": "TOP",
    "Kennen": "TOP",
    "Kled": "TOP",
    "KSante": "TOP",
    "Malphite": "TOP",
    "Mordekaiser": "TOP",
    "Nasus": "TOP",
    "Ornn": "TOP",
    "Pantheon": "TOP",
    "Poppy": "TOP",
    "Quinn": "TOP",
    "Renekton": "TOP",
    "Riven": "TOP",
    "Rumble": "TOP",
    "Sett": "TOP",
    "Shen": "TOP",
    "Singed": "TOP",
    "Sion": "TOP",
    "Teemo": "TOP",
    "Tryndamere": "TOP",
    "Urgot": "TOP",
    "Vladimir": "TOP",
    "Volibear": "TOP",
    "Warwick": "TOP",
    "Yasuo": "TOP",
    "Yorick": "TOP",
    # JUNGLE
    "Amumu": "JUNGLE",
    "Belveth": "JUNGLE",
    "Briar": "JUNGLE",
    "Diana": "JUNGLE",
    "Ekko": "JUNGLE",
    "Elise": "JUNGLE",
    "Evelynn": "JUNGLE",
    "FiddleSticks": "JUNGLE",
    "Gragas": "JUNGLE",
    "Graves": "JUNGLE",
    "Hecarim": "JUNGLE",
    "Ivern": "JUNGLE",
    "JarvanIV": "JUNGLE",
    "Karthus": "JUNGLE",
    "Kayn": "JUNGLE",
    "Khazix": "JUNGLE",
    "Kindred": "JUNGLE",
    "LeeSin": "JUNGLE",
    "Lillia": "JUNGLE",
    "MasterYi": "JUNGLE",
    "MonkeyKing": "JUNGLE",
    "Naafiri": "JUNGLE",
    "Nidalee": "JUNGLE",
    "Nocturne": "JUNGLE",
    "Nunu": "JUNGLE",
    "Olaf": "JUNGLE",
    "Rammus": "JUNGLE",
    "RekSai": "JUNGLE",
    "Rengar": "JUNGLE",
    "Sejuani": "JUNGLE",
    "Shaco": "JUNGLE",
    "Shyvana": "JUNGLE",
    "Skarner": "JUNGLE",
    "Trundle": "JUNGLE",
    "Udyr": "JUNGLE",
    "Vi": "JUNGLE",
    "Viego": "JUNGLE",
    "XinZhao": "JUNGLE",
    "Zac": "JUNGLE",
    # MID
    "Ahri": "MID",
    "Akali": "MID",
    "Akshan": "MID",
    "Anivia": "MID",
    "Annie": "MID",
    "AurelionSol": "MID",
    "Aurora": "MID",
    "Azir": "MID",
    "Cassiopeia": "MID",
    "Fizz": "MID",
    "Galio": "MID",
    "Heimerdinger": "MID",
    "Hwei": "MID",
    "Kassadin": "MID",
    "Katarina": "MID",
    "Leblanc": "MID",
    "Lissandra": "MID",
    "Lux": "MID",
    "Malzahar": "MID",
    "Neeko": "MID",
    "Orianna": "MID",
    "Qiyana": "MID",
    "Ryze": "MID",
    "Sylas": "MID",
    "Syndra": "MID",
    "Taliyah": "MID",
    "Talon": "MID",
    "TwistedFate": "MID",
    "Veigar": "MID",
    "Velkoz": "MID",
    "Vex": "MID",
    "Viktor": "MID",
    "Xerath": "MID",
    "Yone": "MID",
    "Zed": "MID",
    "Ziggs": "MID",
    "Zoe": "MID",
    # ADC
    "Aphelios": "ADC",
    "Ashe": "ADC",
    "Caitlyn": "ADC",
    "Corki": "ADC",
    "Draven": "ADC",
    "Ezreal": "ADC",
    "Jhin": "ADC",
    "Jinx": "ADC",
    "Kaisa": "ADC",
    "Kalista": "ADC",
    "KogMaw": "ADC",
    "Lucian": "ADC",
    "MissFortune": "ADC",
    "Nilah": "ADC",
    "Samira": "ADC",
    "Senna": "ADC",
    "Sivir": "ADC",
    "Smolder": "ADC",
    "Tristana": "ADC",
    "Twitch": "ADC",
    "Varus": "ADC",
    "Vayne": "ADC",
    "Xayah": "ADC",
    "Yunara": "ADC",
    "Zeri": "ADC",
    # SUPPORT
    "Alistar": "SUPPORT",
    "Bard": "SUPPORT",
    "Blitzcrank": "SUPPORT",
    "Brand": "SUPPORT",
    "Braum": "SUPPORT",
    "Janna": "SUPPORT",
    "Karma": "SUPPORT",
    "Leona": "SUPPORT",
    "Lulu": "SUPPORT",
    "Maokai": "SUPPORT",
    "Mel": "SUPPORT",
    "Milio": "SUPPORT",
    "Morgana": "SUPPORT",
    "Nami": "SUPPORT",
    "Nautilus": "SUPPORT",
    "Pyke": "SUPPORT",
    "Rakan": "SUPPORT",
    "Rell": "SUPPORT",
    "Renata": "SUPPORT",
    "Seraphine": "SUPPORT",
    "Sona": "SUPPORT",
    "Soraka": "SUPPORT",
    "Swain": "SUPPORT",
    "TahmKench": "SUPPORT",
    "Taric": "SUPPORT",
    "Thresh": "SUPPORT",
    "Yuumi": "SUPPORT",
    "Zilean": "SUPPORT",
    "Zyra": "SUPPORT",
}


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

# Soft, grey, capless whisker style for every ax.errorbar call.
# Hard black caps + thick lines were stealing visual weight from the bar
# fills they're meant to annotate; muted grey + no caps recedes correctly.
WHISKER_STYLE = {
    "fmt": "none",
    "capsize": 0,
    "ecolor": PALETTE["muted"],
    "elinewidth": 0.9,
    "alpha": 0.45,
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
    """Italic caption immediately under the (already-set) title.

    Tells the reader what to look at — every plot should call this once
    so an audience that's never seen the chart can read it cold.

    Long captions are auto-wrapped to the axes width (not the figure
    width — matplotlib's wrap=True default would spill across adjacent
    panels). The title's pad scales with the rendered line count so the
    caption always clears the title without manual tuning.
    """
    title = ax.get_title()
    txt = ax.text(
        0.0,
        1.01,
        text,
        transform=ax.transAxes,
        ha="left",
        va="bottom",
        fontsize=10,
        color=PALETTE["muted"],
        style="italic",
        wrap=True,
    )
    # Clip wrap to the axes width — wrap=True alone wraps at the figure
    # bbox, which leaks past the panel boundary on multi-panel charts.
    txt._get_wrap_line_width = lambda: ax.bbox.width
    # Pad the title by the rendered visual height of the wrapped caption,
    # so a 1-line caption sits compact and a 3-line wrapped caption still
    # clears the title. The text is va="bottom"-anchored at y=1.01 and
    # grows UPWARD, so pad must exceed the total wrapped text height in
    # points (~12pt per line including leading) or the title collides.
    # Falls back to logical line count if the renderer isn't ready.
    if title:
        n_visual = _wrapped_line_count(txt, ax)
        ax.set_title(title, pad=14 + 13 * n_visual)


def _wrapped_line_count(txt, ax) -> int:
    """Visual line count for an already-placed wrap=True Text artist.

    Uses the figure renderer to compute the wrapped pixel height divided
    by the unwrapped single-line pixel height. Sidesteps having to
    re-implement matplotlib's word-break heuristic. Returns the logical
    line count (\\n + 1) as a safe lower bound if the renderer call fails.
    """
    raw = txt.get_text()
    logical_lines = raw.count("\n") + 1
    try:
        renderer = ax.figure.canvas.get_renderer()
        # Total wrapped height ÷ unwrapped single-line height = visual rows.
        wrapped_bbox = txt.get_window_extent(renderer=renderer)
        single = ax.text(0.0, 0.0, "Ag", transform=ax.transAxes, fontsize=10, style="italic")
        single_bbox = single.get_window_extent(renderer=renderer)
        single.remove()
        if single_bbox.height <= 0:
            return logical_lines
        return max(logical_lines, int(round(wrapped_bbox.height / single_bbox.height)))
    except Exception:
        return logical_lines


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


def bh_adjust(pvalues: list[float]) -> list[float]:
    """Benjamini-Hochberg FDR-adjusted p-values.

    Each adjusted p = min(1, p * m / rank_when_sorted_ascending), then
    enforced monotonic by sweeping from largest p downward. Controls the
    expected proportion of false positives among declared-significant
    findings at the threshold used (q < α). The right knob to turn for
    the "tested 10 features, one came out 'significant' by chance" case.
    """
    p = np.asarray(pvalues, dtype=float)
    n = len(p)
    if n == 0:
        return []
    order = np.argsort(p)
    ranked = p[order]
    adj = ranked * n / np.arange(1, n + 1)
    # Monotonicity sweep from highest-rank down so q-values never decrease
    # as raw p increases — the standard BH step-up adjustment.
    for i in range(n - 2, -1, -1):
        adj[i] = min(adj[i], adj[i + 1])
    adj = np.clip(adj, 0.0, 1.0)
    out = np.empty(n, dtype=float)
    out[order] = adj
    return out.tolist()


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


def _factors_verdict(r2: float) -> str:
    """Plain-English verdict on how much the model's factors explain.

    Drives the subtitle of the logistic-regression chart so the reader
    knows whether the numbers above represent a real driver of outcomes
    or are statistical decoration on top of variance the model never had
    a shot at (teammates, draft, matchmaking).
    """
    if r2 < 0.01:
        return (
            "Factors barely move the needle — outcomes are dominated by "
            "teammates / draft / matchmaking."
        )
    if r2 < 0.05:
        return (
            "Factors explain a small but real slice — most variance is still " "outside the model."
        )
    if r2 < 0.15:
        return "Factors are a meaningful driver — but luck and teammates still dominate."
    return "Factors are a strong driver of outcomes."


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
    df["role"] = df["champion"].map(CHAMPION_ROLES).fillna("UNKNOWN")

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
    df["win_streak_in"] = df.groupby("person")["win"].transform(_win_streak_entering)
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
    ranks["prev_timestamp"] = grp["timestamp"].shift(1)
    ranks["dw"] = ranks["wins"] - ranks["prev_wins"]
    ranks["dl"] = ranks["losses"] - ranks["prev_losses"]
    ranks["delta_score"] = ranks["rank_score"] - ranks["prev_score"]
    ranks["inter_snapshot_min"] = (
        ranks["timestamp"] - ranks["prev_timestamp"]
    ).dt.total_seconds() / 60
    # Single-game events only. dw or dl can occasionally be NaN on the
    # first row per person; drop those.
    one_game = ranks.dropna(subset=["dw", "dl", "delta_score", "inter_snapshot_min"])
    # Drop snapshot pairs that straddle a bot outage: a >2h gap means
    # we missed N intervening games and the LP delta is a sum, not a
    # per-game value.
    one_game = one_game[one_game["inter_snapshot_min"] <= MAX_INTER_SNAPSHOT_MIN]
    one_game = one_game[(one_game["dw"] + one_game["dl"]) == 1]
    one_game = one_game.copy()
    one_game["outcome"] = np.where(one_game["dw"] == 1, "win", "loss")
    return one_game[["person", "timestamp", "outcome", "delta_score"]].reset_index(drop=True)


def compute_tier_at_match(df: pd.DataFrame, history_df: pd.DataFrame) -> pd.DataFrame:
    """For each match in ``df``, stamp the player's visible tier+division
    at game-start by joining against ``history_df`` (one row per LP poll).

    Both frames are keyed by ``person`` rather than ``puuid`` — ``load_rank_history``
    already collapses Riot accounts to the owning Discord person, and the
    matches frame does the same. A multi-account player gets the latest
    rank snapshot from any of their accounts at the time of the match.

    UNRANKED is filled for matches that pre-date any league_history row
    for that person (early backfilled match_stats with no rank coverage yet).
    """
    if df.empty:
        out = df.copy()
        out["tier"] = pd.Series(dtype=object)
        out["division"] = pd.Series(dtype=object)
        return out

    # merge_asof needs both frames globally sorted by the ``on`` key.
    # Don't trust the caller's ordering — load_matches sorts by
    # (person, game_start), which breaks the global ``game_start`` order.
    left = df.sort_values("game_start").reset_index(drop=True)
    if history_df.empty:
        left["tier"] = "UNRANKED"
        left["division"] = None
        return left
    right = (
        history_df[["person", "timestamp", "tier", "division"]]
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    merged = pd.merge_asof(
        left,
        right,
        left_on="game_start",
        right_on="timestamp",
        by="person",
        direction="backward",
    )
    merged["tier"] = merged["tier"].fillna("UNRANKED")
    return merged.drop(columns=["timestamp"])


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


def _win_streak_entering(wins: pd.Series) -> pd.Series:
    """Mirror of ``_loss_streak_entering`` — count of consecutive wins
    immediately PRECEDING each row. A loss resets the streak; the first
    game's value is 0.
    """
    out = np.zeros(len(wins), dtype=int)
    streak = 0
    for i, w in enumerate(wins.values):
        out[i] = streak
        streak = streak + 1 if w == 1 else 0
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


def _is_aggregate(player: str | None) -> bool:
    """True when the selection means "no filter" (all players)."""
    return player is None or player in ("", "all", "__all__")


def _display_label(player: str | None) -> str | None:
    """Strip the dropdown prefix so we can show the bare name in a UI label."""
    if _is_aggregate(player):
        return None
    if player.startswith("account:"):
        return player[len("account:") :]
    if player.startswith("person:"):
        return player[len("person:") :]
    return player


def _title(base: str, player: str | None) -> str:
    label = _display_label(player)
    return base if label is None else f"{base}  ·  {label}"


def _filter_player(df: pd.DataFrame, player: str | None) -> pd.DataFrame:
    """Filter rows to one Discord person OR one Riot account.

    ``player`` is a prefixed dropdown key:
      - aggregate sentinels (None / "" / "all" / "__all__") → unfiltered
      - "person:<name>" → df[df["person"] == name]
      - "account:<name>" → df[df["riot_account"] == name]
      - bare name (legacy) → person match
    """
    if _is_aggregate(player):
        return df
    if player.startswith("account:"):
        return df[df["riot_account"] == player[len("account:") :]]
    if player.startswith("person:"):
        return df[df["person"] == player[len("person:") :]]
    return df[df["person"] == player]


def _resolve_person(df: pd.DataFrame, player: str | None) -> str | None:
    """Map any dropdown key to its owning ``person`` name (or None for aggregate).

    Rank history, LP events and duo/h2h data are person-keyed, so an
    ``account:<X>`` selection has to fall back to the person that owns
    that Riot account when those charts run.
    """
    if _is_aggregate(player):
        return None
    if player.startswith("account:"):
        acct = player[len("account:") :]
        row = df[df["riot_account"] == acct]
        return str(row["person"].iloc[0]) if not row.empty else None
    if player.startswith("person:"):
        return player[len("person:") :]
    return player


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
            **WHISKER_STYLE,
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
            **WHISKER_STYLE,
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
            **WHISKER_STYLE,
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

    if _is_aggregate(player):
        baseline_wr = float(d["win"].mean())
        baseline_label = f"group baseline ({baseline_wr:.0%})"
    else:
        baseline_wr = float(d["win"].mean())
        baseline_label = f"{_display_label(player)}'s baseline ({baseline_wr:.0%})"

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

    # Per-player callout — highlight the top confident "play this more" champ.
    # Skipped in aggregate mode (no single player to advise) and when no
    # confident-positive pick exists.
    if not _is_aggregate(player):
        top_winners = picks[picks["confident"] & (picks["delta_pp"] > 0)]
        if not top_winners.empty:
            top_row = top_winners.iloc[-1]
            top_idx = list(picks.index).index(top_row.name)
            ax.annotate(
                "Play this more — confident lift",
                xy=(top_row["delta_pp"], top_idx),
                xytext=(
                    top_row["delta_pp"] + 4,
                    top_idx - 0.8 if top_idx > 1 else top_idx + 0.8,
                ),
                fontsize=9,
                color=PALETTE["win"],
                arrowprops={
                    "arrowstyle": "->",
                    "color": PALETTE["win"],
                    "alpha": 0.7,
                    "lw": 1.2,
                },
                bbox={
                    "facecolor": "white",
                    "alpha": 0.9,
                    "edgecolor": PALETTE["win"],
                    "linewidth": 0.9,
                    "pad": 4,
                },
            )

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
        "Overall WR (shrunk)",
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

    # Group baseline anchors the Beta prior on every player's WR so small
    # samples (e.g. vyce1 ≈10 games) don't produce wild z-scores in the
    # column heatmap. prior_strength=30 ≈ half a typical career sample.
    group_baseline = float(df["win"].mean())

    rows = []
    for person in people:
        sub = df[df["person"] == person]
        n_games = len(sub)

        # Overall WR — Bayesian-shrunk toward the group baseline so a
        # 10-game outlier doesn't dominate the column z-score.
        wr_overall = bayesian_shrunk_wr(
            int(sub["win"].sum()), n_games, group_baseline, prior_strength=30
        )

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

    # Add a row of vertical breathing room when any person label is long
    # (>18 chars including the "(N games)" suffix), to keep angled x-axis
    # labels from clipping into the bottom of the heatmap.
    y_labels = [f"{p}  ({int(people_games[p])} games)" for p in people]
    has_long_label = any(len(lbl) > 18 for lbl in y_labels)
    row_h = 0.55 if has_long_label else 0.5
    fig, ax = plt.subplots(figsize=(13, max(4, len(people) * row_h + 2.5)))
    cmap = plt.get_cmap("RdYlGn").copy()
    cmap.set_bad("#f3f4f6")
    masked_z = np.ma.masked_invalid(z)
    im = ax.imshow(masked_z, aspect="auto", cmap=cmap, vmin=-1.5, vmax=1.5)

    label_fontsize = 9 if has_long_label else 10
    ax.set_yticks(np.arange(len(people)))
    ax.set_yticklabels(y_labels, fontsize=label_fontsize)
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
            ax.text(j, i, text, ha="center", va="center", fontsize=8, color=color, clip_on=True)

    focal_person = _resolve_person(df, player)
    if focal_person is not None and focal_person in people:
        idx = people.index(focal_person)
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


def plot_actions_card(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Prescriptive 'what should you DO' card grid — per player.

    Six tiles in a 3x2 grid. Each tile distils one corroborated finding
    into a single concrete action: play / drop a champ, stop at N losses,
    MMR verdict, best duo partner, hour to avoid. Green stripe = positive
    action with statistical backing, red = negative warning, grey = no
    signal strong enough to act on.

    Aggregate view is intentionally empty — prescriptions only make sense
    per-person, and the per-person card already shows the headline
    actions for that player.
    """
    if _is_aggregate(player):
        return _empty_figure("Pick a player from the dropdown — actions are per-person.")

    d = _filter_player(df, player)
    label = _display_label(player) or "this player"
    if d.empty:
        return _empty_figure(f"No games for {label}")

    baseline_wr = float(d["win"].mean())
    games_total = len(d)
    focal_person = _resolve_person(df, player)

    # --- Tile 1 + 2: Champion picks ---------------------------------------
    champ_pick_value = "—"
    champ_pick_sub = "no champion with confident lift"
    champ_pick_accent = PALETTE["neutral"]
    champ_pick_color = PALETTE["muted"]
    champ_drop_value = "—"
    champ_drop_sub = "no champion with confident drag"
    champ_drop_accent = PALETTE["neutral"]
    champ_drop_color = PALETTE["muted"]

    champ_stats = d.groupby("champion")["win"].agg(["count", "sum", "mean"])
    champ_stats = champ_stats[champ_stats["count"] >= 5]
    if not champ_stats.empty:
        champ_stats = champ_stats.rename(columns={"count": "n", "sum": "wins", "mean": "raw_wr"})
        champ_stats["shrunk_wr"] = [
            bayesian_shrunk_wr(int(w), int(n), baseline_wr, 10.0)
            for w, n in zip(champ_stats["wins"], champ_stats["n"], strict=False)
        ]
        cis = [
            wilson_ci(int(w), int(n))
            for w, n in zip(champ_stats["wins"], champ_stats["n"], strict=False)
        ]
        champ_stats["ci_lo"] = [c[0] for c in cis]
        champ_stats["ci_hi"] = [c[1] for c in cis]
        champ_stats["delta_pp"] = (champ_stats["shrunk_wr"] - baseline_wr) * 100

        confident_up = champ_stats[
            (champ_stats["ci_lo"] > baseline_wr) & (champ_stats["delta_pp"] > 0)
        ].sort_values("delta_pp", ascending=False)
        if not confident_up.empty:
            top_row = confident_up.iloc[0]
            champ_pick_value = str(top_row.name)
            champ_pick_sub = f"+{top_row['delta_pp']:.1f}pp lift  ·  n={int(top_row['n'])} games"
            champ_pick_accent = PALETTE["win"]
            champ_pick_color = PALETTE["win"]

        confident_down = champ_stats[
            (champ_stats["ci_hi"] < baseline_wr) & (champ_stats["delta_pp"] < 0)
        ].sort_values("delta_pp", ascending=True)
        if not confident_down.empty:
            bot_row = confident_down.iloc[0]
            champ_drop_value = str(bot_row.name)
            champ_drop_sub = f"{bot_row['delta_pp']:.1f}pp lift  ·  n={int(bot_row['n'])} games"
            champ_drop_accent = PALETTE["loss"]
            champ_drop_color = PALETTE["loss"]

    # --- Tile 3: Stop at N losses ----------------------------------------
    # Scan s = 2..6, find smallest s where Wilson CI of P(win|s prior
    # losses) excludes 0.50. Require >=5 observations at that streak
    # length so a single fluke L doesn't trigger a degenerate CI.
    stop_value = "no signal"
    stop_sub = "streak independence holds for you"
    stop_accent = PALETTE["neutral"]
    stop_color = PALETTE["muted"]
    streak_in = d["loss_streak_in"].astype(int)
    for s in range(2, 7):
        mask = streak_in == s
        n_s = int(mask.sum())
        if n_s < 5:
            continue
        wins_s = int(d.loc[mask, "win"].sum())
        lo, hi = wilson_ci(wins_s, n_s)
        if lo > 0.5 or hi < 0.5:
            stop_value = f"{s}L"
            stop_sub = f"CI {lo:.0%}-{hi:.0%}  ·  n={n_s}"
            stop_accent = PALETTE["accent_orange"] if hi < 0.5 else PALETTE["win"]
            stop_color = stop_accent
            break

    # --- Tile 4: MMR verdict ---------------------------------------------
    mmr_value = "—"
    mmr_sub = "no LP events recorded"
    mmr_accent = PALETTE["neutral"]
    mmr_color = PALETTE["muted"]
    try:
        events = compute_lp_events(DEFAULT_DB)
    except Exception:
        events = pd.DataFrame()
    if focal_person and not events.empty:
        sub = events[events["person"] == focal_person]
        if not sub.empty:
            n_evt = len(sub)
            net_per_100 = float(sub["delta_score"].sum() / n_evt * 100)
            if net_per_100 > 50:
                verdict = "climbing fast"
                mmr_accent = PALETTE["win"]
            elif net_per_100 > 5:
                verdict = "climbing"
                mmr_accent = PALETTE["win"]
            elif net_per_100 < -50:
                verdict = "falling fast"
                mmr_accent = PALETTE["loss"]
            elif net_per_100 < -5:
                verdict = "falling"
                mmr_accent = PALETTE["loss"]
            else:
                verdict = "treading water"
                mmr_accent = PALETTE["primary"]
            mmr_value = f"{net_per_100:+.0f}/100"
            mmr_sub = f"{verdict}  ·  n={n_evt} LP events"
            mmr_color = mmr_accent

    # --- Tile 5: Duo partner ---------------------------------------------
    duo_value = "no clear lift"
    duo_sub = "solo or random partners"
    duo_accent = PALETTE["neutral"]
    duo_color = PALETTE["muted"]
    if focal_person:
        duos = compute_duos(df, min_games=5)
        if not duos.empty:
            partners = duos[(duos["a"] == focal_person) | (duos["b"] == focal_person)].copy()
            if not partners.empty:
                partners["partner"] = partners.apply(
                    lambda r: r["b"] if r["a"] == focal_person else r["a"], axis=1
                )
                partners["lift"] = partners["winrate"] - baseline_wr
                qualified = partners[(partners["lift"] > 0.05) & (partners["games"] >= 10)]
                if not qualified.empty:
                    best = qualified.sort_values("winrate", ascending=False).iloc[0]
                    duo_value = str(best["partner"])
                    duo_sub = f"+{best['lift'] * 100:.1f}pp lift  ·  n={int(best['games'])} games"
                    duo_accent = PALETTE["win"]
                    duo_color = PALETTE["win"]

    # --- Tile 6: Avoid hour ----------------------------------------------
    hour_value = "no bad hours"
    hour_sub = "all hours within noise"
    hour_accent = PALETTE["neutral"]
    hour_color = PALETTE["muted"]
    hour_stats = d.groupby("hour")["win"].agg(["count", "sum", "mean"])
    hour_stats = hour_stats[hour_stats["count"] >= 10]
    if not hour_stats.empty:
        flagged = []
        for hr, row in hour_stats.iterrows():
            lo, hi = wilson_ci(int(row["sum"]), int(row["count"]))
            if hi < baseline_wr:
                flagged.append((int(hr), float(row["mean"]), lo, hi, int(row["count"])))
        if flagged:
            # Worst hour = lowest WR among flagged.
            flagged.sort(key=lambda t: t[1])
            hr, wr_h, _lo, _hi, n_h = flagged[0]
            hour_value = f"{hr:02d}:00"
            hour_sub = f"{wr_h:.0%} WR vs {baseline_wr:.0%}  ·  CI excludes baseline  ·  n={n_h}"
            hour_accent = PALETTE["loss"]
            hour_color = PALETTE["loss"]

    # --- Render -----------------------------------------------------------
    fig = plt.figure(figsize=(13, 6.6))
    ax = fig.add_subplot(111)
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    fig.suptitle(
        f"Actions for {label} — what the data says to do",
        fontsize=18,
        fontweight="bold",
        y=0.965,
    )
    fig.text(
        0.5,
        0.905,
        f"{games_total:,} games  ·  baseline WR {baseline_wr:.0%}. "
        "Green = confident positive action, red = confident warning, grey = no signal.",
        ha="center",
        fontsize=10,
        color=PALETTE["muted"],
        style="italic",
    )

    margin_x, margin_y = 0.035, 0.04
    gap_x, gap_y = 0.022, 0.035
    n_cols, n_rows = 3, 2
    grid_top = 0.86
    grid_bottom = margin_y
    card_w = (1 - 2 * margin_x - (n_cols - 1) * gap_x) / n_cols
    card_h = (grid_top - grid_bottom - (n_rows - 1) * gap_y) / n_rows

    def cell(col: int, row: int) -> tuple[float, float]:
        x = margin_x + col * (card_w + gap_x)
        y = grid_top - card_h - row * (card_h + gap_y)
        return x, y

    def tile(col, row, label_text, value, sublabel, accent, value_color):
        x, y = cell(col, row)
        _draw_card(ax, x, y, card_w, card_h, accent=accent)
        _card_text(
            ax,
            x,
            y,
            card_w,
            card_h,
            label=label_text,
            value=value,
            sublabel=sublabel,
            value_color=value_color,
        )

    tile(
        0,
        0,
        "Play this champ more",
        champ_pick_value,
        champ_pick_sub,
        champ_pick_accent,
        champ_pick_color,
    )
    tile(
        1,
        0,
        "Drop this champ",
        champ_drop_value,
        champ_drop_sub,
        champ_drop_accent,
        champ_drop_color,
    )
    tile(2, 0, "Stop at N losses", stop_value, stop_sub, stop_accent, stop_color)
    tile(0, 1, "MMR verdict", mmr_value, mmr_sub, mmr_accent, mmr_color)
    tile(1, 1, "Duo with", duo_value, duo_sub, duo_accent, duo_color)
    tile(2, 1, "Avoid this hour", hour_value, hour_sub, hour_accent, hour_color)

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


#: Left-panel bar buckets — signed-streak ordering from "long win streak" on
#: the far left, through neutral, to "long loss streak" on the far right.
#: Sign encoding: negative = entering win streak, 0 = no prior streak
#: (first game of the data), positive = entering loss streak. The chi²
#: test runs across all 11 buckets as one partition.
_SIGNED_STREAK_LABELS = [
    "W6+",
    "W5-4",
    "W3",
    "W2",
    "W1",
    "0 (start)",
    "L1",
    "L2",
    "L3",
    "L4-5",
    "L6+",
]

#: Survival curves on the right panel — same seed lengths for both sides
#: so the comparison is direct. s=10 is sparse but kept for symmetry; the
#: legend shows the denominator so the reader can judge.
_SURVIVAL_SEEDS = (1, 2, 3, 5, 10)


def _signed_streak_bucket(loss_in: int, win_in: int) -> str:
    """Map (loss_streak_in, win_streak_in) → one of the 11 labelled buckets.

    By construction at most one of the two counters is positive per row
    (a win resets loss, a loss resets win). Both zero only on the very
    first game we have for that person.
    """
    if win_in >= 6:
        return "W6+"
    if win_in >= 4:
        return "W5-4"
    if win_in == 3:
        return "W3"
    if win_in == 2:
        return "W2"
    if win_in == 1:
        return "W1"
    if loss_in == 0:
        return "0 (start)"
    if loss_in == 1:
        return "L1"
    if loss_in == 2:
        return "L2"
    if loss_in == 3:
        return "L3"
    if loss_in <= 5:
        return "L4-5"
    return "L6+"


def _maximal_streak_lengths(wins_by_person: pd.Series, kind: str) -> np.ndarray:
    """Return the lengths of every maximal run of a given outcome across
    all people in ``wins_by_person`` (multi-index: person, time-order).

    ``kind='loss'`` collects runs of 0s, ``kind='win'`` collects runs of 1s.
    Used to compute conditional survival ``S_s(k) = P(L ≥ s+k | L ≥ s)``.
    Counting maximal runs avoids the double-counting you'd get from a
    per-row treatment.
    """
    target = 0 if kind == "loss" else 1
    lengths: list[int] = []
    for _person, ws in wins_by_person.groupby(level=0, sort=False):
        cur = 0
        for w in ws.to_numpy():
            if int(w) == target:
                cur += 1
            else:
                if cur > 0:
                    lengths.append(cur)
                cur = 0
        if cur > 0:
            lengths.append(cur)
    return np.asarray(lengths, dtype=int)


def _survival_curve(lengths: np.ndarray, s: int, max_k: int) -> tuple[np.ndarray, int]:
    """For seed ``s``, return ``(survival[k] for k=0..max_k], denom)`` where
    survival[k] = P(streak length ≥ s+k | length ≥ s). denom is the count
    of streaks meeting the seed condition — labelled in the legend so the
    viewer can judge sample size."""
    qualifying = lengths[lengths >= s]
    denom = int(qualifying.size)
    if denom == 0:
        return (np.full(max_k + 1, np.nan), 0)
    surv = np.array(
        [float((qualifying >= s + k).sum()) / denom for k in range(max_k + 1)],
        dtype=float,
    )
    return (surv, denom)


def _bootstrap_survival_ci(
    lengths: np.ndarray,
    s: int,
    max_k: int,
    *,
    n_boot: int = 1000,
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray]:
    """Bootstrap a 95% CI band for the survival curve at seed ``s``.

    Resampling unit is the streak (not the game) — that's what the survival
    function is defined over. Returns ``(ci_lo[k], ci_hi[k])`` arrays of
    length ``max_k+1``; if no qualifying streaks exist the bands are NaN.
    """
    qualifying = lengths[lengths >= s]
    denom = int(qualifying.size)
    nan_band = np.full(max_k + 1, np.nan)
    if denom == 0:
        return (nan_band, nan_band)
    rng = np.random.default_rng(seed + s)
    boot = np.empty((n_boot, max_k + 1), dtype=float)
    for i in range(n_boot):
        sample = rng.choice(qualifying, size=denom, replace=True)
        for k in range(max_k + 1):
            boot[i, k] = float((sample >= s + k).sum()) / denom
    ci_lo = np.quantile(boot, 0.025, axis=0)
    ci_hi = np.quantile(boot, 0.975, axis=0)
    return (ci_lo, ci_hi)


def plot_streak_recovery(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Symmetric streak view — does winning have momentum the way losing
    has tilt?

    Left panel    Bars: WR of the NEXT game by entering streak length,
                  with the streak SIGN preserved. Win streaks on the
                  left (green), loss streaks on the right (orange).
                  A pure "tilt only" picture is flat on the green side
                  and sloping down on the orange side; symmetric streak
                  effects make both sides slope toward the centre.
    Right panel   Survival curves of maximal streaks. For seeds
                  s ∈ {1, 2, 3, 5, 10} we plot
                      S_s(k) = P(streak length ≥ s + k | length ≥ s),
                  loss streaks as solid lines, win streaks dashed.
                  Bernoulli (independent) reference 0.5^k is the
                  dashed grey baseline. Curves above the baseline mean
                  the streak persists more than chance — tilt on the
                  loss side, momentum on the win side.
    """
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to plot")
    d = d.copy()

    # --- Left panel data: signed-streak bars ---
    if "win_streak_in" not in d.columns:
        d["win_streak_in"] = d.groupby("person")["win"].transform(_win_streak_entering)
    d["streak_bucket"] = pd.Categorical(
        [
            _signed_streak_bucket(int(li), int(wi))
            for li, wi in zip(d["loss_streak_in"], d["win_streak_in"], strict=False)
        ],
        categories=_SIGNED_STREAK_LABELS,
        ordered=True,
    )
    g = _bucket_winrate(d, "streak_bucket")
    g = g.set_index("streak_bucket").reindex(_SIGNED_STREAK_LABELS).reset_index()
    macro_n = int(g["n_people"].max(skipna=True)) if not g["n_people"].isna().all() else 1

    pooled = (
        _bin_winrate(d, "streak_bucket").set_index("streak_bucket").reindex(_SIGNED_STREAK_LABELS)
    )
    pooled_games = pooled["games"].fillna(0).astype(int).to_numpy()
    pooled_wins = (
        (pooled["games"].fillna(0) * pooled["winrate"].fillna(0)).round().astype(int).to_numpy()
    )
    _chi2, _dof, pval = chi2_homogeneity(pooled_wins, pooled_games)

    # --- Right panel data: maximal streak survival curves ---
    wins_indexed = d.sort_values(["person", "game_start"]).set_index(["person", "game_start"])[
        "win"
    ]
    loss_lens = _maximal_streak_lengths(wins_indexed, "loss")
    win_lens = _maximal_streak_lengths(wins_indexed, "win")
    max_k = 10

    # --- Figure layout ---
    fig, axes = plt.subplots(1, 2, figsize=(15, 5.0))

    # Left bar panel.
    ax = axes[0]
    bar_colours: list[str] = []
    for label in _SIGNED_STREAK_LABELS:
        if label.startswith("W"):
            bar_colours.append(PALETTE["win"])
        elif label.startswith("L"):
            bar_colours.append(PALETTE["accent_orange"])
        else:
            bar_colours.append(PALETTE["neutral"])
    xs = range(len(_SIGNED_STREAK_LABELS))
    heights = g["winrate"].to_numpy()
    ax.bar(list(xs), heights, color=bar_colours, width=0.78)
    if macro_n > 1:
        yerr_lo = (g["winrate"] - g["ci_lo"]).clip(lower=0).fillna(0).to_numpy()
        yerr_hi = (g["ci_hi"] - g["winrate"]).clip(lower=0).fillna(0).to_numpy()
        ax.errorbar(
            list(xs),
            heights,
            yerr=[yerr_lo, yerr_hi],
            **WHISKER_STYLE,
        )
    ax.set_xticks(list(xs))
    ax.set_xticklabels(_SIGNED_STREAK_LABELS, rotation=0, fontsize=9)
    ax.set_xlabel("Entering streak  (green = win streak, orange = loss streak)")
    ax.set_ylabel("Win rate of this game")
    ax.set_title(_title("Win rate by entering streak  (W ← → L)", player))
    _subtitle(
        ax,
        f"Both sides sloping toward centre = symmetric streakiness; only L-side drops = tilt-only. χ² across 11 buckets: {_p_marker(pval)} ({_p_verdict(pval)})  ·  {_macro_label(macro_n)}.",
    )
    ax.set_ylim(0, 1.1)
    _baseline(ax)
    _annotate_bars(ax, list(xs), g["winrate"].fillna(0), pd.Series(pooled_games))

    # Callout pointing at the rightmost (most loss-tilted) bucket — corroborated
    # finding from iter 22/24/26: bootstrap CI on s≥5 loss survival excludes 0.5.
    last_loss_idx = len(g) - 1
    last_loss_wr = g["winrate"].iloc[last_loss_idx]
    if not pd.isna(last_loss_wr):
        ax.annotate(
            "Stop queueing here —\nmean reversion proven\n(CI 30–49%)",
            xy=(last_loss_idx, last_loss_wr),
            xytext=(last_loss_idx - 1.5, 0.85),
            ha="center",
            fontsize=9,
            color=PALETTE["loss"],
            bbox={
                "facecolor": "white",
                "alpha": 0.9,
                "edgecolor": PALETTE["loss"],
                "linewidth": 1.2,
                "pad": 4,
            },
            arrowprops={
                "arrowstyle": "->",
                "color": PALETTE["loss"],
                "alpha": 0.7,
                "lw": 1.2,
            },
        )

    _polish_ax(ax)

    # Right survival panel.
    ax2 = axes[1]
    ks = np.arange(0, max_k + 1)
    ax2.plot(
        ks,
        0.5**ks,
        color=PALETTE["muted"],
        linewidth=1.0,
        linestyle=(0, (4, 4)),
        label="0.5^k (independent)",
    )

    # Distinct hues for each seed so loss/win pairs of the same seed read
    # at the same vertical position visually.
    seed_to_loss_color = {
        1: "#d65f5f",
        2: "#ee854a",
        3: "#c4533c",
        5: "#a13e2b",
        10: "#6b1d10",
    }
    seed_to_win_color = {
        1: "#6acc64",
        2: "#4daf94",
        3: "#3a8f6a",
        5: "#2b6b54",
        10: "#16432f",
    }

    for s in _SURVIVAL_SEEDS:
        loss_curve, loss_n = _survival_curve(loss_lens, s, max_k)
        if loss_n > 0:
            loss_lo, loss_hi = _bootstrap_survival_ci(loss_lens, s, max_k)
            ax2.fill_between(
                ks,
                loss_lo,
                loss_hi,
                color=seed_to_loss_color[s],
                alpha=0.15,
                linewidth=0,
            )
            ax2.plot(
                ks,
                loss_curve,
                color=seed_to_loss_color[s],
                linewidth=1.8,
                linestyle="-",
                marker="o",
                markersize=3.5,
                label=f"Loss s={s}  (n={loss_n})",
            )
        win_curve, win_n = _survival_curve(win_lens, s, max_k)
        if win_n > 0:
            win_lo, win_hi = _bootstrap_survival_ci(win_lens, s, max_k)
            ax2.fill_between(
                ks,
                win_lo,
                win_hi,
                color=seed_to_win_color[s],
                alpha=0.15,
                linewidth=0,
            )
            ax2.plot(
                ks,
                win_curve,
                color=seed_to_win_color[s],
                linewidth=1.8,
                linestyle=(0, (2, 2)),
                marker="s",
                markersize=3.5,
                label=f"Win  s={s}  (n={win_n})",
            )

    ax2.set_xlabel("Additional games of the same outcome (k)")
    ax2.set_ylabel("P(streak still alive after k more)")
    ax2.set_xticks(list(ks))
    ax2.set_ylim(0, 1.05)
    ax2.set_title(_title("Streak survival — wins vs losses", player))
    _subtitle(
        ax2,
        "Above the dashed line = the streak persists more than chance (loss → tilt, win → momentum). "
        "Shaded bands = bootstrap 95% CI (1000 resamples over streaks); overlap with the baseline = "
        "the effect is within sampling noise.",
    )
    ax2.legend(loc="upper right", fontsize=8, ncol=2)
    _polish_ax(ax2)

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
            **WHISKER_STYLE,
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
            **WHISKER_STYLE,
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

    if _is_aggregate(player):
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
    # LP events are person-keyed — resolve any account-selection to its owner.
    focal_person = _resolve_person(df, player)
    label = _display_label(player) or "this player"
    sub = events[events["person"] == focal_person] if focal_person else events.iloc[0:0]
    if sub.empty:
        return _empty_figure(f"No LP events recorded for {label}")
    wins = sub.loc[sub["outcome"] == "win", "delta_score"].astype(float)
    losses = sub.loc[sub["outcome"] == "loss", "delta_score"].astype(float)
    if wins.empty or losses.empty:
        return _empty_figure(f"{label} has no recorded LP events for both win and loss")

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


def _find_global_gaps(
    ranks: pd.DataFrame, gap_hours: float
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Find time windows where no person in ``ranks`` had a poll.

    Sorts every snapshot by timestamp regardless of person, and returns
    the (prev_ts, ts) pairs whose gap exceeds the threshold. Those
    windows mark stretches where the polling loop was silent for
    everybody — i.e. bot outages rather than per-player inactivity.
    """
    if ranks.empty:
        return []
    ts = ranks["timestamp"].sort_values().reset_index(drop=True)
    gap_h = ts.diff().dt.total_seconds() / 3600
    gap_idx = np.where(gap_h.to_numpy() > gap_hours)[0]
    return [(ts.iloc[i - 1], ts.iloc[i]) for i in gap_idx]


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

    if _is_aggregate(player):
        # Pick top-N most-active people in match_stats so the rank chart
        # lines up with the same people the rest of the panel describes.
        top_people = df.groupby("person").size().sort_values(ascending=False).head(6).index.tolist()
        ranks = ranks[ranks["person"].isin(top_people)]
        if ranks.empty:
            return _empty_figure("No rank history for the tracked top players")

        fig, ax = plt.subplots(figsize=(13, 5.4))
        for idx, name in enumerate(top_people):
            sub = ranks[ranks["person"] == name].sort_values("timestamp").reset_index(drop=True)
            if sub.empty:
                continue
            # Insert NaN at the start of any RANK_GAP_HOURS-wide gap
            # so matplotlib breaks the line there instead of drawing a
            # fake straight line across a no-data window.
            inter_h = sub["timestamp"].diff().dt.total_seconds() / 3600
            plot_rank = sub["rank_score"].where(~(inter_h > RANK_GAP_HOURS), np.nan)
            ax.plot(
                sub["timestamp"],
                plot_rank,
                color=SERIES_CYCLE[idx % len(SERIES_CYCLE)],
                linewidth=1.6,
                alpha=0.85,
                label=name,
            )

        # Shade GLOBAL outage windows: intervals where every displayed
        # person had no poll. Use the already-filtered top-6 ranks so we
        # only shade where the visible series all went silent.
        global_gaps = _find_global_gaps(ranks, RANK_GAP_HOURS)
        for start, end in global_gaps:
            ax.axvspan(start, end, color=PALETTE["neutral"], alpha=0.15, zorder=0)

        _rank_axis(ax)
        ax.set_xlabel("Date")
        ax.set_ylabel("Rank")
        ax.set_title(_title("Rank trajectory — top 6 most-active people", player))
        gap_note = (
            f"  Grey bands = bot-outage windows (no polls from anyone); "
            f"{len(global_gaps)} gap(s) detected."
            if global_gaps
            else ""
        )
        _subtitle(
            ax,
            "Higher = better rank. Each line is one Discord person; "
            f"{len(ranks):,} LP snapshots across {ranks['person'].nunique()} people."
            f"{gap_note}",
        )
        ax.legend(loc="lower left", ncol=2, fontsize=9)
        _polish_ax(ax)
        fig.autofmt_xdate()
        fig.tight_layout()
        return fig

    # --- Single-person view: rank line + WR overlay ---
    # Rank history is person-keyed — drop to the owning person.
    focal_person = _resolve_person(df, player)
    label = _display_label(player) or "this player"
    sub = (
        ranks[ranks["person"] == focal_person].sort_values("timestamp").reset_index(drop=True)
        if focal_person
        else ranks.iloc[0:0]
    )
    if sub.empty:
        return _empty_figure(f"No rank history for {label}")

    fig, ax_rank = plt.subplots(figsize=(13, 5.4))
    # Break the line across no-poll windows so a 14-month outage no
    # longer looks like a flat-rank stretch.
    sub["inter_snapshot_h"] = sub["timestamp"].diff().dt.total_seconds() / 3600
    plot_rank = sub["rank_score"].where(~(sub["inter_snapshot_h"] > RANK_GAP_HOURS), np.nan)
    ax_rank.plot(
        sub["timestamp"],
        plot_rank,
        color=PALETTE["primary"],
        linewidth=2.0,
        label="Rank",
    )
    # Shade each gap so the no-data region is visible behind the
    # broken line.
    gap_mask = sub["inter_snapshot_h"] > RANK_GAP_HOURS
    for i in np.where(gap_mask.to_numpy())[0]:
        ax_rank.axvspan(
            sub["timestamp"].iloc[i - 1],
            sub["timestamp"].iloc[i],
            color=PALETTE["neutral"],
            alpha=0.15,
            zorder=0,
        )
    _rank_axis(ax_rank)
    ax_rank.set_xlabel("Date")
    ax_rank.set_ylabel("Rank", color=PALETTE["primary"])
    ax_rank.tick_params(axis="y", colors=PALETTE["primary"])

    # WR overlay on twin axis — sourced from match_stats, not history.
    # When an account is selected, the overlay narrows to that account; a
    # person selection rolls up all their Riot accounts.
    matches = _filter_player(df, player).sort_values("game_start").reset_index(drop=True)
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
    gap_count = int(gap_mask.sum())
    gap_note = (
        f" Grey bands = no-data periods (bot outage); {gap_count} gap(s) detected."
        if gap_count
        else ""
    )
    _subtitle(
        ax_rank,
        f"Solid blue = rank ({len(sub):,} LP snapshots). Dashed orange = match-stats rolling-20 WR."
        f"{gap_note}",
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

    if _is_aggregate(player):
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
        sub = _filter_player(df, player)
        if sub.empty:
            return _empty_figure(f"No games for {_display_label(player) or 'this player'}")
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

    if _is_aggregate(player):
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
    # Duos / h2h are computed at person granularity — resolve the dropdown
    # key (account or person) to the owning person for partner lookups.
    focal_person = _resolve_person(df, player)
    display_name = _display_label(player) or "this player"
    solo_wr = (
        df[df["person"] == focal_person]["win"].mean() if focal_person and not df.empty else 0.5
    )

    # Left: this player's partners.
    ax = axes[0]
    partner_rows = (
        duos[(duos["a"] == focal_person) | (duos["b"] == focal_person)].copy()
        if focal_person
        else duos.iloc[0:0]
    )
    if partner_rows.empty:
        ax.text(
            0.5,
            0.5,
            f"No duos for {display_name} with ≥{min_games} games",
            ha="center",
            va="center",
            color=PALETTE["muted"],
            transform=ax.transAxes,
        )
        ax.set_axis_off()
    else:
        partner_rows["partner"] = partner_rows.apply(
            lambda r: r["b"] if r["a"] == focal_person else r["a"], axis=1
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
    h_player = (
        h2h[(h2h["a"] == focal_person) | (h2h["b"] == focal_person)].copy()
        if focal_person
        else h2h.iloc[0:0]
    )
    if h_player.empty:
        ax.text(
            0.5,
            0.5,
            f"No head-to-heads for {display_name} with ≥{h2h_min_games} games",
            ha="center",
            va="center",
            color=PALETTE["muted"],
            transform=ax.transAxes,
        )
        ax.set_axis_off()
    else:
        # Normalise: ``own_wr`` = focal player's win-rate vs this opponent.
        h_player["opponent"] = h_player.apply(
            lambda r: r["b"] if r["a"] == focal_person else r["a"], axis=1
        )
        h_player["own_wr"] = np.where(
            h_player["a"] == focal_person, h_player["a_winrate"], 1 - h_player["a_winrate"]
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
        ax.set_xlabel(f"{display_name}'s WR vs opponent")
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
    """Calendar-time view — games per month + the monthly win rate, with a
    year × month win-rate heatmap on the right to separate seasonal effects
    from year-over-year drift.

    Left panel: when the friend group was actually active and whether
    month-over-month win rate has any trend. For one player it's their
    individual session pattern.

    Right panel: same months side-by-side across years so e.g. "every
    September is a slump" stands out from "we got worse this year".
    """
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to plot")

    d = d.copy()
    d["month"] = d["game_start"].dt.to_period("M").dt.to_timestamp()
    by_month = d.groupby("month").agg(games=("win", "size"), winrate=("win", "mean")).reset_index()

    fig, axes = plt.subplots(1, 2, figsize=(16, 4.8))

    # --- Left: games / month + WR line ------------------------------------
    ax_vol = axes[0]
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
    ax_vol.tick_params(axis="x", rotation=45)

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

    ax_vol.set_title(_title("Activity over time — games / month + win rate", player))
    _subtitle(
        ax_vol,
        "Grey bars = games that month. Blue line = winrate that month "
        "(months with <5 games skipped).",
    )
    _polish_ax(ax_vol)

    # --- Right: year × month WR heatmap -----------------------------------
    ax_heat = axes[1]
    d["year"] = d["game_start"].dt.year
    d["month_num"] = d["game_start"].dt.month

    if _is_aggregate(player):
        # Per (year, month, person) WR, then macro-average across people.
        # Mirrors plot_hour_dow_heatmap's aggregate rule but with a higher
        # threshold (5 games per cell per person) — months are coarser
        # buckets than hour-of-week so the sample size bar is higher.
        per_person = (
            d.groupby(["year", "month_num", "person"])
            .agg(person_games=("win", "size"), person_wins=("win", "sum"))
            .reset_index()
        )
        per_person = per_person[per_person["person_games"] >= 5]
        per_person["person_wr"] = per_person["person_wins"] / per_person["person_games"]
        wr = per_person.pivot_table(
            index="year", columns="month_num", values="person_wr", aggfunc="mean"
        )
        contrib_people = per_person.pivot_table(
            index="year", columns="month_num", values="person", aggfunc="nunique"
        )
        # Require ≥2 contributing people so a single dominant player
        # can't define the cell.
        wr = wr.where(contrib_people.fillna(0) >= 2)
        sub_heat = (
            "Compare same-month across years to separate seasonal effects "
            "from year-over-year improvement. ≥5 games per person, ≥2 people per cell."
        )
    else:
        pooled = (
            d.groupby(["year", "month_num"])
            .agg(games=("win", "size"), winrate=("win", "mean"))
            .reset_index()
        )
        pooled = pooled[pooled["games"] >= 5]
        wr = pooled.pivot_table(index="year", columns="month_num", values="winrate")
        sub_heat = (
            "Compare same-month across years to separate seasonal effects "
            "from year-over-year improvement. Cells with <5 games dimmed."
        )

    years_desc = sorted(d["year"].unique(), reverse=True)
    wr = wr.reindex(index=years_desc, columns=range(1, 13))

    cmap = plt.get_cmap("RdYlGn").copy()
    cmap.set_bad(color="#f3f4f6")
    im = ax_heat.imshow(wr.values, aspect="auto", cmap=cmap, vmin=0.3, vmax=0.7)

    ax_heat.set_yticks(range(len(years_desc)))
    ax_heat.set_yticklabels(years_desc)
    ax_heat.set_xticks(range(12))
    ax_heat.set_xticklabels(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    )
    ax_heat.set_xlabel("Month")
    ax_heat.set_ylabel("Year")
    ax_heat.set_title(_title("Year × month win-rate", player))
    _subtitle(ax_heat, sub_heat)
    ax_heat.grid(False)

    for r in range(wr.shape[0]):
        for c in range(wr.shape[1]):
            v = wr.values[r, c]
            if pd.isna(v):
                continue
            ax_heat.text(
                c,
                r,
                f"{v * 100:.0f}%",
                ha="center",
                va="center",
                fontsize=9,
                color=PALETTE["text"],
            )

    cbar = fig.colorbar(im, ax=ax_heat, label="Win rate", shrink=0.8, pad=0.02)
    cbar.outline.set_visible(False)
    _polish_ax(ax_heat)

    fig.tight_layout()
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
    # Pre-game factors only: KDA + duration are outcome-derived (a rephrasing
    # of the win), so including them here would be data leakage. Same scope
    # as `_build_logistic_design`.
    most_played_champ = d["champion"].value_counts().idxmax() if not d.empty else None

    features: list[tuple[str, pd.Series]] = [
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
    # BH-adjust across the features actually tested so the "significant"
    # threshold survives multiple-comparisons noise (10+ tests at α=0.05
    # expects 0.5 false positives by chance otherwise).
    out["p_adj"] = bh_adjust(out["p"].tolist())
    return out.iloc[out["effect_pp"].abs().sort_values(ascending=False).index].reset_index(
        drop=True
    )


def plot_feature_impact(
    df: pd.DataFrame, player: str | None = None, min_games: int = 25, top: int = 10
) -> plt.Figure:
    """Ranked "what predicts a win?" chart.

    For every candidate pre-game factor (weekend, after loss streak,
    same-team partner, picked your main, …) we compute the percentage-
    point shift in win rate when the factor is true vs false. Bars are
    sorted by absolute shift; bars whose BH-adjusted q<0.05 are drawn
    solid, the rest faded to grey (controls FDR across the feature set).
    """
    impacts = compute_feature_impacts(df, player=player, min_games=min_games)
    if impacts.empty:
        return _empty_figure(f"Not enough games to compare factors (need ≥{min_games} each side)")

    impacts = impacts.head(top).iloc[::-1]  # reverse so largest sits on top
    y = np.arange(len(impacts))

    colours = []
    for _, r in impacts.iterrows():
        sig = r["p_adj"] < 0.05
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
    ax.set_title(_title("Feature impact — what actually moves your wins?", player))
    n_people = int(impacts["n_people"].max()) if "n_people" in impacts.columns else 1
    _subtitle(
        ax,
        f"Solid bars = q<0.05 (likely real). Faded grey = noise. {_macro_label(n_people)}. "
        "q = BH-adjusted p; solid bars require q<0.05 (FDR-controlled at 5%).\n"
        "Pre-game factors only — KDA + duration excluded (they're outcome-derived, not predictors).",
    )
    _polish_ax(ax)

    # Annotate each row with detail: "+8.4pp · 56% vs 47% · n=420/2100 · p=… → q=…".
    # Wide solid bars (|effect_pp| > 5) get the label tucked inside; narrow
    # bars and faded-grey (non-significant) bars stay outside to remain
    # readable against the chart background.
    for yi, (_, r) in enumerate(impacts.iterrows()):
        # If raw p and BH-adjusted q round to the same value within 1%,
        # show only p — the arrow + q just repeats information.
        p_tag = _p_marker(r["p"])
        if abs(r["p"] - r["p_adj"]) < 0.01:
            stat_tag = p_tag
        else:
            q_tag = "q<0.001" if r["p_adj"] < 0.001 else f"q={r['p_adj']:.3f}"
            stat_tag = f"{p_tag} {q_tag}"
        label = (
            f"{r['effect_pp']:+.1f}pp  ·  {r['wr_yes']:.0%} vs {r['wr_no']:.0%}  "
            f"·  n={int(r['n_yes'])} vs {int(r['n_no'])}  ·  {stat_tag}"
        )
        is_grey = not (r["p_adj"] < 0.05)
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
    """Lay out label / value / sublabel inside an already-drawn card.

    The value text is the headline number and it must fit within the
    card width. Two safety nets handle long strings (duo names like
    "Langers69 + Chief Morri Porri", long champion names):
    1. Hard truncate at 28 chars with an ellipsis so absurd values can't
       blow past the right edge.
    2. Step the fontsize down for medium-length values (>16 chars) and
       further for long ones (>24 chars) so the box still breathes.
    """
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
    if len(value) > 28:
        value = value[:27] + "…"
    if len(value) > 24:
        value_fontsize = 11
    elif len(value) > 16:
        value_fontsize = 14
    else:
        value_fontsize = 18
    # Big value, vertically centred-ish.
    ax.text(
        text_x,
        y + h * 0.50,
        value,
        ha="left",
        va="center",
        fontsize=value_fontsize,
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
    df: pd.DataFrame, player: str | None, *, include_person_fe: bool = True
) -> tuple[np.ndarray, np.ndarray, list[str], int]:
    """Build the design matrix for win-probability logistic regression.

    Continuous features are standardised (z-score) so their coefficients
    are comparable: each one is "log-odds shift per +1 standard deviation".
    Binary features are 0/1. When ``player is None`` and
    ``include_person_fe`` we add person fixed effects (one-hot dummies,
    drop one as baseline) so the headline coefficients are net of
    "who's playing" — the biggest source of confounding given the 200:1
    game-count spread. Person dummies are always the trailing columns of
    ``X``; the caller gets back ``n_person_fe_cols`` so it can slice them
    out for a person-only sub-fit when decomposing R².
    """
    d = _filter_player(df, player).copy()
    if d.empty:
        return np.empty((0, 0)), np.empty(0), [], 0

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
        "Loss streak entering (z)": d["loss_streak_in"],
        "Log gap since prev (z)": d["log_gap_min"],
    }
    bin_features = {
        "Late night (00-04h)": d["hour"].between(0, 4).astype(int),
        "Evening peak (18-23h)": d["hour"].between(18, 23).astype(int),
        "Weekend": (d["dow"] >= 5).astype(int),
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
    # Always appended LAST so callers can split factor vs person-FE columns
    # by slicing on n_person_fe_cols.
    n_person_fe_cols = 0
    if include_person_fe and _is_aggregate(player) and d["person"].nunique() > 1:
        people = d["person"].value_counts().index.tolist()
        baseline = people[0]
        for p in people[1:]:
            vals = (d["person"] == p).astype(float).to_numpy()
            if vals.sum() < 20:
                continue
            parts.append(vals[:, None])
            cols.append(f"[person] {p} vs {baseline}")
            n_person_fe_cols += 1

    if not parts:
        return np.empty((0, 0)), np.empty(0), [], 0
    X = np.hstack(parts)
    y = d["win"].to_numpy(dtype=float)
    return X, y, cols, n_person_fe_cols


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
    X, y, cols, n_person_fe_cols = _build_logistic_design(df, player)
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

    # Three-way McFadden decomposition. The full R² is flattered by the
    # person fixed-effect dummies absorbing variance, so we also fit a
    # person-only model and subtract: r2_factors = r2_full - r2_fe is the
    # honest "how much do the features add on top of just knowing who's
    # playing?" number. In single-person mode there are no FE dummies
    # and r2_factors == r2_full.
    p_bar = float(y.mean())
    if 0.0 < p_bar < 1.0:
        n_w = float(y.sum())
        n_l = float(len(y) - n_w)
        loglik_null = n_w * math.log(p_bar) + n_l * math.log(1.0 - p_bar)
    else:
        loglik_null = 0.0

    if loglik_null != 0.0:
        r2_full = 1.0 - (loglik_full / loglik_null)
    else:
        r2_full = 0.0

    if n_person_fe_cols > 0 and loglik_null != 0.0:
        X_fe = X[:, -n_person_fe_cols:]
        _, _, loglik_fe = logistic_fit(X_fe, y, l2=0.5)
        r2_fe: float | None = 1.0 - (loglik_fe / loglik_null)
        r2_factors = r2_full - r2_fe
    else:
        r2_fe = None
        r2_factors = r2_full

    coefs_df = (
        pd.DataFrame(rows)
        .iloc[lambda f: f["coef"].abs().sort_values(ascending=False).index]
        .head(top)
        .iloc[::-1]
        .reset_index(drop=True)
    )
    # BH-adjust across the factor coefficients on the chart so the
    # "significant" threshold survives multiple-comparisons noise.
    coefs_df["p_adj"] = bh_adjust(coefs_df["p"].tolist())

    fig_h = max(4.6, len(coefs_df) * 0.45)
    fig, ax = plt.subplots(figsize=(13, fig_h))
    y_pos = np.arange(len(coefs_df))

    sig_mask = coefs_df["p_adj"] < 0.05
    colours = [
        (PALETTE["win"] if c > 0 else PALETTE["loss"]) if s else PALETTE["neutral"]
        for c, s in zip(coefs_df["coef"], sig_mask, strict=False)
    ]
    ax.barh(y_pos, coefs_df["coef"], color=colours, height=0.7)
    ax.errorbar(
        coefs_df["coef"],
        y_pos,
        xerr=1.96 * coefs_df["se"],
        **WHISKER_STYLE,
    )
    ax.axvline(0, color=PALETTE["text"], linewidth=0.8)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(coefs_df["name"])
    ax.set_xlabel("Log-odds (95% CI whiskers)")

    n_people = df["person"].nunique() if _is_aggregate(player) else 1
    title_macro = (
        f"controlling for person ({n_people} people)" if n_people > 1 else "single-person fit"
    )
    ax.set_title(_title(f"What predicts your wins — {title_macro}", player))
    verdict_line = _factors_verdict(r2_factors)
    if r2_fe is not None:
        subtitle = (
            f"Person identity alone explains {r2_fe:.1%} of variance; the factors add "
            f"{r2_factors:+.1%} on top (full = {r2_full:.1%}). {verdict_line}\n"
            "Pre-game factors only — KDA + duration excluded "
            "(they're outcome-derived, not predictors).\n"
            "q = BH-adjusted p; solid bars require q<0.05 (FDR-controlled at 5%)."
        )
    else:
        subtitle = (
            f"McFadden pseudo-R² = {r2_full:.1%}. {verdict_line}\n"
            "Pre-game factors only — KDA + duration excluded "
            "(they're outcome-derived, not predictors).\n"
            "q = BH-adjusted p; solid bars require q<0.05 (FDR-controlled at 5%)."
        )
    _subtitle(ax, subtitle)
    _polish_ax(ax)

    # Annotation placement: solid (significant) coloured bars wider than
    # 0.5 log-odds get the label tucked inside the bar in white. Narrower
    # bars and faded-grey (non-significant) bars keep the label outside
    # to stay readable.
    for yi, r in coefs_df.iterrows():
        odds_ratio_pct = (r["or"] - 1) * 100
        # If raw p and BH-adjusted q round to the same value within 1%,
        # show only p — the arrow + q just repeats information.
        p_tag = _p_marker(r["p"])
        if abs(r["p"] - r["p_adj"]) < 0.01:
            stat_tag = p_tag
        else:
            q_tag = "q<0.001" if r["p_adj"] < 0.001 else f"q={r['p_adj']:.3f}"
            stat_tag = f"{p_tag} {q_tag}"
        label = f"{r['coef']:+.2f}  ·  OR {r['or']:.2f} " f"({odds_ratio_pct:+.0f}%)  ·  {stat_tag}"
        is_grey = not (r["p_adj"] < 0.05)
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

    # OOS calibration (iter 26) showed pre-game logit has zero predictive
    # signal. Surface that directly here so the chart can't be over-read.
    ax.text(
        0.98,
        0.04,
        "Out-of-sample AUC = 0.498 (random).\nPre-game factors don't predict wins.\n→ For real signal see Picks and LP.",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=8.5,
        color=PALETTE["text"],
        bbox={
            "facecolor": "white",
            "alpha": 0.92,
            "edgecolor": PALETTE["spine"],
            "linewidth": 0.9,
            "pad": 6,
        },
    )

    fig.tight_layout()
    return fig


def auc(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Mann-Whitney U area under the ROC curve.

    Equivalent to the probability that a random positive sample scores
    higher than a random negative sample. Returns 0.5 when one class is
    empty (degenerate; the model has nothing to discriminate).
    """
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    pos = y_pred[y_true > 0.5]
    neg = y_pred[y_true <= 0.5]
    if len(pos) == 0 or len(neg) == 0:
        return 0.5
    all_scores = np.concatenate([pos, neg])
    ranks = np.argsort(np.argsort(all_scores, kind="stable"), kind="stable") + 1
    sum_pos = ranks[: len(pos)].sum()
    u = sum_pos - len(pos) * (len(pos) + 1) / 2
    return float(u / (len(pos) * len(neg)))


def _build_calibration_design(
    df: pd.DataFrame,
    player: str | None,
    spec: dict | None = None,
) -> tuple[np.ndarray, np.ndarray, list[str], int, dict]:
    """Build a logistic design that can be REPLAYED on a holdout split.

    Mirrors ``_build_logistic_design`` but exposes/consumes a ``spec`` so
    train and test produce the same column set, order, and z-score
    standardisation. Without this, the holdout's own means/stds and
    top-3 champions would differ from train's and ``beta`` couldn't be
    applied across the split. Used only by ``plot_model_calibration`` —
    the original ``_build_logistic_design`` is left untouched so the
    coefficient chart keeps its existing behaviour.
    """
    d = _filter_player(df, player).copy()
    if d.empty:
        return np.empty((0, 0)), np.empty(0), [], 0, spec or {}

    same_team = d[["match_id", "win", "person"]].merge(
        df[["match_id", "win", "person"]], on=["match_id", "win"]
    )
    same_team = same_team[same_team["person_x"] != same_team["person_y"]]
    with_duo = set(same_team[["match_id", "person_x"]].itertuples(index=False, name=None))
    d["had_tracked_duo"] = [
        (mid, p) in with_duo for mid, p in zip(d["match_id"], d["person"], strict=False)
    ]

    gap_filled = d["gap_since_prev_min"].fillna(d["gap_since_prev_min"].median()).fillna(60.0)
    d["log_gap_min"] = np.log1p(gap_filled.clip(lower=0.0))

    cont_raw = {
        "Loss streak entering (z)": pd.to_numeric(d["loss_streak_in"], errors="coerce"),
        "Log gap since prev (z)": pd.to_numeric(d["log_gap_min"], errors="coerce"),
    }
    bin_raw = {
        "Late night (00-04h)": d["hour"].between(0, 4).astype(int),
        "Evening peak (18-23h)": d["hour"].between(18, 23).astype(int),
        "Weekend": (d["dow"] >= 5).astype(int),
        "Same-team tracked partner": d["had_tracked_duo"].astype(int),
    }

    is_replay = spec is not None
    if is_replay:
        champ_list: list[str] = list(spec.get("champs", []))
        cont_stats: dict[str, tuple[float, float]] = dict(spec.get("cont_stats", {}))
        bin_keep: list[str] = list(spec.get("bin_keep", []))
        fe_people: list[str] = list(spec.get("fe_people", []))
        fe_baseline: str | None = spec.get("fe_baseline")
        col_order: list[str] = list(spec.get("cols", []))
    else:
        champ_list = d["champion"].value_counts().head(3).index.tolist()
        cont_stats = {}
        bin_keep = []
        fe_people = []
        fe_baseline = None
        col_order = []

    for champ in champ_list:
        bin_raw[f"Played {champ}"] = (d["champion"] == champ).astype(int)

    cols: list[str] = []
    parts: list[np.ndarray] = []

    if is_replay:
        # Replay path: use train's means/stds and train's column order.
        # Continuous first (z-scored with train stats), then binaries the
        # train kept, then person FEs (also from train).
        for name in col_order:
            if name in cont_stats:
                mean, std = cont_stats[name]
                series = cont_raw.get(name)
                if series is None:
                    vals = np.zeros(len(d))
                else:
                    vals = series.fillna(series.median() if series.notna().any() else 0.0).to_numpy(
                        dtype=float
                    )
                if std < 1e-9:
                    parts.append(np.zeros((len(d), 1)))
                else:
                    parts.append(((vals - mean) / std)[:, None])
                cols.append(name)
            elif name in bin_raw:
                vals = bin_raw[name].fillna(0).to_numpy(dtype=float)
                parts.append(vals[:, None])
                cols.append(name)
            elif name.startswith("[person] ") and fe_baseline is not None:
                # "[person] X vs baseline" — extract X.
                person_name = name[len("[person] ") :].split(" vs ", 1)[0]
                vals = (d["person"] == person_name).astype(float).to_numpy()
                parts.append(vals[:, None])
                cols.append(name)
            else:
                # Missing in test data — pad with zeros so the column survives.
                parts.append(np.zeros((len(d), 1)))
                cols.append(name)
        n_person_fe_cols = sum(1 for c in cols if c.startswith("[person]"))
    else:
        # Training pass: compute stats, decide which binaries to keep,
        # decide which FE people to include, and stash everything in spec.
        for name, series in cont_raw.items():
            vals = series.fillna(series.median() if series.notna().any() else 0.0).to_numpy(
                dtype=float
            )
            mean = float(vals.mean())
            std = float(vals.std())
            if std < 1e-9:
                continue
            parts.append(((vals - mean) / std)[:, None])
            cols.append(name)
            cont_stats[name] = (mean, std)

        for name, series in bin_raw.items():
            vals = series.fillna(0).to_numpy(dtype=float)
            if vals.sum() < 20 or vals.sum() > len(vals) - 20:
                continue
            parts.append(vals[:, None])
            cols.append(name)
            bin_keep.append(name)

        n_person_fe_cols = 0
        if _is_aggregate(player) and d["person"].nunique() > 1:
            people = d["person"].value_counts().index.tolist()
            fe_baseline = people[0]
            for p in people[1:]:
                vals = (d["person"] == p).astype(float).to_numpy()
                if vals.sum() < 20:
                    continue
                parts.append(vals[:, None])
                cols.append(f"[person] {p} vs {fe_baseline}")
                fe_people.append(p)
                n_person_fe_cols += 1

    if not parts:
        return np.empty((0, 0)), np.empty(0), [], 0, spec or {}
    X = np.hstack(parts)
    y = d["win"].to_numpy(dtype=float)

    if not is_replay:
        spec_out = {
            "champs": champ_list,
            "cont_stats": cont_stats,
            "bin_keep": bin_keep,
            "fe_people": fe_people,
            "fe_baseline": fe_baseline,
            "cols": list(cols),
        }
    else:
        spec_out = spec  # type: ignore[assignment]
    return X, y, cols, n_person_fe_cols, spec_out


def plot_model_calibration(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Out-of-sample calibration + headline metrics for the logit.

    Splits chronologically at the median ``game_start`` (50/50 train /
    holdout), fits the same logistic model used elsewhere on the train
    half, predicts on the holdout half, and reports AUC / accuracy /
    log-loss / Brier alongside their trivial baselines. The question
    isn't "do the features look significant in-sample?" (they barely do
    once KDA leakage is removed) but "do they predict the future at
    all?" — i.e. is the signal real or matchmaking noise.
    """
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to validate")

    # Per-person split needs ≥30 games on each side. For aggregate, ≥60
    # total isn't enough — we also want both halves usable, but the
    # design build itself will refuse <~20 of any binary.
    d = d.sort_values("game_start").reset_index(drop=True)
    if not _is_aggregate(player):
        if len(d) < 60:
            return _empty_figure("Need ≥60 games to validate")
    elif len(d) < 60:
        return _empty_figure("Need ≥60 games to validate")

    cut = d["game_start"].median()
    train_df = d[d["game_start"] <= cut]
    test_df = d[d["game_start"] > cut]
    if not _is_aggregate(player) and (len(train_df) < 30 or len(test_df) < 30):
        return _empty_figure("Need ≥60 games to validate")
    if len(train_df) < 20 or len(test_df) < 20:
        return _empty_figure("Not enough games per half")

    X_tr, y_tr, cols, _n_fe, spec = _build_calibration_design(train_df, player)
    if X_tr.size == 0 or len(np.unique(y_tr)) < 2:
        return _empty_figure("Not enough variance in train half")

    beta, _se, _ll = logistic_fit(X_tr, y_tr, l2=0.5)

    X_te, y_te, _cols_te, _n_fe_te, _ = _build_calibration_design(test_df, player, spec=spec)
    if X_te.size == 0 or len(np.unique(y_te)) < 2:
        return _empty_figure("Holdout has no class variance")

    z = np.clip(X_te @ beta[1:] + beta[0], -30.0, 30.0)
    p = 1.0 / (1.0 + np.exp(-z))
    p_safe = np.clip(p, 1e-12, 1 - 1e-12)

    acc = float(((p > 0.5) == (y_te > 0.5)).mean())
    log_loss = float(-(y_te * np.log(p_safe) + (1 - y_te) * np.log(1 - p_safe)).mean())
    brier = float(((p - y_te) ** 2).mean())

    auc_val = auc(y_te, p)

    # Trivial baselines — what you'd get predicting train-mean WR for everyone.
    p_bar = float(y_tr.mean())
    p_bar_safe = float(np.clip(p_bar, 1e-12, 1 - 1e-12))
    triv_acc = float(max(y_tr.mean(), 1 - y_tr.mean()))
    triv_auc = 0.5
    y_te_mean = float(y_te.mean())
    triv_logloss = float(
        -(y_te_mean * math.log(p_bar_safe) + (1 - y_te_mean) * math.log(1 - p_bar_safe))
    )
    triv_brier = float(((p_bar - y_te) ** 2).mean())

    fig, (ax_cal, ax_card) = plt.subplots(
        1, 2, figsize=(13, 5.4), gridspec_kw={"width_ratios": [1.2, 1.0]}
    )

    # --- Left: calibration curve ---
    bins = np.linspace(0.0, 1.0, 11)
    bin_idx = np.clip(np.digitize(p, bins, right=False) - 1, 0, 9)
    bucket_rows = []
    for b in range(10):
        mask = bin_idx == b
        n = int(mask.sum())
        if n == 0:
            continue
        bucket_rows.append(
            {
                "mean_pred": float(p[mask].mean()),
                "observed_wr": float(y_te[mask].mean()),
                "count": n,
            }
        )
    ax_cal.plot([0, 1], [0, 1], linestyle=":", color=PALETTE["muted"], linewidth=1.2, zorder=1)
    if bucket_rows:
        xs = [r["mean_pred"] for r in bucket_rows]
        ys = [r["observed_wr"] for r in bucket_rows]
        sizes = [max(30, min(400, r["count"] * 6)) for r in bucket_rows]
        ax_cal.scatter(
            xs,
            ys,
            s=sizes,
            color=PALETTE["primary"],
            edgecolor="white",
            linewidth=1.2,
            alpha=0.85,
            zorder=3,
        )
        ax_cal.plot(xs, ys, color=PALETTE["primary"], linewidth=1.1, alpha=0.45, zorder=2)
    ax_cal.set_xlim(0, 1)
    ax_cal.set_ylim(0, 1)
    ax_cal.set_xlabel("Predicted win probability")
    ax_cal.set_ylabel("Observed win rate (holdout)")
    ax_cal.set_title(_title("Calibration curve (holdout)", player))
    _subtitle(
        ax_cal,
        "Markers on the 45° line = well-calibrated. Below the line = overconfident "
        "wins; above = underconfident. Marker size ∝ bucket count.",
    )
    _polish_ax(ax_cal)

    # --- Right: metrics card ---
    ax_card.set_axis_off()
    ax_card.set_xlim(0, 1)
    ax_card.set_ylim(0, 1)
    if auc_val > 0.55:
        accent = PALETTE["win"]
        verdict = "Some real predictive signal."
    elif auc_val < 0.51:
        accent = PALETTE["loss"]
        verdict = "Indistinguishable from noise."
    else:
        accent = PALETTE["primary"]
        verdict = "Marginal — close to chance."

    card_x, card_y, card_w, card_h = 0.04, 0.08, 0.92, 0.84
    _draw_card(ax_card, card_x, card_y, card_w, card_h, accent)
    ax_card.text(
        card_x + card_w * 0.07,
        card_y + card_h - card_h * 0.08,
        "OUT-OF-SAMPLE METRICS",
        ha="left",
        va="center",
        fontsize=11,
        color=PALETTE["muted"],
        fontweight="bold",
        zorder=4,
    )

    metric_rows = [
        ("Accuracy", f"{acc:.1%}", f"vs trivial {triv_acc:.1%}"),
        ("AUC", f"{auc_val:.3f}", "vs random 0.500"),
        ("Log-loss", f"{log_loss:.3f}", f"vs trivial {triv_logloss:.3f}"),
        ("Brier", f"{brier:.3f}", f"vs trivial {triv_brier:.3f}"),
    ]
    # Stack 4 metric lines inside the card from top to bottom.
    top_y = card_y + card_h - card_h * 0.22
    line_h = card_h * 0.17
    for i, (label, value, sub) in enumerate(metric_rows):
        y_line = top_y - i * line_h
        ax_card.text(
            card_x + card_w * 0.10,
            y_line,
            label.upper(),
            ha="left",
            va="center",
            fontsize=10,
            color=PALETTE["muted"],
            fontweight="bold",
            zorder=4,
        )
        ax_card.text(
            card_x + card_w * 0.40,
            y_line,
            value,
            ha="left",
            va="center",
            fontsize=16,
            color=PALETTE["text"],
            fontweight="bold",
            zorder=4,
        )
        ax_card.text(
            card_x + card_w * 0.62,
            y_line,
            sub,
            ha="left",
            va="center",
            fontsize=10,
            color=PALETTE["muted"],
            zorder=4,
        )

    ax_card.text(
        card_x + card_w * 0.10,
        card_y + card_h * 0.06,
        verdict,
        ha="left",
        va="center",
        fontsize=11,
        color=accent,
        fontweight="bold",
        zorder=4,
    )

    fig.suptitle(
        _title("Out-of-sample model calibration", player),
        fontsize=14,
        fontweight="bold",
        y=0.99,
    )
    fig.text(
        0.5,
        0.02,
        f"Trained on {len(train_df)} games, tested on {len(test_df)} games (chronological 50/50 split). "
        "AUC > 0.55 = some real signal; ≈ 0.50 = pure matchmaking noise.",
        ha="center",
        va="bottom",
        fontsize=9,
        color=PALETTE["muted"],
    )
    fig.tight_layout(rect=[0, 0.04, 1, 0.96])
    return fig


def _per_player_auc(df: pd.DataFrame, person: str) -> float | None:
    """Fit the calibration logit on this person's first half and return
    holdout AUC. ``None`` when the split can't produce a usable model
    (too few games, no class variance, or empty design)."""
    d = df[df["person"] == person].sort_values("game_start").reset_index(drop=True)
    if len(d) < 60:
        return None

    cut = d["game_start"].median()
    train_df = d[d["game_start"] <= cut]
    test_df = d[d["game_start"] > cut]
    if len(train_df) < 30 or len(test_df) < 30:
        return None

    # ``player=None`` here means "no filter on the slice we already cut";
    # the FE-people branch in _build_calibration_design is gated on
    # nunique(person) > 1, which is false for a single-person df, so the
    # person dummies drop out cleanly.
    X_tr, y_tr, _cols, _n_fe, spec = _build_calibration_design(train_df, None)
    if X_tr.size == 0 or len(np.unique(y_tr)) < 2:
        return None

    beta, _se, _ll = logistic_fit(X_tr, y_tr, l2=0.5)
    X_te, y_te, _c, _n, _ = _build_calibration_design(test_df, None, spec=spec)
    if X_te.size == 0 or len(np.unique(y_te)) < 2:
        return None

    z = np.clip(X_te @ beta[1:] + beta[0], -30.0, 30.0)
    p = 1.0 / (1.0 + np.exp(-z))
    return auc(y_te, p)


def plot_per_player_predictability(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """OOS AUC per person — is the 0.498 aggregate uniform noise, or is
    it averaging high-predictability players with low ones?

    Refits the calibration logit on each eligible player's chronological
    first half and reports holdout AUC on the second half. Bars sorted
    descending; green > 0.55 (real signal), red < 0.45 (anti-signal),
    grey in between (noise band). A 0.50 reference line marks chance.
    """
    counts = df["person"].value_counts()
    eligible = [p for p, n in counts.items() if n >= 100]

    rows: list[dict] = []
    for person in eligible:
        score = _per_player_auc(df, person)
        if score is None:
            continue
        rows.append({"person": person, "auc": score, "n": int(counts[person])})

    if len(rows) < 3:
        return _empty_figure("Need ≥3 players with 100+ games")

    rows.sort(key=lambda r: r["auc"], reverse=True)
    median_auc = float(np.median([r["auc"] for r in rows]))

    is_single = not _is_aggregate(player)
    focus_person = _resolve_person(df, player) if is_single else None
    focus_row = next((r for r in rows if r["person"] == focus_person), None)
    if is_single and focus_row is None:
        return _empty_figure("Not enough games for this player to validate")

    if is_single:
        display = [focus_row]
    else:
        display = rows

    n_bars = len(display)
    fig, ax = plt.subplots(figsize=(11, max(3.2, 0.55 * n_bars + 1.6)))

    y_pos = np.arange(n_bars)
    aucs = [r["auc"] for r in display]
    labels = [f"{r['person']}  (n={r['n']})" for r in display]

    def _bar_color(v: float) -> str:
        if v > 0.55:
            return PALETTE["win"]
        if v < 0.45:
            return PALETTE["loss"]
        return PALETTE["neutral"]

    colors = [_bar_color(v) for v in aucs]
    ax.barh(y_pos, aucs, color=colors, edgecolor="white", linewidth=0.8, zorder=2)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels)
    ax.invert_yaxis()

    ax.axvline(0.5, color=PALETTE["muted"], linewidth=1.0, linestyle="--", alpha=0.7, zorder=1)
    if not is_single:
        ax.axvline(
            median_auc,
            color=PALETTE["accent_purple"],
            linewidth=1.0,
            linestyle=":",
            alpha=0.8,
            zorder=1,
            label=f"Group median {median_auc:.3f}",
        )
        ax.legend(loc="lower right", frameon=False, fontsize=9)

    # Annotate each bar with the AUC and game count.
    x_max = max(0.65, max(aucs) + 0.04)
    x_min = min(0.35, min(aucs) - 0.02)
    ax.set_xlim(x_min, x_max)
    for i, r in enumerate(display):
        ax.text(
            r["auc"] + 0.005,
            i,
            f"{r['auc']:.3f}  ·  n={r['n']}",
            ha="left",
            va="center",
            fontsize=9,
            color=PALETTE["text"],
        )

    ax.set_xlabel("Out-of-sample AUC (holdout half)")
    ax.set_title(_title("Per-player predictability", player), fontsize=14, fontweight="bold")

    if is_single:
        delta = focus_row["auc"] - median_auc
        verdict = "more" if delta > 0 else "less"
        _subtitle(
            ax,
            f"Your AUC: {focus_row['auc']:.3f}, median across group: {median_auc:.3f}. "
            f"You are {verdict} predictable than peers (Δ {delta:+.3f}).",
        )
    else:
        _subtitle(
            ax,
            "OOS AUC by player. >0.55 = real predictive signal exists. "
            "≈0.50 = outcomes are pure noise for this player.",
        )

    _polish_ax(ax)
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
    duo_label = "Most-played duo" if _is_aggregate(player) else "Best duo partner"
    duo_value = "—"
    duo_sublabel = "needs ≥5 same-team games"
    duo_accent = PALETTE["neutral"]
    duo_value_color = PALETTE["text"]
    # Duos are person-keyed — an account selection inherits its person's duos.
    focal_person = _resolve_person(df, player)
    if not duos.empty:
        if _is_aggregate(player):
            row = duos.sort_values("games", ascending=False).iloc[0]
            duo_value = f"{row['a']} + {row['b']}"
            duo_sublabel = f"{int(row['games'])} games · {row['winrate']:.0%} WR"
            duo_accent = PALETTE["win"] if row["winrate"] >= 0.5 else PALETTE["loss"]
        elif focal_person is not None:
            partners = duos[(duos["a"] == focal_person) | (duos["b"] == focal_person)].copy()
            if not partners.empty:
                partners["partner"] = partners.apply(
                    lambda r: r["b"] if r["a"] == focal_person else r["a"], axis=1
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

    title_who = _display_label(player) or "all players"
    fig.suptitle(f"Stats summary — {title_who}", fontsize=18, fontweight="bold", y=0.965)

    n_people = df["person"].nunique() if _is_aggregate(player) else 1
    if _is_aggregate(player):
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


TIER_ORDER = [
    "IRON",
    "BRONZE",
    "SILVER",
    "GOLD",
    "PLATINUM",
    "EMERALD",
    "DIAMOND",
    "MASTER",
    "GRANDMASTER",
    "CHALLENGER",
]


def plot_tier_winrate(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Per-tier WR — does the player's edge survive when the competition gets harder?

    Joins match_stats outcomes with league_history's tier at game-start.
    Aggregate view: macro-averages per-person WR per tier across players
    who have ≥20 games in that tier. Single-person view: their tier WR
    with Wilson 95% CIs. Empty tiers (never reached) are skipped.
    """
    try:
        ranks = load_rank_history(DEFAULT_DB)
    except Exception as exc:
        return _empty_figure(f"Could not load rank history: {exc!r}")
    if ranks.empty:
        return _empty_figure("No rank history available")

    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No matches for the selection")

    stamped = compute_tier_at_match(d, ranks)
    stamped = stamped[stamped["tier"].isin(TIER_ORDER)]
    if stamped.empty:
        return _empty_figure("No matches overlap rank history")

    min_per_tier = 20
    if _is_aggregate(player):
        # Per-person WR per tier, then macro-mean across people with
        # ≥20 games at that tier. Macro keeps a heavy Emerald grinder
        # from drowning out the rest of the cohort's Emerald signal.
        per_person = (
            stamped.groupby(["tier", "person"])
            .agg(games=("win", "size"), wins=("win", "sum"))
            .reset_index()
        )
        per_person = per_person[per_person["games"] >= min_per_tier]
        if per_person.empty:
            return _empty_figure(f"No tier has ≥{min_per_tier} games for any player")
        per_person["wr"] = per_person["wins"] / per_person["games"]
        agg = (
            per_person.groupby("tier")
            .agg(
                winrate=("wr", "mean"),
                std=("wr", "std"),
                n_people=("person", "nunique"),
                games=("games", "sum"),
            )
            .reset_index()
        )
        agg["ci_lo"] = (agg["winrate"] - agg["std"].fillna(0)).clip(lower=0)
        agg["ci_hi"] = (agg["winrate"] + agg["std"].fillna(0)).clip(upper=1)
        subtitle_extra = (
            f"Macro-mean across {agg['n_people'].max()} players "
            f"(each weighted equally; ≥{min_per_tier} games per tier per player)."
        )
    else:
        # Single player — Wilson CI on pooled tier wr.
        agg = stamped.groupby("tier").agg(games=("win", "size"), wins=("win", "sum")).reset_index()
        agg = agg[agg["games"] >= min_per_tier]
        if agg.empty:
            return _empty_figure(f"No tier with ≥{min_per_tier} games for this player")
        agg["winrate"] = agg["wins"] / agg["games"]
        cis = [wilson_ci(int(w), int(n)) for w, n in zip(agg["wins"], agg["games"], strict=False)]
        agg["ci_lo"] = [c[0] for c in cis]
        agg["ci_hi"] = [c[1] for c in cis]
        agg["n_people"] = 1
        subtitle_extra = "Whiskers = Wilson 95% CI."

    # Order tiers top→bottom (Challenger at top). Reverse TIER_ORDER so
    # the highest tier sits at the top row when matplotlib draws ascending y.
    rank_pos = {t: i for i, t in enumerate(TIER_ORDER)}
    agg["order"] = agg["tier"].map(rank_pos)
    agg = agg.sort_values("order").reset_index(drop=True)

    n_tiers = len(agg)
    fig_h = max(3.5, n_tiers * 0.55 + 1.2)
    fig, ax = plt.subplots(figsize=(11, fig_h))

    y = np.arange(n_tiers)
    means = agg["winrate"].to_numpy()
    lo = agg["ci_lo"].to_numpy()
    hi = agg["ci_hi"].to_numpy()
    counts = agg["games"].to_numpy()

    # Sample-weighted alpha so a tier built on 30 games visually recedes
    # behind a tier built on 800. sqrt softens the contrast so the small
    # bars stay readable.
    max_n = max(counts.max(), 1)
    alphas = np.clip(0.35 + 0.65 * np.sqrt(counts / max_n), 0.35, 1.0)
    colours = [(*plt.matplotlib.colors.to_rgb(PALETTE["primary"]), a) for a in alphas]

    ax.barh(y, means, color=colours, height=0.7)
    ax.errorbar(
        means,
        y,
        xerr=[means - lo, hi - means],
        **WHISKER_STYLE,
    )
    ax.axvline(0.5, color=PALETTE["muted"], linewidth=0.8, linestyle=(0, (4, 4)), alpha=0.55)
    ax.set_yticks(y)
    ax.set_yticklabels([t.title() for t in agg["tier"]], color=PALETTE["text"])
    ax.invert_yaxis()  # Challenger on top, Iron on bottom
    ax.set_xlim(0, 1)
    ax.set_xlabel("Win rate")
    ax.set_title(_title("Win rate by rank at match time", player))
    _subtitle(
        ax,
        "Higher tier = harder competition. Drops typically signal you're near your "
        f"skill ceiling. {subtitle_extra}",
    )
    for yi, (wr, n) in enumerate(zip(means, counts, strict=False)):
        ax.annotate(
            f"{wr:.0%}  ·  n={int(n)}",
            xy=(min(wr + 0.02, 0.97), yi),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            fontsize=9,
            color=PALETTE["text"],
        )
    _polish_ax(ax)
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


def plot_match_highlights(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Six record-holding individual matches as a card grid.

    Per-player view: the player's own highlights (best KDA win, most
    kills, worst loss, longest match, shortest win, most recent ranked).
    Aggregate view: the same six categories taken across every tracked
    person, with the owning player named in each sublabel.

    The grid layout mirrors ``plot_stats_summary`` and ``plot_actions_card``
    (3x2 cards) so the dashboard tile style stays consistent.
    """
    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games yet.")

    is_agg = _is_aggregate(player)

    def _fmt_date(ts) -> str:
        return pd.Timestamp(ts).strftime("%Y-%m-%d")

    def _fmt_duration(secs) -> str:
        mins, rem = divmod(int(secs), 60)
        return f"{mins}:{rem:02d}"

    def _outcome(win) -> str:
        return "W" if int(win) == 1 else "L"

    def _owner(row) -> str:
        # Aggregate sublabels name the owning person so ownership is
        # obvious without reading the chart title.
        return f"  ·  {row['person']}" if is_agg else ""

    wins = d[d["win"] == 1]
    losses = d[d["win"] == 0]

    # --- Tile 1: best KDA win ------------------------------------------------
    if not wins.empty:
        row = wins.loc[wins["kda"].idxmax()]
        kda1_value = f"KDA {row['kills']}/{row['deaths']}/{row['assists']}"
        kda1_sub = f"{row['champion']}  ·  {_fmt_date(row['game_start'])}{_owner(row)}"
        kda1_accent = PALETTE["win"]
        kda1_color = PALETTE["win"]
    else:
        kda1_value = "—"
        kda1_sub = "no wins yet"
        kda1_accent = PALETTE["neutral"]
        kda1_color = PALETTE["muted"]

    # --- Tile 2: most kills --------------------------------------------------
    row = d.loc[d["kills"].idxmax()]
    kills_value = f"{int(row['kills'])} kills"
    kills_sub = (
        f"{row['champion']}  ·  {_fmt_date(row['game_start'])}  ·  "
        f"{_outcome(row['win'])}{_owner(row)}"
    )
    kills_accent = PALETTE["primary"]
    kills_color = PALETTE["primary"]

    # --- Tile 3: worst game (most deaths in a loss) --------------------------
    if not losses.empty:
        row = losses.loc[losses["deaths"].idxmax()]
        worst_value = f"{int(row['deaths'])} deaths"
        worst_sub = f"{row['champion']}  ·  {_fmt_date(row['game_start'])}{_owner(row)}"
        worst_accent = PALETTE["loss"]
        worst_color = PALETTE["loss"]
    else:
        worst_value = "—"
        worst_sub = "no losses yet"
        worst_accent = PALETTE["neutral"]
        worst_color = PALETTE["muted"]

    # --- Tile 4: longest match -----------------------------------------------
    row = d.loc[d["duration_sec"].idxmax()]
    long_value = _fmt_duration(row["duration_sec"])
    long_sub = (
        f"{row['champion']}  ·  {_fmt_date(row['game_start'])}  ·  "
        f"{_outcome(row['win'])}{_owner(row)}"
    )
    long_accent = PALETTE["accent_teal"]
    long_color = PALETTE["accent_teal"]

    # --- Tile 5: shortest WIN ------------------------------------------------
    if not wins.empty:
        row = wins.loc[wins["duration_sec"].idxmin()]
        short_value = _fmt_duration(row["duration_sec"])
        short_sub = f"{row['champion']}  ·  {_fmt_date(row['game_start'])}{_owner(row)}"
        short_accent = PALETTE["accent_orange"]
        short_color = PALETTE["accent_orange"]
    else:
        short_value = "—"
        short_sub = "no wins yet"
        short_accent = PALETTE["neutral"]
        short_color = PALETTE["muted"]

    # --- Tile 6: most recent ranked -----------------------------------------
    # match_stats is ranked-only by design, so the newest row is the
    # latest ranked match without further filtering.
    row = d.loc[d["game_start"].idxmax()]
    recent_value = str(row["champion"])
    recent_sub = (
        f"KDA {row['kills']}/{row['deaths']}/{row['assists']}  ·  "
        f"{_fmt_date(row['game_start'])}  ·  {_outcome(row['win'])}{_owner(row)}"
    )
    recent_accent = PALETTE["neutral"]
    recent_color = PALETTE["text"]

    # --- Render --------------------------------------------------------------
    fig = plt.figure(figsize=(13, 6.6))
    ax = fig.add_subplot(111)
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    title_who = _display_label(player) or "all players"
    fig.suptitle(f"Match highlights — {title_who}", fontsize=18, fontweight="bold", y=0.965)
    if is_agg:
        n_people = df["person"].nunique()
        sub_text = (
            f"Record-holding matches across {n_people} tracked people. "
            "Each card names the owning player."
        )
    else:
        sub_text = "Record-holding individual matches for this player."
    fig.text(
        0.5,
        0.905,
        sub_text,
        ha="center",
        fontsize=10,
        color=PALETTE["muted"],
        style="italic",
    )

    margin_x, margin_y = 0.035, 0.04
    gap_x, gap_y = 0.022, 0.035
    n_cols, n_rows = 3, 2
    grid_top = 0.86
    grid_bottom = margin_y
    card_w = (1 - 2 * margin_x - (n_cols - 1) * gap_x) / n_cols
    card_h = (grid_top - grid_bottom - (n_rows - 1) * gap_y) / n_rows

    def cell(col: int, row_i: int) -> tuple[float, float]:
        x = margin_x + col * (card_w + gap_x)
        y = grid_top - card_h - row_i * (card_h + gap_y)
        return x, y

    def tile(col, row_i, label_text, value, sublabel, accent, value_color):
        x, y = cell(col, row_i)
        _draw_card(ax, x, y, card_w, card_h, accent=accent)
        _card_text(
            ax,
            x,
            y,
            card_w,
            card_h,
            label=label_text,
            value=value,
            sublabel=sublabel,
            value_color=value_color,
        )

    tile(0, 0, "Best KDA win", kda1_value, kda1_sub, kda1_accent, kda1_color)
    tile(1, 0, "Most kills", kills_value, kills_sub, kills_accent, kills_color)
    tile(2, 0, "Worst game", worst_value, worst_sub, worst_accent, worst_color)
    tile(0, 1, "Longest match", long_value, long_sub, long_accent, long_color)
    tile(1, 1, "Shortest win", short_value, short_sub, short_accent, short_color)
    tile(
        2,
        1,
        "Most recent ranked",
        recent_value,
        recent_sub,
        recent_accent,
        recent_color,
    )

    return fig


# --- 27. Recent sessions ----------------------------------------------------


def plot_recent_sessions(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Per-person report card: newest 10 sessions in a list view.

    Each row is one session — date, games played, win-rate, top champion
    of the session, and the chronological W/L sequence drawn as small
    colored squares. Aggregate ("all players") view is skipped because a
    cross-person session list doesn't tell a coherent story.
    """
    from matplotlib.patches import Rectangle

    if _is_aggregate(player):
        return _empty_figure("Pick a player from the dropdown — sessions are per-person.")

    d = _filter_player(df, player)
    if d.empty or "session_id" not in d.columns:
        return _empty_figure("No games yet.")

    sessions = (
        d.groupby("session_id")
        .agg(
            start_time=("game_start", "min"),
            end_time=("game_start", "max"),
            games=("win", "size"),
            wins=("win", "sum"),
            top_champ=("champion", lambda s: s.mode().iloc[0]),
            top_champ_n=("champion", lambda s: s.value_counts().iloc[0]),
        )
        .reset_index()
    )
    sessions["wr"] = sessions["wins"] / sessions["games"]
    total_sessions = len(sessions)
    sessions = sessions.sort_values("start_time", ascending=False).head(10)

    # Chronological W/L tape per session for the squares column.
    sequences = {
        sid: d[d["session_id"] == sid].sort_values("game_start")["win"].astype(int).tolist()
        for sid in sessions["session_id"]
    }

    n = len(sessions)
    fig, ax = plt.subplots(figsize=(13, max(4.5, 0.55 * n + 1.5)))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, n)
    ax.set_axis_off()

    col_date = 0.03
    col_games = 0.22
    col_wr = 0.32
    col_champ = 0.52
    col_seq = 0.72

    for i, (_, sess) in enumerate(sessions.iterrows()):
        y = n - 1 - i + 0.5
        ax.text(
            col_date,
            y,
            pd.Timestamp(sess["start_time"]).strftime("%a %d %b, %H:%M"),
            ha="left",
            va="center",
            fontsize=10,
            color=PALETTE["text"],
        )
        ax.text(
            col_games,
            y,
            f"{int(sess['games'])} games",
            ha="left",
            va="center",
            fontsize=10,
            color=PALETTE["muted"],
        )
        if sess["wr"] > 0.5:
            wr_color = PALETTE["win"]
        elif sess["wr"] < 0.5:
            wr_color = PALETTE["loss"]
        else:
            wr_color = PALETTE["text"]
        ax.text(
            col_wr,
            y,
            f"{sess['wr']:.0%} WR",
            ha="left",
            va="center",
            fontsize=11,
            fontweight="bold",
            color=wr_color,
        )
        ax.text(
            col_champ,
            y,
            f"{sess['top_champ']} x{int(sess['top_champ_n'])}",
            ha="left",
            va="center",
            fontsize=10,
            color=PALETTE["text"],
        )
        seq = sequences[sess["session_id"]]
        sq_w = min(0.022, (1 - col_seq - 0.02) / max(1, len(seq)))
        for j, won in enumerate(seq):
            rect = Rectangle(
                (col_seq + j * sq_w * 1.15, y - 0.15),
                sq_w,
                0.3,
                facecolor=PALETTE["win"] if won == 1 else PALETTE["loss"],
                edgecolor="white",
                linewidth=0.4,
            )
            ax.add_patch(rect)
        if i < n - 1:
            ax.axhline(y - 0.5, color=PALETTE["spine"], linewidth=0.6, alpha=0.6)

    fig.suptitle(
        _title("Recent sessions", player),
        fontsize=16,
        fontweight="bold",
        y=0.97,
    )
    fig.text(
        0.5,
        0.93,
        f"Newest {n} sessions of {total_sessions} total.",
        ha="center",
        fontsize=10,
        color=PALETTE["muted"],
        style="italic",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return fig


# --- 28. Playstyle clusters (k-means + PCA) ---------------------------------


def kmeans_simple(
    X: np.ndarray,
    k: int = 3,
    n_init: int = 10,
    max_iter: int = 100,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Numpy k-means with k-means++ init and multiple restarts.

    Returns (labels, centroids, inertia). Inertia is the sum of squared
    distances of each point to its assigned centroid.
    """
    rng = np.random.default_rng(seed)
    n = X.shape[0]
    best_inertia = np.inf
    best_labels: np.ndarray | None = None
    best_centroids: np.ndarray | None = None
    for _trial in range(n_init):
        # k-means++ init
        centroids = X[rng.integers(n)].reshape(1, -1)
        for _ in range(k - 1):
            dist = np.min(((X[:, None] - centroids[None]) ** 2).sum(-1), axis=1)
            total = dist.sum()
            if total <= 0:
                next_idx = int(rng.integers(n))
            else:
                probs = dist / total
                next_idx = int(rng.choice(n, p=probs))
            centroids = np.vstack([centroids, X[next_idx]])
        # Lloyd iterations
        labels = np.zeros(n, dtype=int)
        for _ in range(max_iter):
            dist = ((X[:, None] - centroids[None]) ** 2).sum(-1)
            labels = dist.argmin(axis=1)
            new_centroids = np.array(
                [X[labels == j].mean(0) if (labels == j).any() else centroids[j] for j in range(k)]
            )
            if np.allclose(new_centroids, centroids):
                centroids = new_centroids
                break
            centroids = new_centroids
        inertia = float(((X - centroids[labels]) ** 2).sum())
        if inertia < best_inertia:
            best_inertia = inertia
            best_labels = labels
            best_centroids = centroids
    assert best_labels is not None and best_centroids is not None
    return best_labels, best_centroids, best_inertia


def pca_2d(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Project X (n, d) to (n, 2) via top-2 principal components.

    Returns (projected, components, variance_ratio).
    """
    X_centered = X - X.mean(0)
    cov = np.cov(X_centered.T)
    eigvals, eigvecs = np.linalg.eigh(cov)
    # eigh returns ascending; take top 2 in descending order
    top2 = eigvecs[:, -2:][:, ::-1]
    total = float(eigvals.sum())
    var_ratio = (eigvals[-2:][::-1] / total) if total > 0 else np.array([0.0, 0.0])
    return X_centered @ top2, top2, var_ratio


def _shannon_entropy(counts: pd.Series) -> float:
    """Shannon entropy of a count distribution (in bits)."""
    total = counts.sum()
    if total <= 0:
        return 0.0
    p = counts.values / total
    p = p[p > 0]
    return float(-(p * np.log2(p)).sum())


def _loss_streak_propensity(wins: pd.Series) -> float:
    """Fraction of a player's games that fall inside a loss-run of length >= 3.

    Labels each maximal run of consecutive losses with its length, then
    asks what share of all games belong to a run with length >= 3.
    """
    arr = wins.astype(int).values
    n = len(arr)
    if n == 0:
        return 0.0
    run_len = np.zeros(n, dtype=int)
    current = 0
    starts: list[int] = []
    for i, w in enumerate(arr):
        if w == 0:
            if current == 0:
                starts.append(i)
            current += 1
            run_len[i] = current
        else:
            current = 0
    # Backfill so every game in the run carries the full run length
    in_run = np.zeros(n, dtype=bool)
    for s in starts:
        e = s
        while e < n and arr[e] == 0:
            e += 1
        if (e - s) >= 3:
            in_run[s:e] = True
    return float(in_run.mean())


def plot_playstyle_clusters(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """K-means playstyle archetypes over the friend group.

    Builds a per-player feature vector (KDA, deaths, duration, champ
    diversity, time-of-day, session shape, loss-streak propensity),
    z-scores it, runs numpy k-means (k=3, k-means++ init, multi-restart),
    then projects players into a 2D PCA scatter coloured by cluster. The
    aim is to surface natural archetypes ("late-night spam grinders"
    vs "early-evening one-trick steady players") that none of the
    per-player charts can show.
    """
    if not _is_aggregate(player):
        return _empty_figure(
            "Clustering is a group-level chart - pick 'All players' from the dropdown."
        )

    if df.empty:
        return _empty_figure("No games to cluster.")

    feature_names = [
        "avg_kda",
        "avg_kills",
        "avg_deaths",
        "avg_duration_min",
        "champ_diversity",
        "top_champ_concentration",
        "prime_time_share",
        "weekend_share",
        "avg_session_length",
        "loss_streak_propensity",
    ]

    rows: list[dict] = []
    for person, g in df.groupby("person"):
        if len(g) < 100:
            continue
        champ_counts = g["champion"].value_counts()
        top_champ_n = int(champ_counts.iloc[0]) if not champ_counts.empty else 0
        if "session_id" in g.columns:
            sess_sizes = g.groupby("session_id").size()
            avg_session = float(sess_sizes.mean())
        else:
            avg_session = float("nan")
        rows.append(
            {
                "person": person,
                "avg_kda": float(g["kda"].mean()),
                "avg_kills": float(g["kills"].mean()),
                "avg_deaths": float(g["deaths"].mean()),
                "avg_duration_min": float(g["duration_min"].mean()),
                "champ_diversity": _shannon_entropy(champ_counts),
                "top_champ_concentration": top_champ_n / len(g),
                "prime_time_share": float(g["hour"].between(19, 23).mean()),
                "weekend_share": float(g["dow"].isin([5, 6]).mean()),
                "avg_session_length": avg_session,
                "loss_streak_propensity": _loss_streak_propensity(
                    g.sort_values("game_start")["win"]
                ),
            }
        )

    if len(rows) < 3:
        return _empty_figure("Need at least 3 players with 100+ games to cluster.")

    feat_df = pd.DataFrame(rows).set_index("person")
    feat_df = feat_df.dropna()
    if len(feat_df) < 3:
        return _empty_figure("Need at least 3 players with 100+ games to cluster.")

    raw = feat_df[feature_names].values.astype(float)
    mean = raw.mean(0)
    std = raw.std(0)
    safe_std = np.where(std == 0, 1.0, std)
    X_z = (raw - mean) / safe_std

    k = min(3, len(feat_df))
    labels, centroids, _inertia = kmeans_simple(X_z, k=k, n_init=20, seed=0)

    pca_pts, _components, var_ratio = pca_2d(X_z)
    centroid_pca = (centroids - X_z.mean(0)) @ _components

    cluster_colors = [
        PALETTE["primary"],
        PALETTE["accent_orange"],
        PALETTE["accent_teal"],
        PALETTE["accent_purple"],
    ]

    fig, ax = plt.subplots(figsize=(12, 7.5))

    # Per-cluster distinguishing features (top-2 by |mean z-score|).
    cluster_descriptions: list[str] = []
    for c in range(k):
        mask = labels == c
        members = feat_df.index[mask].tolist()
        if not members:
            cluster_descriptions.append(f"Cluster {chr(65 + c)} (empty)")
            continue
        cluster_z = X_z[mask].mean(0)
        # Two largest absolute deviations from group mean (z-score is
        # already a deviation from the overall mean, which is 0).
        top_idx = np.argsort(np.abs(cluster_z))[::-1][:2]
        descriptors = []
        for idx in top_idx:
            direction = "high" if cluster_z[idx] >= 0 else "low"
            descriptors.append(f"{direction} {feature_names[idx]}")
        cluster_descriptions.append(
            f"Cluster {chr(65 + c)} (n={len(members)}): " + ", ".join(descriptors)
        )

    # Scatter points coloured by cluster
    for c in range(k):
        mask = labels == c
        if not mask.any():
            continue
        ax.scatter(
            pca_pts[mask, 0],
            pca_pts[mask, 1],
            s=180,
            color=cluster_colors[c % len(cluster_colors)],
            edgecolor="white",
            linewidth=1.2,
            alpha=0.9,
            label=f"Cluster {chr(65 + c)}",
            zorder=3,
        )

    # Labels next to each point
    for i, name in enumerate(feat_df.index):
        ax.annotate(
            str(name),
            (pca_pts[i, 0], pca_pts[i, 1]),
            xytext=(7, 4),
            textcoords="offset points",
            fontsize=10,
            color=PALETTE["text"],
            zorder=4,
        )

    # Centroid X markers
    for c in range(k):
        ax.scatter(
            centroid_pca[c, 0],
            centroid_pca[c, 1],
            marker="x",
            s=220,
            color=cluster_colors[c % len(cluster_colors)],
            linewidth=3,
            zorder=5,
        )
        ax.annotate(
            f"Cluster {chr(65 + c)}",
            (centroid_pca[c, 0], centroid_pca[c, 1]),
            xytext=(10, -12),
            textcoords="offset points",
            fontsize=10,
            fontweight="bold",
            color=cluster_colors[c % len(cluster_colors)],
            zorder=5,
        )

    ax.axhline(0, color=PALETTE["grid"], linewidth=0.8, zorder=1)
    ax.axvline(0, color=PALETTE["grid"], linewidth=0.8, zorder=1)
    ax.set_xlabel(f"PC1 ({var_ratio[0] * 100:.0f}% var)")
    ax.set_ylabel(f"PC2 ({var_ratio[1] * 100:.0f}% var)")
    ax.legend(loc="best")
    _polish_ax(ax)

    var_total = float((var_ratio[0] + var_ratio[1]) * 100)
    subtitle = f"PC1 + PC2 = {var_total:.0f}% of variance  |  " + "  |  ".join(cluster_descriptions)
    fig.suptitle(
        f"Playstyle clusters ({len(feat_df)} players, k={k})",
        fontsize=16,
        fontweight="bold",
        y=0.98,
    )
    fig.text(
        0.5,
        0.93,
        subtitle,
        ha="center",
        fontsize=9,
        color=PALETTE["muted"],
        style="italic",
        wrap=True,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.91))
    return fig


def plot_champion_freshness(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Per-champion "how long since I last played this?" for a focal player.

    Champion mastery decays. The bot is full of players who insist they
    "still know" a pick they haven't touched in 4 months — this chart
    surfaces that gap explicitly. Bars are coloured by warmth bucket so
    a glance reads "what's warm, what's medium, what's rusty".

    ``today`` is anchored on the latest game in the dataframe (not
    wall-clock), so reruns against a frozen snapshot produce identical
    output regardless of when the chart is rendered.
    """
    if _is_aggregate(player):
        return _empty_figure("Champion freshness is per-person. Pick a player from the dropdown.")

    d = _filter_player(df, player)
    if d.empty:
        return _empty_figure("No games to analyse")

    today = df["game_start"].max()
    champ_stats = d.groupby("champion").agg(
        games_total=("win", "size"),
        last_played=("game_start", "max"),
    )
    champ_stats = champ_stats[champ_stats["games_total"] >= 5]
    if champ_stats.empty:
        return _empty_figure("No champions with >=5 games")

    champ_stats["days_since"] = (today - champ_stats["last_played"]).dt.days.astype(int)

    # Keep the 20 most-played, then sort so freshest is at the top of the
    # horizontal chart (small days_since at top → ascending sort + invert_yaxis).
    champ_stats = champ_stats.sort_values("games_total", ascending=False).head(20)
    champ_stats = champ_stats.sort_values("days_since", ascending=True)

    def warmth_colour(days: int) -> str:
        if days <= 30:
            return PALETTE["win"]
        if days <= 90:
            return PALETTE["accent_orange"]
        return PALETTE["loss"]

    colours = [warmth_colour(int(d)) for d in champ_stats["days_since"]]

    fig_h = max(4.6, len(champ_stats) * 0.42)
    fig, ax = plt.subplots(figsize=(13, fig_h))
    y = np.arange(len(champ_stats))
    ax.barh(y, champ_stats["days_since"], color=colours, height=0.7)
    ax.set_yticks(y)
    ax.set_yticklabels(champ_stats.index)
    ax.invert_yaxis()
    ax.set_xlabel(f"Days since last played (snapshot: {today.date().isoformat()})")
    ax.set_title(_title("Champion freshness — days since last played", player))
    _subtitle(
        ax,
        "Green = played in last 30 days (warm). "
        "Orange = 30-90 days. Red = >90 days (rusty). "
        "Top 20 most-played champions (min 5 games).",
    )
    _polish_ax(ax)

    max_days = float(champ_stats["days_since"].max())
    # Pad right so the n=NN tail of the annotation doesn't run off the axes.
    ax.set_xlim(0, max(30.0, max_days) * 1.25)

    for yi, (_, r) in enumerate(champ_stats.iterrows()):
        days = int(r["days_since"])
        n = int(r["games_total"])
        last = r["last_played"].date().isoformat()
        label = f"{days}d ago  ·  n={n}  ·  last: {last}"
        ax.annotate(
            label,
            xy=(r["days_since"], yi),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            ha="left",
            fontsize=9,
            color=PALETTE["text"],
        )

    fig.tight_layout()
    return fig


def plot_role_winrate(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Win rate split by inferred champion role.

    Role is a heuristic from CHAMPION_ROLES — champions that flex get
    their more common role. Rows with an unmapped champion (role
    "UNKNOWN") are skipped.

    Per-person: bars are coloured against the player's overall baseline
    so a glance reads "which roles am I outperforming my own average on?".
    Aggregate: macro-mean across players with >=30 games in that role.
    """
    d = df[df["role"].isin(ROLE_ORDER)]
    if d.empty:
        return _empty_figure("No matches with a mapped role")

    if _is_aggregate(player):
        per_person = (
            d.groupby(["role", "person"])
            .agg(games=("win", "size"), wins=("win", "sum"))
            .reset_index()
        )
        # Macro across people with a credible sample at that role — a
        # single 60% Zac main shouldn't swing the cohort JUNGLE WR.
        per_person = per_person[per_person["games"] >= 30]
        if per_person.empty:
            return _empty_figure("No role has >=30 games for any player")
        per_person["wr"] = per_person["wins"] / per_person["games"]
        agg = (
            per_person.groupby("role")
            .agg(
                winrate=("wr", "mean"),
                std=("wr", "std"),
                n_people=("person", "nunique"),
                games=("games", "sum"),
            )
            .reset_index()
        )
        agg["ci_lo"] = (agg["winrate"] - agg["std"].fillna(0)).clip(lower=0)
        agg["ci_hi"] = (agg["winrate"] + agg["std"].fillna(0)).clip(upper=1)
        # Cohort baseline = macro-mean of per-person overall WR (people
        # with >=30 mapped games), matching the per-role aggregation rule.
        per_person_overall = (
            d.groupby("person").agg(games=("win", "size"), wins=("win", "sum")).query("games >= 30")
        )
        baseline = (
            (per_person_overall["wins"] / per_person_overall["games"]).mean()
            if not per_person_overall.empty
            else 0.5
        )
        subtitle = (
            "Macro-mean per-role WR across players with >=30 games at that role. "
            "Compare to the cohort baseline (dashed)."
        )
    else:
        d_p = _filter_player(d, player)
        if d_p.empty:
            return _empty_figure("No matches for the selection")
        agg = d_p.groupby("role").agg(games=("win", "size"), wins=("win", "sum")).reset_index()
        agg = agg[agg["games"] >= 10]
        if agg.empty:
            return _empty_figure("No role with >=10 games for this player")
        agg["winrate"] = agg["wins"] / agg["games"]
        cis = [wilson_ci(int(w), int(n)) for w, n in zip(agg["wins"], agg["games"], strict=False)]
        agg["ci_lo"] = [c[0] for c in cis]
        agg["ci_hi"] = [c[1] for c in cis]
        baseline = float(d_p["win"].mean())
        subtitle = "Per-role WR with Wilson 95% CI. Compare to your overall baseline (dashed)."

    role_pos = {r: i for i, r in enumerate(ROLE_ORDER)}
    agg["order"] = agg["role"].map(role_pos)
    agg = agg.sort_values("order").reset_index(drop=True)

    means = agg["winrate"].to_numpy()
    lo = agg["ci_lo"].to_numpy()
    hi = agg["ci_hi"].to_numpy()
    counts = agg["games"].to_numpy()

    # Colour each bar against the focal baseline so the chart answers
    # "where am I above/below my own average?" at a glance. 5pp deadband
    # keeps tiny noise-level deltas neutral.
    def role_colour(wr: float) -> str:
        if wr > baseline + 0.05:
            return PALETTE["win"]
        if wr < baseline - 0.05:
            return PALETTE["loss"]
        return PALETTE["neutral"]

    colours = [role_colour(float(wr)) for wr in means]

    fig, ax = plt.subplots(figsize=(11, 5.2))
    x = np.arange(len(agg))
    ax.bar(x, means, color=colours, width=0.65, edgecolor="white", linewidth=0.6)
    ax.errorbar(x, means, yerr=[means - lo, hi - means], **WHISKER_STYLE)

    ax.axhline(
        baseline,
        color=PALETTE["muted"],
        linewidth=1.0,
        linestyle=(0, (4, 4)),
        alpha=0.7,
        label=f"baseline {baseline:.0%}",
    )
    ax.set_xticks(x)
    ax.set_xticklabels(agg["role"].tolist())
    ax.set_ylim(0, 1)
    ax.set_ylabel("Win rate")
    ax.set_title(_title("Win rate by role", player))
    _subtitle(ax, subtitle)
    ax.legend(loc="upper right")

    for xi, (wr, n) in enumerate(zip(means, counts, strict=False)):
        ax.annotate(
            f"{wr:.0%}  ·  n={int(n)}",
            xy=(xi, wr),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=9,
            color=PALETTE["text"],
        )

    _polish_ax(ax)
    fig.tight_layout()
    return fig


def plot_player_role_matrix(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Player x role WR heatmap, coloured by each row's gap to that
    player's own overall WR.

    A flat heatmap of role WRs would be dominated by the fact that some
    players are just better than others. Re-centring each row on the
    player's personal baseline pulls out the role-specific story: who
    over-performs on jungle relative to their own mean, who tanks on
    support, etc.

    Aggregate-only: per-person view returns an empty figure pointing the
    reader back to the dropdown.
    """
    if not _is_aggregate(player):
        return _empty_figure("Aggregate view only - pick 'All' from the dropdown")

    mapped = df[df["role"].isin(ROLE_ORDER)]
    if mapped.empty:
        return _empty_figure("No matches with a mapped role")

    # Per-player totals come from mapped games only — the same basis the
    # cells and baselines use, so the n shown beside the row matches the
    # n the baseline was computed from.
    totals = mapped.groupby("person").agg(games=("win", "size"), wins=("win", "sum")).reset_index()
    totals = totals[totals["games"] >= 100]
    if totals.empty:
        return _empty_figure("No player has >=100 mapped games")
    totals["wr"] = totals["wins"] / totals["games"]
    totals = totals.sort_values("games", ascending=False).head(10).reset_index(drop=True)
    players = totals["person"].tolist()

    cell = (
        mapped[mapped["person"].isin(players)]
        .groupby(["person", "role"])
        .agg(games=("win", "size"), wins=("win", "sum"))
        .reset_index()
    )
    cell["wr"] = cell["wins"] / cell["games"]

    n_players = len(players)
    n_roles = len(ROLE_ORDER)
    wr_grid = np.full((n_players, n_roles), np.nan)
    games_grid = np.zeros((n_players, n_roles), dtype=int)
    delta_grid = np.full((n_players, n_roles), np.nan)

    baseline_by_person = dict(zip(totals["person"], totals["wr"], strict=False))
    for _, row in cell.iterrows():
        pi = players.index(row["person"])
        ri = ROLE_ORDER.index(row["role"])
        games_grid[pi, ri] = int(row["games"])
        if row["games"] >= 15:
            wr_grid[pi, ri] = float(row["wr"])
            delta_grid[pi, ri] = float(row["wr"]) - baseline_by_person[row["person"]]

    # Colour scale: clip delta to +/-10pp, map to RdYlGn so the strongest
    # over-/under-performers anchor the extremes. Anything inside the band
    # interpolates linearly. set_bad paints low-n cells grey via NaN.
    import matplotlib as mpl

    cmap = mpl.colormaps.get_cmap("RdYlGn").copy()
    cmap.set_bad(color="#dcdcdc")
    norm = mpl.colors.Normalize(vmin=-0.10, vmax=0.10)
    # delta_grid carries NaN where games < 15, so masking is automatic.
    colour_data = np.clip(delta_grid, -0.10, 0.10)

    fig, (ax, cax) = plt.subplots(
        1, 2, figsize=(11.5, 0.55 * n_players + 2.5), gridspec_kw={"width_ratios": [22, 1]}
    )
    im = ax.imshow(colour_data, cmap=cmap, norm=norm, aspect="auto")

    ax.set_xticks(np.arange(n_roles))
    ax.set_xticklabels(ROLE_ORDER)
    ax.set_yticks(np.arange(n_players))
    # Bake the personal baseline into the y-tick label — keeps the layout
    # simple (no extra axes to fight tight_layout) and puts the reference
    # number physically next to the row it belongs to.
    ax.set_yticklabels(
        [
            f"{_display_label(p) or p}\n{baseline_by_person[p]:.0%} (n={int(totals.loc[i, 'games'])})"
            for i, p in enumerate(players)
        ],
        fontsize=9,
    )

    for pi in range(n_players):
        for ri in range(n_roles):
            n = games_grid[pi, ri]
            if n < 15 or np.isnan(wr_grid[pi, ri]):
                ax.text(
                    ri,
                    pi,
                    "-",
                    ha="center",
                    va="center",
                    fontsize=11,
                    color=PALETTE["muted"],
                )
                continue
            wr = wr_grid[pi, ri]
            d = delta_grid[pi, ri]
            # Near-baseline cells sit in the yellow zone of RdYlGn — dark
            # text reads, white text disappears. Outside ~4pp the cmap
            # darkens and white wins.
            txt_colour = PALETTE["text"] if abs(d) < 0.04 else "white"
            ax.text(
                ri,
                pi,
                f"{wr:.0%}\nn={n}",
                ha="center",
                va="center",
                fontsize=9,
                color=txt_colour,
            )

    ax.set_xticks(np.arange(n_roles + 1) - 0.5, minor=True)
    ax.set_yticks(np.arange(n_players + 1) - 0.5, minor=True)
    ax.grid(which="minor", color="white", linewidth=2)
    ax.tick_params(which="minor", length=0)
    ax.tick_params(which="major", length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)

    ax.set_title("Role winrate vs personal baseline")
    _subtitle(
        ax,
        "Each cell coloured by gap to player's overall WR "
        "(red below, green above, +/-10pp). Cells with <15 games shown grey.",
    )

    cbar = fig.colorbar(im, cax=cax)
    cbar.set_label("Delta vs personal baseline (pp)")
    cbar.set_ticks([-0.10, -0.05, 0.0, 0.05, 0.10])
    cbar.ax.set_yticklabels(["-10", "-5", "0", "+5", "+10"])

    fig.tight_layout()
    return fig


# --- 32. Tilt by gap since previous game ----------------------------------

# Bin edges in minutes for the tilt-by-gap chart. Distinct from the
# load_matches gap_bucket bins — this set zooms in on the short-gap region
# (sub-5m rage-queue, 5-15m re-queue) where the tilt hypothesis lives, and
# stretches out to a multi-day break so the "tilt is gone" baseline is
# visible on the same axis.
_TILT_GAP_BINS_MIN = [0.0, 5.0, 15.0, 30.0, 60.0, 180.0, 1440.0, np.inf]
_TILT_GAP_LABELS = ["<5m", "5-15m", "15-30m", "30-60m", "1-3h", "3-24h", ">24h"]


def plot_tilt_by_gap(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """WR by inter-game gap, split by whether the previous game was a win
    or a loss. The "tilt" hypothesis predicts the after-loss line dips at
    short gaps and converges to the after-win line as the gap grows.

    Aggregate mode macro-averages per-person WRs (each person weighted
    equally) and bootstraps a 95% CI across people. Per-person mode uses
    a Wilson CI on the binomial outcome — bootstrap adds nothing for a
    single proportion.
    """
    d_all = _filter_player(df, player)
    if d_all.empty:
        return _empty_figure("No games to plot")

    # Sort by (person, game_start) so the shift below picks up the right
    # previous game even when the caller hands us a multi-person df.
    d = d_all.sort_values(["person", "game_start"]).copy()
    d["prev_win"] = d.groupby("person")["win"].shift(1)
    d = d.dropna(subset=["prev_win", "gap_since_prev_min"])
    if d.empty:
        return _empty_figure("Not enough consecutive games to plot")

    d["prev_outcome"] = np.where(d["prev_win"] >= 0.5, "win", "loss")
    d["gap_clamped"] = d["gap_since_prev_min"].clip(lower=0.0)
    d["tilt_bucket"] = pd.cut(
        d["gap_clamped"],
        bins=_TILT_GAP_BINS_MIN,
        labels=_TILT_GAP_LABELS,
        right=False,
        ordered=True,
    )
    d = d.dropna(subset=["tilt_bucket"])
    if d.empty:
        return _empty_figure("No games inside the gap buckets")

    is_aggregate_mode = _is_aggregate(player)
    rng = np.random.default_rng(40)
    n_boot = 2000
    min_per_bucket = 8

    # Wide-form per (prev_outcome, bucket): WR, n, ci_lo, ci_hi.
    rows: list[dict] = []
    if is_aggregate_mode:
        # Per-person WR per cell, drop sparse cells, then macro-average and
        # bootstrap across the surviving per-person WRs.
        per_person = (
            d.groupby(["prev_outcome", "tilt_bucket", "person"], observed=True)
            .agg(games=("win", "size"), wins=("win", "sum"))
            .reset_index()
        )
        per_person = per_person[per_person["games"] >= min_per_bucket]
        per_person["wr"] = per_person["wins"] / per_person["games"]

        for prev_outcome in ("loss", "win"):
            for bucket in _TILT_GAP_LABELS:
                cell = per_person[
                    (per_person["prev_outcome"] == prev_outcome)
                    & (per_person["tilt_bucket"] == bucket)
                ]
                if cell.empty:
                    continue
                wrs = cell["wr"].to_numpy()
                if wrs.size < 2:
                    ci_lo = ci_hi = np.nan
                else:
                    boot = rng.choice(wrs, size=(n_boot, wrs.size), replace=True).mean(axis=1)
                    ci_lo = float(np.quantile(boot, 0.025))
                    ci_hi = float(np.quantile(boot, 0.975))
                rows.append(
                    {
                        "prev_outcome": prev_outcome,
                        "bucket": bucket,
                        "wr": float(wrs.mean()),
                        "n": int(cell["games"].sum()),
                        "n_people": int(wrs.size),
                        "ci_lo": ci_lo,
                        "ci_hi": ci_hi,
                    }
                )
    else:
        # Single person: Wilson 95% CI on the binomial.
        cell = (
            d.groupby(["prev_outcome", "tilt_bucket"], observed=True)
            .agg(games=("win", "size"), wins=("win", "sum"))
            .reset_index()
        )
        cell = cell[cell["games"] >= min_per_bucket]
        for _, row in cell.iterrows():
            lo, hi = wilson_ci(int(row["wins"]), int(row["games"]))
            rows.append(
                {
                    "prev_outcome": row["prev_outcome"],
                    "bucket": str(row["tilt_bucket"]),
                    "wr": float(row["wins"]) / float(row["games"]),
                    "n": int(row["games"]),
                    "n_people": 1,
                    "ci_lo": lo,
                    "ci_hi": hi,
                }
            )

    if not rows:
        return _empty_figure("No buckets had >=8 samples")

    series = pd.DataFrame(rows)

    # X positions: integer index into the bucket label list so missing
    # buckets leave the right visual gap.
    bucket_to_x = {label: i for i, label in enumerate(_TILT_GAP_LABELS)}

    # Baseline: macro-averaged overall WR for aggregate mode, per-person
    # overall WR otherwise.
    if is_aggregate_mode:
        per_person_overall = d.groupby("person")["win"].mean()
        baseline_wr = float(per_person_overall.mean())
        baseline_label = f"macro-average overall WR {baseline_wr:.0%}"
    else:
        baseline_wr = float(d["win"].mean())
        baseline_label = f"overall WR {baseline_wr:.0%}"

    fig, ax = plt.subplots(figsize=(11, 5.0))

    series_styles = {
        "loss": {"colour": PALETTE["loss"], "label": "After loss"},
        "win": {"colour": PALETTE["win"], "label": "After win"},
    }
    for prev_outcome in ("loss", "win"):
        sub = series[series["prev_outcome"] == prev_outcome].copy()
        if sub.empty:
            continue
        sub = sub.sort_values("bucket", key=lambda s: s.map(bucket_to_x))
        xs = sub["bucket"].map(bucket_to_x).to_numpy()
        ys = sub["wr"].to_numpy()
        colour = series_styles[prev_outcome]["colour"]
        ax.plot(
            xs,
            ys,
            color=colour,
            marker="o",
            markersize=6,
            linewidth=2.2,
            label=series_styles[prev_outcome]["label"],
        )
        # Ribbon only where CI is defined (>=2 people in aggregate, always
        # in per-person via Wilson).
        ci_ok = sub["ci_lo"].notna() & sub["ci_hi"].notna()
        if ci_ok.any():
            ax.fill_between(
                xs[ci_ok.to_numpy()],
                sub.loc[ci_ok, "ci_lo"].to_numpy(),
                sub.loc[ci_ok, "ci_hi"].to_numpy(),
                color=colour,
                alpha=0.18,
                linewidth=0,
            )
        # n=... annotation under each point; loss series sits below the
        # marker, win series above, so labels don't collide when the two
        # lines cross.
        y_offset = -14 if prev_outcome == "loss" else 10
        va = "top" if prev_outcome == "loss" else "bottom"
        for xi, yi, ni in zip(xs, ys, sub["n"].to_numpy(), strict=False):
            ax.annotate(
                f"n={int(ni)}",
                xy=(xi, yi),
                xytext=(0, y_offset),
                textcoords="offset points",
                ha="center",
                va=va,
                fontsize=8,
                color=colour,
            )

    _baseline(ax, y=baseline_wr, label=baseline_label)

    ax.set_xticks(list(range(len(_TILT_GAP_LABELS))))
    ax.set_xticklabels(_TILT_GAP_LABELS)
    ax.set_xlabel("Gap since previous game")
    ax.set_ylabel("Win rate")
    ax.set_ylim(0.0, 1.0)

    if is_aggregate_mode:
        ax.set_title("Tilt analysis - does replaying right after a loss hurt?")
        _subtitle(
            ax,
            "Macro-averaged WR by gap since previous game, split by previous outcome. "
            "Tilt hypothesis: after-loss line dips at short gaps. "
            "Ribbons = bootstrap 95% CI across players (B=2000); per-person cells need >=8 games.",
        )
    else:
        name = _display_label(player) or ""
        ax.set_title(f"Tilt analysis - {name}")
        _subtitle(
            ax,
            "Your WR by gap since previous game, split by previous outcome. "
            "Tilt hypothesis: after-loss line dips at short gaps. "
            "Ribbons = Wilson 95% CI; buckets need >=8 games.",
        )

    ax.legend(loc="lower right", fontsize=9)
    _polish_ax(ax)
    fig.tight_layout()
    return fig


# --- 33. Win rate by position within a play session -----------------------

# Position labels for the X axis. Pool everything >=7 into "7+" so a long
# tail of marathon-session positions (8th, 9th, 10th game...) doesn't
# explode the chart with small-n buckets.
_SESSION_POS_CAP = 7
_SESSION_POS_LABELS = ["1", "2", "3", "4", "5", "6", "7+"]
# Per-person cell needs at least this many games to contribute to a
# bucket. Two thresholds because aggregate also needs enough contributors.
_SESSION_POS_MIN_PER_PERSON = 10
_SESSION_POS_MIN_PEOPLE = 3
# Per-person mode keeps a position if it has at least this many games.
_SESSION_POS_MIN_PER_BUCKET = 8


def plot_session_position_wr(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """WR by game-position-within-session. Tests the warm-up hypothesis
    (WR climbs from game 1 to 2) against the stamina-decay hypothesis
    (WR drops as the session grinds on).

    Sessions come from ``load_matches`` (gap-threshold split). Aggregate
    mode macro-averages per-person WRs and bootstraps a 95% CI across
    people. Per-person mode uses Wilson 95% CI on the binomial.
    """
    d_all = _filter_player(df, player)
    if d_all.empty or "session_game_idx" not in d_all.columns:
        return _empty_figure("No games to plot")

    d = d_all.copy()
    # session_game_idx is already 1-indexed; clip to the cap to pool the
    # long tail into "7+". This also makes "sessions of length 1
    # contribute only to position 1" automatic — they only ever produce a
    # single row with idx=1.
    d["pos"] = d["session_game_idx"].clip(upper=_SESSION_POS_CAP).astype(int)
    d = d.dropna(subset=["win", "pos"])
    if d.empty:
        return _empty_figure("Not enough session data to plot")

    is_aggregate_mode = _is_aggregate(player)
    rng = np.random.default_rng(41)
    n_boot = 2000

    rows: list[dict] = []
    if is_aggregate_mode:
        # Per-person WR per position, drop sparse cells, then macro-average
        # and bootstrap across the surviving per-person WRs.
        per_person = (
            d.groupby(["pos", "person"], observed=True)
            .agg(games=("win", "size"), wins=("win", "sum"))
            .reset_index()
        )
        per_person = per_person[per_person["games"] >= _SESSION_POS_MIN_PER_PERSON]
        per_person["wr"] = per_person["wins"] / per_person["games"]

        for pos in range(1, _SESSION_POS_CAP + 1):
            cell = per_person[per_person["pos"] == pos]
            if len(cell) < _SESSION_POS_MIN_PEOPLE:
                continue
            wrs = cell["wr"].to_numpy()
            boot = rng.choice(wrs, size=(n_boot, wrs.size), replace=True).mean(axis=1)
            rows.append(
                {
                    "pos": pos,
                    "wr": float(wrs.mean()),
                    "n": int(cell["games"].sum()),
                    "n_people": int(wrs.size),
                    "ci_lo": float(np.quantile(boot, 0.025)),
                    "ci_hi": float(np.quantile(boot, 0.975)),
                }
            )
    else:
        cell = (
            d.groupby("pos", observed=True)
            .agg(games=("win", "size"), wins=("win", "sum"))
            .reset_index()
        )
        cell = cell[cell["games"] >= _SESSION_POS_MIN_PER_BUCKET]
        for _, row in cell.iterrows():
            lo, hi = wilson_ci(int(row["wins"]), int(row["games"]))
            rows.append(
                {
                    "pos": int(row["pos"]),
                    "wr": float(row["wins"]) / float(row["games"]),
                    "n": int(row["games"]),
                    "n_people": 1,
                    "ci_lo": lo,
                    "ci_hi": hi,
                }
            )

    if not rows:
        return _empty_figure("No session positions met the minimum sample threshold")

    series = pd.DataFrame(rows).sort_values("pos").reset_index(drop=True)

    # Baseline: macro-averaged overall WR for aggregate mode, player's
    # overall WR otherwise.
    if is_aggregate_mode:
        per_person_overall = d.groupby("person")["win"].mean()
        baseline_wr = float(per_person_overall.mean())
        baseline_label = f"macro-average overall WR {baseline_wr:.0%}"
    else:
        baseline_wr = float(d["win"].mean())
        baseline_label = f"overall WR {baseline_wr:.0%}"

    fig, ax = plt.subplots(figsize=(10, 5.0))

    xs = (series["pos"] - 1).to_numpy()
    ys = series["wr"].to_numpy()
    colour = PALETTE["primary"]

    ax.plot(
        xs,
        ys,
        color=colour,
        marker="o",
        markersize=7,
        linewidth=2.2,
    )
    ax.fill_between(
        xs,
        series["ci_lo"].to_numpy(),
        series["ci_hi"].to_numpy(),
        color=colour,
        alpha=0.18,
        linewidth=0,
    )

    # n=... annotation above each marker; consistent placement since this
    # is a single-series chart with no collision risk.
    for xi, yi, ni in zip(xs, ys, series["n"].to_numpy(), strict=False):
        ax.annotate(
            f"n={int(ni)}",
            xy=(xi, yi),
            xytext=(0, 10),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=8,
            color=PALETTE["muted"],
        )

    _baseline(ax, y=baseline_wr, label=baseline_label)

    ax.set_xticks(list(range(_SESSION_POS_CAP)))
    ax.set_xticklabels(_SESSION_POS_LABELS)
    ax.set_xlabel("Game position within session")
    ax.set_ylabel("Win rate")
    ax.set_ylim(0.0, 1.0)

    subtitle = (
        f"Each game's position within a play session (sessions split when gap >"
        f"{SESSION_GAP_MIN}min). Tests whether you warm up or fatigue as you play longer."
    )

    if is_aggregate_mode:
        ax.set_title("Session-position WR - warm-up or stamina decay?")
    else:
        name = _display_label(player) or ""
        ax.set_title(f"Session-position WR - {name}")
    _subtitle(ax, subtitle)

    ax.legend(loc="lower right", fontsize=9)
    _polish_ax(ax)
    fig.tight_layout()
    return fig


_IMPROVEMENT_MIN_GAMES = 200
_IMPROVEMENT_ROLLING_WINDOW = 50


def _fit_improvement_slope(wins: np.ndarray) -> dict | None:
    """Logistic regression of win on game-index for one player.

    Design is a single column ``i / 100`` so the slope coefficient is in
    log-odds per 100 games. ``logistic_fit`` adds its own intercept, so
    ``X`` is shape (n, 1). Returns None when the fit is degenerate
    (all wins or all losses — no variance to explain).
    """
    n = wins.size
    if n < _IMPROVEMENT_MIN_GAMES:
        return None
    if wins.sum() in (0, n):
        return None

    idx_scaled = (np.arange(n, dtype=float) / 100.0)[:, None]
    beta, se, _ = logistic_fit(idx_scaled, wins.astype(float), l2=0.5)
    beta_slope = float(beta[1])
    se_slope = float(se[1]) if np.isfinite(se[1]) else float("nan")
    p_bar = float(wins.mean())
    # Local-derivative conversion: dP/di * 100 = beta * p(1-p) * 100,
    # evaluated at the player's mean WR. Sign-preserving on the CI bounds
    # because p(1-p) >= 0.
    scale = p_bar * (1.0 - p_bar) * 100.0
    slope_pp = beta_slope * scale
    if np.isfinite(se_slope) and se_slope > 0:
        beta_lo = beta_slope - 1.96 * se_slope
        beta_hi = beta_slope + 1.96 * se_slope
        ci_lo_pp = beta_lo * scale
        ci_hi_pp = beta_hi * scale
        p_value = wald_pvalue(beta_slope, se_slope)
    else:
        ci_lo_pp = float("nan")
        ci_hi_pp = float("nan")
        p_value = 1.0

    return {
        "n_games": int(n),
        "mean_wr": p_bar,
        "beta": beta_slope,
        "se": se_slope,
        "slope_pp": slope_pp,
        "ci_lo_pp": ci_lo_pp,
        "ci_hi_pp": ci_hi_pp,
        "p_value": p_value,
        "intercept": float(beta[0]),
    }


def plot_improvement_slope(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Per-player WR-over-time slope from a logistic fit on game-index.

    Aggregate: horizontal bar chart of slopes (pp WR change per 100
    games) with Wald 95% CI whiskers, one bar per person who has at
    least 200 games. Sign-significant bars get win/loss colour, others
    are neutral grey.

    Per-person: 50-game rolling WR (solid) overlaid with the dashed
    fitted-logistic predicted P(win) curve, annotated with slope and
    Wald p-value.
    """
    if _is_aggregate(player):
        results: list[dict] = []
        for person, sub in df.groupby("person", observed=True):
            wins = sub.sort_values("game_start")["win"].to_numpy(dtype=float)
            fit = _fit_improvement_slope(wins)
            if fit is None:
                continue
            fit["person"] = str(person)
            results.append(fit)

        if not results:
            return _empty_figure(
                f"No player has the {_IMPROVEMENT_MIN_GAMES}+ games needed for a slope estimate"
            )

        rows = pd.DataFrame(results).sort_values("slope_pp", ascending=True).reset_index(drop=True)
        y = np.arange(len(rows))

        colours: list[str] = []
        for _, r in rows.iterrows():
            lo, hi = r["ci_lo_pp"], r["ci_hi_pp"]
            if np.isfinite(lo) and np.isfinite(hi) and lo > 0:
                colours.append(PALETTE["win"])
            elif np.isfinite(lo) and np.isfinite(hi) and hi < 0:
                colours.append(PALETTE["loss"])
            else:
                colours.append(PALETTE["neutral"])

        fig_h = max(4.6, len(rows) * 0.45)
        fig, ax = plt.subplots(figsize=(13, fig_h))
        ax.barh(y, rows["slope_pp"], color=colours, height=0.7)

        # CI whiskers — only draw where the SE was finite.
        finite_ci = rows[["ci_lo_pp", "ci_hi_pp"]].apply(
            lambda r: np.isfinite(r["ci_lo_pp"]) and np.isfinite(r["ci_hi_pp"]), axis=1
        )
        if finite_ci.any():
            xerr = np.vstack(
                [
                    (rows["slope_pp"] - rows["ci_lo_pp"]).to_numpy(),
                    (rows["ci_hi_pp"] - rows["slope_pp"]).to_numpy(),
                ]
            )
            xerr[:, ~finite_ci.to_numpy()] = 0.0
            ax.errorbar(rows["slope_pp"], y, xerr=xerr, **WHISKER_STYLE)

        ax.axvline(0, color=PALETTE["text"], linewidth=0.8, linestyle=(0, (4, 4)))
        ax.set_yticks(y)
        ax.set_yticklabels(rows["person"])
        ax.set_xlabel("Slope (percentage points of WR per 100 games)")
        ax.set_title("Improvement slope - pp WR change per 100 games")
        _subtitle(
            ax,
            "Logistic regression of win on game-index, slope reported at each "
            "player's mean WR. CI from Wald. Positive = trending up over dataset window.",
        )
        _polish_ax(ax)

        max_abs = max(
            8.0,
            float(np.nanmax(np.abs(rows[["ci_lo_pp", "ci_hi_pp", "slope_pp"]].to_numpy()))) + 4.0,
        )
        ax.set_xlim(-max_abs, max_abs)

        for yi, (_, r) in enumerate(rows.iterrows()):
            label = f"{r['slope_pp']:+.1f}pp / 100g  (beta p={r['p_value']:.3f})"
            sign_pad = 1 if r["slope_pp"] >= 0 else -1
            anchor = r["ci_hi_pp"] if r["slope_pp"] >= 0 else r["ci_lo_pp"]
            if not np.isfinite(anchor):
                anchor = r["slope_pp"]
            ax.annotate(
                label,
                xy=(anchor, yi),
                xytext=(6 * sign_pad, 0),
                textcoords="offset points",
                va="center",
                ha="left" if r["slope_pp"] >= 0 else "right",
                fontsize=9,
                color=PALETTE["text"],
            )

        fig.tight_layout()
        return fig

    # --- per-person trajectory ----------------------------------------------
    person = _resolve_person(df, player)
    if person is None:
        return _empty_figure("No games to plot")
    sub = df[df["person"] == person].sort_values("game_start").reset_index(drop=True)
    n = len(sub)
    if n < _IMPROVEMENT_MIN_GAMES:
        return _empty_figure(
            f"Need >={_IMPROVEMENT_MIN_GAMES} games for slope estimate ({n} games)"
        )

    wins = sub["win"].to_numpy(dtype=float)
    fit = _fit_improvement_slope(wins)
    if fit is None:
        return _empty_figure(f"Cannot fit slope - degenerate outcomes ({n} games)")

    idx = np.arange(n, dtype=float)
    rolling = sub["win"].rolling(window=_IMPROVEMENT_ROLLING_WINDOW, min_periods=10).mean()

    # Predicted P(win) at evenly-spaced indices for the dashed overlay.
    smooth_idx = np.linspace(0.0, n - 1, num=min(400, n))
    z = fit["intercept"] + fit["beta"] * (smooth_idx / 100.0)
    z = np.clip(z, -30.0, 30.0)
    fit_curve = 1.0 / (1.0 + np.exp(-z))

    fig, ax = plt.subplots(figsize=(13, 4.6))
    ax.plot(
        idx,
        rolling,
        color=PALETTE["primary"],
        linewidth=2.0,
        label=f"Rolling {_IMPROVEMENT_ROLLING_WINDOW}-game WR",
    )
    ax.plot(
        smooth_idx,
        fit_curve,
        color=PALETTE["accent_orange"],
        linewidth=1.8,
        linestyle=(0, (5, 4)),
        label="Logistic fit",
    )
    _baseline(ax)
    ax.set_ylim(0.0, 1.05)
    ax.set_xlabel("Game index (0 = oldest)")
    ax.set_ylabel("Win rate")
    name = _display_label(player) or person
    ax.set_title(f"Improvement trajectory - {name}")
    if np.isfinite(fit["ci_lo_pp"]) and np.isfinite(fit["ci_hi_pp"]):
        ci_txt = f" [95% CI {fit['ci_lo_pp']:+.1f}, {fit['ci_hi_pp']:+.1f}]"
    else:
        ci_txt = ""
    _subtitle(
        ax,
        f"{_IMPROVEMENT_ROLLING_WINDOW}-game rolling WR (solid) vs logistic fit (dashed). "
        f"Slope: {fit['slope_pp']:+.1f}pp / 100 games (p={fit['p_value']:.3f}).",
    )

    annotation = (
        f"slope {fit['slope_pp']:+.1f}pp / 100g{ci_txt}\n"
        f"beta p={fit['p_value']:.3f}  ·  n={n}  ·  mean WR {fit['mean_wr']:.0%}"
    )
    ax.text(
        0.99,
        0.02,
        annotation,
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=9,
        color=PALETTE["text"],
        bbox={
            "facecolor": "white",
            "edgecolor": PALETTE["spine"],
            "boxstyle": "round,pad=0.4",
            "alpha": 0.9,
        },
    )
    ax.legend(loc="upper left", fontsize=9)
    _polish_ax(ax)
    fig.tight_layout()
    return fig


# --- 35. Per-player stat sheet ---------------------------------------------

_STAT_SHEET_MIN_GAMES = 50

# Metric definitions: (label, column in per-person frame, higher_is_better).
# higher_is_better drives the percentile inversion for "deaths/game" so the
# annotation always reads "high percentile = good".
_STAT_SHEET_METRICS: list[tuple[str, str, bool]] = [
    ("Win rate", "wr", True),
    ("KDA", "kda", True),
    ("Kills / game", "kpg", True),
    ("Deaths / game", "dpg", False),
    ("Assists / game", "apg", True),
    ("Avg game duration (min)", "dur_min", True),
]


def _stat_sheet_frame(df: pd.DataFrame) -> pd.DataFrame:
    """One row per person with the six stat-sheet metrics.

    KDA is computed per-game (the existing ``df["kda"]`` column already
    floors deaths at 1) then averaged — matches the rest of the codebase's
    "per-game KDA, then mean" convention.
    """
    grp = df.groupby("person", observed=True)
    out = grp.agg(
        games=("match_id", "size"),
        wr=("win", "mean"),
        kda=("kda", "mean"),
        kpg=("kills", "mean"),
        dpg=("deaths", "mean"),
        apg=("assists", "mean"),
        dur_min=("duration_min", "mean"),
    )
    return out[out["games"] >= _STAT_SHEET_MIN_GAMES].reset_index()


def _stat_sheet_format(metric_col: str, value: float) -> str:
    """Format a metric value the same way it appears on each subplot."""
    if metric_col == "wr":
        return f"{value:.0%}"
    return f"{value:.1f}"


def plot_stat_sheet(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Per-player "stat sheet" — where each player ranks across the group.

    Aggregate: 2x3 grid of horizontal strip plots, one per metric, with a
    dot per qualifying player, a median rule, and labels on the lowest
    and highest values.

    Per-person: same grid with the focal player's dot highlighted and a
    "You: VALUE (PCT pct)" annotation in each subplot. Percentiles are
    inverted for Deaths/game so "low = good" reads as a high score.
    """
    frame = _stat_sheet_frame(df)

    if _is_aggregate(player):
        if len(frame) < 3:
            return _empty_figure("Need >=3 players with >=50 games")
        focal = None
        focal_row = None
    else:
        person = _resolve_person(df, player)
        n_games = int((df["person"] == person).sum()) if person is not None else 0
        if person is None or person not in set(frame["person"]):
            return _empty_figure(f"Need >=50 games for stat sheet ({n_games} games)")
        focal = person
        focal_row = frame[frame["person"] == person].iloc[0]

    fig, axes = plt.subplots(2, 3, figsize=(12, 7))
    flat_axes = axes.flatten()

    for ax, (label, col, higher_better) in zip(flat_axes, _STAT_SHEET_METRICS, strict=False):
        values = frame[col].to_numpy(dtype=float)
        names = frame["person"].tolist()
        median = float(np.median(values))

        if focal is None:
            # Aggregate: every dot in the primary colour at moderate alpha.
            ax.scatter(
                values,
                np.zeros_like(values),
                s=70,
                color=PALETTE["primary"],
                alpha=0.75,
                edgecolors="white",
                linewidths=0.8,
                zorder=3,
            )
            # Label outermost low/high so the spread is readable at a glance.
            lo_idx = int(np.argmin(values))
            hi_idx = int(np.argmax(values))
            ax.annotate(
                names[hi_idx],
                xy=(values[hi_idx], 0),
                xytext=(0, 9),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=8,
                color=PALETTE["text"],
            )
            if hi_idx != lo_idx:
                ax.annotate(
                    names[lo_idx],
                    xy=(values[lo_idx], 0),
                    xytext=(0, -10),
                    textcoords="offset points",
                    ha="center",
                    va="top",
                    fontsize=8,
                    color=PALETTE["text"],
                )
        else:
            # Per-person: focal in primary, others desaturated grey.
            is_focal = frame["person"].to_numpy() == focal
            ax.scatter(
                values[~is_focal],
                np.zeros(int((~is_focal).sum())),
                s=55,
                color=PALETTE["neutral"],
                alpha=0.55,
                edgecolors="white",
                linewidths=0.6,
                zorder=2,
            )
            ax.scatter(
                values[is_focal],
                np.zeros(int(is_focal.sum())),
                s=120,
                color=PALETTE["primary"],
                edgecolors="white",
                linewidths=1.2,
                zorder=4,
            )

            focal_value = float(focal_row[col])
            # Average-rank percentile: lowest value -> 0, highest -> 100.
            ranks = pd.Series(values).rank(method="average")
            n = len(values)
            if n > 1:
                pct = (ranks.iloc[int(np.where(is_focal)[0][0])] - 1) / (n - 1) * 100.0
            else:
                pct = 50.0
            if not higher_better:
                pct = 100.0 - pct
            pct_int = int(round(pct))
            ax.text(
                0.5,
                0.92,
                f"You: {_stat_sheet_format(col, focal_value)} ({pct_int}th pct)",
                transform=ax.transAxes,
                ha="center",
                va="top",
                fontsize=9,
                color=PALETTE["text"],
                bbox={
                    "facecolor": "white",
                    "edgecolor": PALETTE["spine"],
                    "boxstyle": "round,pad=0.3",
                    "alpha": 0.9,
                },
            )

        # Median vertical, matched to the dashed muted style used elsewhere.
        ax.axvline(
            median,
            color=PALETTE["muted"],
            linewidth=0.9,
            linestyle=(0, (4, 4)),
            alpha=0.7,
            zorder=1,
        )

        ax.set_yticks([])
        ax.set_ylim(-0.6, 0.6)
        # Pad horizontal range so the outermost labels don't clip.
        v_min, v_max = float(np.min(values)), float(np.max(values))
        span = v_max - v_min if v_max > v_min else max(abs(v_max), 1.0)
        pad = span * 0.15 if span > 0 else 0.5
        ax.set_xlim(v_min - pad, v_max + pad)
        if col == "wr":
            ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _pos: f"{v:.0%}"))
        ax.set_title(label, fontsize=11, pad=6)
        _polish_ax(ax)

    if focal is None:
        suptitle = "Player stat sheet - friend group distributions"
        subtitle = "Each dot is one player. Median marked. Outliers labelled."
    else:
        display_name = _display_label(player) or focal
        suptitle = f"Stat sheet - {display_name}"
        subtitle = "Your position on each metric vs the friend group. Higher percentile = better."

    fig.suptitle(suptitle, fontsize=16, fontweight="bold", y=0.99)
    fig.text(
        0.5,
        0.945,
        subtitle,
        ha="center",
        va="top",
        fontsize=10,
        color=PALETTE["muted"],
        style="italic",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    return fig


# --- 36. Game pace - early stomper vs late scaler --------------------------

_PACE_MIN_GAMES = 50

# 5-minute duration bins from 15 to 50 min for the per-person WR-by-bin panel.
# Games outside [15, 50) are folded into the edge bins so they don't
# silently disappear via pd.cut returning NaN.
_PACE_BIN_EDGES = [15, 20, 25, 30, 35, 40, 45, 50]
_PACE_BIN_LABELS = ["15-20", "20-25", "25-30", "30-35", "35-40", "40-45", "45-50"]

# Shared bin edges for the overlaid win/loss duration histograms — both
# distributions must use the same bins or the overlay misleads.
_PACE_HIST_EDGES = np.arange(10, 56, 2)


def _welch_t(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """Welch's two-sample t-test, returns (mean_a - mean_b, two-sided p).

    Uses the Welch-Satterthwaite df and a normal-tail p-value approximation
    (erfc) — accurate enough for the n>=20-per-group regime this function
    sees and keeps us off scipy. Degenerate cases (n<2 in either group,
    zero variance in both) return p=1.0.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return (float("nan"), 1.0)
    m1, m2 = float(np.mean(a)), float(np.mean(b))
    s1, s2 = float(np.var(a, ddof=1)), float(np.var(b, ddof=1))
    diff = m1 - m2
    se2 = s1 / n1 + s2 / n2
    if se2 <= 0:
        return (diff, 1.0)
    t = diff / math.sqrt(se2)
    p = math.erfc(abs(t) / math.sqrt(2.0))
    return (diff, float(p))


def plot_game_pace(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Are wins shorter than losses (stomper) or longer (late scaler)?

    Aggregate: horizontal bar per player of ``avg_win_dur - avg_loss_dur``
    in minutes, sorted ascending. Negative bars are clear stompers (their
    wins end fast), positive bars are scalers. Welch's t p-value annotates
    each bar.

    Per-person: top panel overlays win/loss duration histograms with mean
    markers. Bottom panel shows WR per 5-minute duration bin with Wilson
    95% CIs - descriptive only, since duration is partly an outcome (stomps
    end fast).
    """
    if _is_aggregate(player):
        rows: list[dict] = []
        for person, sub in df.groupby("person", observed=True):
            if len(sub) < _PACE_MIN_GAMES:
                continue
            wins = sub.loc[sub["win"] == 1, "duration_min"].to_numpy()
            losses = sub.loc[sub["win"] == 0, "duration_min"].to_numpy()
            if len(wins) < 2 or len(losses) < 2:
                continue
            diff, p = _welch_t(wins, losses)
            rows.append(
                {
                    "person": str(person),
                    "avg_win_dur": float(np.mean(wins)),
                    "avg_loss_dur": float(np.mean(losses)),
                    "win_minus_loss": diff,
                    "p_value": p,
                    "n": len(sub),
                }
            )

        if len(rows) < 3:
            return _empty_figure("Need >=3 players with >=50 games")

        # Sort descending so the most-negative (stomper) bar lands at the
        # highest y, which renders at the TOP of a default barh axis.
        frame = (
            pd.DataFrame(rows).sort_values("win_minus_loss", ascending=False).reset_index(drop=True)
        )
        y = np.arange(len(frame))

        colours: list[str] = []
        for v in frame["win_minus_loss"]:
            if v < -1:
                colours.append(PALETTE["loss"])  # stomper - wins short, losses drag
            elif v > 1:
                colours.append(PALETTE["win"])  # scaler - wins long, losses end fast
            else:
                colours.append(PALETTE["neutral"])

        fig_h = max(4.2, len(frame) * 0.45)
        fig, ax = plt.subplots(figsize=(10, fig_h))
        ax.barh(y, frame["win_minus_loss"], color=colours, height=0.7)
        ax.axvline(0, color=PALETTE["text"], linewidth=0.8, linestyle=(0, (4, 4)))
        ax.set_yticks(y)
        ax.set_yticklabels(frame["person"])
        ax.set_xlabel("Avg win duration - avg loss duration (min)")
        ax.set_title("Game-pace style - wins vs losses duration delta")
        _subtitle(
            ax,
            "Negative = stomper (wins short, losses drag). Positive = scaler "
            "(wins long, losses end fast). Welch's t-test p-value shown.",
        )
        _polish_ax(ax)

        # Pad x-range so the right-aligned annotations don't clip.
        max_abs = max(2.0, float(np.max(np.abs(frame["win_minus_loss"]))) + 1.5)
        ax.set_xlim(-max_abs, max_abs)

        for yi, (_, r) in enumerate(frame.iterrows()):
            val = r["win_minus_loss"]
            label = f"{val:+.1f} min  (p={r['p_value']:.3f})"
            sign_pad = 1 if val >= 0 else -1
            ax.annotate(
                label,
                xy=(val, yi),
                xytext=(6 * sign_pad, 0),
                textcoords="offset points",
                va="center",
                ha="left" if val >= 0 else "right",
                fontsize=9,
                color=PALETTE["text"],
            )

        fig.tight_layout()
        return fig

    # --- per-person ---------------------------------------------------------
    person = _resolve_person(df, player)
    if person is None:
        return _empty_figure("No games to plot")
    sub = df[df["person"] == person]
    n = len(sub)
    if n < _PACE_MIN_GAMES:
        return _empty_figure(f"Need >=50 games for pace analysis ({n} games)")

    wins = sub.loc[sub["win"] == 1, "duration_min"].to_numpy()
    losses = sub.loc[sub["win"] == 0, "duration_min"].to_numpy()
    avg_win = float(np.mean(wins)) if len(wins) else float("nan")
    avg_loss = float(np.mean(losses)) if len(losses) else float("nan")

    fig, axes = plt.subplots(2, 1, figsize=(10, 7))
    name = _display_label(player) or person

    # --- top: duration histograms ---
    ax_top = axes[0]
    ax_top.hist(
        wins,
        bins=_PACE_HIST_EDGES,
        color=PALETTE["win"],
        alpha=0.55,
        label=f"Wins (n={len(wins)})",
    )
    ax_top.hist(
        losses,
        bins=_PACE_HIST_EDGES,
        color=PALETTE["loss"],
        alpha=0.55,
        label=f"Losses (n={len(losses)})",
    )
    if math.isfinite(avg_win):
        ax_top.axvline(
            avg_win,
            color=PALETTE["win"],
            linewidth=1.6,
            linestyle=(0, (5, 4)),
        )
        ax_top.annotate(
            f"win avg {avg_win:.1f}",
            xy=(avg_win, ax_top.get_ylim()[1] if False else 0),
            xytext=(4, 4),
            textcoords="offset points",
            xycoords=("data", "axes fraction"),
            fontsize=9,
            color=PALETTE["win"],
            va="bottom",
        )
    if math.isfinite(avg_loss):
        ax_top.axvline(
            avg_loss,
            color=PALETTE["loss"],
            linewidth=1.6,
            linestyle=(0, (5, 4)),
        )
        # Stagger the loss annotation vertically so the two means don't
        # overlap when win/loss averages are close together.
        ax_top.annotate(
            f"loss avg {avg_loss:.1f}",
            xy=(avg_loss, 0),
            xytext=(4, 18),
            textcoords="offset points",
            xycoords=("data", "axes fraction"),
            fontsize=9,
            color=PALETTE["loss"],
            va="bottom",
        )
    ax_top.set_xlabel("Game duration (min)")
    ax_top.set_ylabel("Games")
    ax_top.set_title(f"Game pace - {name}")
    diff, p = _welch_t(wins, losses)
    _subtitle(
        ax_top,
        "Top: duration distributions of wins vs losses. Bottom: WR by duration bin. "
        "Note: duration is partly determined by outcome (stomps end fast) - read this "
        f"as descriptive, not causal. Win-loss delta {diff:+.1f} min (p={p:.3f}).",
    )
    ax_top.legend(loc="upper right", fontsize=9)
    _polish_ax(ax_top)

    # --- bottom: WR by 5-min duration bin (15-50 min) ---
    # Fold games <15 into the first bin and games >=50 into the last so
    # they aren't silently dropped by pd.cut returning NaN.
    dur_clipped = sub["duration_min"].clip(
        lower=_PACE_BIN_EDGES[0], upper=_PACE_BIN_EDGES[-1] - 0.01
    )
    binned = pd.cut(dur_clipped, bins=_PACE_BIN_EDGES, labels=_PACE_BIN_LABELS, right=False)
    pace = (
        pd.DataFrame({"bin": binned, "win": sub["win"].to_numpy()})
        .groupby("bin", observed=False)["win"]
        .agg(["count", "sum", "mean"])
        .rename(columns={"count": "games", "sum": "wins_n", "mean": "winrate"})
        .reset_index()
    )
    # Wilson CI per bin - bin can legitimately have 0 games, in which case
    # we skip drawing the bar but keep the x-tick.
    cis = [
        wilson_ci(int(w), int(g)) if g > 0 else (float("nan"), float("nan"))
        for w, g in zip(pace["wins_n"], pace["games"], strict=False)
    ]
    lo = np.array([c[0] for c in cis])
    hi = np.array([c[1] for c in cis])
    means = pace["winrate"].to_numpy(dtype=float)

    ax_bot = axes[1]
    x = np.arange(len(pace))
    has_data = pace["games"].to_numpy() > 0
    ax_bot.bar(
        x[has_data],
        means[has_data],
        color=PALETTE["primary"],
        width=0.7,
    )
    if has_data.any():
        yerr = np.vstack(
            [
                means - lo,
                hi - means,
            ]
        )
        yerr[:, ~has_data] = 0.0
        ax_bot.errorbar(x, means, yerr=yerr, **WHISKER_STYLE)
    _baseline(ax_bot)
    ax_bot.set_xticks(x)
    ax_bot.set_xticklabels(pace["bin"])
    ax_bot.set_ylim(0, 1.1)
    ax_bot.set_xlabel("Game duration bin (min)")
    ax_bot.set_ylabel("Win rate")
    ax_bot.set_title("Win rate by duration bin")
    _subtitle(ax_bot, "Bars = pooled WR in bin; whiskers = Wilson 95% CI; n labelled per bin.")
    _annotate_bars(ax_bot, x, means, pace["games"])
    _polish_ax(ax_bot)

    fig.tight_layout()
    return fig


def plot_shrunk_champ_rankings(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Top champions ranked by Bayesian-shrunk WR.

    Answers "is this 65% WR real or just sample-size noise?". Raw WRs on
    thin samples get pulled toward a baseline (personal WR for per-person,
    group WR for aggregate) by a Beta prior. The shrunken bar is what
    matters; the raw bar shows the pre-shrinkage WR so the size of the
    correction is visible.
    """
    if _is_aggregate(player):
        baseline_wr = float(df["win"].mean())
        min_games = 30
        prior_n = 50.0
        title = "Top champions in the friend group - shrunken WR"
        subtitle = (
            "Combined wins/games across all players. Bayesian shrinkage "
            "(prior_n=50) pulls thin samples toward group baseline."
        )
        baseline_label = f"group baseline ({baseline_wr:.0%})"
        g = df.groupby("champion")["win"].agg(["count", "sum"])
        g = g.rename(columns={"count": "games", "sum": "wins"})
        g = g[g["games"] >= min_games]
        if g.empty:
            return _empty_figure(f"No champion has enough games (>={min_games})")
    else:
        d = _filter_player(df, player)
        if len(d) < 50:
            return _empty_figure(f"Need >=50 games for shrunk rankings ({len(d)} games)")
        baseline_wr = float(d["win"].mean())
        min_games = 5
        prior_n = 30.0
        name = _display_label(player) or "this player"
        title = f"Top champions by shrunken WR - {name}"
        subtitle = (
            "Bayesian shrinkage with personal-baseline prior (prior_n=30). "
            "Raw bar shows pre-shrinkage WR - shrinkage pulls small samples "
            "toward your baseline so you see real signal."
        )
        baseline_label = f"{name}'s baseline ({baseline_wr:.0%})"
        g = d.groupby("champion")["win"].agg(["count", "sum"])
        g = g.rename(columns={"count": "games", "sum": "wins"})
        g = g[g["games"] >= min_games]
        if g.empty:
            return _empty_figure(f"No champion has enough games (>={min_games})")

    g["raw_wr"] = g["wins"] / g["games"]
    g["shrunk_wr"] = [
        bayesian_shrunk_wr(int(w), int(n), baseline_wr, prior_n)
        for w, n in zip(g["wins"], g["games"], strict=False)
    ]
    # Top 15 by shrunken WR. Reversed so highest-WR row plots at the top
    # of the horizontal bar chart (barh y=0 is at the bottom).
    top = g.sort_values("shrunk_wr", ascending=False).head(15)
    top = top.iloc[::-1]

    fig, ax = plt.subplots(figsize=(10, 8))
    y = np.arange(len(top))

    colours = []
    for shrunk in top["shrunk_wr"]:
        if shrunk > baseline_wr + 0.03:
            colours.append(PALETTE["win"])
        elif shrunk < baseline_wr - 0.03:
            colours.append(PALETTE["loss"])
        else:
            colours.append(PALETTE["neutral"])

    ax.barh(y, top["shrunk_wr"], color=colours, height=0.72, label="Shrunken WR")
    # Raw WR as a small marker on the same row — shows the pre-shrinkage value.
    ax.scatter(
        top["raw_wr"],
        y,
        marker="|",
        s=140,
        color=PALETTE["text"],
        linewidths=1.6,
        zorder=3,
        label="Raw WR",
    )
    ax.axvline(
        baseline_wr,
        color=PALETTE["muted"],
        linestyle="--",
        linewidth=1.0,
        label=baseline_label,
    )

    ax.set_yticks(y)
    ax.set_yticklabels(
        [
            f"{champ}  n={int(n)}  raw={raw:.0%} -> shrunk={shr:.0%}"
            for champ, n, raw, shr in zip(
                top.index, top["games"], top["raw_wr"], top["shrunk_wr"], strict=False
            )
        ]
    )
    ax.set_xlabel("Win rate")
    ax.set_xlim(0, max(1.0, float(top["shrunk_wr"].max()) + 0.05))
    ax.set_title(title)
    _subtitle(ax, subtitle)
    ax.legend(loc="lower right", framealpha=0.85, fontsize=9)
    _polish_ax(ax)

    fig.tight_layout()
    return fig


def _pearson_r_with_p(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Pearson correlation + 2-sided p-value via t -> normal approximation.

    Returns (nan, nan) when n < 3 or the variance is too small for the t
    statistic to be defined (perfect correlation, zero variance, etc.).
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(x)
    if n < 3:
        return float("nan"), float("nan")
    xm = x - x.mean()
    ym = y - y.mean()
    denom = float(np.sqrt((xm * xm).sum() * (ym * ym).sum()))
    if denom <= 1e-12:
        return float("nan"), float("nan")
    r = float((xm * ym).sum() / denom)
    r = max(-1.0, min(1.0, r))
    if 1.0 - r * r <= 1e-12:
        return r, 0.0
    t = r * math.sqrt((n - 2) / (1.0 - r * r))
    # 2-sided p via standard-normal approximation to the t distribution.
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))
    return r, float(p)


def plot_champ_pool_concentration(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Champion pool concentration via Shannon entropy / effective N.

    Effective N = exp(H) where H is Shannon entropy (nats) of the pick
    distribution. Identifies one-tricks (low N) vs flex players (high N).
    Per-person view is a Pareto chart of pick frequency; aggregate view
    compares everyone's effective N and tests whether pool size correlates
    with WR.
    """
    if _is_aggregate(player):
        per_person = df.groupby("person").agg(games=("win", "size"), wins=("win", "sum"))
        per_person = per_person[per_person["games"] >= 50]
        if len(per_person) < 3:
            return _empty_figure("Need >=3 players with >=50 games")

        rows = []
        for person in per_person.index:
            d = df[df["person"] == person]
            counts = d["champion"].value_counts()
            total = int(counts.sum())
            p = counts.values / total
            p = p[p > 0]
            h = float(-(p * np.log(p)).sum())
            eff_n = float(np.exp(h))
            top3 = float(counts.head(3).sum()) / total
            rows.append(
                {
                    "person": person,
                    "eff_n": eff_n,
                    "distinct": int((counts > 0).sum()),
                    "top3_frac": top3,
                    "wr": float(per_person.loc[person, "wins"])
                    / float(per_person.loc[person, "games"]),
                    "games": int(per_person.loc[person, "games"]),
                }
            )

        agg = pd.DataFrame(rows).sort_values("eff_n", ascending=True)

        fig, axes = plt.subplots(2, 1, figsize=(10, 9))

        # Top panel: horizontal bar chart of eff_N.
        y_pos = np.arange(len(agg))
        axes[0].barh(y_pos, agg["eff_n"].values, color=PALETTE["primary"], height=0.7, alpha=0.85)
        axes[0].set_yticks(y_pos)
        axes[0].set_yticklabels(
            [
                f"{p}  eff_N={en:.1f}  distinct={k}  top3={t3:.0%}"
                for p, en, k, t3 in zip(
                    agg["person"], agg["eff_n"], agg["distinct"], agg["top3_frac"], strict=False
                )
            ]
        )
        median_eff = float(agg["eff_n"].median())
        axes[0].axvline(
            median_eff,
            color=PALETTE["muted"],
            linestyle="--",
            linewidth=1.0,
            label=f"median ({median_eff:.1f})",
        )
        axes[0].set_xlabel("Effective number of champions  (exp(Shannon entropy))")
        axes[0].set_title("Champion pool concentration - one-tricks vs flex")
        _subtitle(
            axes[0],
            "Top: effective number of champions (= exp(Shannon entropy)). "
            "Higher = wider pool. Lower = one-trick.",
        )
        axes[0].legend(loc="lower right", framealpha=0.85, fontsize=9)
        _polish_ax(axes[0])

        # Bottom panel: scatter of eff_N vs WR with regression line.
        x = agg["eff_n"].values
        y = agg["wr"].values
        axes[1].scatter(x, y, s=70, color=PALETTE["primary"], alpha=0.85, zorder=3)
        # Player name labels — slight x-offset so they sit beside the dot.
        x_span = float(x.max() - x.min()) if x.max() > x.min() else 1.0
        x_offset = 0.012 * x_span
        for xi, yi, name in zip(x, y, agg["person"], strict=False):
            axes[1].annotate(
                str(name),
                xy=(xi, yi),
                xytext=(xi + x_offset, yi),
                fontsize=9,
                color=PALETTE["text"],
                va="center",
            )
        # Least-squares fit line spanning the observed x range.
        if len(x) >= 2 and x.std() > 1e-9:
            slope, intercept = np.polyfit(x, y, 1)
            xs = np.array([x.min(), x.max()])
            axes[1].plot(
                xs,
                slope * xs + intercept,
                color=PALETTE["accent_orange"],
                linewidth=1.4,
                alpha=0.8,
                label="least-squares fit",
            )
            axes[1].legend(loc="lower right", framealpha=0.85, fontsize=9)
        r, p_val = _pearson_r_with_p(x, y)
        axes[1].set_xlabel("Effective number of champions")
        axes[1].set_ylabel("Win rate")
        axes[1].set_title("Pool size vs win rate")
        if np.isnan(r):
            sub = "Not enough data to compute Pearson correlation."
        else:
            sub = f"Pearson r={r:.2f}, p={p_val:.3f}. Does pool size correlate with WR?"
        _subtitle(axes[1], sub)
        _polish_ax(axes[1])

        fig.tight_layout()
        return fig

    # --- per-person view ---------------------------------------------------
    d = _filter_player(df, player)
    n = len(d)
    if n < 50:
        return _empty_figure(f"Need >=50 games for pool analysis ({n} games)")

    counts = d["champion"].value_counts()
    total = int(counts.sum())
    probs = counts.values / total
    probs = probs[probs > 0]
    h = float(-(probs * np.log(probs)).sum())
    eff_n = float(np.exp(h))
    k_distinct = int((counts > 0).sum())
    top3_pct = float(counts.head(3).sum()) / total
    top10_pct = float(counts.head(10).sum()) / total

    top = counts.head(30)
    cum_frac = top.cumsum().values / total

    fig, ax = plt.subplots(figsize=(12, 6))
    x_pos = np.arange(len(top))
    ax.bar(x_pos, top.values, color=PALETTE["primary"], alpha=0.85, width=0.78)
    ax.set_xticks(x_pos)
    ax.set_xticklabels(top.index, rotation=45, ha="right", fontsize=9)
    ax.set_ylabel("Games on champion")
    name = _display_label(player) or "this player"
    ax.set_title(f"Champion pool - {name}")
    _subtitle(
        ax,
        f"Effective N = {eff_n:.1f}, distinct = {k_distinct}, "
        f"top3 cover {top3_pct:.0%}, top10 cover {top10_pct:.0%}.",
    )
    _polish_ax(ax)

    ax2 = ax.twinx()
    ax2.plot(
        x_pos,
        cum_frac,
        color=PALETTE["accent_orange"],
        linewidth=1.6,
        marker="o",
        markersize=3.5,
        label="cumulative share",
    )
    ax2.set_ylim(0, 1.02)
    ax2.set_ylabel("Cumulative share of games")
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax2.grid(False)
    ax2.spines["top"].set_visible(False)

    # Annotate threshold crossings. With heavy concentration these can fall
    # on adjacent bars, so y-stagger keeps the labels readable.
    y_offsets = [0.08, 0.16, 0.24]
    for threshold, y_off in zip((0.5, 0.8, 1.0), y_offsets, strict=False):
        hit = np.where(cum_frac >= threshold - 1e-9)[0]
        if len(hit) == 0:
            continue
        idx = int(hit[0])
        ax2.axvline(idx, color=PALETTE["muted"], linewidth=0.7, linestyle=":", alpha=0.7)
        ax2.annotate(
            f"{threshold:.0%} at {top.index[idx]}",
            xy=(idx, cum_frac[idx]),
            xytext=(idx + 0.4, min(1.0, cum_frac[idx]) - y_off),
            fontsize=9,
            color=PALETTE["muted"],
            arrowprops={
                "arrowstyle": "-",
                "color": PALETTE["muted"],
                "linewidth": 0.7,
                "alpha": 0.6,
            },
        )

    fig.tight_layout()
    return fig


# --- 39. Champion mastery learning curve -----------------------------------

_MASTERY_BUCKET_EDGES = [1, 11, 21, 51, np.inf]
_MASTERY_BUCKET_LABELS = ["1-10", "11-20", "21-50", "51+"]
_MASTERY_MIN_GAMES_PER_PAIR = 30
_MASTERY_MIN_GAMES_PER_BUCKET = 5


def _mastery_pair_buckets(df: pd.DataFrame) -> pd.DataFrame:
    """Per-(person, champion) game-on-champ index and bucket assignment.

    Re-cumcounts on ``(person, champion)`` rather than reusing the
    prebuilt ``nth_on_champ`` (which is per-Riot-account — a player
    with two smurfs would otherwise restart their Zac counter on each).
    Returns rows of qualifying pairs only (>= MIN_GAMES_PER_PAIR games).
    """
    d = df[["person", "champion", "game_start", "win"]].copy()
    d = d.sort_values(["person", "champion", "game_start"]).reset_index(drop=True)
    d["nth"] = d.groupby(["person", "champion"]).cumcount() + 1

    games_per_pair = d.groupby(["person", "champion"])["win"].transform("size")
    d = d[games_per_pair >= _MASTERY_MIN_GAMES_PER_PAIR].copy()
    if d.empty:
        return d

    d["bucket"] = pd.cut(
        d["nth"],
        bins=_MASTERY_BUCKET_EDGES,
        labels=_MASTERY_BUCKET_LABELS,
        right=False,
        ordered=True,
    )
    return d


def _mastery_pair_bucket_wr(d: pd.DataFrame) -> pd.DataFrame:
    """Per-(person, champion, bucket) WR for buckets with >=5 games."""
    grp = d.groupby(["person", "champion", "bucket"], observed=True).agg(
        games=("win", "size"), wins=("win", "sum")
    )
    grp = grp[grp["games"] >= _MASTERY_MIN_GAMES_PER_BUCKET].reset_index()
    grp["wr"] = grp["wins"] / grp["games"]
    return grp


def _mastery_bootstrap_ci(values: np.ndarray, n_boot: int = 1000) -> tuple[float, float]:
    """2.5 / 97.5 percentile of resampled means. Returns (lo, hi)."""
    if len(values) == 0:
        return (np.nan, np.nan)
    rng = np.random.default_rng(20240524)
    n = len(values)
    means = np.empty(n_boot)
    for i in range(n_boot):
        sample = rng.choice(values, size=n, replace=True)
        means[i] = sample.mean()
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def plot_champion_mastery(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Champion mastery learning curve — does WR improve with reps?

    For each (person, champion) pair with >=30 games we sort by game
    start, label each game 1, 2, 3, ... and bucket by play count
    (1-10, 11-20, 21-50, 51+). Per bucket we compute that pair's WR
    on that champ (>=5 games per bucket to count). Aggregate is the
    macro mean of those per-pair WR values per bucket.
    """
    pair_rows = _mastery_pair_buckets(df)
    if _is_aggregate(player):
        if pair_rows.empty:
            return _empty_figure("Need >=5 (player, champion) pairs with >=30 games")
        return _plot_mastery_aggregate(pair_rows)

    label = _display_label(player) or "this player"
    person = _resolve_person(df, player)
    if person is None:
        return _empty_figure(f"No champion with >=30 games for {label}")
    person_rows = pair_rows[pair_rows["person"] == person]
    if person_rows.empty:
        return _empty_figure(f"No champion with >=30 games for {label}")
    return _plot_mastery_per_person(person_rows, label, player)


def _plot_mastery_aggregate(pair_rows: pd.DataFrame) -> plt.Figure:
    bucket_wr = _mastery_pair_bucket_wr(pair_rows)

    n_pairs_total = bucket_wr[["person", "champion"]].drop_duplicates().shape[0]
    if n_pairs_total < 5:
        return _empty_figure("Need >=5 (player, champion) pairs with >=30 games")

    per_bucket = (
        bucket_wr.groupby("bucket", observed=True)
        .agg(
            wr=("wr", "mean"),
            n_pairs=("wr", "size"),
            n_games=("games", "sum"),
        )
        .reindex(_MASTERY_BUCKET_LABELS)
    )

    cis: list[tuple[float, float]] = []
    for bucket in _MASTERY_BUCKET_LABELS:
        vals = bucket_wr.loc[bucket_wr["bucket"] == bucket, "wr"].to_numpy()
        cis.append(_mastery_bootstrap_ci(vals))
    per_bucket["ci_lo"] = [c[0] for c in cis]
    per_bucket["ci_hi"] = [c[1] for c in cis]

    global_macro = float(bucket_wr["wr"].mean())

    # Paired test: pairs with data in BOTH "first 10" AND ("21-50" or "51+").
    first = bucket_wr[bucket_wr["bucket"] == "1-10"].set_index(["person", "champion"])
    late = bucket_wr[bucket_wr["bucket"].isin(["21-50", "51+"])]
    # Macro WR over pooled "21+" games per pair (weighted by games inside the pair).
    late_agg = late.groupby(["person", "champion"]).apply(
        lambda g: pd.Series({"wr_late": g["wins"].sum() / g["games"].sum()})
    )
    paired = first[["wr"]].rename(columns={"wr": "wr_early"}).join(late_agg, how="inner")
    paired_diff = (paired["wr_late"] - paired["wr_early"]).to_numpy()
    n_paired = len(paired_diff)
    if n_paired >= 2:
        mean_diff = float(paired_diff.mean())
        diff_ci = _mastery_bootstrap_ci(paired_diff)
        paired_summary = (
            f"Paired late-minus-early WR: {mean_diff:+.1%} "
            f"[{diff_ci[0]:+.1%}, {diff_ci[1]:+.1%}], n_pairs={n_paired}."
        )
    else:
        paired_summary = "Not enough pairs spanning early and late buckets for a paired test."

    fig, ax = plt.subplots(figsize=(10, 6))
    x = np.arange(len(_MASTERY_BUCKET_LABELS))
    means = per_bucket["wr"].to_numpy()
    lo = per_bucket["ci_lo"].to_numpy()
    hi = per_bucket["ci_hi"].to_numpy()
    err_lo = np.where(np.isnan(means), 0.0, means - lo)
    err_hi = np.where(np.isnan(means), 0.0, hi - means)
    bar_means = np.where(np.isnan(means), 0.0, means)

    ax.bar(
        x,
        bar_means,
        color=PALETTE["primary"],
        width=0.62,
        zorder=2,
    )
    ax.errorbar(
        x,
        bar_means,
        yerr=[err_lo, err_hi],
        fmt="none",
        ecolor=PALETTE["muted"],
        elinewidth=1.0,
        capsize=4,
        alpha=0.7,
        zorder=3,
    )

    _baseline(ax, y=global_macro, label=f"Global macro WR ({global_macro:.0%})")
    ax.set_xticks(x)
    ax.set_xticklabels([f"Games {b}" for b in _MASTERY_BUCKET_LABELS])
    ax.set_ylabel("Macro-averaged win rate")
    ax.set_xlabel("Game-on-champion bucket")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    top_hi = float(np.nanmax(np.where(np.isnan(hi), 0.0, hi))) if len(hi) else 0.6
    ax.set_ylim(0, max(0.85, top_hi + 0.08))

    ax.set_title("Champion mastery - does WR improve with practice?")
    _subtitle(
        ax,
        "Macro-averaged across all (player, champion) pairs with >=30 games. "
        "Each (pair, bucket) contributes one WR value to the mean. " + paired_summary,
    )

    for xi, bucket in enumerate(_MASTERY_BUCKET_LABELS):
        wr = per_bucket.loc[bucket, "wr"]
        n_pairs = per_bucket.loc[bucket, "n_pairs"]
        n_games = per_bucket.loc[bucket, "n_games"]
        if pd.isna(wr):
            ax.annotate(
                "no data",
                xy=(xi, 0.02),
                ha="center",
                va="bottom",
                fontsize=9,
                color=PALETTE["muted"],
            )
            continue
        ax.annotate(
            f"{wr:.0%}  pairs={int(n_pairs)}  games={int(n_games)}",
            xy=(xi, float(wr)),
            xytext=(0, 6),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=9,
            color=PALETTE["text"],
        )

    ax.legend(loc="upper left", fontsize=9)
    _polish_ax(ax)
    fig.tight_layout()
    return fig


def _plot_mastery_per_person(
    person_rows: pd.DataFrame, label: str, player: str | None
) -> plt.Figure:
    bucket_wr = _mastery_pair_bucket_wr(person_rows)
    if bucket_wr.empty:
        return _empty_figure(f"No champion with >=30 games for {label}")

    # Order champions by total qualifying games desc — colour cycle gets
    # assigned to the most-played first so the busiest curves dominate.
    champs_by_games = (
        bucket_wr.groupby("champion")["games"].sum().sort_values(ascending=False).index.tolist()
    )

    fig, ax = plt.subplots(figsize=(12, 6))
    x_pos = {b: i for i, b in enumerate(_MASTERY_BUCKET_LABELS)}

    # Track end-of-line label positions so we can nudge collisions.
    end_labels: list[tuple[float, float, str, str]] = []  # (x, y, name, colour)
    for idx, champ in enumerate(champs_by_games):
        sub = bucket_wr[bucket_wr["champion"] == champ].sort_values("bucket")
        xs = [x_pos[b] for b in sub["bucket"]]
        ys = sub["wr"].to_numpy()
        colour = SERIES_CYCLE[idx % len(SERIES_CYCLE)]
        ax.plot(
            xs,
            ys,
            color=colour,
            linewidth=2.0,
            marker="o",
            markersize=5,
            alpha=0.9,
            label=champ,
        )
        end_labels.append((float(xs[-1]), float(ys[-1]), champ, colour))

    _baseline(ax, y=0.5, label="50%")
    ax.set_xticks(list(x_pos.values()))
    ax.set_xticklabels([f"Games {b}" for b in _MASTERY_BUCKET_LABELS])
    ax.set_ylim(0, 1.0)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax.set_xlabel("Game-on-champion bucket")
    ax.set_ylabel("Win rate")
    ax.set_title(_title("Mastery curves", player))
    _subtitle(ax, "WR by play-count bucket for your most-played champions (>=30 games each).")

    # Pad right so end-of-line annotations don't fall off the axes.
    ax.set_xlim(-0.4, len(_MASTERY_BUCKET_LABELS) - 1 + 0.9)

    # Greedy y-nudge: sort by y, push the next one down if it overlaps.
    # The annotation's anchor stays at the true (x_end, y_end); only the
    # text label position is shifted in data coordinates with a thin
    # leader line so the reader can still match label -> endpoint.
    end_labels.sort(key=lambda t: t[1], reverse=True)
    last_y = float("inf")
    min_gap = 0.04
    for x_end, y_end, name, colour in end_labels:
        text_y = min(y_end, last_y - min_gap)
        last_y = text_y
        needs_leader = abs(text_y - y_end) > 1e-3
        ax.annotate(
            name,
            xy=(x_end, y_end),
            xytext=(x_end + 0.08, text_y),
            textcoords="data",
            ha="left",
            va="center",
            fontsize=9,
            color=colour,
            arrowprops=(
                {
                    "arrowstyle": "-",
                    "color": colour,
                    "linewidth": 0.6,
                    "alpha": 0.5,
                }
                if needs_leader
                else None
            ),
        )

    _polish_ax(ax)
    fig.tight_layout()
    return fig


# --- 40. Champion rust effect ----------------------------------------------

_RUST_BUCKET_EDGES = [0, 1, 3, 7, 30, 90, np.inf]
_RUST_BUCKET_LABELS = ["<1d", "1-3d", "3-7d", "7-30d", "30-90d", ">90d"]
_RUST_MIN_GAMES_PER_PAIR = 10
_RUST_MIN_SAMPLES_PER_BUCKET = 3
_RUST_MIN_GAMES_PER_PERSON = 100


def _rust_pair_buckets(df: pd.DataFrame) -> pd.DataFrame:
    """Per-game rust_days + bucket for (person, champion) pairs with >=10 games.

    Sort by (person, champion, game_start) then shift to get the previous
    game on THAT champion for THAT person. The first game on a champion
    has no rust (NaN) and is dropped.
    """
    d = df[["person", "champion", "game_start", "win"]].copy()
    d = d.sort_values(["person", "champion", "game_start"]).reset_index(drop=True)

    games_per_pair = d.groupby(["person", "champion"])["win"].transform("size")
    d = d[games_per_pair >= _RUST_MIN_GAMES_PER_PAIR].copy()
    if d.empty:
        return d

    prev = d.groupby(["person", "champion"])["game_start"].shift(1)
    d["rust_days"] = (d["game_start"] - prev) / pd.Timedelta(days=1)
    d = d.dropna(subset=["rust_days"]).copy()
    if d.empty:
        return d

    d["bucket"] = pd.cut(
        d["rust_days"],
        bins=_RUST_BUCKET_EDGES,
        labels=_RUST_BUCKET_LABELS,
        right=False,
        ordered=True,
    )
    return d


def _rust_pair_bucket_wr(d: pd.DataFrame) -> pd.DataFrame:
    """Per-(person, champion, bucket) WR, restricted to cells with >=3 samples."""
    grp = d.groupby(["person", "champion", "bucket"], observed=True).agg(
        games=("win", "size"), wins=("win", "sum")
    )
    grp = grp[grp["games"] >= _RUST_MIN_SAMPLES_PER_BUCKET].reset_index()
    grp["wr"] = grp["wins"] / grp["games"]
    return grp


def _rust_bootstrap_ci(values: np.ndarray, n_boot: int = 1000) -> tuple[float, float]:
    """2.5 / 97.5 percentile of resampled means. Returns (lo, hi)."""
    if len(values) == 0:
        return (np.nan, np.nan)
    rng = np.random.default_rng(20260524)
    n = len(values)
    means = np.empty(n_boot)
    for i in range(n_boot):
        sample = rng.choice(values, size=n, replace=True)
        means[i] = sample.mean()
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def plot_champion_rust(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Champion rust effect — does time-off-a-champion hurt WR?

    For each (person, champion) pair with >=10 games we compute the gap
    since the previous game on that champion, bucket it, and roll up WR
    per bucket. Aggregate is the macro mean of (pair, bucket) WRs across
    pairs with >=3 samples in that bucket. Per-person view pools games
    across all the focal player's champions and uses Wilson CIs.
    """
    pair_rows = _rust_pair_buckets(df)
    if _is_aggregate(player):
        if pair_rows.empty:
            return _empty_figure("Need >=5 (player, champion) pairs with >=10 games")
        return _plot_rust_aggregate(pair_rows)

    label = _display_label(player) or "this player"
    person = _resolve_person(df, player)
    if person is None:
        return _empty_figure(f"No games for {label}")
    person_total_games = int((df["person"] == person).sum())
    if person_total_games < _RUST_MIN_GAMES_PER_PERSON:
        return _empty_figure(f"Need >=100 games for rust analysis ({person_total_games} games)")
    person_rows = pair_rows[pair_rows["person"] == person]
    if person_rows.empty:
        return _empty_figure(f"No champion with >=10 games for {label}")
    return _plot_rust_per_person(person_rows, label, player)


def _plot_rust_aggregate(pair_rows: pd.DataFrame) -> plt.Figure:
    bucket_wr = _rust_pair_bucket_wr(pair_rows)

    n_pairs_total = bucket_wr[["person", "champion"]].drop_duplicates().shape[0]
    if n_pairs_total < 5:
        return _empty_figure("Need >=5 (player, champion) pairs with >=10 games")

    per_bucket = (
        bucket_wr.groupby("bucket", observed=True)
        .agg(
            wr=("wr", "mean"),
            n_pairs=("wr", "size"),
            n_games=("games", "sum"),
        )
        .reindex(_RUST_BUCKET_LABELS)
    )

    cis: list[tuple[float, float]] = []
    for bucket in _RUST_BUCKET_LABELS:
        vals = bucket_wr.loc[bucket_wr["bucket"] == bucket, "wr"].to_numpy()
        cis.append(_rust_bootstrap_ci(vals))
    per_bucket["ci_lo"] = [c[0] for c in cis]
    per_bucket["ci_hi"] = [c[1] for c in cis]

    global_macro = float(bucket_wr["wr"].mean())

    fig, ax = plt.subplots(figsize=(10, 6))
    x = np.arange(len(_RUST_BUCKET_LABELS))
    means = per_bucket["wr"].to_numpy(dtype=float)
    lo = per_bucket["ci_lo"].to_numpy(dtype=float)
    hi = per_bucket["ci_hi"].to_numpy(dtype=float)
    valid = ~np.isnan(means)

    ax.plot(
        x[valid],
        means[valid],
        color=PALETTE["primary"],
        linewidth=2.0,
        marker="o",
        markersize=7,
        zorder=3,
    )
    err_lo = np.where(np.isnan(means), 0.0, means - lo)
    err_hi = np.where(np.isnan(means), 0.0, hi - means)
    ax.errorbar(
        x[valid],
        means[valid],
        yerr=[err_lo[valid], err_hi[valid]],
        **WHISKER_STYLE,
        zorder=2,
    )

    _baseline(ax, y=global_macro, label=f"Overall macro WR ({global_macro:.0%})")
    ax.set_xticks(x)
    ax.set_xticklabels(_RUST_BUCKET_LABELS)
    ax.set_xlabel("Days since you last played this champion")
    ax.set_ylabel("Macro-averaged win rate")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    if np.any(valid):
        top_hi = float(np.nanmax(hi[valid]))
        bot_lo = float(np.nanmin(lo[valid]))
    else:
        top_hi, bot_lo = 0.6, 0.4
    ax.set_ylim(max(0.0, bot_lo - 0.08), min(1.0, top_hi + 0.10))

    ax.set_title("Champion rust - does time-off-a-champion hurt WR?")
    _subtitle(
        ax,
        "Macro-averaged WR by days since you last played the same champion. "
        "Each (player, champion) pair with >=10 games contributes; "
        "per-bucket cells need >=3 samples to enter the mean.",
    )

    for xi, bucket in enumerate(_RUST_BUCKET_LABELS):
        wr = per_bucket.loc[bucket, "wr"]
        n_pairs = per_bucket.loc[bucket, "n_pairs"]
        if pd.isna(wr):
            ax.annotate(
                "no data",
                xy=(xi, ax.get_ylim()[0] + 0.01),
                ha="center",
                va="bottom",
                fontsize=9,
                color=PALETTE["muted"],
            )
            continue
        ax.annotate(
            f"n={int(n_pairs)}",
            xy=(xi, float(wr)),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=9,
            color=PALETTE["text"],
        )

    ax.legend(loc="upper right", fontsize=9)
    _polish_ax(ax)
    fig.tight_layout()
    return fig


def _plot_rust_per_person(person_rows: pd.DataFrame, label: str, player: str | None) -> plt.Figure:
    # Pool across all of this person's qualifying champions; one WR per
    # bucket via wins / games (Wilson CI on the binomial proportion).
    per_bucket = (
        person_rows.groupby("bucket", observed=True)
        .agg(games=("win", "size"), wins=("win", "sum"))
        .reindex(_RUST_BUCKET_LABELS)
        .fillna(0)
        .astype(int)
    )
    per_bucket["wr"] = np.where(
        per_bucket["games"] > 0, per_bucket["wins"] / per_bucket["games"], np.nan
    )

    cis = [
        wilson_ci(int(w), int(g)) if g > 0 else (float("nan"), float("nan"))
        for w, g in zip(per_bucket["wins"], per_bucket["games"], strict=False)
    ]
    per_bucket["ci_lo"] = [c[0] for c in cis]
    per_bucket["ci_hi"] = [c[1] for c in cis]

    total_wins = int(per_bucket["wins"].sum())
    total_games = int(per_bucket["games"].sum())
    overall = total_wins / total_games if total_games > 0 else float("nan")

    fig, ax = plt.subplots(figsize=(10, 6))
    x = np.arange(len(_RUST_BUCKET_LABELS))
    means = per_bucket["wr"].to_numpy(dtype=float)
    lo = per_bucket["ci_lo"].to_numpy(dtype=float)
    hi = per_bucket["ci_hi"].to_numpy(dtype=float)
    valid = ~np.isnan(means)

    ax.plot(
        x[valid],
        means[valid],
        color=PALETTE["primary"],
        linewidth=2.0,
        marker="o",
        markersize=7,
        zorder=3,
    )
    err_lo = np.where(np.isnan(means), 0.0, means - lo)
    err_hi = np.where(np.isnan(means), 0.0, hi - means)
    ax.errorbar(
        x[valid],
        means[valid],
        yerr=[err_lo[valid], err_hi[valid]],
        **WHISKER_STYLE,
        zorder=2,
    )

    if not np.isnan(overall):
        _baseline(ax, y=overall, label=f"Personal WR ({overall:.0%})")

    ax.set_xticks(x)
    ax.set_xticklabels(_RUST_BUCKET_LABELS)
    ax.set_xlabel("Days since you last played this champion")
    ax.set_ylabel("Win rate")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    if np.any(valid):
        top_hi = float(np.nanmax(hi[valid]))
        bot_lo = float(np.nanmin(lo[valid]))
    else:
        top_hi, bot_lo = 0.6, 0.4
    ax.set_ylim(max(0.0, bot_lo - 0.08), min(1.0, top_hi + 0.10))

    ax.set_title(_title("Champion rust", player))
    _subtitle(ax, "Your WR by days since last playing the same champion.")

    for xi, bucket in enumerate(_RUST_BUCKET_LABELS):
        wr = per_bucket.loc[bucket, "wr"]
        n = int(per_bucket.loc[bucket, "games"])
        if pd.isna(wr):
            ax.annotate(
                "no data",
                xy=(xi, ax.get_ylim()[0] + 0.01),
                ha="center",
                va="bottom",
                fontsize=9,
                color=PALETTE["muted"],
            )
            continue
        ax.annotate(
            f"n={n}",
            xy=(xi, float(wr)),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=9,
            color=PALETTE["text"],
        )

    ax.legend(loc="upper right", fontsize=9)
    _polish_ax(ax)
    fig.tight_layout()
    return fig


# --- DOW x time-of-day WR heatmap -----------------------------------------


#: Order matters for the 4-column heatmap layout.
_HOUR_BUCKET_LABELS = [
    "Morning (5-12)",
    "Afternoon (12-18)",
    "Evening (18-22)",
    "Night (22-5)",
]


def _hour_bucket(hour: int) -> str:
    """Map 0-23 hour-of-day to one of the four named buckets.

    Night wraps midnight (22, 23, 0, 1, 2, 3, 4) so ``pd.cut`` won't fit;
    a tiny lookup is clearer than a ranged categorical with a join.
    """
    if 5 <= hour < 12:
        return "Morning (5-12)"
    if 12 <= hour < 18:
        return "Afternoon (12-18)"
    if 18 <= hour < 22:
        return "Evening (18-22)"
    return "Night (22-5)"


def plot_dow_hour_heatmap(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Win-rate heatmap on the day-of-week x time-of-day grid.

    Coarser bucketing than the 24x7 hour heatmap so each cell has enough
    samples to colour confidently. Aggregate cells need >=30 games AND
    >=3 contributing players; per-person cells need >=15 games. Colour
    encodes the gap to the relevant baseline (overall macro WR for
    aggregate, personal overall WR for the per-person view), clipped at
    +/- 10pp and mapped on RdYlGn so red is below baseline and green
    above.
    """
    d = _filter_player(df, player).copy()
    if d.empty:
        return _empty_figure("No games to plot")

    is_aggregate = _is_aggregate(player)
    label = _display_label(player)

    if not is_aggregate:
        person_total = len(d)
        if person_total < 100:
            return _empty_figure(f"Need >=100 games for heatmap ({person_total} games)")

    d["hour_bucket"] = d["hour"].map(_hour_bucket)
    d["hour_bucket"] = pd.Categorical(
        d["hour_bucket"], categories=_HOUR_BUCKET_LABELS, ordered=True
    )

    n_rows = len(DOW_LABELS)
    n_cols = len(_HOUR_BUCKET_LABELS)
    wr_grid = np.full((n_rows, n_cols), np.nan)
    n_grid = np.zeros((n_rows, n_cols), dtype=int)
    qualified = np.zeros((n_rows, n_cols), dtype=bool)

    if is_aggregate:
        per_person_cell = (
            d.groupby(["dow", "hour_bucket", "person"], observed=True)
            .agg(games=("win", "size"), wins=("win", "sum"))
            .reset_index()
        )
        per_person_cell["wr"] = per_person_cell["wins"] / per_person_cell["games"]
        cells = (
            per_person_cell.groupby(["dow", "hour_bucket"], observed=True)
            .agg(
                wr=("wr", "mean"),
                n_games=("games", "sum"),
                n_players=("person", "nunique"),
            )
            .reset_index()
        )
        # Overall baseline: macro-average across people of their pooled WR.
        per_person_overall = d.groupby("person")["win"].mean()
        baseline = float(per_person_overall.mean())

        for _, row in cells.iterrows():
            r = int(row["dow"])
            c = _HOUR_BUCKET_LABELS.index(str(row["hour_bucket"]))
            n_grid[r, c] = int(row["n_games"])
            if int(row["n_games"]) >= 30 and int(row["n_players"]) >= 3:
                wr_grid[r, c] = float(row["wr"])
                qualified[r, c] = True

        if not qualified.any():
            return _empty_figure("Not enough games for any cell")

        title = "When does the group win? - DOW x time heatmap"
        subtitle = (
            "Macro-averaged WR per cell, coloured by gap to overall WR. "
            "Cells with <30 games or <3 contributing players shown grey."
        )
    else:
        cells = (
            d.groupby(["dow", "hour_bucket"], observed=True)
            .agg(games=("win", "size"), wins=("win", "sum"))
            .reset_index()
        )
        cells["wr"] = cells["wins"] / cells["games"]
        baseline = float(d["win"].mean())

        for _, row in cells.iterrows():
            r = int(row["dow"])
            c = _HOUR_BUCKET_LABELS.index(str(row["hour_bucket"]))
            n_grid[r, c] = int(row["games"])
            if int(row["games"]) >= 15:
                wr_grid[r, c] = float(row["wr"])
                qualified[r, c] = True

        title = f"When does {label} win? - DOW x time heatmap"
        subtitle = (
            "WR per cell, coloured by gap to your overall WR. " "Cells with <15 games shown grey."
        )

    # Delta in percentage points, clipped at +/- 10pp.
    delta_pp = (wr_grid - baseline) * 100.0
    delta_clipped = np.clip(delta_pp, -10.0, 10.0)

    fig, ax = plt.subplots(figsize=(8, 7))
    cmap = plt.get_cmap("RdYlGn").copy()
    cmap.set_bad(color="#d9d9d9")

    masked = np.ma.array(delta_clipped, mask=~qualified)
    im = ax.imshow(
        masked,
        aspect="auto",
        cmap=cmap,
        vmin=-10.0,
        vmax=10.0,
        origin="upper",
    )

    ax.set_xticks(range(n_cols))
    ax.set_xticklabels(_HOUR_BUCKET_LABELS, rotation=20, ha="right")
    ax.set_yticks(range(n_rows))
    ax.set_yticklabels(DOW_LABELS)
    ax.set_xlabel("Time of day (local)")
    ax.set_ylabel("Day of week")
    ax.set_title(title)
    _subtitle(ax, subtitle)
    ax.grid(False)
    ax.tick_params(axis="both", which="both", length=0)

    for r in range(n_rows):
        for c in range(n_cols):
            if qualified[r, c]:
                # Text colour: black on the lighter middle of the cmap,
                # white at the extremes where the cmap goes saturated.
                intensity = abs(delta_clipped[r, c])
                txt_color = "white" if intensity >= 7.5 else PALETTE["text"]
                ax.text(
                    c,
                    r,
                    f"{wr_grid[r, c]:.0%}\nn={n_grid[r, c]}",
                    ha="center",
                    va="center",
                    fontsize=8,
                    color=txt_color,
                )
            else:
                ax.text(
                    c,
                    r,
                    "-",
                    ha="center",
                    va="center",
                    fontsize=12,
                    color=PALETTE["muted"],
                )

    cbar = fig.colorbar(im, ax=ax, label="Δ vs baseline (pp)", shrink=0.85, pad=0.03)
    cbar.outline.set_visible(False)
    cbar.ax.axhline(0.0, color=PALETTE["muted"], linewidth=0.8, alpha=0.6)

    fig.tight_layout()
    return fig


# --- 42. Same-champion behaviour after win vs after loss -------------------

_SAME_CHAMP_MIN_GAMES = 100
_SAME_CHAMP_BOOT = 2000


def _same_champ_per_player(df: pd.DataFrame) -> pd.DataFrame:
    """For each game, label whether the previous game (same person) used the
    same champion, and what the outcome of that previous game was. Returns
    per-person conditional proportions and sample sizes.
    """
    d = df.sort_values(["person", "game_start"]).reset_index(drop=True)
    d = d.assign(
        prev_champion=d.groupby("person")["champion"].shift(1),
        prev_win=d.groupby("person")["win"].shift(1),
    )
    d = d.dropna(subset=["prev_win"]).copy()
    d["prev_win"] = d["prev_win"].astype(int)
    d["same_champion"] = (d["champion"] == d["prev_champion"]).astype(int)

    rows = []
    for person, g in d.groupby("person", sort=False):
        total = len(g)
        wins = g[g["prev_win"] == 1]
        losses = g[g["prev_win"] == 0]
        n_w, n_l = len(wins), len(losses)
        rows.append(
            {
                "person": person,
                "total_games": total,
                "n_after_win": n_w,
                "n_after_loss": n_l,
                "same_after_win": int(wins["same_champion"].sum()),
                "same_after_loss": int(losses["same_champion"].sum()),
                "p_same_win": float(wins["same_champion"].mean()) if n_w > 0 else float("nan"),
                "p_same_loss": float(losses["same_champion"].mean()) if n_l > 0 else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def _chi_square_2x2_p(a: int, b: int, c: int, d_: int) -> float:
    """Pearson chi-square p-value for a 2x2 contingency table, df=1.

    Returns NaN when any expected cell is 0 (test undefined).
    The df=1 survival function reduces to erfc(sqrt(chi2/2)).
    """
    n = a + b + c + d_
    if n == 0:
        return float("nan")
    row1, row2 = a + b, c + d_
    col1, col2 = a + c, b + d_
    if row1 == 0 or row2 == 0 or col1 == 0 or col2 == 0:
        return float("nan")
    chi2 = 0.0
    for obs, (r, col) in zip(
        (a, b, c, d_),
        ((row1, col1), (row1, col2), (row2, col1), (row2, col2)),
        strict=True,
    ):
        exp = r * col / n
        chi2 += (obs - exp) ** 2 / exp
    return math.erfc(math.sqrt(chi2 / 2.0))


def _paired_bootstrap_diff_ci(
    p_win: np.ndarray, p_loss: np.ndarray, n_boot: int = _SAME_CHAMP_BOOT
) -> tuple[float, float, float]:
    """Paired bootstrap (resample players with replacement) for the macro
    mean difference P(same|win) - P(same|loss). Returns (mean_diff, lo, hi).
    """
    n = len(p_win)
    if n == 0:
        return (float("nan"), float("nan"), float("nan"))
    rng = np.random.default_rng(20260524)
    diffs = np.empty(n_boot)
    for i in range(n_boot):
        idx = rng.integers(0, n, size=n)
        diffs[i] = p_win[idx].mean() - p_loss[idx].mean()
    return (
        float(p_win.mean() - p_loss.mean()),
        float(np.percentile(diffs, 2.5)),
        float(np.percentile(diffs, 97.5)),
    )


def plot_same_champ_behavior(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Same-champion behaviour conditional on previous-game outcome.

    Aggregate view: per-player P(same|win) vs P(same|loss), horizontal grouped
    bars sorted by total games descending. A summary row at the top shows
    the macro mean of each conditional with a paired-bootstrap 95% CI on the
    win-minus-loss difference.

    Per-person view: two-bar chart with Wilson 95% CIs and a chi-square
    p-value on the 2x2 same/different x prev-win/prev-loss table.
    """
    per_player = _same_champ_per_player(df)

    if _is_aggregate(player):
        qualifying = per_player[
            (per_player["total_games"] >= _SAME_CHAMP_MIN_GAMES)
            & (per_player["n_after_win"] > 0)
            & (per_player["n_after_loss"] > 0)
        ].copy()
        if len(qualifying) < 3:
            return _empty_figure("Need >=3 players with >=100 games")
        return _plot_same_champ_aggregate(qualifying)

    label = _display_label(player) or "this player"
    person = _resolve_person(df, player)
    if person is None:
        return _empty_figure(f"No games for {label}")
    row = per_player[per_player["person"] == person]
    if row.empty:
        return _empty_figure(f"No games for {label}")
    total = int(row["total_games"].iloc[0])
    if total < _SAME_CHAMP_MIN_GAMES:
        return _empty_figure(f"Need >=100 games ({total} games)")
    return _plot_same_champ_per_person(row.iloc[0], label)


def _plot_same_champ_aggregate(qualifying: pd.DataFrame) -> plt.Figure:
    qualifying = qualifying.sort_values("total_games", ascending=False).reset_index(drop=True)
    p_win = qualifying["p_same_win"].to_numpy()
    p_loss = qualifying["p_same_loss"].to_numpy()
    mean_diff, lo, hi = _paired_bootstrap_diff_ci(p_win, p_loss)
    macro_win = float(p_win.mean())
    macro_loss = float(p_loss.mean())

    if mean_diff > 0 and lo > 0:
        verdict = "ride hot champs"
    elif mean_diff < 0 and hi < 0:
        verdict = "comfort-pick after losses"
    else:
        verdict = "no detectable difference"

    # One y slot per player + one for the macro summary row at the top.
    n = len(qualifying)
    y_players = np.arange(n) + 1  # 1..n
    y_macro = 0
    bar_h = 0.36

    fig, ax = plt.subplots(figsize=(10, 7))
    ax.barh(
        y_players - bar_h / 2,
        p_win,
        height=bar_h,
        color=PALETTE["primary"],
        label="P(same | prev win)",
        zorder=2,
    )
    ax.barh(
        y_players + bar_h / 2,
        p_loss,
        height=bar_h,
        color=PALETTE["loss"],
        label="P(same | prev loss)",
        zorder=2,
    )
    # Macro summary row at top.
    ax.barh(y_macro - bar_h / 2, macro_win, height=bar_h, color=PALETTE["primary"], alpha=0.55)
    ax.barh(y_macro + bar_h / 2, macro_loss, height=bar_h, color=PALETTE["loss"], alpha=0.55)
    ax.axhline(0.5, color=PALETTE["spine"], linewidth=0.8, alpha=0.6)

    for i, (_, row) in enumerate(qualifying.iterrows()):
        ax.text(
            row["p_same_win"] + 0.005,
            y_players[i] - bar_h / 2,
            f"{row['p_same_win']:.0%} (n={int(row['n_after_win'])})",
            va="center",
            ha="left",
            fontsize=8,
            color=PALETTE["text"],
        )
        ax.text(
            row["p_same_loss"] + 0.005,
            y_players[i] + bar_h / 2,
            f"{row['p_same_loss']:.0%} (n={int(row['n_after_loss'])})",
            va="center",
            ha="left",
            fontsize=8,
            color=PALETTE["text"],
        )
    ax.text(
        macro_win + 0.005,
        y_macro - bar_h / 2,
        f"{macro_win:.0%} macro",
        va="center",
        ha="left",
        fontsize=8,
        color=PALETTE["text"],
        fontweight="bold",
    )
    ax.text(
        macro_loss + 0.005,
        y_macro + bar_h / 2,
        f"{macro_loss:.0%} macro",
        va="center",
        ha="left",
        fontsize=8,
        color=PALETTE["text"],
        fontweight="bold",
    )

    yticks = [y_macro, *list(y_players)]
    yticklabels = ["MACRO MEAN", *[str(p) for p in qualifying["person"].tolist()]]
    ax.set_yticks(yticks)
    ax.set_yticklabels(yticklabels)
    ax.invert_yaxis()
    ax.set_xlim(0, max(0.8, float(max(p_win.max(), p_loss.max(), macro_win, macro_loss)) + 0.15))
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax.set_xlabel("P(next-game champion same as previous game)")
    ax.legend(loc="lower right", frameon=False)

    ax.set_title("Same champion after win vs after loss")
    _subtitle(
        ax,
        "P(same champion next game | prev win) vs P(same | prev loss). "
        f"Macro diff (win-loss) = {mean_diff:+.1%} [95% CI {lo:+.1%}, {hi:+.1%}]; "
        f"verdict: {verdict}.",
    )
    _polish_ax(ax)
    fig.tight_layout()
    return fig


def _plot_same_champ_per_person(row: pd.Series, label: str) -> plt.Figure:
    n_w = int(row["n_after_win"])
    n_l = int(row["n_after_loss"])
    same_w = int(row["same_after_win"])
    same_l = int(row["same_after_loss"])
    p_w = float(row["p_same_win"]) if n_w > 0 else float("nan")
    p_l = float(row["p_same_loss"]) if n_l > 0 else float("nan")

    ci_w = wilson_ci(same_w, n_w) if n_w > 0 else (float("nan"), float("nan"))
    ci_l = wilson_ci(same_l, n_l) if n_l > 0 else (float("nan"), float("nan"))

    # 2x2: rows = prev outcome, cols = (same, different)
    p_val = _chi_square_2x2_p(same_w, n_w - same_w, same_l, n_l - same_l)
    if math.isnan(p_val):
        p_text = "p=n/a"
        interp = "insufficient data for chi-square test"
    else:
        p_text = f"p={p_val:.3f}"
        if p_val < 0.05:
            if (p_w - p_l) > 0:
                interp = "rides hot champs (significant)"
            else:
                interp = "comfort-picks after losses (significant)"
        else:
            interp = "no significant difference"

    fig, ax = plt.subplots(figsize=(8, 5))
    x = np.arange(2)
    means = np.array([p_w if not math.isnan(p_w) else 0.0, p_l if not math.isnan(p_l) else 0.0])
    colours = [PALETTE["primary"], PALETTE["loss"]]
    ax.bar(x, means, color=colours, width=0.55, zorder=2)

    err_lo = np.array(
        [
            (p_w - ci_w[0]) if not math.isnan(p_w) else 0.0,
            (p_l - ci_l[0]) if not math.isnan(p_l) else 0.0,
        ]
    )
    err_hi = np.array(
        [
            (ci_w[1] - p_w) if not math.isnan(p_w) else 0.0,
            (ci_l[1] - p_l) if not math.isnan(p_l) else 0.0,
        ]
    )
    ax.errorbar(x, means, yerr=[err_lo, err_hi], **WHISKER_STYLE, zorder=3)

    for i, (mean, n) in enumerate(zip(means, (n_w, n_l), strict=True)):
        ax.text(
            i,
            mean + 0.02,
            f"{mean:.0%}\nn={n}",
            ha="center",
            va="bottom",
            fontsize=10,
            color=PALETTE["text"],
        )

    ax.set_xticks(x)
    ax.set_xticklabels(["After win", "After loss"])
    ax.set_ylabel("P(same champion next game)")
    ax.set_ylim(0, max(0.6, float(means.max()) + 0.20))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))

    ax.set_title(f"Same-champion behavior - {label}")
    _subtitle(
        ax,
        f"P(same|win)={p_w:.0%} (n={n_w}), P(same|loss)={p_l:.0%} (n={n_l}). "
        f"Chi-square {p_text} - {interp}.",
    )
    _polish_ax(ax)
    fig.tight_layout()
    return fig


# --- 43. Ride payoff (does riding hot champs actually pay off?) ------------

_RIDE_MIN_GAMES = 100
_RIDE_MIN_COHORT = 10
_RIDE_BOOT = 2000


def _ride_per_player_cohorts(df: pd.DataFrame) -> pd.DataFrame:
    """For each game with a previous game on record, label it into one of
    four cohorts (prev_outcome x same_champion) and aggregate WR per
    person per cohort. Returns one row per (person, cohort) with wins/n.
    """
    d = df.sort_values(["person", "game_start"]).reset_index(drop=True)
    d = d.assign(
        prev_champion=d.groupby("person")["champion"].shift(1),
        prev_win=d.groupby("person")["win"].shift(1),
    )
    d = d.dropna(subset=["prev_win"]).copy()
    d["prev_win"] = d["prev_win"].astype(int)
    d["same_champion"] = (d["champion"] == d["prev_champion"]).astype(int)
    # Cohort letter: A=prev_win+same, B=prev_win+diff, C=prev_loss+same,
    # D=prev_loss+diff. Stored as a categorical for stable ordering.
    cohort = np.where(
        d["prev_win"] == 1,
        np.where(d["same_champion"] == 1, "A", "B"),
        np.where(d["same_champion"] == 1, "C", "D"),
    )
    d["cohort"] = cohort

    grouped = (
        d.groupby(["person", "cohort"], observed=True)["win"]
        .agg(["sum", "count"])
        .rename(columns={"sum": "wins", "count": "n"})
        .reset_index()
    )
    grouped["wr"] = grouped["wins"] / grouped["n"]
    # Also carry overall total games per person so the caller can apply the
    # >=100 games threshold without re-grouping.
    totals = d.groupby("person").size().rename("total_games").reset_index()
    grouped = grouped.merge(totals, on="person", how="left")
    return grouped


def _ride_cohort_matrix(cohorts: pd.DataFrame) -> pd.DataFrame:
    """Pivot per-(person, cohort) rows into one row per person with columns
    wr_A, wr_B, wr_C, wr_D, n_A, n_B, n_C, n_D, total_games. Missing cohort
    cells become NaN in the WR columns and 0 in the n columns.
    """
    wr = cohorts.pivot(index="person", columns="cohort", values="wr")
    n = cohorts.pivot(index="person", columns="cohort", values="n").fillna(0).astype(int)
    for letter in ("A", "B", "C", "D"):
        if letter not in wr.columns:
            wr[letter] = np.nan
        if letter not in n.columns:
            n[letter] = 0
    wr = wr[["A", "B", "C", "D"]].rename(columns=lambda c: f"wr_{c}")
    n = n[["A", "B", "C", "D"]].rename(columns=lambda c: f"n_{c}")
    totals = cohorts.groupby("person")["total_games"].first()
    out = wr.join(n).join(totals.rename("total_games"))
    return out.reset_index()


def _paired_bootstrap_wr_diff(
    wr_lhs: np.ndarray,
    wr_rhs: np.ndarray,
    n_boot: int = _RIDE_BOOT,
    seed: int = 20260524,
) -> tuple[float, float, float]:
    """Paired bootstrap (resample players with replacement) for the macro
    mean of per-player WR differences ``wr_lhs - wr_rhs``. Both arrays must
    be aligned per player and contain no NaNs (caller filters).
    """
    n = len(wr_lhs)
    if n == 0:
        return (float("nan"), float("nan"), float("nan"))
    diffs_obs = wr_lhs - wr_rhs
    rng = np.random.default_rng(seed)
    boot = np.empty(n_boot)
    for i in range(n_boot):
        idx = rng.integers(0, n, size=n)
        boot[i] = diffs_obs[idx].mean()
    return (
        float(diffs_obs.mean()),
        float(np.percentile(boot, 2.5)),
        float(np.percentile(boot, 97.5)),
    )


def plot_ride_payoff(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Does riding a hot champion actually pay off?

    Iter 52 showed players ride the same champion ~18pp more after a win.
    This chart tests whether that behaviour is rewarded: are the WRs
    higher when you ride hot (cohort A) vs switch off after a win
    (cohort B), and higher when you comfort-pick after a loss (cohort C)
    vs try something else (cohort D)?

    Aggregate view: macro mean WR per cohort across players with >=10
    games in the cohort, with paired-bootstrap 95% CIs on the two
    differences A-B and C-D.

    Per-person view: that player's WR in each cohort with Wilson 95% CIs
    and the same two differences.
    """
    cohorts = _ride_per_player_cohorts(df)

    if _is_aggregate(player):
        return _plot_ride_payoff_aggregate(df, cohorts)

    label = _display_label(player) or "this player"
    person = _resolve_person(df, player)
    if person is None:
        return _empty_figure(f"No games for {label}")
    matrix = _ride_cohort_matrix(cohorts)
    row = matrix[matrix["person"] == person]
    if row.empty:
        return _empty_figure(f"No games for {label}")
    total = int(row["total_games"].iloc[0])
    if total < _RIDE_MIN_GAMES:
        return _empty_figure(f"Need >=100 games ({total} games)")
    return _plot_ride_payoff_per_person(row.iloc[0], df, person, label)


def _plot_ride_payoff_aggregate(df: pd.DataFrame, cohorts: pd.DataFrame) -> plt.Figure:
    matrix = _ride_cohort_matrix(cohorts)
    qualifying = matrix[matrix["total_games"] >= _RIDE_MIN_GAMES].copy()
    if len(qualifying) < 3:
        return _empty_figure("Need >=3 players with >=100 games")

    # Per-cohort macro means: per-player WR with NaN where n < min_cohort,
    # then nanmean across players. Pooled n = sum of n in qualifying cells.
    macro: dict[str, float] = {}
    pooled_n: dict[str, int] = {}
    cohort_wr_by_player: dict[str, np.ndarray] = {}
    for letter in ("A", "B", "C", "D"):
        wr_col = qualifying[f"wr_{letter}"].astype(float)
        n_col = qualifying[f"n_{letter}"].astype(int)
        mask = n_col >= _RIDE_MIN_COHORT
        wr_valid = wr_col.where(mask)
        cohort_wr_by_player[letter] = wr_valid.to_numpy()
        macro[letter] = float(np.nanmean(wr_valid)) if mask.any() else float("nan")
        pooled_n[letter] = int(n_col[mask].sum())

    # Paired bootstraps: filter independently for the two diffs.
    def _paired_for(left: str, right: str) -> tuple[float, float, float, int]:
        left_n = qualifying[f"n_{left}"].astype(int)
        right_n = qualifying[f"n_{right}"].astype(int)
        keep = (left_n >= _RIDE_MIN_COHORT) & (right_n >= _RIDE_MIN_COHORT)
        if keep.sum() < 3:
            return (float("nan"), float("nan"), float("nan"), int(keep.sum()))
        lhs = qualifying.loc[keep, f"wr_{left}"].to_numpy(dtype=float)
        rhs = qualifying.loc[keep, f"wr_{right}"].to_numpy(dtype=float)
        mean, lo, hi = _paired_bootstrap_wr_diff(lhs, rhs)
        return (mean, lo, hi, int(keep.sum()))

    ride_mean, ride_lo, ride_hi, ride_n_players = _paired_for("A", "B")
    comfort_mean, comfort_lo, comfort_hi, comfort_n_players = _paired_for("C", "D")

    # Reference line: overall WR across the qualifying non-first games
    # (the games actually entering the cohorts).
    total_n = sum(pooled_n.values())
    total_wins = 0
    for letter in ("A", "B", "C", "D"):
        wr_col = qualifying[f"wr_{letter}"].astype(float)
        n_col = qualifying[f"n_{letter}"].astype(int)
        mask = n_col >= _RIDE_MIN_COHORT
        total_wins += float((wr_col.where(mask) * n_col.where(mask)).sum(skipna=True))
    overall_wr = total_wins / total_n if total_n > 0 else float("nan")

    title = "Does riding hot champs pay off?"
    subtitle = (
        "WR conditional on prev outcome x same-champion. Iter 52 showed players ride "
        "hot ~18pp more after a win - does the payoff justify it? "
        f"Ride (A-B) {ride_mean:+.1%} [95% CI {ride_lo:+.1%}, {ride_hi:+.1%}], "
        f"Comfort (C-D) {comfort_mean:+.1%} [95% CI {comfort_lo:+.1%}, {comfort_hi:+.1%}]."
    )

    fig = _ride_payoff_figure(
        macro_a=macro["A"],
        macro_b=macro["B"],
        macro_c=macro["C"],
        macro_d=macro["D"],
        n_a=pooled_n["A"],
        n_b=pooled_n["B"],
        n_c=pooled_n["C"],
        n_d=pooled_n["D"],
        ci_a=None,
        ci_b=None,
        ci_c=None,
        ci_d=None,
        overall_wr=overall_wr,
        title=title,
        subtitle=subtitle,
    )
    return fig


def _plot_ride_payoff_per_person(
    row: pd.Series, df: pd.DataFrame, person: str, label: str
) -> plt.Figure:
    n_a = int(row["n_A"])
    n_b = int(row["n_B"])
    n_c = int(row["n_C"])
    n_d = int(row["n_D"])
    wr_a = float(row["wr_A"]) if n_a > 0 else float("nan")
    wr_b = float(row["wr_B"]) if n_b > 0 else float("nan")
    wr_c = float(row["wr_C"]) if n_c > 0 else float("nan")
    wr_d = float(row["wr_D"]) if n_d > 0 else float("nan")

    def _ci(wr: float, n: int) -> tuple[float, float] | None:
        if n <= 0 or math.isnan(wr):
            return None
        wins = int(round(wr * n))
        return wilson_ci(wins, n)

    ci_a = _ci(wr_a, n_a)
    ci_b = _ci(wr_b, n_b)
    ci_c = _ci(wr_c, n_c)
    ci_d = _ci(wr_d, n_d)

    # Overall WR for this person across the non-first games used.
    overall_wr = (
        row["wr_A"] * n_a + row["wr_B"] * n_b + row["wr_C"] * n_c + row["wr_D"] * n_d
    ) / max(1, n_a + n_b + n_c + n_d)

    # Ride / comfort point estimates as differences of two binomial WRs.
    ride_diff = (wr_a - wr_b) if (n_a > 0 and n_b > 0) else float("nan")
    comfort_diff = (wr_c - wr_d) if (n_c > 0 and n_d > 0) else float("nan")

    def _diff_ci(wr_l: float, n_l: int, wr_r: float, n_r: int) -> tuple[float, float] | None:
        if n_l <= 0 or n_r <= 0 or math.isnan(wr_l) or math.isnan(wr_r):
            return None
        # Standard error of difference of two independent proportions.
        var = wr_l * (1 - wr_l) / n_l + wr_r * (1 - wr_r) / n_r
        if var <= 0:
            return (wr_l - wr_r, wr_l - wr_r)
        se = math.sqrt(var)
        margin = 1.96 * se
        return (wr_l - wr_r - margin, wr_l - wr_r + margin)

    ride_ci = _diff_ci(wr_a, n_a, wr_b, n_b)
    comfort_ci = _diff_ci(wr_c, n_c, wr_d, n_d)

    def _fmt_diff(value: float, ci: tuple[float, float] | None) -> str:
        if math.isnan(value) or ci is None:
            return "insufficient data"
        return f"{value:+.1%} [95% CI {ci[0]:+.1%}, {ci[1]:+.1%}]"

    title = f"Riding vs switching - {label}"
    subtitle = (
        f"WR conditional on prev outcome x same-champion. "
        f"Ride (A-B) {_fmt_diff(ride_diff, ride_ci)}, "
        f"Comfort (C-D) {_fmt_diff(comfort_diff, comfort_ci)}."
    )

    fig = _ride_payoff_figure(
        macro_a=wr_a,
        macro_b=wr_b,
        macro_c=wr_c,
        macro_d=wr_d,
        n_a=n_a,
        n_b=n_b,
        n_c=n_c,
        n_d=n_d,
        ci_a=ci_a,
        ci_b=ci_b,
        ci_c=ci_c,
        ci_d=ci_d,
        overall_wr=overall_wr,
        title=title,
        subtitle=subtitle,
    )
    return fig


def _ride_payoff_figure(
    *,
    macro_a: float,
    macro_b: float,
    macro_c: float,
    macro_d: float,
    n_a: int,
    n_b: int,
    n_c: int,
    n_d: int,
    ci_a: tuple[float, float] | None,
    ci_b: tuple[float, float] | None,
    ci_c: tuple[float, float] | None,
    ci_d: tuple[float, float] | None,
    overall_wr: float,
    title: str,
    subtitle: str,
) -> plt.Figure:
    """Shared 2-panel renderer. Left panel = after WIN (cohorts A, B);
    right panel = after LOSS (cohorts C, D). Pass Wilson CIs to draw
    whiskers (per-person), or None to skip them (aggregate macro means).
    """
    fig, axes = plt.subplots(1, 2, figsize=(10, 6), sharey=True)

    panels = [
        (
            axes[0],
            "After WIN",
            (macro_a, macro_b),
            (n_a, n_b),
            (ci_a, ci_b),
            (PALETTE["win"], PALETTE["accent_orange"]),
            ("Same champ\n(ride hot)", "Diff champ\n(switch off)"),
        ),
        (
            axes[1],
            "After LOSS",
            (macro_c, macro_d),
            (n_c, n_d),
            (ci_c, ci_d),
            (PALETTE["accent_teal"], PALETTE["loss"]),
            ("Same champ\n(comfort)", "Diff champ\n(escape cold)"),
        ),
    ]

    ymax_cap = 0.0
    for ax, panel_title, wrs, ns, cis, colours, xlabs in panels:
        x = np.arange(2)
        bar_values = np.array([0.0 if math.isnan(v) else v for v in wrs])
        ax.bar(x, bar_values, color=colours, width=0.55, zorder=2)
        # Whiskers when CIs are provided (per-person view).
        if any(ci is not None for ci in cis):
            err_lo = []
            err_hi = []
            for wr, ci in zip(wrs, cis, strict=True):
                if ci is None or math.isnan(wr):
                    err_lo.append(0.0)
                    err_hi.append(0.0)
                else:
                    err_lo.append(wr - ci[0])
                    err_hi.append(ci[1] - wr)
            ax.errorbar(x, bar_values, yerr=[err_lo, err_hi], **WHISKER_STYLE, zorder=3)
            for ci in cis:
                if ci is not None:
                    ymax_cap = max(ymax_cap, ci[1])
        for v in wrs:
            if not math.isnan(v):
                ymax_cap = max(ymax_cap, v)

        for xi, wr, n in zip(x, wrs, ns, strict=True):
            if math.isnan(wr) or n == 0:
                ax.text(
                    xi,
                    0.02,
                    "n/a",
                    ha="center",
                    va="bottom",
                    fontsize=10,
                    color=PALETTE["muted"],
                )
                continue
            ax.text(
                xi,
                wr + 0.02,
                f"{wr:.0%}\nn={n}",
                ha="center",
                va="bottom",
                fontsize=10,
                color=PALETTE["text"],
            )

        if not math.isnan(overall_wr):
            ax.axhline(
                overall_wr,
                color=PALETTE["muted"],
                linewidth=0.8,
                linestyle=(0, (4, 4)),
                alpha=0.6,
                label=f"Overall WR ({overall_wr:.0%})",
            )
        ax.set_xticks(x)
        ax.set_xticklabels(xlabs)
        ax.set_title(panel_title)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
        _polish_ax(ax)

    axes[0].set_ylabel("Win rate")
    ymax = max(0.75, ymax_cap + 0.18)
    axes[0].set_ylim(0, ymax)
    axes[0].legend(loc="upper right", frameon=False, fontsize=9)

    fig.suptitle(title)
    # Use _subtitle on left axes so subtitle sits under the suptitle area;
    # but suptitle + per-axes subtitle would collide. Simpler: place the
    # subtitle as a single figure-level text below the suptitle.
    # Clear axes titles first so _subtitle on ax[0] doesn't pad the small
    # panel title. We've already drawn panel titles, so place the caption
    # at the figure level.
    fig.text(
        0.5,
        0.93,
        subtitle,
        ha="center",
        va="top",
        fontsize=9.5,
        color=PALETTE["muted"],
        style="italic",
        wrap=True,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.88))
    return fig


# --- 44. Win-sequence autocorrelation --------------------------------------

# Minimum games for a player's win series to qualify. Below ~200 the
# Bartlett ±1.96/sqrt(n) band is so wide that lag-k bars rarely cross,
# which makes the chart misleadingly "all noise".
_ACF_MIN_GAMES = 200

# Lags we estimate. Beyond ~10 the sample size for the inner sum shrinks
# noticeably and the cross-player overlap (different career lengths) gets
# harder to interpret.
_ACF_MAX_LAG = 10

# Bootstrap iterations for the aggregate per-lag CI ribbon.
_ACF_BOOTSTRAP = 2000


def _win_acf(wins: np.ndarray, max_lag: int = _ACF_MAX_LAG) -> np.ndarray:
    """Sample autocorrelation r(k) for k=1..max_lag on a binary series.

    Uses the biased (n-denominator) estimator the task spec calls for so
    the white-noise ±1.96/sqrt(n) Bartlett band is directly comparable.
    Returns NaN at lag k if the series mean has zero variance (all wins
    or all losses — the denominator vanishes).
    """
    w = np.asarray(wins, dtype=float)
    n = w.size
    mean = w.mean()
    centred = w - mean
    denom = float((centred * centred).sum())
    if denom <= 0 or n <= 1:
        return np.full(max_lag, np.nan)
    out = np.empty(max_lag)
    for k in range(1, max_lag + 1):
        if k >= n:
            out[k - 1] = np.nan
            continue
        out[k - 1] = float((centred[: n - k] * centred[k:]).sum()) / denom
    return out


def _person_win_series(df: pd.DataFrame, person: str) -> np.ndarray:
    """Chronological 0/1 win sequence for one person across all their
    accounts, sorted by game_start (the load_matches sort is by person
    then game_start, but we re-sort defensively in case a caller pre-
    filtered)."""
    d = df[df["person"] == person].sort_values("game_start")
    return d["win"].to_numpy(dtype=float)


def plot_win_autocorrelation(df: pd.DataFrame, player: str | None = None) -> plt.Figure:
    """Lag-k autocorrelation of the win/loss sequence — direct momentum
    test that doesn't condition on a streak.

    Per-person: stem plot of r(k) for k=1..10 against the Bartlett white-
    noise band ±1.96/sqrt(n). Bars outside the band are evidence that
    "win at game t" carries information about "win at game t+k".

    Aggregate: macro-average r(k) across players with >=200 games, with
    bootstrap 95% CIs (B=2000) on the per-player vector. A lag whose CI
    excludes zero is group-wide non-random structure.
    """
    if _is_aggregate(player):
        return _plot_win_autocorrelation_aggregate(df)

    person = _resolve_person(df, player)
    label = _display_label(player) or "this player"
    if person is None:
        return _empty_figure(f"No games for {label}")
    wins = _person_win_series(df, person)
    n = wins.size
    if n < _ACF_MIN_GAMES:
        return _empty_figure(f"Need >=200 games for autocorrelation ({n} games)")

    r = _win_acf(wins, _ACF_MAX_LAG)
    ci = 1.96 / math.sqrt(n)
    lags = np.arange(1, _ACF_MAX_LAG + 1)

    fig, ax = plt.subplots(figsize=(10, 6))
    markerline, stemlines, baseline = ax.stem(
        lags,
        r,
        linefmt="-",
        markerfmt="o",
        basefmt=" ",
    )
    plt.setp(stemlines, color=PALETTE["primary"], linewidth=1.8, alpha=0.85)
    plt.setp(markerline, color=PALETTE["primary"], markersize=6, markeredgecolor="white")
    plt.setp(baseline, visible=False)

    ax.axhline(0.0, color=PALETTE["spine"], linewidth=1.0)
    ax.axhline(
        ci,
        color=PALETTE["muted"],
        linewidth=0.9,
        linestyle=(0, (4, 4)),
        alpha=0.7,
        label=f"95% white-noise band (+/- {ci:.3f})",
    )
    ax.axhline(
        -ci,
        color=PALETTE["muted"],
        linewidth=0.9,
        linestyle=(0, (4, 4)),
        alpha=0.7,
    )

    # Annotate any lag that escapes the IID band — the whole point of the
    # chart is "which lags are real signal".
    for lag, val in zip(lags, r, strict=False):
        if np.isnan(val):
            continue
        if abs(val) > ci:
            colour = PALETTE["win"] if val > 0 else PALETTE["loss"]
            ax.annotate(
                f"{val:+.3f}",
                xy=(lag, val),
                xytext=(0, 10 if val > 0 else -14),
                textcoords="offset points",
                ha="center",
                fontsize=9,
                color=colour,
                fontweight="bold",
            )

    ax.set_xticks(lags)
    ax.set_xlabel("Lag k (games)")
    ax.set_ylabel("Autocorrelation r(k)")
    ax.set_title(_title(f"Win autocorrelation - {label}", None))
    _subtitle(
        ax,
        f"Lag-k correlation of win/loss sequence (n={n}). Bars outside "
        f"+/- {ci:.3f} indicate non-random structure (momentum if positive, "
        f"mean-reversion if negative).",
    )
    # Symmetric y-limits so the +/- band reads visually balanced.
    ymax = max(0.15, float(np.nanmax(np.abs(r))) * 1.3 if np.isfinite(r).any() else 0.15, ci * 1.6)
    ax.set_ylim(-ymax, ymax)
    ax.legend(loc="upper right", frameon=False, fontsize=9)
    _polish_ax(ax)
    fig.tight_layout()
    return fig


def _plot_win_autocorrelation_aggregate(df: pd.DataFrame) -> plt.Figure:
    counts = df.groupby("person").size()
    qualifying = counts[counts >= _ACF_MIN_GAMES].index.tolist()
    if len(qualifying) < 3:
        return _empty_figure("Need >=3 players with >=200 games")

    # Per-player ACF matrix: rows = players, cols = lag 1..max_lag.
    rows: list[np.ndarray] = []
    for person in qualifying:
        wins = _person_win_series(df, person)
        rows.append(_win_acf(wins, _ACF_MAX_LAG))
    acf_matrix = np.vstack(rows)
    n_players = acf_matrix.shape[0]

    macro = np.nanmean(acf_matrix, axis=0)

    # Bootstrap: resample player indices with replacement, take nanmean
    # per lag, percentile across resamples.
    rng = np.random.default_rng(0)
    boot = np.empty((_ACF_BOOTSTRAP, _ACF_MAX_LAG))
    for b in range(_ACF_BOOTSTRAP):
        idx = rng.integers(0, n_players, size=n_players)
        boot[b] = np.nanmean(acf_matrix[idx], axis=0)
    ci_lo = np.nanpercentile(boot, 2.5, axis=0)
    ci_hi = np.nanpercentile(boot, 97.5, axis=0)

    lags = np.arange(1, _ACF_MAX_LAG + 1)

    fig, ax = plt.subplots(figsize=(10, 6))

    # Bootstrap ribbon first so the stem sits on top.
    ax.fill_between(
        lags,
        ci_lo,
        ci_hi,
        color=PALETTE["primary"],
        alpha=0.18,
        linewidth=0,
        label="Bootstrap 95% CI",
    )

    markerline, stemlines, baseline = ax.stem(
        lags,
        macro,
        linefmt="-",
        markerfmt="o",
        basefmt=" ",
    )
    plt.setp(stemlines, color=PALETTE["primary"], linewidth=1.8, alpha=0.9)
    plt.setp(markerline, color=PALETTE["primary"], markersize=6, markeredgecolor="white")
    plt.setp(baseline, visible=False)

    ax.axhline(0.0, color=PALETTE["spine"], linewidth=1.0)

    # Lags whose bootstrap CI excludes zero = group-wide signal at that lag.
    for lag, val, lo, hi in zip(lags, macro, ci_lo, ci_hi, strict=False):
        if np.isnan(val) or np.isnan(lo) or np.isnan(hi):
            continue
        if lo > 0 or hi < 0:
            colour = PALETTE["win"] if val > 0 else PALETTE["loss"]
            ax.annotate(
                f"{val:+.3f}\n[{lo:+.3f}, {hi:+.3f}]",
                xy=(lag, val),
                xytext=(0, 14 if val > 0 else -28),
                textcoords="offset points",
                ha="center",
                fontsize=8.5,
                color=colour,
                fontweight="bold",
            )

    ax.set_xticks(lags)
    ax.set_xlabel("Lag k (games)")
    ax.set_ylabel("Macro-averaged r(k)")
    ax.set_title("Win autocorrelation - aggregate")
    _subtitle(
        ax,
        f"Macro-averaged lag-k correlation across {n_players} players "
        f"(>=200 games each). Bands = bootstrap 95% CI on per-player values; "
        f"a lag whose CI excludes zero is group-wide momentum or mean-reversion.",
    )
    span = max(
        0.05,
        float(np.nanmax(np.abs(np.concatenate([macro, ci_lo, ci_hi])))) * 1.35
        if np.isfinite(np.concatenate([macro, ci_lo, ci_hi])).any()
        else 0.05,
    )
    ax.set_ylim(-span, span)
    ax.legend(loc="upper right", frameon=False, fontsize=9)
    _polish_ax(ax)
    fig.tight_layout()
    return fig


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
    ("22_model_calibration", plot_model_calibration),
    ("23_actions_card", plot_actions_card),
    ("24_per_player_predictability", plot_per_player_predictability),
    ("25_tier_winrate", plot_tier_winrate),
    ("26_match_highlights", plot_match_highlights),
    ("27_recent_sessions", plot_recent_sessions),
    ("28_playstyle_clusters", plot_playstyle_clusters),
    ("29_champion_freshness", plot_champion_freshness),
    ("30_role_winrate", plot_role_winrate),
    ("31_player_role_matrix", plot_player_role_matrix),
    ("32_tilt_by_gap", plot_tilt_by_gap),
    ("33_session_position_wr", plot_session_position_wr),
    ("34_improvement_slope", plot_improvement_slope),
    ("35_stat_sheet", plot_stat_sheet),
    ("36_game_pace", plot_game_pace),
    ("37_shrunk_champ_rankings", plot_shrunk_champ_rankings),
    ("38_champ_pool_concentration", plot_champ_pool_concentration),
    ("39_champion_mastery", plot_champion_mastery),
    ("40_champion_rust", plot_champion_rust),
    ("41_dow_hour_heatmap", plot_dow_hour_heatmap),
    ("42_same_champ_behavior", plot_same_champ_behavior),
    ("43_ride_payoff", plot_ride_payoff),
    ("44_win_autocorrelation", plot_win_autocorrelation),
]
