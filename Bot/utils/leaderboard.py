"""Shared core for the ranked leaderboard cogs.

Both boards — solo/duo (cogs/league_table_updater.py, class FetchFromRiot)
and the weekend Ranked 5s ladder (cogs/ranked5s_table_updater.py, class
Ranked5sBoard) — post the same entry format and record the same
league_history snapshots. They differ only in which ``league_history.queue``
tag they read/write plus thin cog-specific behaviour (streak ping,
min-games filter, window gating, channel discovery) that stays in the cogs.
Everything queue-agnostic lives here: data in, strings/rows out.

league_history key note: solo-queue rows written pre-2024 are keyed by the
legacy encrypted summoner ID (league_players.leagueId) instead of the real
puuid, so solo reads must union both keys (``legacy_dual_key=True``).
Ranked 5s rows are always puuid-keyed.
"""

import discord
from utils import db

APEX_TIERS = ("Master", "Grandmaster", "Challenger")

WIN_SQUARE = "\U0001f7e9"  # green square
LOSS_SQUARE = "\U0001f7e5"  # red square
DUO_WIN_SQUARE = "❎"  # green box with an X — duo win
DUO_LOSS_SQUARE = "❌"  # red X — duo loss


# ------------------------------------------------------------------ history


async def fetch_history_wl(
    puuid: str, queue: str, limit: int, *, legacy_dual_key: bool = False
) -> list[tuple]:
    """Newest-first cumulative (wins, losses) rows for one player + queue.

    ``legacy_dual_key`` additionally matches rows keyed by the legacy
    encrypted summoner ID (league_players.leagueId) — required for solo
    reads so legacy-tracked players still surface their history; pointless
    for Ranked 5s. Filtering on ``queue`` matters either way: both boards
    share the table, and cross-queue rows would corrupt the win/loss diffs.
    """
    if legacy_dual_key:
        return await db.fetchall(
            "SELECT wins, losses FROM league_history "
            "WHERE puuid IN ("
            "    SELECT leagueId FROM league_players WHERE puuid = %s"
            "    UNION"
            "    SELECT puuid    FROM league_players WHERE puuid = %s"
            ") "
            "AND queue = %s "
            "AND wins IS NOT NULL AND losses IS NOT NULL "
            "ORDER BY id DESC LIMIT %s",
            (puuid, puuid, queue, limit),
        )
    return await db.fetchall(
        "SELECT wins, losses FROM league_history "
        "WHERE puuid = %s AND queue = %s "
        "AND wins IS NOT NULL AND losses IS NOT NULL "
        "ORDER BY id DESC LIMIT %s",
        (puuid, queue, limit),
    )


async def latest_history_wl(puuid: str, queue: str) -> list[tuple]:
    """``[(wins, losses)]`` of the newest snapshot, ``[]`` if none exists.

    Unlike :func:`fetch_history_wl` this does NOT filter NULL wins/losses —
    the snapshot writer must see a NULL-w/l row as "latest" rather than skip
    past it. Explicit columns (not SELECT *) so the ``queue`` column can't
    shift the wins/losses positions; queue-scoped so a Ranked 5s snapshot
    never masks a pending solo insert (and vice versa).
    """
    return await db.fetchall(
        "SELECT wins, losses FROM league_history "
        "WHERE puuid = %s AND queue = %s "
        "ORDER BY id DESC LIMIT 1",
        (puuid, queue),
    )


async def insert_history_snapshot(entry: dict, queue: str) -> None:
    """INSERT one league_history snapshot row tagged with ``queue``.

    leaguePoints arrives as int from Riot; keep it int (psycopg will not
    cast str -> INTEGER the way sqlite silently did).
    """
    await db.execute(
        "INSERT INTO league_history (puuid, lp, division, tier, wins, losses, queue) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            entry["puuid"],
            int(entry["leaguePoints"]),
            entry["rank"],
            entry["tier"],
            int(entry["wins"]),
            int(entry["losses"]),
            queue,
        ),
    )


async def record_history_snapshot(entry: dict, queue: str) -> bool:
    """SELECT latest -> skip if unchanged -> INSERT. True if a row was written."""
    last = await latest_history_wl(entry["puuid"], queue)
    if last and last[0] == (entry["wins"], entry["losses"]):
        return False
    await insert_history_snapshot(entry, queue)
    return True


# -------------------------------------------------------- outcome sequences


def _delta_outcomes(rows: list[tuple], win_token: str, loss_token: str) -> list[str]:
    """Per-game outcome tokens from newest-first cumulative (w, l) rows.

    Within one multi-game diff the true order is unknown; wins are placed
    BEFORE losses. The max(..., 0) clamps guard against season resets
    driving a diff negative.
    """
    sequence: list[str] = []
    for i in range(len(rows) - 1):
        newer_w, newer_l = rows[i]
        older_w, older_l = rows[i + 1]
        delta_w = max(newer_w - older_w, 0)
        delta_l = max(newer_l - older_l, 0)
        sequence.extend([win_token] * delta_w + [loss_token] * delta_l)
    return sequence


def build_last_five(rows: list[tuple]) -> str:
    """Green/red squares for the last <=5 games, newest on the right.

    ``rows`` are newest-first cumulative (wins, losses) snapshots
    (see :func:`fetch_history_wl` with limit 6).
    """
    if not rows:
        return ""
    sequence = _delta_outcomes(rows, WIN_SQUARE, LOSS_SQUARE)
    # Implicit (0,0) baseline for brand-new accounts: only valid when the
    # oldest fetched row's cumulative game count is small.
    oldest_w, oldest_l = rows[-1]
    if oldest_w + oldest_l <= 5:
        sequence.extend([WIN_SQUARE] * oldest_w + [LOSS_SQUARE] * oldest_l)
    # Cap to last 5; reverse so the newest game is on the right.
    return "".join(reversed(sequence[:5]))


def build_last_five_from_wins(wins_newest_first: list) -> str:
    """Green/red squares straight from per-match win flags, newest right.

    Used by the match-derived Ranked 5s fallback ladder, where exact
    per-game outcomes are known from match_stats.win — no cumulative-diff
    reconstruction (and none of its ordering caveats) needed.
    """
    squares = [WIN_SQUARE if win else LOSS_SQUARE for win in wins_newest_first[:5]]
    return "".join(reversed(squares))


def build_last_five_with_duo(games_newest_first: list[tuple]) -> str:
    """Squares for up to 5 games with duo marking, newest on the right.

    ``games_newest_first`` are (win, duo) pairs straight from match_stats:
    win 1/0, duo True when another tracked player shared the player's team
    in that game. Duo games swap the plain square for the X'd variant:
    solo win 🟩 / duo win ❎ / solo loss 🟥 / duo loss ❌.
    """
    squares = []
    for win, duo in games_newest_first[:5]:
        if win:
            squares.append(DUO_WIN_SQUARE if duo else WIN_SQUARE)
        else:
            squares.append(DUO_LOSS_SQUARE if duo else LOSS_SQUARE)
    return "".join(reversed(squares))


def count_leading_losses(rows: list[tuple]) -> int:
    """Consecutive losses ending at the most recent game; 0 on a fresh win.

    Because :func:`_delta_outcomes` places wins before losses inside one
    diff, a mixed diff at the head reports 0 — false-negatives over
    false-positives (this feeds the solo board's loss-streak ping).
    """
    if len(rows) < 2:
        return 0
    streak = 0
    for outcome in _delta_outcomes(rows, "W", "L"):
        if outcome == "L":
            streak += 1
        else:
            break
    return streak


# ------------------------------------------------------------------- render


def render_board_entry(
    posting: dict,
    position: int,
    previous_position: int | None,
    updated: bool,
    last_five: str,
    *,
    apex_omits_games_word: bool = False,
) -> str:
    """One board entry: name/arrow/flag line, rank line, played line, last-5.

    ``apex_omits_games_word`` reproduces the solo board's long-standing
    quirk where apex entries read "Played: N with a ..." (no "games") while
    every other entry on either board reads "N games with a".
    """
    # \U00002B06\U0000FE0F = up arrow, \U00002B07\U0000FE0F = down arrow
    if previous_position is None or previous_position == position:
        position_arrow = ""
    elif position < previous_position:
        position_arrow = "\U00002b06\U0000fe0f "
    else:
        position_arrow = "\U00002b07\U0000fe0f "
    updated_flag = " \U0001f6a9" if updated else ""  # triangular flag
    last_five_line = f"Last 5: {last_five}\n" if last_five else ""

    tier = posting["tier"].title()
    is_apex = tier in APEX_TIERS
    # Apex tiers have no real division (league-v4 reports rank "I").
    if is_apex:
        rank_line = f"Rank: {tier} {posting['leaguePoints']}lp"
    else:
        rank_line = f"Rank: {tier} {posting['rank']} {posting['leaguePoints']}lp"
    games_word = "" if is_apex and apex_omits_games_word else " games"
    return (
        f"{position_arrow}{position}. {posting['summonerName']} - <@{posting['user_id']}>"
        f"{updated_flag}\n"
        f"{rank_line}\n"
        f"Played: {posting['GamesPlayed']}{games_word} with a {posting['WinRate']:.2f}% winrate\n"
        f"{last_five_line}"
    )


async def render_board_entries(
    sorted_results: list[dict],
    previous_positions: dict[str, int],
    last_updated_by: list[str],
    fetch_last_five,
    *,
    apex_omits_games_word: bool = False,
) -> tuple[list[str], dict[str, int]]:
    """Render every entry of an already-sorted board.

    Returns ``(entry_strings, current_positions)`` — the caller stores
    ``current_positions`` for the next cycle's arrow comparisons.
    ``fetch_last_five`` is an async ``puuid -> squares-string`` callable so
    each cog keeps its own history scoping (queue tag / legacy dual key).
    """
    output_list: list[str] = []
    current_positions: dict[str, int] = {}
    for index, posting in enumerate(sorted_results):
        position = index + 1
        name = posting["summonerName"]
        current_positions[name] = position
        output_list.append(
            render_board_entry(
                posting,
                position,
                previous_positions.get(name),
                name in last_updated_by,
                await fetch_last_five(posting["puuid"]),
                apex_omits_games_word=apex_omits_games_word,
            )
        )
    return output_list, current_positions


# ------------------------------------------------------------------ posting


async def wipe_and_post(channel, content: str, log) -> None:
    """Delete the channel's message history, then silently post ``content``.

    Boards are ping-free: silent send + no mentions resolved. Empty
    ``content`` still wipes but posts nothing (solo board with an empty
    entry list).
    """
    try:
        async for message in channel.history():
            await message.delete()
    except discord.errors.Forbidden:
        log.warning("Missing permissions to delete messages, skipping cleanup")
    if content:
        await channel.send(
            content,
            silent=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
