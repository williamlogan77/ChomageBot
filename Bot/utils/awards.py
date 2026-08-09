"""Weekly awards ceremony: computation core.

Five awards computed over one calendar week (Monday 00:00 -> Monday
00:00, Europe/London wall time) from existing tables — match_stats for
games, league_history for LP — aggregated per Discord user: a person's
tracked league accounts sum together.

Queues (2026-08): match-derived awards score EVERY queue by default —
ARAM, flex, Arena, normals, Clash, the lot (remakes never count; see
GameRow). The LP awards stay ranked solo by nature: LP only exists
there. The default is steered by the ``awards_scope`` bot_config key
("all" | "ranked", default "all"), and each award's measure can be
re-pointed per week from the dashboard: a queue scope (SCOPES) times a
metric (METRICS — most deaths, most time dead, lowest winrate, ...).
Precedence: exclusion > forced winner > chosen metric/scope > defaults.

Layering: pure, deterministic functions do the math and the rendering;
thin async wrappers own the SQL; cogs/weekly_awards.py owns scheduling,
posting and persistence. Roast lines rotate deterministically on the ISO
week number so consecutive weeks differ without any randomness.

Dashboard controls (2026-08): the ceremony consults AwardAdjustments —
per-award enable/disable + tagline from bot_config
(award_<key>_enabled / award_<key>_tagline), the default queue scope
(awards_scope) and per-week forced winners / exclusions / chosen
measures from the award_overrides table, all written by the dashboard.
Qualification bars (award_<key>_min bot_config keys) auto-skip an award
whose winning number is too small to be interesting — the LP awards
default to "more than 10 LP" — and a deterministic commentary line per
award ("closely followed by ...") is generated from the same standings.
The dashboard's live standings (proxmox/dashboard app/queries/awards.py)
port this module's math and MUST stay in lockstep when rules change
here (dashboard tests/test_award_parity.py enforces it).
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from fractions import Fraction
from zoneinfo import ZoneInfo

from utils import db, seasons
from utils.riot_client import RANKED_SOLO_QUEUE_ID

LONDON = ZoneInfo("Europe/London")

# league_history.queue tag for solo snapshots (mirrors SOLO_QUEUE in
# cogs/league_table_updater.py — utils must not import cogs).
SOLO_QUEUE = "RANKED_SOLO_5x5"

# Award keys as stored in weekly_awards.award — stable, do not rename.
LP_LOSS = "lp_loss"
LP_CHAD = "lp_chad"
PUSSY = "pussy_of_the_weak"
DUO_LEECH = "duo_leech"
INT = "int_of_the_week"

AWARD_ORDER = (LP_LOSS, LP_CHAD, PUSSY, DUO_LEECH, INT)

# ------------------------------------------------------ queues + scopes

# match-v5 queueId -> short human name. MUST stay identical to the
# dashboard's app/queries/live.QUEUE_NAMES (the parity suite checks) —
# verified against Riot's official queues.json.
QUEUE_NAMES = {
    400: "Normal Draft",
    420: "Ranked Solo/Duo",
    430: "Normal Blind",
    440: "Ranked Flex",
    450: "ARAM",
    480: "Swiftplay",
    490: "Quickplay",
    700: "Clash",
    720: "ARAM Clash",
    870: "Co-op vs AI",
    880: "Co-op vs AI",
    890: "Co-op vs AI",
    900: "ARURF",
    1010: "Snow ARURF",
    1300: "Nexus Blitz",
    1400: "Ultimate Spellbook",
    1700: "Arena",
    1710: "Arena",
    1900: "Pick URF",
    2400: "ARAM: Mayhem",
}


def queue_name(queue_id: int | None) -> str:
    """Short display name; custom games report id 0 (or none at all)."""
    if not queue_id:
        return "Custom game"
    return QUEUE_NAMES.get(queue_id, "Other mode")


# Arena deaths are the game mode (one per round, many rounds), so a
# mediocre Arena evening structurally outscores a genuinely griefed
# Rift game. Death-counting metrics drop Arena from the "all" scope;
# explicitly picking the Arena scope opts back in.
ARENA_QUEUES = frozenset({1700, 1710})

SCOPE_ALL = "all"
SCOPE_RANKED = "ranked"

# bot_config key holding the DEFAULT scope for match-derived awards.
# Registered in the dashboard's app/botconfig.py; anything that isn't a
# SCOPES key falls back to "all" (parse_scope).
SCOPE_KEY = "awards_scope"

# scope key -> (label, queue ids; None = every queue). Keys are stored
# in award_overrides.chosen_scope and in winner detail — stable.
SCOPES: dict[str, tuple[str, tuple[int, ...] | None]] = {
    SCOPE_ALL: ("all queues", None),
    SCOPE_RANKED: ("Ranked Solo/Duo", (RANKED_SOLO_QUEUE_ID,)),
    "flex": ("Ranked Flex", (440,)),
    "aram": ("ARAM", (450,)),
    "arena": ("Arena", (1700, 1710)),
    "normals": ("Normals", (400, 430, 480, 490)),
    "clash": ("Clash", (700, 720)),
}


def parse_scope(value: str | None) -> str:
    """Canonical scope key from a stored value; anything unknown -> all."""
    value = (value or "").strip().lower()
    return value if value in SCOPES else SCOPE_ALL


def in_scope(queue_id: int | None, scope: str) -> bool:
    ids = SCOPES[scope][1]
    return ids is None or queue_id in ids


# ------------------------------------------------------ metric registry


@dataclass(frozen=True)
class MetricSpec:
    """One pickable weekly measure. ``lp_based`` metrics live on the
    ranked-solo LP ladder only — scope selection doesn't apply."""

    key: str
    label: str  # lower-case phrase, composes into "measured by ..."
    description: str
    lp_based: bool = False


# Registry order is dropdown order. Keys are stored in
# award_overrides.chosen_metric and winner detail — stable, do not
# rename. MUST stay identical to the dashboard's copy (parity suite).
METRICS: dict[str, MetricSpec] = {
    spec.key: spec
    for spec in (
        MetricSpec(
            "lp_loss",
            "biggest LP loss",
            "Largest net LP drop across the week (ranked solo — the only place LP exists).",
            lp_based=True,
        ),
        MetricSpec(
            "lp_gain",
            "biggest LP gain",
            "Largest net LP climb across the week (ranked solo — the only place LP exists).",
            lp_based=True,
        ),
        MetricSpec(
            "games_drop",
            "sharpest drop in games played",
            "Biggest collapse vs the player's own 4-week habit (5+ games/week habit required).",
        ),
        MetricSpec(
            "fewest_games",
            "fewest games played",
            "Least games this week among people with a 5+ games/week habit.",
        ),
        MetricSpec(
            "duo_share",
            "highest premade share",
            "Largest share of games queued with other tracked players on the team — "
            "duos and full stacks alike (min 5 games, 2 of them premade).",
        ),
        MetricSpec(
            "most_deaths_game",
            "most deaths in one game",
            "The single game with the most deaths; worse KDA breaks ties. Arena sits out "
            "of the all-queues pool — dying there is the game mode.",
        ),
        MetricSpec(
            "lowest_kda_game",
            "lowest KDA in one game",
            "The single game with the worst (kills+assists)/deaths. Arena sits out of the "
            "all-queues pool.",
        ),
        MetricSpec(
            "most_deaths_total",
            "most deaths across the week",
            "Deaths summed over every game played. Arena sits out of the all-queues pool.",
        ),
        MetricSpec(
            "most_time_dead",
            "most time spent dead",
            "Total time spent watching the gray screen across the week.",
        ),
        MetricSpec(
            "most_missing_pings",
            "most '?' pings",
            "Enemy-missing pings fired across the week. The passive-aggression index.",
        ),
        MetricSpec(
            "most_pings",
            "most pings overall",
            "Every ping of any kind, summed across the week.",
        ),
        MetricSpec(
            "lowest_kp",
            "lowest kill participation",
            "Worst average share of the team's kills (min 3 games with data).",
        ),
        MetricSpec(
            "lowest_winrate",
            "lowest winrate",
            "Worst win fraction on the week (min 5 games).",
        ),
        MetricSpec(
            "most_first_bloods",
            "most first bloods",
            "First bloods drawn across the week.",
        ),
        MetricSpec(
            "biggest_damage_share",
            "biggest team-damage share",
            "The single game carrying the largest share of the team's damage.",
        ),
        MetricSpec(
            "largest_multikill",
            "largest multikill",
            "The biggest multikill of the week (double or better).",
        ),
    )
}

# Each award's built-in measure — what runs when nobody picked anything.
DEFAULT_METRIC: dict[str, str] = {
    LP_LOSS: "lp_loss",
    LP_CHAD: "lp_gain",
    PUSSY: "games_drop",
    DUO_LEECH: "duo_share",
    INT: "most_deaths_game",
}

# Metrics distorted by Arena's round-based dying (see ARENA_QUEUES).
DEATH_METRICS = frozenset({"most_deaths_game", "lowest_kda_game", "most_deaths_total"})

# ---------------------------------------------- qualification thresholds
# A per-award minimum bar the winning number must BEAT (strictly — the
# user's spec: "10 LP and below should be excluded") or the award skips
# the week with a "nobody earned it" note. Configured via the
# award_<key>_min bot_config keys (registered in the dashboard's
# botconfig.py, editable inline on the cabinet); these are the defaults
# when the key is absent. The bar is expressed in the award's DEFAULT
# metric's unit (MIN_UNITS) and therefore applies ONLY while the award
# runs its default metric: re-pointing the measure at e.g. "most time
# spent dead" would make a 10-LP bar silently compare LP against
# seconds, so a per-week metric pick — itself an explicit admin decision
# — pauses the automatic bar. Forced winners bypass it outright;
# exclusions apply before it (the bar judges whoever is left).

MIN_DEFAULTS: dict[str, float] = {
    LP_LOSS: 10.0,
    LP_CHAD: 10.0,
    PUSSY: 0.0,
    DUO_LEECH: 0.0,
    INT: 0.0,
}

# Human unit for each award's bar — UI labels and skip lines.
MIN_UNITS: dict[str, str] = {
    LP_LOSS: "LP lost",
    LP_CHAD: "LP gained",
    PUSSY: "% drop",
    DUO_LEECH: "% of games premade",
    INT: "deaths",
}


def qualifying_magnitude(award: str, value: float) -> float:
    """The winning value on the award's threshold scale (MIN_UNITS):
    LP magnitude for the LP awards (lp_loss values are negative),
    percent for the share/drop awards, deaths for the int."""
    if award == LP_LOSS:
        return -float(value)
    if award in (PUSSY, DUO_LEECH):
        return float(value) * 100
    return float(value)


def parse_minimum(value: str | None, default: float) -> float:
    """Threshold from a bot_config string; unparseable/negative -> default."""
    if value is None:
        return default
    try:
        parsed = float(value.strip())
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


# Replaces the LP awards' skip lines on a reset week — the normal lines
# ("Disgustingly competent") would read as a stats claim that's wrong.
RESET_LP_SKIP_LINE = "Season reset — the ladder ate everyone's LP. Fresh climb, fresh pain."


@dataclass(frozen=True)
class AwardMeta:
    emoji: str
    title: str
    roasts: tuple[str, ...]  # rotated by ISO week number, see roast_line
    skip_line: str  # posted when the award has no winner


AWARDS: dict[str, AwardMeta] = {
    LP_LOSS: AwardMeta(
        emoji="\U0001f4c9",  # chart decreasing
        title="Biggest LP Loss Loser",
        roasts=(
            "Uninstall was right there.",
            "The ladder called — it wants a restraining order.",
            "Riot should be sending a welfare check.",
            "That's not a loss streak, that's a lifestyle.",
            "LP is a renewable resource, apparently.",
            "Somewhere a Challenger smurf says thank you for the donation.",
            "The rank decay was faster with him playing.",
            "His LP graph needs a content warning.",
            "Queue up again, the floor has more floors.",
            "Certified ELO philanthropist.",
        ),
        skip_line="Nobody lost net LP this week. Disgustingly competent.",
    ),
    LP_CHAD: AwardMeta(
        emoji="\U0001f4c8",  # chart increasing
        title="LP Chad of the Week",
        roasts=(
            "Leave some LP for the rest of us.",
            "The ladder's dad now.",
            "Built different, allegedly.",
            "Somebody run an anticheat on this man.",
            "Peak performance. Suspiciously peak.",
            "The boosted allegations start Monday.",
            "Won the week, still can't win an argument in voice.",
            "Drug test this man's peripherals.",
            "Carried his winrate like it owed him money.",
            "New duo applications open Friday, bring offerings.",
        ),
        skip_line="Nobody gained net LP this week. Group therapy is on Thursdays.",
    ),
    PUSSY: AwardMeta(
        emoji="\U0001f408",  # cat
        title="Pussy of the Weak",
        roasts=(
            "The queue button doesn't bite.",
            "Ranked anxiety remains undefeated.",
            "ARAM counts now. There is nowhere left to hide.",
            "The grind waited. They never came.",
            "Protecting that rank like it's a family heirloom.",
            "Touch grass achieved. Wrong week for it.",
            "The LP was too scared to be lost this week.",
            "Scheduled maintenance on the mental.",
            "Witness protection but for ranked.",
            "His main is filing for abandonment.",
        ),
        skip_line="Everyone kept up their habit this week — no cowards detected. \U0001f44f",
    ),
    DUO_LEECH: AwardMeta(
        emoji="\U0001f91d",  # handshake
        title="Duo Leech",
        roasts=(
            "Solo queue, allegedly.",
            "Can't queue alone, won't queue alone.",
            "The duo carries, the leech collects.",
            "Emotional support duo required at all times.",
            "Separation anxiety, ranked edition.",
            "Two names on the account application next season.",
            "The rank is a joint bank account and he's not the earner.",
            "Files taxes jointly with his duo.",
            "One more duo game and Riot sends a wedding gift.",
            "His solo queue placement is 'accompanied minor'.",
        ),
        skip_line="No duo leeches this week — everyone queued like adults.",
    ),
    INT: AwardMeta(
        emoji="\U0001f480",  # skull
        title="Int of the Week",
        roasts=(
            "That's not inting, that's performance art.",
            "Deaths bought in bulk this week.",
            "Gray screen simulator: 100% completion.",
            "Running it down with purpose and conviction.",
            "The enemy team owes him a cut of the LP.",
            "Respawn timer counted as screen time.",
            "Minimap was a suggestion.",
            "Speedran the fountain-to-fountain any% category.",
            "His death recap needed a scroll bar.",
            "KDA readable only in scientific notation.",
        ),
        skip_line="Nobody managed a proper int this week. Boring.",
    ),
}


# Default cabinet taglines per award — the dashboard shows these on its
# trophy cabinet and registers award_<key>_tagline bot_config keys whose
# default is exactly this text. A stored tagline EQUAL to the default is
# treated as "not customized" (the ceremony post stays unchanged).
# The LP lines carry the ranked-solo note because those two awards never
# widen to other queues — LP exists nowhere else.
DEFAULT_TAGLINES: dict[str, str] = {
    LP_LOSS: "Donated the most LP back to the ladder in a single week. Ranked solo — "
    "the only place LP exists.",
    LP_CHAD: "Climbed hardest. Touched the least grass. Ranked solo — the only place " "LP exists.",
    PUSSY: "Sharpest drop in games played — ARAM spam counts. Queue anxiety is real.",
    DUO_LEECH: "Physically incapable of pressing the queue button alone. Any queue.",
    INT: "The single most disgusting scoreline recorded that week, in any queue.",
}

# Rendered next to a forced winner — banter, but also an audit trail.
MANAGEMENT_NOTE = "⚖️ *winner chosen by management*"


@dataclass(frozen=True)
class Winner:
    """One winner of one award — ties produce several per award."""

    user_id: int
    display_name: str
    value: float  # the number that condemns them (weekly_awards.value)
    detail: dict  # award-specific evidence (weekly_awards.detail)


@dataclass(frozen=True)
class GameRow:
    """One tracked player's game inside the award week.

    Produced by fetch_game_rows — remakes (early_surrender) are already
    excluded there, in every scope: a 3-minute remake is not a game.
    The rich-detail columns are nullable (rows predating the 2026-08
    capture pass, or payloads too old to carry challenges/pings) —
    metric pickers skip rows without the stat they need.
    """

    user_id: int
    display_name: str
    queue_id: int
    kills: int
    deaths: int
    assists: int
    champion: str
    win: int
    match_id: str
    # How many OTHER tracked players shared this player's team in this
    # match: 0 = solo, 1 = duo, 2+ = a stack. The group runs full
    # premades, so this distinguishes "duo'd" from "queued with the
    # whole friend group" in the Duo Leech evidence.
    allies: int
    time_dead_sec: int | None = None
    pings_total: int | None = None
    pings_missing: int | None = None
    kill_participation: float | None = None
    team_damage_pct: float | None = None
    largest_multi_kill: int | None = None
    first_blood: int | None = None


@dataclass(frozen=True)
class AwardAdjustments:
    """Dashboard-driven controls the ceremony applies before posting.

    ``disabled``: awards skipped outright (no block, no recorded rows).
    ``taglines``: award -> CUSTOM tagline (only entries differing from
    DEFAULT_TAGLINES), rendered as an italic line under the title.
    ``forced``: award -> user id whose win is forced — honored only
    while they still qualify as a candidate (the dashboard offers
    computed candidates, but data can move between click and Monday);
    a no-longer-qualifying forced pick falls back to the computed
    winner(s).
    ``excluded``: award -> user ids recomputed away ("on holiday").
    ``metrics`` / ``scopes``: award -> per-week chosen measure (METRICS
    key) and queue scope (SCOPES key) from award_overrides.
    ``default_scope``: the awards_scope bot_config key — scope applied
    when no per-week choice exists.
    ``minimums``: award -> qualification bar from the award_<key>_min
    bot_config keys; absent awards use MIN_DEFAULTS (LP awards: 10).

    Precedence: exclusion beats forcing (a user both excluded and forced
    stays out); forcing beats the metric (it picks WHO wins, the metric
    still defines what is measured) and BYPASSES the qualification bar;
    chosen metric/scope beat the defaults and pause the bar (it is
    denominated in the default metric's unit — see MIN_DEFAULTS).
    """

    disabled: frozenset[str] = frozenset()
    taglines: dict[str, str] | None = None
    forced: dict[str, int] | None = None
    excluded: dict[str, frozenset[int]] | None = None
    metrics: dict[str, str] | None = None
    scopes: dict[str, str] | None = None
    default_scope: str = SCOPE_ALL
    minimums: dict[str, float] | None = None

    def tagline_for(self, award: str) -> str | None:
        return (self.taglines or {}).get(award)

    def forced_for(self, award: str) -> int | None:
        return (self.forced or {}).get(award)

    def excluded_for(self, award: str) -> frozenset[int]:
        return (self.excluded or {}).get(award, frozenset())

    def metric_for(self, award: str) -> str | None:
        return (self.metrics or {}).get(award)

    def scope_for(self, award: str) -> str | None:
        return (self.scopes or {}).get(award)

    def minimum_for(self, award: str) -> float:
        return (self.minimums or {}).get(award, MIN_DEFAULTS[award])


@dataclass(frozen=True)
class AwardInputs:
    """Everything the pure award math needs, fetched in one pass.

    ``window_rows``: (user_id, display_name, queue_id, in_week) per game
    over the 4 prior weeks + the award week — the games-count metrics
    derive per-scope prior/week counts from it.
    ``games``: GameRow per in-week game (all queues, remakes excluded).
    ``partner_rows``: (user_id, partner_user_id, partner_name, queue_id,
    games_together) per tracked pair per queue.
    ``boundary_reset`` carries the seasons-table signal so
    compute_results stays synchronous and fixture-testable.
    """

    baseline: list[tuple]
    first_in_week: list[tuple]
    last_in_week: list[tuple]
    window_rows: list[tuple]
    games: list[GameRow]
    partner_rows: list[tuple]
    boundary_reset: bool = False


@dataclass(frozen=True)
class AwardPools:
    """The per-award input pools, LP already aggregated — the pure-math
    substrate every metric picker draws from."""

    per_user: dict[int, dict]
    window_rows: list[tuple]
    games: list[GameRow]
    partner_rows: list[tuple]


# ----------------------------------------------------------------- weeks


def week_bounds(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """[Monday 00:00, next Monday 00:00) London wall time containing ``now``.

    Built with ``combine(date, 00:00, LONDON)`` rather than timedelta
    arithmetic on an aware datetime so a DST transition inside the week
    can't shift the boundary — London switches at 01:00 on a Sunday, so
    Monday 00:00 itself is never ambiguous.
    """
    local = now.astimezone(LONDON)
    monday = (local - dt.timedelta(days=local.weekday())).date()
    return (
        dt.datetime.combine(monday, dt.time(), tzinfo=LONDON),
        dt.datetime.combine(monday + dt.timedelta(days=7), dt.time(), tzinfo=LONDON),
    )


def previous_week_bounds(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """Bounds of the calendar week that ended most recently before ``now``."""
    start, _ = week_bounds(now)
    monday = start.date()
    return (
        dt.datetime.combine(monday - dt.timedelta(days=7), dt.time(), tzinfo=LONDON),
        start,
    )


def week_epoch(week_start: dt.date) -> int:
    """Unix seconds of Monday 00:00 London for Discord <t:...:d> markup."""
    return int(dt.datetime.combine(week_start, dt.time(), tzinfo=LONDON).timestamp())


# ----------------------------------------------------------- LP scale


_TIER_ORDER = ("Iron", "Bronze", "Silver", "Gold", "Platinum", "Emerald", "Diamond")
_APEX_TIERS = ("Master", "Grandmaster", "Challenger")
_DIVISION_OFFSET = {"IV": 0, "III": 1, "II": 2, "I": 3}
_APEX_FLOOR = len(_TIER_ORDER) * 400  # top of Diamond I 100lp == Master 0lp


def absolute_lp(tier: str, division: str | None, lp: int) -> int:
    """(tier, division, lp) -> one absolute LP number. Iron IV 0lp = 0.

    Each division is 100 LP (IV -> I within a tier), tiers stack 400
    apart. Master/Grandmaster/Challenger share a single continuum above
    Diamond: apex tiers have no divisions and apex "promotion" doesn't
    reset LP, so all three map to ``2800 + lp`` (GM at 500lp and
    Challenger at 500lp are the same height by construction).

    Deliberately NOT Ranker._score — its adjustment_factor is a sort
    hack, unusable as a difference scale. Raises ValueError on a tier or
    division string it doesn't know so callers can skip bad rows.
    """
    tier_name = tier.title()
    if tier_name in _APEX_TIERS:
        return _APEX_FLOOR + int(lp)
    try:
        tier_index = _TIER_ORDER.index(tier_name)
        division_offset = _DIVISION_OFFSET[(division or "").upper()]
    except (ValueError, KeyError) as exc:
        raise ValueError(f"unknown tier/division {tier!r} {division!r}") from exc
    return tier_index * 400 + division_offset * 100 + int(lp)


# --------------------------------------------------- pure award pickers


def net_lp_deltas(
    baseline_rows: list[tuple],
    first_in_week_rows: list[tuple],
    last_in_week_rows: list[tuple],
) -> tuple[dict[int, dict], int]:
    """Net weekly LP delta per Discord user, summed across their accounts.

    All three row lists share the shape ``(puuid, discord_user_id,
    display_name, league_username, tier, division, lp, wins, losses)``
    with one row per account (see fetch_lp_snapshot_rows). Per account:
    end = last snapshot inside the week; start = latest snapshot
    at-or-before week start, falling back to the earliest snapshot inside
    the week. Accounts with no in-week snapshot contribute nothing —
    league_history only writes on LP change, so no row means no movement.
    Accounts whose tier string absolute_lp doesn't know are skipped
    rather than poisoning the sum.

    Season-reset handling — a player's wins+losses total only ever grows
    within a split, so a shrink marks a reset crossing:

    - baseline -> first-in-week shrank: the account crossed a reset
      BEFORE the week (late returner). Its baseline is old-ladder LP, so
      the delta rebases to the first in-week snapshot — the week counts
      new-ladder movement only. Doesn't count toward reset detection.
      A baseline with UNKNOWN games (legacy row, NULL wins/losses) also
      rebases: it can't be trusted across a possible reset, and one
      slipping through once mis-measured a Silver placement climb as
      +723 LP against a years-old snapshot.
    - start -> last-in-week shrank: the reset happened INSIDE the week.
      No comparable pair of snapshots exists (a placement demotion reads
      as -700 LP), so the account is excluded and counted in the second
      return value. The caller compares that count to
      seasons.RESET_MIN_ACCOUNTS to decide the whole week was a reset
      week.
    """
    baselines = {row[0]: row for row in baseline_rows}
    firsts = {row[0]: row for row in first_in_week_rows}
    per_user: dict[int, dict] = {}
    reset_accounts = 0

    def games(row: tuple) -> int | None:
        if row[7] is None and row[8] is None:
            return None  # legacy snapshot predating the wins/losses columns
        return (row[7] or 0) + (row[8] or 0)

    for row in last_in_week_rows:
        puuid, user_id, display_name, league_username, tier, division, lp, wins, losses = row
        start = baselines.get(puuid) or firsts.get(puuid)
        if start is None:
            continue  # unreachable: a last-in-week row implies a first-in-week row
        first = firsts.get(puuid)
        if first is not None and start is not first:
            start_games, first_games = games(start), games(first)
            if start_games is None or (first_games is not None and first_games < start_games):
                start = first  # crossed a reset (or untrustable baseline) — rebase
        start_games, end_games = games(start), games(row)
        if start_games is not None and end_games is not None and end_games < start_games:
            reset_accounts += 1
            continue
        try:
            delta = absolute_lp(tier, division, lp) - absolute_lp(start[4], start[5], start[6])
        except ValueError:
            continue
        entry = per_user.setdefault(
            user_id, {"display_name": display_name, "delta": 0, "per_account": {}}
        )
        entry["delta"] += delta
        entry["per_account"][league_username] = delta
    return per_user, reset_accounts


def pick_lp_extreme(per_user: dict[int, dict], *, gain: bool) -> list[Winner]:
    """Largest net gain (``gain=True``) or net loss winner(s).

    Only strictly-positive (resp. strictly-negative) net deltas compete;
    a 0 net week competes for neither award.
    """
    sign = 1 if gain else -1
    candidates = [(uid, e) for uid, e in sorted(per_user.items()) if sign * e["delta"] > 0]
    if not candidates:
        return []
    best = max(sign * entry["delta"] for _, entry in candidates)
    return [
        Winner(
            user_id=uid,
            display_name=entry["display_name"],
            value=float(entry["delta"]),
            detail={"delta": entry["delta"], "per_account": entry["per_account"]},
        )
        for uid, entry in candidates
        if sign * entry["delta"] == best
    ]


# ------------------------------------------- scope-filtered derivations


def games_for_scope(games: list[GameRow], scope: str, metric: str | None = None) -> list[GameRow]:
    """The games a metric sees under ``scope``.

    Death-counting metrics drop Arena from the "all" pool (dying there
    is the game mode, see ARENA_QUEUES); the explicit "arena" scope opts
    back in.
    """
    rows = [g for g in games if in_scope(g.queue_id, scope)]
    if scope == SCOPE_ALL and metric in DEATH_METRICS:
        rows = [g for g in rows if g.queue_id not in ARENA_QUEUES]
    return rows


def volume_rows_for_scope(window_rows: list[tuple], scope: str) -> list[tuple]:
    """(user_id, display_name, prior_4w_games, this_week_games) per user
    under ``scope`` — derived from the raw per-game window rows so every
    scope shares one fetch."""
    per_user: dict[int, list] = {}
    for user_id, display_name, queue_id, in_week in window_rows:
        if not in_scope(queue_id, scope):
            continue
        entry = per_user.setdefault(user_id, [display_name, 0, 0])
        entry[2 if in_week else 1] += 1
    return [(uid, name, prior, week) for uid, (name, prior, week) in sorted(per_user.items())]


def duo_rows_for_scope(games: list[GameRow], scope: str) -> list[tuple]:
    """(user_id, display_name, games, duo_games, duo_wins, solo_wins,
    stack_games) per user under ``scope``. A premade ("duo") game =
    another tracked player has a match_stats row for the same match on
    the same team (match_stats holds only tracked players);
    ``stack_games`` counts the subset with THREE OR MORE tracked players
    on that team (allies >= 2) — the group runs full stacks, and the
    evidence phrasing distinguishes duos from convoys. In Arena "the
    same team" is the 8-player half Riot reports as teamId, not the
    2-man subteam — close enough to "queued together" for this award's
    purposes."""
    per_user: dict[int, list] = {}
    for g in games:
        if not in_scope(g.queue_id, scope):
            continue
        entry = per_user.setdefault(g.user_id, [g.display_name, 0, 0, 0, 0, 0])
        entry[1] += 1
        if g.allies:
            entry[2] += 1
            entry[3] += 1 if g.win == 1 else 0
            if g.allies >= 2:
                entry[5] += 1
        else:
            entry[4] += 1 if g.win == 1 else 0
    return [
        (uid, name, games_n, duo_games, duo_wins, solo_wins, stack_games)
        for uid, (
            name,
            games_n,
            duo_games,
            duo_wins,
            solo_wins,
            stack_games,
        ) in sorted(per_user.items())
    ]


def partner_rows_for_scope(partner_rows: list[tuple], scope: str) -> list[tuple]:
    """(user_id, partner_user_id, partner_name, games_together) under
    ``scope`` — sums the per-queue pair counts fetch_partner_rows
    returns."""
    pairs: dict[tuple[int, int], list] = {}
    for user_id, partner_id, partner_name, queue_id, games_together in partner_rows:
        if not in_scope(queue_id, scope):
            continue
        entry = pairs.setdefault((user_id, partner_id), [partner_name, 0])
        entry[1] += games_together
    return [(uid, pid, name, count) for (uid, pid), (name, count) in sorted(pairs.items())]


# ------------------------------------------------- games-count pickers


def pick_volume_collapse(rows: list[tuple]) -> list[Winner]:
    """Biggest games-played collapse vs the player's own 4-week habit.

    ``rows``: ``(discord_user_id, display_name, prior_4w_games,
    this_week_games)`` per user. Baseline = prior_4w_games / 4; only users
    with a baseline >= 5 games/week qualify, and only if this week came in
    UNDER it. Winner = largest drop ratio (baseline - week) / baseline,
    tie-broken by the higher baseline (the bigger habit fell further).
    Exact Fraction arithmetic so ties are real ties, not float noise.
    """
    candidates = []
    for user_id, display_name, prior_games, week_games in sorted(rows):
        if prior_games < 20:  # baseline (prior/4) must be >= 5 games/week
            continue
        baseline = Fraction(prior_games, 4)
        if week_games >= baseline:
            continue
        ratio = Fraction(prior_games - 4 * week_games, prior_games)  # (baseline-week)/baseline
        candidates.append((ratio, baseline, user_id, display_name, week_games))
    if not candidates:
        return []
    best = max((ratio, baseline) for ratio, baseline, *_ in candidates)
    return [
        Winner(
            user_id=user_id,
            display_name=display_name,
            value=float(ratio),
            detail={
                "baseline": float(baseline),
                "this_week": week_games,
                "drop_pct": round(float(ratio) * 100),
            },
        )
        for ratio, baseline, user_id, display_name, week_games in candidates
        if (ratio, baseline) == best
    ]


def pick_fewest_games(rows: list[tuple]) -> list[Winner]:
    """Fewest games this week among people with a real habit.

    Same row shape and habit gate as pick_volume_collapse (prior 4 weeks
    >= 20 games, i.e. a 5+/week habit) so a lurker who never plays can't
    win by default. Fewest games wins; ties break toward the bigger
    habit (the larger addiction going quiet is the story).
    """
    candidates = []
    for user_id, display_name, prior_games, week_games in sorted(rows):
        if prior_games < 20:
            continue
        candidates.append((week_games, -prior_games, user_id, display_name, prior_games))
    if not candidates:
        return []
    best = min((week, neg_prior) for week, neg_prior, *_ in candidates)
    return [
        Winner(
            user_id=user_id,
            display_name=display_name,
            value=float(week_games),
            detail={"baseline": float(Fraction(prior_games, 4)), "this_week": week_games},
        )
        for week_games, neg_prior, user_id, display_name, prior_games in candidates
        if (week_games, neg_prior) == best
    ]


def pick_duo_leech(rows: list[tuple], partner_rows: list[tuple] = ()) -> list[Winner]:
    """Highest premade-game fraction this week; min 5 games, min 2 of
    them premade.

    ``rows``: ``(discord_user_id, display_name, games, duo_games,
    duo_wins, solo_wins, stack_games)`` per user from
    duo_rows_for_scope. ``partner_rows``: ``(discord_user_id,
    partner_user_id, partner_name, games_together)`` from
    partner_rows_for_scope.

    The detail names EVERY partner with per-pair counts ("partners",
    best-first) rather than a single top partner: the group runs 3+
    stacks, so one game can count toward several pairs and per-pair
    numbers legitimately sum past duo_games — Jack can be 15/15 with
    Gabes while Samuel is 5/5 with Jack, and naming only one partner
    made those true numbers read as a contradiction. ``stack_games``
    (3+ tracked on the team) is kept so the rendering can say "stacked"
    instead of "duo'd" when that's what actually happened.
    """
    partners: dict[int, list[tuple[int, str]]] = {}
    for user_id, _partner_id, partner_name, games_together in partner_rows:
        partners.setdefault(user_id, []).append((games_together, partner_name))

    candidates = []
    for user_id, display_name, games, duo_games, duo_wins, solo_wins, stack_games in sorted(rows):
        if games < 5 or duo_games < 2:
            continue
        fraction = Fraction(duo_games, games)
        candidates.append(
            (fraction, user_id, display_name, games, duo_games, duo_wins, solo_wins, stack_games)
        )
    if not candidates:
        return []
    best = max(fraction for fraction, *_ in candidates)
    winners = []
    for (
        fraction,
        user_id,
        display_name,
        games,
        duo_games,
        duo_wins,
        solo_wins,
        stack_games,
    ) in candidates:
        if fraction != best:
            continue
        solo_games = games - duo_games
        detail = {
            "games": games,
            "duo_games": duo_games,
            "duo_wins": duo_wins,
            "duo_losses": duo_games - duo_wins,
            "solo_wins": solo_wins,
            "solo_losses": solo_games - solo_wins,
        }
        if stack_games:
            detail["stack_games"] = stack_games
        mates = sorted(partners.get(user_id, []), key=lambda pair: (-pair[0], pair[1]))
        if mates:
            detail["partners"] = [[name, games_together] for games_together, name in mates]
        winners.append(
            Winner(
                user_id=user_id,
                display_name=display_name,
                value=float(fraction),
                detail=detail,
            )
        )
    return winners


# ------------------------------------------------- single-game pickers


def _game_detail(g: GameRow) -> dict:
    """The evidence a single-game trophy records — scoreline, champion,
    result, match link AND the queue, so an ARAM 2/15/3 reads as the
    ARAM it was."""
    return {
        "kills": g.kills,
        "deaths": g.deaths,
        "assists": g.assists,
        "champion": g.champion,
        "win": int(g.win),
        "match_id": g.match_id,
        "queue_id": g.queue_id,
        "queue": queue_name(g.queue_id),
    }


def _single_game_pick(games: list[GameRow], badness, value_of) -> list[Winner]:
    """Generic worst-single-game pick: max ``badness`` wins, one trophy
    per user even when two of their own games tie (deterministic: the
    lexically-first match_id supplies the detail)."""
    scored = [(badness(g), g) for g in games]
    scored = [(b, g) for b, g in scored if b is not None]
    if not scored:
        return []
    worst = max(b for b, _ in scored)
    winners: list[Winner] = []
    seen: set[int] = set()
    for b, g in sorted(scored, key=lambda pair: (pair[1].user_id, pair[1].match_id)):
        if b != worst or g.user_id in seen:
            continue
        seen.add(g.user_id)
        winners.append(
            Winner(
                user_id=g.user_id,
                display_name=g.display_name,
                value=float(value_of(g)),
                detail=_game_detail(g),
            )
        )
    return winners


def pick_int(games: list[GameRow]) -> list[Winner]:
    """Most deaths in a single game; ties broken by the worse (k+a)/d.

    0-death games can't win (and an empty pool returns [], letting the
    skip line run).
    """

    def badness(g: GameRow):
        if g.deaths <= 0:
            return None
        return (g.deaths, -Fraction(g.kills + g.assists, g.deaths))

    return _single_game_pick(games, badness, lambda g: g.deaths)


def pick_lowest_kda(games: list[GameRow]) -> list[Winner]:
    """Worst (kills+assists)/deaths in a single game; ties broken by the
    higher death count. Deathless games can't compete."""

    def badness(g: GameRow):
        if g.deaths <= 0:
            return None
        return (-Fraction(g.kills + g.assists, g.deaths), g.deaths)

    return _single_game_pick(
        games, badness, lambda g: float(Fraction(g.kills + g.assists, g.deaths))
    )


def pick_damage_share(games: list[GameRow]) -> list[Winner]:
    """Largest single-game share of the team's damage (challenges-derived
    column; rows without it can't compete)."""

    def badness(g: GameRow):
        return None if g.team_damage_pct is None else g.team_damage_pct

    return _single_game_pick(games, badness, lambda g: g.team_damage_pct)


def pick_multikill(games: list[GameRow]) -> list[Winner]:
    """Biggest multikill of the week — double or better, a lone kill is
    not a multikill."""

    def badness(g: GameRow):
        if g.largest_multi_kill is None or g.largest_multi_kill < 2:
            return None
        return g.largest_multi_kill

    return _single_game_pick(games, badness, lambda g: g.largest_multi_kill)


# ----------------------------------------------- week-aggregate pickers


def _week_totals(games: list[GameRow], stat) -> dict[int, list]:
    """user_id -> [display_name, total, games_with_data] for a nullable
    per-game stat."""
    per_user: dict[int, list] = {}
    for g in games:
        value = stat(g)
        if value is None:
            continue
        entry = per_user.setdefault(g.user_id, [g.display_name, 0, 0])
        entry[1] += value
        entry[2] += 1
    return per_user


def pick_week_total(games: list[GameRow], stat, *, min_total: int = 1) -> list[Winner]:
    """Highest week-total of a per-game stat; joint winners on ties.
    Totals under ``min_total`` don't compete (zero of something is not
    an achievement)."""
    per_user = _week_totals(games, stat)
    candidates = [
        (total, uid, name, n)
        for uid, (name, total, n) in sorted(per_user.items())
        if total >= min_total
    ]
    if not candidates:
        return []
    best = max(total for total, *_ in candidates)
    return [
        Winner(
            user_id=uid,
            display_name=name,
            value=float(total),
            detail={"total": total, "games": n},
        )
        for total, uid, name, n in candidates
        if total == best
    ]


def pick_low_kp(games: list[GameRow]) -> list[Winner]:
    """Lowest average kill participation; min 3 games with the stat.

    The average is over games that HAVE the challenges-derived value —
    old payloads without it neither help nor hurt.
    """
    per_user: dict[int, list] = {}
    for g in games:
        if g.kill_participation is None:
            continue
        entry = per_user.setdefault(g.user_id, [g.display_name, 0.0, 0])
        entry[1] += g.kill_participation
        entry[2] += 1
    candidates = [
        (total / n, uid, name, n) for uid, (name, total, n) in sorted(per_user.items()) if n >= 3
    ]
    if not candidates:
        return []
    best = min(avg for avg, *_ in candidates)
    return [
        Winner(
            user_id=uid,
            display_name=name,
            value=float(avg),
            detail={"avg_kp": round(avg, 4), "games": n},
        )
        for avg, uid, name, n in candidates
        if avg == best
    ]


def pick_low_winrate(games: list[GameRow]) -> list[Winner]:
    """Lowest win fraction on the week; min 5 games, ties broken by the
    larger sample (losing more often over more games is worse)."""
    per_user: dict[int, list] = {}
    for g in games:
        entry = per_user.setdefault(g.user_id, [g.display_name, 0, 0])
        entry[1] += 1 if g.win == 1 else 0
        entry[2] += 1
    candidates = []
    for uid, (name, wins, n) in sorted(per_user.items()):
        if n < 5:
            continue
        candidates.append((-Fraction(wins, n), n, uid, name, wins))
    if not candidates:
        return []
    best = max((neg, n) for neg, n, *_ in candidates)
    return [
        Winner(
            user_id=uid,
            display_name=name,
            value=float(-neg),
            detail={"wins": wins, "losses": n - wins, "games": n},
        )
        for neg, n, uid, name, wins in candidates
        if (neg, n) == best
    ]


# --------------------------------------------------- metric dispatcher


def pick_metric(metric: str, pools: AwardPools, scope: str, keep=None) -> list[Winner]:
    """Winner(s) of one metric over ``pools`` under ``scope``.

    ``keep(user_id)`` filters the pool BEFORE picking (exclusions and
    forced-winner re-picks) so thresholds recompute over the remaining
    players. LP metrics ignore the scope — LP lives on the ranked-solo
    ladder only. Season-reset skipping is the caller's job (it needs the
    reset flag).
    """
    if keep is None:

        def keep(user_id: int) -> bool:
            return True

    if metric in ("lp_loss", "lp_gain"):
        pool = {uid: entry for uid, entry in pools.per_user.items() if keep(uid)}
        return pick_lp_extreme(pool, gain=metric == "lp_gain")
    if metric in ("games_drop", "fewest_games"):
        rows = [row for row in volume_rows_for_scope(pools.window_rows, scope) if keep(row[0])]
        return pick_volume_collapse(rows) if metric == "games_drop" else pick_fewest_games(rows)
    if metric == "duo_share":
        return pick_duo_leech(
            [row for row in duo_rows_for_scope(pools.games, scope) if keep(row[0])],
            partner_rows_for_scope(pools.partner_rows, scope),
        )
    games = [g for g in games_for_scope(pools.games, scope, metric) if keep(g.user_id)]
    if metric == "most_deaths_game":
        return pick_int(games)
    if metric == "lowest_kda_game":
        return pick_lowest_kda(games)
    if metric == "most_deaths_total":
        return pick_week_total(games, lambda g: g.deaths)
    if metric == "most_time_dead":
        return pick_week_total(games, lambda g: g.time_dead_sec)
    if metric == "most_missing_pings":
        return pick_week_total(games, lambda g: g.pings_missing)
    if metric == "most_pings":
        return pick_week_total(games, lambda g: g.pings_total)
    if metric == "most_first_bloods":
        return pick_week_total(games, lambda g: g.first_blood)
    if metric == "lowest_kp":
        return pick_low_kp(games)
    if metric == "lowest_winrate":
        return pick_low_winrate(games)
    if metric == "biggest_damage_share":
        return pick_damage_share(games)
    if metric == "largest_multikill":
        return pick_multikill(games)
    raise ValueError(f"unknown metric {metric!r}")


def effective_metric(adj: AwardAdjustments, award: str) -> str:
    """The metric this award measures this week — the per-week choice
    when it names a real metric, else the award's built-in one."""
    metric = adj.metric_for(award)
    return metric if metric in METRICS else DEFAULT_METRIC[award]


def effective_scope(adj: AwardAdjustments, award: str) -> str:
    """The queue scope this award scores this week. LP metrics pin to
    ranked (LP exists nowhere else); otherwise the per-week choice, else
    the awards_scope default."""
    if METRICS[effective_metric(adj, award)].lp_based:
        return SCOPE_RANKED
    scope = adj.scope_for(award)
    if scope in SCOPES:
        return scope
    return parse_scope(adj.default_scope)


def measure_note(award: str, metric: str, scope: str, default_scope: str) -> str | None:
    """Human "this week: ..." note for a re-pointed award, or None when
    the award runs its defaults. Woven into the ceremony block so the
    banter self-explains."""
    parts = []
    if metric != DEFAULT_METRIC[award]:
        parts.append(f"measured by {METRICS[metric].label}")
    if not METRICS[metric].lp_based and scope != parse_scope(default_scope):
        label = SCOPES[scope][0]
        parts.append("scored across all queues" if scope == SCOPE_ALL else f"{label} games only")
    if not parts:
        return None
    return "this week: " + " · ".join(parts)


def measure_notes(adj: AwardAdjustments) -> dict[str, str]:
    """award -> measure note for every award running a non-default
    measure this week (build_ceremony_blocks' ``measures``)."""
    notes = {}
    for award in AWARD_ORDER:
        note = measure_note(
            award, effective_metric(adj, award), effective_scope(adj, award), adj.default_scope
        )
        if note:
            notes[award] = note
    return notes


# ---------------------------------------------------------- commentary
# One deterministic, data-driven line per award, rendered in both the
# Monday ceremony block and on the live cabinet card. Situation-keyed
# template banks; the variant is picked by hashing (award, week,
# situation) — NOT random — so the bot and the dashboard render the
# identical line and a re-posted ceremony repeats itself exactly.
# Pure code, no LLM: an opt-in LLM flavor tier can layer on later.

COMMENTARY_VARIANTS: dict[str, tuple[str, ...]] = {
    "tie": (
        "A dead heat — {n} names, one trophy, zero dignity.",
        "Tied. The engraver is furious.",
        "Joint winners. Misery loves company.",
    ),
    "stack_week": (
        "Not even duos — {stacks} of those were three-plus stacks. A family reunion in queue.",
        "{stacks} games stacked three deep or more. That's not a duo, that's a convoy.",
        "The queue was a group chat: {stacks} full-stack games.",
    ),
    "close_race": (
        "Closely followed by {runner} ({gap} behind) — heartbreak.",
        "{runner} finished just {gap} short. So close to glory. Or shame.",
        "Photo finish: {runner} was {gap} away.",
    ),
    "streak": (
        "That's {n} weeks running. A dynasty.",
        "{n} consecutive weeks now. At this point it's a residency.",
        "Week {n} of this. Somebody check on him.",
    ),
    "landslide": (
        "Nobody else was close — {runner} trailed by {gap}.",
        "A landslide. Second place ({runner}) needed a telescope.",
        "Daylight in second: {runner} finished {gap} back.",
    ),
    "first_win": (
        "Their first “{title}” this season. They grow up so fast.",
        "A new name on this trophy — first time this season.",
        "First “{title}” of the season. The taste of it changes you.",
    ),
    "thin_sample": (
        "Only {n} games between them this week — small sample, big feelings.",
        "Barely {n} games of evidence. The court convicts anyway.",
    ),
    "near_miss": (
        "No one qualified — {closest} came closest at {value}; the bar is more than {min} {unit}.",
        "The bar sits above {min} {unit}. {closest} walked under it at {value}.",
    ),
}

# A close race: runner within 15% of the winner's number. A landslide:
# the winner leads by half the field's larger number or more.
CLOSE_GAP = 0.15
LANDSLIDE_GAP = 0.5


def _variant(situation: str, award: str, week_start: dt.date) -> str:
    """Deterministic phrasing pick — same digest recipe as roast_line so
    both codebases and any rerun agree."""
    pool = COMMENTARY_VARIANTS[situation]
    digest = hashlib.md5(f"{award}:{week_start.isoformat()}:{situation}".encode()).digest()
    return pool[int.from_bytes(digest[:4], "big") % len(pool)]


def gap_text(metric: str, gap: float) -> str:
    """A value DIFFERENCE in the metric's unit, e.g. '6 LP', '2 deaths',
    '4m 10s', '8%'. Companion to metric_value_text (which formats whole
    values)."""
    if metric in ("lp_loss", "lp_gain"):
        return f"{round(gap)} LP"
    if metric in ("games_drop", "duo_share", "lowest_kp", "lowest_winrate", "biggest_damage_share"):
        return f"{gap:.0%}"
    if metric == "most_time_dead":
        return fmt_duration(gap)
    if metric == "lowest_kda_game":
        return f"{gap:.2f} KDA"
    if metric == "fewest_games":
        return _plural(round(gap), "game")
    if metric in ("most_deaths_game", "most_deaths_total"):
        return _plural(round(gap), "death")
    if metric == "most_missing_pings":
        return _plural(round(gap), "? ping")
    if metric == "most_pings":
        return _plural(round(gap), "ping")
    if metric == "most_first_bloods":
        return _plural(round(gap), "first blood")
    if metric == "largest_multikill":
        return _plural(round(gap), "kill")
    return f"{gap:g}"


def _streak_length(
    week_start: dt.date, winner_ids: set[int], history: list[tuple[dt.date, frozenset[int]]]
) -> int:
    """Consecutive weeks (current one included) a current winner has held
    this award — strictly week-on-week, a skipped week breaks the run.
    ``history`` is (week_start, winner id set) most-recent-first."""
    streak = 1
    expected = week_start - dt.timedelta(days=7)
    for week, ids in history:
        if week != expected or not (winner_ids & ids):
            break
        streak += 1
        expected -= dt.timedelta(days=7)
    return streak


def _games_played(detail: dict) -> int | None:
    """This-week game count from a winner detail, when it carries one."""
    for key in ("games", "this_week"):
        if isinstance(detail.get(key), int):
            return detail[key]
    return None


def commentary_line(
    award: str,
    metric: str,
    week_start: dt.date,
    winners: list[Winner],
    runners_up: list[Winner],
    history: list[tuple[dt.date, frozenset[int]]],
) -> str | None:
    """ONE data-driven line for the award, or None when nothing is worth
    saying. Priority (first hit wins): tie > stack-heavy premade week
    (Duo Leech flavor) > close race > win streak > landslide >
    first win this season > thin sample. Forced awards get no line —
    the management note is the story (callers skip them); below-the-bar
    weeks use below_min_line instead.
    """
    if not winners:
        return None
    if len(winners) > 1:
        return _variant("tie", award, week_start).format(n=len(winners))
    winner = winners[0]
    detail = winner.detail or {}
    if metric == "duo_share":
        stacks = detail.get("stack_games", 0)
        if stacks >= 2 and stacks * 2 >= detail.get("duo_games", 0):
            return _variant("stack_week", award, week_start).format(stacks=stacks)
    runner = runners_up[0] if runners_up else None
    relative = None
    if runner is not None:
        gap = abs(winner.value - runner.value)
        largest = max(abs(winner.value), abs(runner.value))
        relative = gap / largest if largest else 0.0
        if relative <= CLOSE_GAP:
            return _variant("close_race", award, week_start).format(
                runner=runner.display_name, gap=gap_text(metric, gap)
            )
    streak = _streak_length(week_start, {winner.user_id}, history)
    if streak >= 2:
        return _variant("streak", award, week_start).format(n=streak)
    if runner is not None and relative >= LANDSLIDE_GAP:
        return _variant("landslide", award, week_start).format(
            runner=runner.display_name, gap=gap_text(metric, abs(winner.value - runner.value))
        )
    prior_winners: set[int] = set()
    for _week, ids in history:
        prior_winners |= ids
    if winner.user_id not in prior_winners:
        return _variant("first_win", award, week_start).format(title=AWARDS[award].title)
    if runner is not None:
        winner_games = _games_played(detail)
        runner_games = _games_played(runner.detail or {})
        if winner_games is not None and runner_games is not None:
            total = winner_games + runner_games
            if total < 10:
                return _variant("thin_sample", award, week_start).format(n=total)
    return None


def build_commentaries(
    week_start: dt.date,
    results: dict[str, list[Winner]],
    runners_up: dict[str, list[Winner]],
    history: dict[str, list[tuple[dt.date, frozenset[int]]]],
    forced: frozenset[str] | set[str],
    adj: AwardAdjustments,
) -> dict[str, str]:
    """award -> commentary line for every award with something to say
    (build_ceremony_blocks' ``commentary``). Forced awards are skipped —
    comparing a hand-picked winner to the field would be nonsense."""
    lines = {}
    for award in AWARD_ORDER:
        if award in forced:
            continue
        line = commentary_line(
            award,
            effective_metric(adj, award),
            week_start,
            results.get(award) or [],
            runners_up.get(award) or [],
            history.get(award) or [],
        )
        if line:
            lines[award] = line
    return lines


def below_min_line(award: str, week_start: dt.date, info: dict) -> str:
    """The 'nobody earned it' ceremony line for an award whose winning
    number didn't beat the qualification bar — names the near-miss so
    the almost-shame is public. Distinct from an admin-disabled award
    (those post nothing at all)."""
    closest = " & ".join(w.display_name for w in info["winners"])
    first = info["winners"][0]
    value = metric_value_text(DEFAULT_METRIC[award], first.value, first.detail)
    return _variant("near_miss", award, week_start).format(
        closest=closest,
        value=value,
        min=f"{info['min']:g}",
        unit=MIN_UNITS[award],
    )


# ----------------------------------------------------------- rendering


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def fmt_duration(seconds) -> str:
    """Seconds -> '34m 12s' (or '1h 05m' past the hour)."""
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"


def multikill_label(count: int) -> str:
    return {2: "Double kill", 3: "Triple kill", 4: "Quadra kill", 5: "Penta kill"}.get(
        count, f"{count}-kill"
    )


def metric_value_text(metric: str, value, detail: dict | None) -> str:
    """Compact value tag for one metric win, e.g. '-187 LP', '2/15/3 ·
    ARAM', '41m 03s dead'. Shared shape with the dashboard's port —
    keep identical (the parity suite compares outputs).
    """
    detail = detail or {}
    queue_bit = f" · {detail['queue']}" if detail.get("queue") else ""
    if metric in ("lp_loss", "lp_gain"):
        return f"{int(value):+d} LP"
    if metric == "games_drop":
        return f"-{detail.get('drop_pct', round(float(value) * 100))}% volume"
    if metric == "fewest_games":
        return _plural(int(value), "game")
    if metric == "duo_share":
        if "duo_games" in detail and "games" in detail:
            return f"{detail['duo_games']}/{detail['games']} duo"
        return f"{float(value):.0%} duo"
    if metric == "most_deaths_game":
        if {"kills", "deaths", "assists"} <= detail.keys():
            return f"{detail['kills']}/{detail['deaths']}/{detail['assists']}{queue_bit}"
        return f"{int(value)} deaths"
    if metric == "lowest_kda_game":
        return f"{float(value):.2f} KDA{queue_bit}"
    if metric == "most_deaths_total":
        return _plural(int(value), "death")
    if metric == "most_time_dead":
        return f"{fmt_duration(value)} dead"
    if metric == "most_missing_pings":
        return _plural(int(value), "? ping")
    if metric == "most_pings":
        return _plural(int(value), "ping")
    if metric == "lowest_kp":
        return f"{float(value):.0%} avg KP"
    if metric == "lowest_winrate":
        return f"{float(value):.0%} winrate"
    if metric == "most_first_bloods":
        return _plural(int(value), "first blood")
    if metric == "biggest_damage_share":
        return f"{float(value):.0%} team dmg{queue_bit}"
    if metric == "largest_multikill":
        return f"{multikill_label(int(value))}{queue_bit}"
    return str(value)


def _special_roast(award: str, detail: dict) -> str | None:
    """Severity-triggered line that outranks the weekly rotation, or None.

    Only fires on genuinely notable numbers so the regular pool stays the
    common case. Thresholds are absolute, not relative, so the same feat
    earns the same line in any week. Keyed on detail fields, so an award
    re-pointed at a different metric simply never trips them.
    """
    if award == LP_LOSS and detail.get("delta", 0) <= -200:
        return "Two whole divisions, gone. That's a demotion speedrun."
    if award == LP_CHAD and detail.get("delta", 0) >= 200:
        return "Two divisions in a week. Check his basement for a Challenger."
    if award == PUSSY and detail.get("this_week") == 0:
        return "Zero games. The ranked button has filed a missing person report."
    if award == DUO_LEECH and detail.get("games", 0) == detail.get("duo_games", -1):
        return "Not one solo game. Not one."
    if award == INT and detail.get("deaths", 0) >= 15:
        return "Fifteen-plus deaths. The fountain knows him by name."
    return None


def roast_line(award: str, week_start: dt.date, detail: dict | None = None) -> str:
    """Roast for the award: severity override first, else a seeded pick.

    The pick is deterministic for (award, week) — hashlib rather than
    hash() because PYTHONHASHSEED varies per process, and a ceremony
    retried after a partial send failure must post identical text. The
    hash (unlike the old ISO-week index) decorrelates the awards so they
    don't all step through their pools in lockstep.
    """
    special = _special_roast(award, detail or {})
    if special is not None:
        return special
    meta = AWARDS[award]
    digest = hashlib.md5(f"{award}:{week_start.isoformat()}".encode()).digest()
    return meta.roasts[int.from_bytes(digest[:4], "big") % len(meta.roasts)]


def _single_game_where(detail: dict) -> str:
    """'on Ahri (ARAM, loss)' — queue included whenever recorded, so the
    scoreline reads in context (an ARAM 2/15/3 is a different crime)."""
    result = "win" if detail.get("win") else "loss"
    queue_bit = f"{detail['queue']}, " if detail.get("queue") else ""
    return f"on {detail['champion']} ({queue_bit}{result})"


def partner_list_text(detail: dict, cap: int = 3) -> str:
    """'Gabes 15, Sanders 7, Zak 5' (capped, '…' when more) from the
    plural "partners" detail; falls back to the legacy single
    partner/partner_games keys on pre-2026-08 rows. Per-pair counts may
    legitimately sum past duo_games — one 3-stack game counts toward
    every pair in it."""
    partners = detail.get("partners")
    if partners:
        shown = ", ".join(f"{name} {games}" for name, games in partners[:cap])
        return shown + ("…" if len(partners) > cap else "")
    if "partner" in detail:
        return f"{detail['partner']} {detail.get('partner_games', '?')}"
    return ""


def _premade_phrase(detail: dict) -> str:
    """ "stacked with the group"/"duo'd" + the partner names — honest
    about whether the games were duos or full 3+ stacks."""
    partner_text = partner_list_text(detail)
    partner_bit = f" ({partner_text})" if partner_text else ""
    verb = "stacked with the group" if detail.get("stack_games") else "duo'd"
    return f"{verb}{partner_bit}"


def condemn_line(award: str, winner: Winner) -> str:
    """The one-line number that condemns the winner, from their detail.

    Renders whatever metric produced the win — the built-in one, or the
    per-week choice recorded in detail["metric"].
    """
    detail = winner.detail
    metric = detail.get("metric") or DEFAULT_METRIC[award]
    if metric in ("lp_loss", "lp_gain"):
        line = f"Net **{detail['delta']:+d} LP** on the week"
        # Per-account breakdown, but only when it adds information: at
        # least two accounts actually moved (zero-delta accounts stay in
        # the stored detail, just not in the post).
        moved = {name: delta for name, delta in detail["per_account"].items() if delta}
        if len(moved) > 1:
            parts = " · ".join(f"{name} {delta:+d}" for name, delta in moved.items())
            line += f" ({parts})"
        return line + "."
    if metric == "games_drop":
        week_games = detail["this_week"]
        games_word = "game" if week_games == 1 else "games"
        return (
            f"**{week_games}** {games_word} vs a {detail['baseline']:g}-game/week habit "
            f"(**-{detail['drop_pct']}%**)."
        )
    if metric == "fewest_games":
        return (
            f"**{_plural(detail['this_week'], 'game')}** all week "
            f"vs a {detail['baseline']:g}-game/week habit."
        )
    if metric == "duo_share":
        return (
            f"**{detail['duo_games']} of {detail['games']}** games {_premade_phrase(detail)} — "
            f"premade {detail['duo_wins']}W-{detail['duo_losses']}L "
            f"vs solo {detail['solo_wins']}W-{detail['solo_losses']}L."
        )
    if metric == "most_deaths_game":
        return (
            f"**{detail['kills']}/{detail['deaths']}/{detail['assists']}** "
            f"{_single_game_where(detail)}."
        )
    if metric == "lowest_kda_game":
        return (
            f"**{float(winner.value):.2f} KDA** "
            f"({detail['kills']}/{detail['deaths']}/{detail['assists']}) "
            f"{_single_game_where(detail)}."
        )
    if metric == "biggest_damage_share":
        return f"**{float(winner.value):.0%} of the team's damage** {_single_game_where(detail)}."
    if metric == "largest_multikill":
        return f"**{multikill_label(int(winner.value))}** {_single_game_where(detail)}."
    if metric == "most_deaths_total":
        return (
            f"**{_plural(int(winner.value), 'death')}** "
            f"across {_plural(detail['games'], 'game')}."
        )
    if metric == "most_time_dead":
        return (
            f"**{fmt_duration(winner.value)}** spent dead "
            f"across {_plural(detail['games'], 'game')}."
        )
    if metric == "most_missing_pings":
        return (
            f"**{_plural(int(winner.value), '? ping')}** "
            f"across {_plural(detail['games'], 'game')}."
        )
    if metric == "most_pings":
        return (
            f"**{_plural(int(winner.value), 'ping')}** across {_plural(detail['games'], 'game')}."
        )
    if metric == "most_first_bloods":
        return (
            f"**{_plural(int(winner.value), 'first blood')}** "
            f"in {_plural(detail['games'], 'game')}."
        )
    if metric == "lowest_kp":
        return (
            f"**{float(winner.value):.0%} average kill participation** "
            f"over {_plural(detail['games'], 'game')}."
        )
    if metric == "lowest_winrate":
        return (
            f"**{detail['wins']}W-{detail['losses']}L** — "
            f"{float(winner.value):.0%} winrate on the week."
        )
    raise ValueError(f"unknown metric {metric!r} for award {award!r}")


def build_ceremony_blocks(
    week_start: dt.date,
    results: dict[str, list[Winner]],
    header: str | None = None,
    *,
    season_reset: bool = False,
    disabled: frozenset[str] | set[str] = frozenset(),
    taglines: dict[str, str] | None = None,
    forced: frozenset[str] | set[str] = frozenset(),
    measures: dict[str, str] | None = None,
    below_min: dict[str, dict] | None = None,
    commentary: dict[str, str] | None = None,
) -> list[str]:
    """Ceremony post as blocks for leaderboard.chunk_blocks.

    One block per award: emoji + name, winner mention(s), the condemning
    number, one roast, and (when the data says something) a commentary
    line. Joint winners share a block, one detail line each. Every block
    ends with a newline so chunked messages get a blank line between
    awards.

    ``season_reset``: swaps the LP awards' skip lines for the reset line
    — their normal skip lines claim nobody moved, which would be false.

    Dashboard controls: ``disabled`` awards are omitted entirely,
    ``taglines`` (custom only — see AwardAdjustments) render as an
    italic line under the title, awards in ``forced`` carry the
    "chosen by management" note, ``measures`` (award -> measure_note
    text) explain a re-pointed measure, ``below_min`` (from
    compute_results) swaps the skip line for the "nobody earned it"
    near-miss line, and ``commentary`` (award -> build_commentaries
    text) adds the data-driven line. Defaults keep the output
    byte-identical to the pre-controls format.
    """
    if header is None:
        header = f"\U0001f3c6 **Weekly Awards** — week of <t:{week_epoch(week_start)}:d>"
    blocks = [f"{header}\n"]
    taglines = taglines or {}
    measures = measures or {}
    below_min = below_min or {}
    commentary = commentary or {}
    for award in AWARD_ORDER:
        if award in disabled:
            continue
        meta = AWARDS[award]
        measure_line = f"\U0001f4cf *{measures[award]}*\n" if measures.get(award) else ""
        tagline_line = f"*{taglines[award]}*\n" if taglines.get(award) else ""
        winners = results.get(award) or []
        if not winners:
            if season_reset and award in (LP_LOSS, LP_CHAD):
                skip = RESET_LP_SKIP_LINE
            elif award in below_min:
                skip = below_min_line(award, week_start, below_min[award])
            else:
                skip = meta.skip_line
            blocks.append(
                f"{meta.emoji} **{meta.title}** — no winner\n"
                f"{measure_line}{tagline_line}{skip}\n"
            )
            continue
        mentions = " & ".join(f"<@{winner.user_id}>" for winner in winners)
        note = f" {MANAGEMENT_NOTE}" if award in forced else ""
        if len(winners) == 1:
            body = condemn_line(award, winners[0])
        else:
            body = "\n".join(
                f"{winner.display_name}: {condemn_line(award, winner)}" for winner in winners
            )
        # Severity overrides only make sense for a lone winner — joint
        # winners' details differ, so they get the weekly pool line.
        roast_detail = winners[0].detail if len(winners) == 1 else None
        commentary_line_text = (
            f"\U0001f4ac *{commentary[award]}*\n" if commentary.get(award) else ""
        )
        blocks.append(
            f"{meta.emoji} **{meta.title}** — {mentions}{note}\n"
            f"{measure_line}{tagline_line}{body} {roast_line(award, week_start, roast_detail)}\n"
            f"{commentary_line_text}"
        )
    return blocks


def cabinet_value(award: str, value, detail: dict | None) -> str:
    """Compact value tag for a cabinet last-week line, e.g. '(-187 LP)'.

    ``value`` arrives as Decimal from the numeric column; ``detail`` as a
    dict from jsonb (defensively optional). Renders whatever metric won
    the trophy — historical rows without a metric key use the award's
    built-in one and keep their exact pre-2026-08 text.
    """
    detail = detail or {}
    metric = detail.get("metric") or DEFAULT_METRIC.get(award)
    if metric is None:
        return str(value)
    return metric_value_text(metric, value, detail)


def build_cabinet_blocks(
    latest_week: dt.date | None,
    latest_rows: list[tuple],
    count_rows: list[tuple],
) -> list[str]:
    """Trophy cabinet board blocks for leaderboard.wipe_and_post.

    ``latest_rows``: ``(award, discord_user_id, display_name, value,
    detail)`` for the most recent recorded week; ``count_rows``:
    ``(award, display_name, titles)`` all-time. Awards keep AWARD_ORDER;
    within an award, most titles first, then name.
    """
    header = "\U0001f3c6 **Trophy Cabinet** — weekly awards, all time\n"
    if latest_week is None:
        return [header, "No ceremonies recorded yet — the first one posts Monday morning."]

    last_lines = [f"**Last week** (<t:{week_epoch(latest_week)}:d>):"]
    latest_by_award: dict[str, list[tuple]] = {}
    for award, user_id, name, value, detail in latest_rows:
        latest_by_award.setdefault(award, []).append((user_id, name, value, detail))
    for award in AWARD_ORDER:
        rows = latest_by_award.get(award)
        if not rows:
            continue
        meta = AWARDS[award]
        mentions = " & ".join(f"<@{user_id}>" for user_id, *_ in rows)
        last_lines.append(
            f"{meta.emoji} {meta.title} — {mentions} ({cabinet_value(award, rows[0][2], rows[0][3])})"
        )

    count_lines = ["**All-time titles**"]
    counts_by_award: dict[str, list[tuple]] = {}
    for award, name, titles in count_rows:
        counts_by_award.setdefault(award, []).append((titles, name or ""))
    for award in AWARD_ORDER:
        entries = counts_by_award.get(award)
        if not entries:
            continue
        entries.sort(key=lambda pair: (-pair[0], pair[1]))
        joined = " · ".join(f"{titles}× {name}" for titles, name in entries)
        count_lines.append(f"{AWARDS[award].emoji} {AWARDS[award].title}: {joined}")

    return [header, "\n".join(last_lines) + "\n", "\n".join(count_lines)]


# -------------------------------------------------------- SQL wrappers


# One row per tracked account, mapped to its Discord user + display name.
# DISTINCT ON (puuid): league_players is keyed by the legacy leagueid, so a
# renamed account could leave two rows with the same puuid — joining both
# would double-count games. Display name prefers the dashboard-managed
# alias (user_aliases — guaranteed by the bot's own schema), then the
# guild nickname/tag, then league_username, matching the dashboard chain.
_TRACKED_CTE = """
    tracked AS (
        SELECT DISTINCT ON (lp.puuid)
               lp.puuid,
               lp.discord_user_id,
               lp.league_username,
               COALESCE(
                   (SELECT NULLIF(a.alias, '') FROM user_aliases a
                    WHERE a.user_id = lp.discord_user_id),
                   NULLIF(
                       CASE
                           WHEN COALESCE(u.nickname, '') = '' THEN u.discord_tag
                           ELSE u.nickname
                       END,
                       ''),
                   lp.league_username) AS display_name
        FROM league_players lp
            LEFT JOIN users u ON u.user_id = lp.discord_user_id
        WHERE lp.puuid IS NOT NULL
        ORDER BY lp.puuid
    )
"""

# Remakes never count as games, in any queue or scope — a 3-minute
# early-surrender is noise, not volume and not an int. NULL (rows
# predating the column / un-backfilled) means "not a remake".
_NO_REMAKES = "COALESCE(ms.early_surrender, 0) = 0"


def _snapshot_query(where: str, order: str) -> str:
    return (
        "WITH "
        + _TRACKED_CTE
        + """
        SELECT DISTINCT ON (lh.puuid)
               lh.puuid, t.discord_user_id, t.display_name, t.league_username,
               lh.tier, lh.division, lh.lp, lh.wins, lh.losses
        FROM league_history lh
            JOIN tracked t ON t.puuid = lh.puuid
        WHERE lh.queue = %(queue)s
          AND lh.tier IS NOT NULL AND lh.lp IS NOT NULL
          AND """
        + where
        + " ORDER BY lh.puuid, "
        + order
    )


async def fetch_lp_snapshot_rows(
    week_start: dt.datetime, week_end: dt.datetime
) -> tuple[list[tuple], list[tuple], list[tuple]]:
    """(baseline, first-in-week, last-in-week) solo snapshot rows.

    One row per account each: baseline = latest snapshot at-or-before
    week_start, the other two bracket the week. Rows keyed by real puuid
    only — the pre-2024 legacy-key rows can't matter inside a recent
    weekly window.
    """
    params = {"queue": SOLO_QUEUE, "start": week_start, "end": week_end}
    baseline = await db.fetchall(
        _snapshot_query("lh.timestamp <= %(start)s", "lh.timestamp DESC, lh.id DESC"),
        params,
    )
    in_week = "lh.timestamp >= %(start)s AND lh.timestamp < %(end)s"
    first = await db.fetchall(
        _snapshot_query(in_week, "lh.timestamp ASC, lh.id ASC"),
        params,
    )
    last = await db.fetchall(
        _snapshot_query(in_week, "lh.timestamp DESC, lh.id DESC"),
        params,
    )
    return baseline, first, last


async def fetch_window_rows(week_start: dt.datetime, week_end: dt.datetime) -> list[tuple]:
    """(discord_user_id, display_name, queue_id, in_week) — one row per
    game over the 4 full weeks before the award week plus the week
    itself, every queue, remakes excluded. The games-count metrics
    derive per-scope prior/week counts from this single fetch
    (volume_rows_for_scope).
    """
    prior_start = week_start - dt.timedelta(days=28)
    return await db.fetchall(
        "WITH "
        + _TRACKED_CTE
        + f"""
        SELECT t.discord_user_id, t.display_name, ms.queue_id,
               (ms.game_start >= %(start)s) AS in_week
        FROM match_stats ms
            JOIN tracked t ON t.puuid = ms.puuid
        WHERE ms.game_start >= %(prior_start)s AND ms.game_start < %(end)s
          AND {_NO_REMAKES}""",
        {"prior_start": prior_start, "start": week_start, "end": week_end},
    )


async def fetch_game_rows(week_start: dt.datetime, week_end: dt.datetime) -> list[GameRow]:
    """One GameRow per tracked player's game this week — EVERY queue
    (scope filtering is pure Python so per-award scopes share one
    fetch), remakes excluded. ``allies`` counts the OTHER tracked
    players on the same team (the COUNT flavor of the EXISTS pattern in
    FetchFromRiot.get_last_five_games — 2+ marks a stack); NULL team_id
    (rows predating the column) never matches, so it counts as solo.
    """
    rows = await db.fetchall(
        "WITH "
        + _TRACKED_CTE
        + f"""
        SELECT t.discord_user_id, t.display_name, ms.queue_id,
               ms.kills, ms.deaths, ms.assists, ms.champion, ms.win, ms.match_id,
               (
                   SELECT COUNT(*) FROM match_stats o
                   WHERE o.match_id = ms.match_id
                     AND o.team_id = ms.team_id
                     AND o.puuid <> ms.puuid
               ) AS allies,
               ms.time_dead_sec, ms.pings_total, ms.pings_missing,
               ms.kill_participation, ms.team_damage_pct,
               ms.largest_multi_kill, ms.first_blood
        FROM match_stats ms
            JOIN tracked t ON t.puuid = ms.puuid
        WHERE ms.game_start >= %(start)s AND ms.game_start < %(end)s
          AND {_NO_REMAKES}
        ORDER BY ms.game_start, ms.match_id""",
        {"start": week_start, "end": week_end},
    )
    return [GameRow(*row) for row in rows]


async def fetch_partner_rows(week_start: dt.datetime, week_end: dt.datetime) -> list[tuple]:
    """(discord_user_id, partner_user_id, partner_name, queue_id,
    games_together) — per tracked pair per queue this week, feeding the
    "N of them with X" naming in the Duo Leech line after per-scope
    aggregation (partner_rows_for_scope). Same-user pairs are excluded
    so a player's alt can never be their own "partner". Partner accounts
    aggregate to the partner's Discord user like everything else.
    """
    return await db.fetchall(
        "WITH "
        + _TRACKED_CTE
        + f"""
        SELECT t.discord_user_id,
               pt.discord_user_id AS partner_user_id,
               MIN(pt.display_name) AS partner_name,
               ms.queue_id,
               COUNT(*) AS games_together
        FROM match_stats ms
            JOIN match_stats o
                ON o.match_id = ms.match_id
               AND o.team_id = ms.team_id
               AND o.puuid <> ms.puuid
            JOIN tracked t ON t.puuid = ms.puuid
            JOIN tracked pt ON pt.puuid = o.puuid
        WHERE ms.game_start >= %(start)s AND ms.game_start < %(end)s
          AND {_NO_REMAKES}
          AND t.discord_user_id <> pt.discord_user_id
        GROUP BY t.discord_user_id, pt.discord_user_id, ms.queue_id""",
        {"start": week_start, "end": week_end},
    )


async def fetch_inputs(week_start: dt.datetime, week_end: dt.datetime) -> AwardInputs:
    """Every row set the award math needs for [week_start, week_end)."""
    baseline, first, last = await fetch_lp_snapshot_rows(week_start, week_end)
    return AwardInputs(
        baseline=baseline,
        first_in_week=first,
        last_in_week=last,
        window_rows=await fetch_window_rows(week_start, week_end),
        games=await fetch_game_rows(week_start, week_end),
        partner_rows=await fetch_partner_rows(week_start, week_end),
        boundary_reset=await seasons.reset_within(week_start, week_end),
    )


async def fetch_adjustments(week_start: dt.date) -> AwardAdjustments:
    """Dashboard controls for the week: bot_config toggles/taglines and
    the awards_scope default, plus the award_overrides row set (forced
    winner, exclusions, chosen metric + scope). Tolerates
    award_overrides not existing yet (first boot after this deploy
    creates it via the schema)."""
    disabled: set[str] = set()
    taglines: dict[str, str] = {}
    config_rows = await db.fetchall("SELECT key, value FROM bot_config WHERE key LIKE 'award\\_%'")
    by_key = {key: value for key, value in config_rows}
    for award in AWARD_ORDER:
        enabled_value = by_key.get(f"award_{award}_enabled")
        if enabled_value is not None and enabled_value.strip().lower() in (
            "0",
            "false",
            "no",
            "off",
        ):
            disabled.add(award)
        tagline = (by_key.get(f"award_{award}_tagline") or "").strip()
        if tagline and tagline != DEFAULT_TAGLINES[award]:
            taglines[award] = tagline
    minimums: dict[str, float] = {}
    for award in AWARD_ORDER:
        raw = by_key.get(f"award_{award}_min")
        if raw is not None:
            minimums[award] = parse_minimum(raw, MIN_DEFAULTS[award])
    scope_row = await db.fetchone("SELECT value FROM bot_config WHERE key = %s", (SCOPE_KEY,))
    default_scope = parse_scope(scope_row[0] if scope_row else None)
    forced: dict[str, int] = {}
    excluded: dict[str, frozenset[int]] = {}
    metrics: dict[str, str] = {}
    scopes: dict[str, str] = {}
    try:
        override_rows = await db.fetchall(
            "SELECT award_key, forced_winner, excluded_user_ids, chosen_metric, chosen_scope "
            "FROM award_overrides WHERE week_start = %s",
            (week_start,),
        )
    except Exception:  # table not created yet — no overrides to honor
        override_rows = []
    for award_key, forced_winner, excluded_ids, chosen_metric, chosen_scope in override_rows:
        if award_key not in AWARDS:
            continue
        if forced_winner is not None:
            forced[award_key] = forced_winner
        if excluded_ids:
            excluded[award_key] = frozenset(excluded_ids)
        if chosen_metric in METRICS:
            metrics[award_key] = chosen_metric
        if chosen_scope in SCOPES:
            scopes[award_key] = chosen_scope
    return AwardAdjustments(
        disabled=frozenset(disabled),
        taglines=taglines,
        forced=forced,
        excluded=excluded,
        metrics=metrics,
        scopes=scopes,
        default_scope=default_scope,
        minimums=minimums,
    )


async def fetch_prior_winner_history(
    week_start: dt.date,
) -> dict[str, list[tuple[dt.date, frozenset[int]]]]:
    """award -> [(week_start, winner ids)] most-recent-first for weeks
    BEFORE ``week_start``, scoped to the current season (all recorded
    history when the seasons table is empty). Feeds the streak and
    first-win commentary situations."""
    rows = await db.fetchall(
        "SELECT wa.award, wa.week_start, wa.discord_user_id FROM weekly_awards wa "
        "WHERE wa.week_start < %s AND wa.week_start >= COALESCE("
        "(SELECT MAX(started_at)::date FROM seasons), DATE '1970-01-01') "
        "ORDER BY wa.week_start DESC",
        (week_start,),
    )
    grouped: dict[str, dict[dt.date, set[int]]] = {}
    for award, week, user_id in rows:
        grouped.setdefault(award, {}).setdefault(week, set()).add(user_id)
    return {
        award: [(week, frozenset(ids)) for week, ids in sorted(weeks.items(), reverse=True)]
        for award, weeks in grouped.items()
    }


def compute_results(
    inputs: AwardInputs, adjustments: AwardAdjustments | None = None
) -> tuple[dict[str, list[Winner]], bool, frozenset[str], dict[str, dict], dict[str, list[Winner]]]:
    """All five awards from ``inputs``, honoring dashboard adjustments.

    Pure — the fixture tests drive this directly. Returns (results,
    season_reset, forced_applied, below_min, runners_up):

    - season_reset is True when the reset happened inside the window,
      from either of two signals — the in-window shrink count reaching
      seasons.RESET_MIN_ACCOUNTS, or a recorded seasons-table boundary
      (inputs.boundary_reset). On a reset week LP-based metrics are
      skipped outright — cross-reset deltas aren't a comparable field —
      and the ceremony swaps in the reset skip line. Match-derived
      metrics are unaffected: games played are games played.
    - Each award measures its effective metric over its effective scope
      (per-week choices from the dashboard, else DEFAULT_METRIC and the
      awards_scope default). Non-default choices are stamped into the
      winner detail ("metric"/"scope") so history renders correctly.
    - Exclusions recompute an award over the remaining players only.
    - A forced winner is re-picked over their own rows alone, so their
      value/detail are exactly what they earned — and if they no longer
      qualify (dashboard offered candidates, data moved by Monday) the
      computed winner(s) stand and the award is absent from
      forced_applied.
    - Qualification bars: a computed winner whose number doesn't BEAT
      the award's minimum (strictly — "10 LP and below is excluded")
      moves to ``below_min`` (award -> {"min", "winners"}) and the award
      records no winner. The bar lives in the DEFAULT metric's unit, so
      it only judges weeks running the default metric; forced winners
      bypass it. Full precedence: exclusion > forced > chosen measure >
      qualification bar > defaults.
    - ``runners_up``: the next-best group behind the computed leaders
      (threshold-free, forced awards get []) — commentary fuel.
    - ``disabled`` is deliberately NOT applied here: results stay
      complete so previews can show what a disabled award would have
      said; posting and persistence do the skipping.
    """
    adj = adjustments or AwardAdjustments()
    per_user, reset_accounts = net_lp_deltas(
        inputs.baseline, inputs.first_in_week, inputs.last_in_week
    )
    season_reset = reset_accounts >= seasons.RESET_MIN_ACCOUNTS or inputs.boundary_reset
    pools = AwardPools(
        per_user=per_user,
        window_rows=inputs.window_rows,
        games=inputs.games,
        partner_rows=inputs.partner_rows,
    )

    def compute(
        award: str, only: int | None = None, also_excluded: frozenset[int] = frozenset()
    ) -> list[Winner]:
        metric = effective_metric(adj, award)
        scope = effective_scope(adj, award)
        if METRICS[metric].lp_based and season_reset:
            return []
        excluded = adj.excluded_for(award)

        def keep(user_id: int) -> bool:
            return (
                user_id not in excluded
                and user_id not in also_excluded
                and (only is None or user_id == only)
            )

        winners = pick_metric(metric, pools, scope, keep)
        extra: dict = {}
        if metric != DEFAULT_METRIC[award]:
            extra["metric"] = metric
        if not METRICS[metric].lp_based and scope != SCOPE_ALL:
            extra["scope"] = scope
        if not extra:
            return winners
        return [Winner(w.user_id, w.display_name, w.value, {**w.detail, **extra}) for w in winners]

    results: dict[str, list[Winner]] = {}
    forced_applied: set[str] = set()
    below_min: dict[str, dict] = {}
    runners_up: dict[str, list[Winner]] = {}
    for award in AWARD_ORDER:
        winners: list[Winner] | None = None
        forced = adj.forced_for(award)
        if forced is not None and forced not in adj.excluded_for(award):
            forced_winners = compute(award, only=forced)
            if forced_winners:
                winners = forced_winners
                forced_applied.add(award)
        computed: list[Winner] | None = None
        if winners is None:
            computed = compute(award)
            winners = computed
            minimum = adj.minimum_for(award)
            if (
                winners
                and minimum > 0
                and effective_metric(adj, award) == DEFAULT_METRIC[award]
                and qualifying_magnitude(award, winners[0].value) <= minimum
            ):
                below_min[award] = {"min": minimum, "winners": winners}
                winners = []
        results[award] = winners
        if award in forced_applied:
            runners_up[award] = []  # no commentary for managed wins
        else:
            leaders = frozenset(w.user_id for w in (computed or []))
            runners_up[award] = compute(award, also_excluded=leaders) if leaders else []
    return results, season_reset, frozenset(forced_applied), below_min, runners_up


async def compute_all_awards(
    week_start: dt.datetime, week_end: dt.datetime
) -> tuple[dict[str, list[Winner]], bool]:
    """All five awards for [week_start, week_end), no adjustments beyond
    the built-in defaults (all-queues scope, default metrics, MIN_DEFAULTS
    bars) — empty list = skipped. Kept as the stable public entry point;
    the ceremony path goes fetch_inputs + fetch_adjustments +
    compute_results (see the cog). Second return: season reset flag
    (full semantics on compute_results)."""
    results, season_reset, _, _, _ = compute_results(await fetch_inputs(week_start, week_end))
    return results, season_reset
