"""Weekly awards ceremony: computation core.

Five awards computed over one calendar week (Monday 00:00 -> Monday 00:00,
Europe/London wall time) from existing tables — match_stats for games,
league_history for LP — aggregated per Discord user: a person's tracked
league accounts sum together. Solo queue only (queue_id 420 /
league_history.queue 'RANKED_SOLO_5x5').

Layering: pure, deterministic functions do the math and the rendering;
thin async wrappers own the SQL; cogs/weekly_awards.py owns scheduling,
posting and persistence. Roast lines rotate deterministically on the ISO
week number so consecutive weeks differ without any randomness.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from fractions import Fraction
from zoneinfo import ZoneInfo

from utils import db
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
            "Hiding in ARAM doesn't count.",
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


@dataclass(frozen=True)
class Winner:
    """One winner of one award — ties produce several per award."""

    user_id: int
    display_name: str
    value: float  # the number that condemns them (weekly_awards.value)
    detail: dict  # award-specific evidence (weekly_awards.detail)


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
) -> dict[int, dict]:
    """Net weekly LP delta per Discord user, summed across their accounts.

    All three row lists share the shape ``(puuid, discord_user_id,
    display_name, league_username, tier, division, lp)`` with one row per
    account (see fetch_lp_snapshot_rows). Per account: end = last snapshot
    inside the week; start = latest snapshot at-or-before week start,
    falling back to the earliest snapshot inside the week. Accounts with
    no in-week snapshot contribute nothing — league_history only writes
    on LP change, so no row means no movement. Accounts whose tier string
    absolute_lp doesn't know are skipped rather than poisoning the sum.
    """
    baselines = {row[0]: row for row in baseline_rows}
    firsts = {row[0]: row for row in first_in_week_rows}
    per_user: dict[int, dict] = {}
    for puuid, user_id, display_name, league_username, tier, division, lp in last_in_week_rows:
        start = baselines.get(puuid) or firsts.get(puuid)
        if start is None:
            continue  # unreachable: a last-in-week row implies a first-in-week row
        try:
            delta = absolute_lp(tier, division, lp) - absolute_lp(start[4], start[5], start[6])
        except ValueError:
            continue
        entry = per_user.setdefault(
            user_id, {"display_name": display_name, "delta": 0, "per_account": {}}
        )
        entry["delta"] += delta
        entry["per_account"][league_username] = delta
    return per_user


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


def pick_duo_leech(rows: list[tuple], partner_rows: list[tuple] = ()) -> list[Winner]:
    """Highest duo-game fraction this week; min 5 games, min 2 duo games.

    ``rows``: ``(discord_user_id, display_name, games, duo_games,
    duo_wins, solo_wins)`` per user, duo as defined by fetch_duo_rows.
    ``partner_rows``: ``(discord_user_id, partner_user_id, partner_name,
    games_together)`` from fetch_duo_partner_rows. When one partner
    accounts for at least half of the winner's duo games (and >= 2 of
    them), they're named in the detail — the enabler deserves credit.
    The detail keeps the duo-vs-solo records — the leech evidence.
    """
    partners: dict[int, list[tuple[int, str]]] = {}
    for user_id, _partner_id, partner_name, games_together in partner_rows:
        partners.setdefault(user_id, []).append((games_together, partner_name))

    candidates = []
    for user_id, display_name, games, duo_games, duo_wins, solo_wins in sorted(rows):
        if games < 5 or duo_games < 2:
            continue
        fraction = Fraction(duo_games, games)
        candidates.append((fraction, user_id, display_name, games, duo_games, duo_wins, solo_wins))
    if not candidates:
        return []
    best = max(fraction for fraction, *_ in candidates)
    winners = []
    for fraction, user_id, display_name, games, duo_games, duo_wins, solo_wins in candidates:
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
        top = max(partners.get(user_id, []), default=None)  # (games, name): ties by name
        if top is not None and top[0] >= 2 and top[0] * 2 >= duo_games:
            detail["partner"] = top[1]
            detail["partner_games"] = top[0]
        winners.append(
            Winner(
                user_id=user_id,
                display_name=display_name,
                value=float(fraction),
                detail=detail,
            )
        )
    return winners


def pick_int(rows: list[tuple]) -> list[Winner]:
    """Most deaths in a single game; ties broken by the worse (k+a)/d.

    ``rows``: ``(discord_user_id, display_name, kills, deaths, assists,
    champion, win, match_id)`` — one row per game. 0-death games can't
    win (and an empty week returns [], letting the skip line run). A user
    tying with two of their own games still gets one trophy.
    """
    games = [row for row in rows if row[3] > 0]
    if not games:
        return []

    def badness(row: tuple) -> tuple:
        _, _, kills, deaths, assists, *_ = row
        return (deaths, -Fraction(kills + assists, deaths))

    worst = max(badness(row) for row in games)
    winners: list[Winner] = []
    seen: set[int] = set()
    for row in sorted(games):
        if badness(row) != worst:
            continue
        user_id, display_name, kills, deaths, assists, champion, win, match_id = row
        if user_id in seen:
            continue
        seen.add(user_id)
        winners.append(
            Winner(
                user_id=user_id,
                display_name=display_name,
                value=float(deaths),
                detail={
                    "kills": kills,
                    "deaths": deaths,
                    "assists": assists,
                    "champion": champion,
                    "win": int(win),
                    "match_id": match_id,
                },
            )
        )
    return winners


# ----------------------------------------------------------- rendering


def _special_roast(award: str, detail: dict) -> str | None:
    """Severity-triggered line that outranks the weekly rotation, or None.

    Only fires on genuinely notable numbers so the regular pool stays the
    common case. Thresholds are absolute, not relative, so the same feat
    earns the same line in any week.
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


def condemn_line(award: str, winner: Winner) -> str:
    """The one-line number that condemns the winner, from their detail."""
    detail = winner.detail
    if award in (LP_LOSS, LP_CHAD):
        line = f"Net **{detail['delta']:+d} LP** on the week"
        # Per-account breakdown, but only when it adds information: at
        # least two accounts actually moved (zero-delta accounts stay in
        # the stored detail, just not in the post).
        moved = {name: delta for name, delta in detail["per_account"].items() if delta}
        if len(moved) > 1:
            parts = " · ".join(f"{name} {delta:+d}" for name, delta in moved.items())
            line += f" ({parts})"
        return line + "."
    if award == PUSSY:
        week_games = detail["this_week"]
        games_word = "game" if week_games == 1 else "games"
        return (
            f"**{week_games}** {games_word} vs a {detail['baseline']:g}-game/week habit "
            f"(**-{detail['drop_pct']}%**)."
        )
    if award == DUO_LEECH:
        partner_bit = ""
        if "partner" in detail:
            partner_bit = f" ({detail['partner_games']} of them with {detail['partner']})"
        return (
            f"**{detail['duo_games']} of {detail['games']}** games duo'd{partner_bit} — "
            f"duo {detail['duo_wins']}W-{detail['duo_losses']}L "
            f"vs solo {detail['solo_wins']}W-{detail['solo_losses']}L."
        )
    if award == INT:
        result = "win" if detail["win"] else "loss"
        return (
            f"**{detail['kills']}/{detail['deaths']}/{detail['assists']}** "
            f"on {detail['champion']} ({result})."
        )
    raise ValueError(f"unknown award {award!r}")


def build_ceremony_blocks(
    week_start: dt.date,
    results: dict[str, list[Winner]],
    header: str | None = None,
) -> list[str]:
    """Ceremony post as blocks for leaderboard.chunk_blocks.

    One block per award: emoji + name, winner mention(s), the condemning
    number, one roast. Joint winners share a block, one detail line each.
    Every block ends with a newline so chunked messages get a blank line
    between awards.
    """
    if header is None:
        header = f"\U0001f3c6 **Weekly Awards** — week of <t:{week_epoch(week_start)}:d>"
    blocks = [f"{header}\n"]
    for award in AWARD_ORDER:
        meta = AWARDS[award]
        winners = results.get(award) or []
        if not winners:
            blocks.append(f"{meta.emoji} **{meta.title}** — no winner\n{meta.skip_line}\n")
            continue
        mentions = " & ".join(f"<@{winner.user_id}>" for winner in winners)
        if len(winners) == 1:
            body = condemn_line(award, winners[0])
        else:
            body = "\n".join(
                f"{winner.display_name}: {condemn_line(award, winner)}" for winner in winners
            )
        # Severity overrides only make sense for a lone winner — joint
        # winners' details differ, so they get the weekly pool line.
        roast_detail = winners[0].detail if len(winners) == 1 else None
        blocks.append(
            f"{meta.emoji} **{meta.title}** — {mentions}\n"
            f"{body} {roast_line(award, week_start, roast_detail)}\n"
        )
    return blocks


def cabinet_value(award: str, value, detail: dict | None) -> str:
    """Compact value tag for a cabinet last-week line, e.g. '(-187 LP)'.

    ``value`` arrives as Decimal from the numeric column; ``detail`` as a
    dict from jsonb (defensively optional).
    """
    detail = detail or {}
    if award in (LP_LOSS, LP_CHAD):
        return f"{int(value):+d} LP"
    if award == PUSSY:
        drop = detail.get("drop_pct", round(float(value) * 100))
        return f"-{drop}% volume"
    if award == DUO_LEECH:
        if "duo_games" in detail and "games" in detail:
            return f"{detail['duo_games']}/{detail['games']} duo"
        return f"{float(value):.0%} duo"
    if award == INT:
        if {"kills", "deaths", "assists"} <= detail.keys():
            return f"{detail['kills']}/{detail['deaths']}/{detail['assists']}"
        return f"{int(value)} deaths"
    return str(value)


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
# would double-count games. Display name falls back to league_username when
# the users row is missing/blank.
_TRACKED_CTE = """
    tracked AS (
        SELECT DISTINCT ON (lp.puuid)
               lp.puuid,
               lp.discord_user_id,
               lp.league_username,
               COALESCE(
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


def _snapshot_query(where: str, order: str) -> str:
    return (
        "WITH "
        + _TRACKED_CTE
        + """
        SELECT DISTINCT ON (lh.puuid)
               lh.puuid, t.discord_user_id, t.display_name, t.league_username,
               lh.tier, lh.division, lh.lp
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


async def fetch_volume_rows(week_start: dt.datetime, week_end: dt.datetime) -> list[tuple]:
    """(discord_user_id, display_name, prior_4w_games, this_week_games).

    Prior window = the 4 full weeks before the award week. Users with
    prior games but zero this week ARE included (that's the award); users
    who only appeared this week get prior_games=0 and can't qualify.
    """
    prior_start = week_start - dt.timedelta(days=28)
    return await db.fetchall(
        "WITH "
        + _TRACKED_CTE
        + """
        SELECT t.discord_user_id,
               MIN(t.display_name),
               COUNT(*) FILTER (WHERE ms.game_start < %(start)s) AS prior_games,
               COUNT(*) FILTER (WHERE ms.game_start >= %(start)s) AS week_games
        FROM match_stats ms
            JOIN tracked t ON t.puuid = ms.puuid
        WHERE ms.queue_id = %(queue_id)s
          AND ms.game_start >= %(prior_start)s AND ms.game_start < %(end)s
        GROUP BY t.discord_user_id""",
        {
            "queue_id": RANKED_SOLO_QUEUE_ID,
            "prior_start": prior_start,
            "start": week_start,
            "end": week_end,
        },
    )


async def fetch_duo_rows(week_start: dt.datetime, week_end: dt.datetime) -> list[tuple]:
    """(discord_user_id, display_name, games, duo_games, duo_wins, solo_wins).

    Duo = another tracked player has a match_stats row for the same match
    on the same team (match_stats holds only tracked players) — the same
    EXISTS pattern as FetchFromRiot.get_last_five_games. NULL team_id
    (rows predating the column) never counts as duo.
    """
    return await db.fetchall(
        "WITH "
        + _TRACKED_CTE
        + """,
        games AS (
            SELECT ms.puuid, ms.win,
                   EXISTS (
                       SELECT 1 FROM match_stats o
                       WHERE o.match_id = ms.match_id
                         AND o.team_id = ms.team_id
                         AND o.puuid <> ms.puuid
                   ) AS duo
            FROM match_stats ms
            WHERE ms.queue_id = %(queue_id)s
              AND ms.game_start >= %(start)s AND ms.game_start < %(end)s
        )
        SELECT t.discord_user_id,
               MIN(t.display_name),
               COUNT(*) AS games,
               COUNT(*) FILTER (WHERE g.duo) AS duo_games,
               COUNT(*) FILTER (WHERE g.duo AND g.win = 1) AS duo_wins,
               COUNT(*) FILTER (WHERE NOT g.duo AND g.win = 1) AS solo_wins
        FROM games g
            JOIN tracked t ON t.puuid = g.puuid
        GROUP BY t.discord_user_id""",
        {"queue_id": RANKED_SOLO_QUEUE_ID, "start": week_start, "end": week_end},
    )


async def fetch_duo_partner_rows(week_start: dt.datetime, week_end: dt.datetime) -> list[tuple]:
    """(discord_user_id, partner_user_id, partner_name, games_together).

    One row per (user, tracked partner) pair with how many of this week's
    solo-queue games they shared a team in — feeds the "N of them with X"
    naming in the Duo Leech line. Same-user pairs are excluded so a
    player's alt can never be their own "partner" (impossible in one
    match anyway, but cheap to make structural). Partner accounts
    aggregate to the partner's Discord user like everything else.
    """
    return await db.fetchall(
        "WITH "
        + _TRACKED_CTE
        + """
        SELECT t.discord_user_id,
               pt.discord_user_id AS partner_user_id,
               MIN(pt.display_name) AS partner_name,
               COUNT(*) AS games_together
        FROM match_stats ms
            JOIN match_stats o
                ON o.match_id = ms.match_id
               AND o.team_id = ms.team_id
               AND o.puuid <> ms.puuid
            JOIN tracked t ON t.puuid = ms.puuid
            JOIN tracked pt ON pt.puuid = o.puuid
        WHERE ms.queue_id = %(queue_id)s
          AND ms.game_start >= %(start)s AND ms.game_start < %(end)s
          AND t.discord_user_id <> pt.discord_user_id
        GROUP BY t.discord_user_id, pt.discord_user_id""",
        {"queue_id": RANKED_SOLO_QUEUE_ID, "start": week_start, "end": week_end},
    )


async def fetch_int_rows(week_start: dt.datetime, week_end: dt.datetime) -> list[tuple]:
    """(discord_user_id, display_name, kills, deaths, assists, champion,
    win, match_id) — one row per solo game this week with >= 1 death."""
    return await db.fetchall(
        "WITH "
        + _TRACKED_CTE
        + """
        SELECT t.discord_user_id, t.display_name,
               ms.kills, ms.deaths, ms.assists, ms.champion, ms.win, ms.match_id
        FROM match_stats ms
            JOIN tracked t ON t.puuid = ms.puuid
        WHERE ms.queue_id = %(queue_id)s
          AND ms.game_start >= %(start)s AND ms.game_start < %(end)s
          AND ms.deaths > 0
        ORDER BY ms.deaths DESC""",
        {"queue_id": RANKED_SOLO_QUEUE_ID, "start": week_start, "end": week_end},
    )


async def compute_all_awards(
    week_start: dt.datetime, week_end: dt.datetime
) -> dict[str, list[Winner]]:
    """All five awards for [week_start, week_end) — empty list = skipped."""
    baseline, first, last = await fetch_lp_snapshot_rows(week_start, week_end)
    deltas = net_lp_deltas(baseline, first, last)
    return {
        LP_LOSS: pick_lp_extreme(deltas, gain=False),
        LP_CHAD: pick_lp_extreme(deltas, gain=True),
        PUSSY: pick_volume_collapse(await fetch_volume_rows(week_start, week_end)),
        DUO_LEECH: pick_duo_leech(
            await fetch_duo_rows(week_start, week_end),
            await fetch_duo_partner_rows(week_start, week_end),
        ),
        INT: pick_int(await fetch_int_rows(week_start, week_end)),
    }
