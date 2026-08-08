"""Global rate-limited client for Riot's APIs.

Riot's developer-tier limits apply to the *application key* as a whole — not
per endpoint and not per process module. Spreading enforcement across cogs
would silently exceed the limit when, say, the rank refresh loop and a
/kda invocation collide. All Riot API requests in the codebase should go
through the public functions in this module so the budget is shared.

Limits (confirmed 2026-08-05 from the X-App-Rate-Limit response header,
owner-confirmed; registered key, no dev-key expiry):
  - 20 requests per 1 second
  - 100 requests per 2 minutes

On 429 (rate-limit) responses the client honours `Retry-After` (with jitter)
and retries internally up to ``MAX_RETRIES`` times before surfacing the
failure.

Public API:
  - :func:`get_league_entries` — solo/duo/flex ranked entries for a puuid.
  - :func:`get_match_ids` — recent match IDs for a puuid (Match-V5).
  - :func:`get_match` — full match details by match ID (Match-V5).
  - :func:`get_account_by_riot_id` / :func:`get_account_by_puuid` —
    account-v1 lookups (formerly via pantheon, which bypassed this budget).
  - :func:`get_active_game` — spectator-v5 live game for a puuid.
  - :func:`get_champion_mastery` — champion-mastery-v4 list for a puuid.
  - :func:`get_match_timeline` — per-minute Match-V5 timeline by match ID.
  - :func:`get_player_challenges` — challenges-v1 player data for a puuid.
  - :func:`get_summoner_by_puuid` — summoner-v4 basics (level, icon).
  - :func:`get_apex_league` — league-v4 Challenger/GM/Master ladder list.
  - :func:`get_clash_tournaments` / :func:`get_clash_players` /
    :func:`get_clash_team` — clash-v1 schedule + registrations + rosters.
  - :func:`get_challenges_config` / :func:`get_challenges_percentiles` —
    challenges-v1 static catalogue (names, thresholds, tier percentiles).
  - :func:`get_champion_rotation` — champion-v3 free-to-play rotation.
  - :func:`get_platform_status` — lol-status-v4 EUW incidents/maintenances.
  - :func:`set_rate_limits` — replace the process-wide budget (standalone
    runners on their own key; the bot itself never calls this).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from urllib.parse import quote

import aiohttp
from utils import config

# (max_requests, window_seconds)
LIMITS: list[tuple[int, float]] = [
    (20, 1.0),
    (100, 120.0),
]
_LONGEST_WINDOW = max(window for _, window in LIMITS)
MAX_RETRIES = 2


def set_rate_limits(limits: list[tuple[int, float]]) -> None:
    """Replace the process-wide request budget.

    For standalone processes running on their OWN key (e.g.
    scripts/backfill_deep_standalone.py on a spare dev key) whose limits
    differ from the bot's. Mutates LIMITS in place so _wait_for_slot —
    which reads the module global on every call — picks it up immediately.
    The bot process itself must never call this: its budget is the prod
    key's, hardcoded above.
    """
    global _LONGEST_WINDOW
    LIMITS[:] = limits
    _LONGEST_WINDOW = max(window for _, window in LIMITS)


PLATFORM_HOST = "https://euw1.api.riotgames.com"  # league entries
REGION_HOST = "https://europe.api.riotgames.com"  # match-v5

RANKED_SOLO_QUEUE_ID = 420
RANKED_5S_QUEUE_ID = 710  # match-v5 queue for the weekend Ranked 5s queue

# League entries are consumed by two board cogs (solo + ranked 5s) on
# independent 120s loops. A response lists ALL of a player's ranked queues,
# so a short TTL cache lets the second consumer reuse the first's response
# instead of doubling API spend. TTL sits just ABOVE the loops' 120s
# period so whichever board fetches first always serves the other from
# cache — at 115s the loops drifting out of phase made both fetch, up to
# doubling entries spend (~21 extra requests/2min). The second consumer
# reads data at most one cycle old, which a board redraw can tolerate.
# ~20 tracked players → no eviction needed.
_ENTRIES_TTL_SECONDS = 130.0
_entries_cache: dict[str, tuple[float, list[dict]]] = {}

# How stale a cached entry may be served when the caller KNOWS it can't
# have changed (allow_stale=True: player mid-game or idle — LP only
# moves when a game ends). Capped so a bug in the caller's change
# detection can never freeze the board for more than a couple of hours;
# the solo board's hourly full sweep refreshes well before this.
_ENTRIES_STALE_MAX_SECONDS = 2 * 3600.0

log = logging.getLogger(__name__)

_lock = asyncio.Lock()
_timestamps: deque[float] = deque()


async def _wait_for_slot() -> None:
    """Block until both rate-limit windows have headroom for one more request."""
    while True:
        async with _lock:
            now = time.monotonic()
            # Trim history older than the longest window.
            while _timestamps and now - _timestamps[0] > _LONGEST_WINDOW:
                _timestamps.popleft()

            wait = 0.0
            for max_count, window in LIMITS:
                in_window = [t for t in _timestamps if now - t <= window]
                if len(in_window) >= max_count:
                    # Need to wait until the oldest in-window request ages out.
                    needed = (in_window[0] + window) - now + 0.01
                    wait = max(wait, needed)

            if wait <= 0:
                _timestamps.append(now)
                # Budget telemetry: one line as usage crosses each mark,
                # so "are we pushing the rate limit?" is answerable from
                # the logs instead of guessed at.
                used = sum(1 for t in _timestamps if now - t <= 120)
                if used in (70, 85, 95):
                    log.info(f"Riot budget: {used}/100 requests in the current 2min window")
                return

        # Release the lock while sleeping so other coroutines can re-check.
        # Long waits get INFO: sustained budget contention is what froze
        # post_ranks during the position backfill, and DEBUG lines are
        # invisible at the prod log level.
        if wait > 5.0:
            log.info(f"Riot budget contended: waiting {wait:.1f}s for a slot")
        else:
            log.debug(f"Riot rate limit reached, waiting {wait:.2f}s")
        await asyncio.sleep(wait)


async def _get_json(
    url: str, params: dict | None = None, *, quiet_404: bool = False
) -> tuple[int, list | dict | None]:
    """Rate-limited GET returning (status, parsed JSON or None).

    Internal — callers use the endpoint-specific wrappers below so they get
    typed return values instead of a bare ``list | dict``.

    On 429 honours ``Retry-After`` with small jitter and retries internally.
    ``quiet_404``: for endpoints where 404 is an expected answer, not a
    failure (spectator's "not in a game") — skips the error log.
    """
    riot_key = config.riot_api_key()
    if not riot_key:
        log.error("riot_key env var not set")
        return (0, None)
    headers = {"X-Riot-Token": riot_key}

    for attempt in range(MAX_RETRIES + 1):
        await _wait_for_slot()
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, params=params) as r:
                    if r.status == 200:
                        return (r.status, await r.json())
                    if r.status == 404 and quiet_404:
                        return (r.status, None)
                    if r.status == 429:
                        retry_after = int(r.headers.get("Retry-After", 10))
                        # Jitter so concurrent 429s don't retry in lockstep.
                        sleep_for = retry_after + random.uniform(0, 1)
                        log.warning(
                            f"Riot 429 on attempt {attempt + 1}/{MAX_RETRIES + 1}, "
                            f"sleeping {sleep_for:.2f}s"
                        )
                        await asyncio.sleep(sleep_for)
                        continue
                    body = await r.text()
                    log.error(f"Riot {r.status} for {url}: {body[:200]}")
                    return (r.status, None)
        except aiohttp.ClientError as exc:
            log.error(f"Riot request failed for {url}: {exc}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(1.0)
                continue
            return (0, None)

    log.error(f"Riot 429 retries exhausted for {url}")
    return (429, None)


async def get_league_entries(
    puuid: str, *, fresh: bool = False, allow_stale: bool = False
) -> list[dict] | None:
    """Ranked league entries for a player.

    Returns the list of league entries (one per ranked queue type the player
    has participated in), or ``None`` if the request failed. Responses are
    cached for ``_ENTRIES_TTL_SECONDS``; pass ``fresh=True`` to bypass.

    ``allow_stale=True``: serve the cache up to ``_ENTRIES_STALE_MAX_SECONDS``
    old — for callers that know the player's entries can't have changed
    (mid-game or idle since the last fetch). Falls through to a real
    fetch when nothing is cached.
    """
    cached = _entries_cache.get(puuid)
    if not fresh and cached is not None:
        age = time.monotonic() - cached[0]
        max_age = _ENTRIES_STALE_MAX_SECONDS if allow_stale else _ENTRIES_TTL_SECONDS
        if age < max_age:
            # Copy per hit: callers mutate the entry dicts in place
            # (board cogs inject Ranker/user_id keys), and a polluted
            # cache would alias one board's state into the other's.
            return [dict(entry) for entry in cached[1]]
    url = f"{PLATFORM_HOST}/lol/league/v4/entries/by-puuid/{puuid}"
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, list):
        return None
    _entries_cache[puuid] = (time.monotonic(), [dict(entry) for entry in body])
    return body


async def get_match_ids(
    puuid: str,
    count: int = 20,
    queue: int | None = RANKED_SOLO_QUEUE_ID,
    start: int = 0,
) -> list[str] | None:
    """Recent match IDs for a player, newest first.

    ``queue`` defaults to ranked solo/duo (420); pass ``None`` to drop the
    filter and get EVERY queue (ARAM, flex, normals, customs — all of LoL
    match-v5; TFT lives on a different API) in one request — same spend as
    a filtered call, which is why the capture-everything ingest uses it.
    ``start`` is the offset into the player's match history (0 = newest).
    Match-V5 returns up to 100 per page; paginate by incrementing
    ``start``. A short response (< requested count) signals end of history.

    Match-V5 uses the regional host (europe), not the platform host (euw1).
    """
    url = f"{REGION_HOST}/lol/match/v5/matches/by-puuid/{puuid}/ids"
    params: dict = {"count": count, "start": start}
    if queue is not None:
        params["queue"] = queue
    status, body = await _get_json(url, params=params)
    if status != 200 or not isinstance(body, list):
        return None
    return body


async def get_match(match_id: str) -> dict | None:
    """Full match details by match ID. Match-V5."""
    url = f"{REGION_HOST}/lol/match/v5/matches/{match_id}"
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, dict):
        return None
    return body


async def get_match_timeline(match_id: str) -> dict | None:
    """Per-minute Match-V5 timeline by match ID, or None.

    Frames (participant gold/XP/CS snapshots each minute) + the full event
    stream (kills with positions, objectives, item buys, wards). 404 is a
    real answer — timelines age out of Riot's window before match details
    do, and some old matches never had one — so it's handled quietly and
    reads as None just like a transient failure: callers treat None as
    "skip, maybe retry later", which is right for both.

    Regional host (europe), like match details.
    """
    url = f"{REGION_HOST}/lol/match/v5/matches/{match_id}/timeline"
    status, body = await _get_json(url, quiet_404=True)
    if status != 200 or not isinstance(body, dict):
        return None
    return body


async def get_account_by_riot_id(game_name: str, tag_line: str) -> dict | None:
    """Account-v1 lookup by Riot ID (gameName#tagLine). Regional host.

    Returns the account dict ({"puuid", "gameName", "tagLine"}) or None on
    failure / unknown account. Names can contain spaces — path-quoted.
    """
    url = (
        f"{REGION_HOST}/riot/account/v1/accounts/by-riot-id/"
        f"{quote(game_name, safe='')}/{quote(tag_line, safe='')}"
    )
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, dict):
        return None
    return body


async def get_account_by_puuid(puuid: str) -> dict | None:
    """Account-v1 lookup by puuid (current gameName/tagLine). Regional host."""
    url = f"{REGION_HOST}/riot/account/v1/accounts/by-puuid/{puuid}"
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, dict):
        return None
    return body


async def get_active_game(puuid: str) -> tuple[bool, dict | None]:
    """Spectator-v5 live game for a player: ``(known, game)``.

    ``(True, game)``  — in a game right now.
    ``(True, None)``  — definitively NOT in a game (spectator 404, the
                        overwhelmingly common answer, handled quietly).
    ``(False, None)`` — transient failure; the caller must NOT treat it
                        as "game over" or live rows flicker on API blips.

    Platform host (euw1), like league entries.
    """
    url = f"{PLATFORM_HOST}/lol/spectator/v5/active-games/by-summoner/{puuid}"
    status, body = await _get_json(url, quiet_404=True)
    if status == 404:
        return (True, None)
    if status == 200 and isinstance(body, dict):
        return (True, body)
    return (False, None)


async def get_champion_mastery(puuid: str) -> list[dict] | None:
    """Champion-mastery-v4: every champion the player has mastery on.

    Returns the full list (one dict per champion: championId,
    championLevel, championPoints, lastPlayTime epoch-millis, token /
    milestone fields), sorted by Riot highest-points-first, or ``None``
    on failure. One request per player — cheap enough that the mastery
    snapshot loop (cogs/mastery_updater.py) just takes the whole list.

    Platform host (euw1), like league entries.
    """
    url = f"{PLATFORM_HOST}/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}"
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, list):
        return None
    return body


async def get_player_challenges(puuid: str) -> dict | None:
    """Challenges-v1 player data: per-challenge progress + totals.

    Returns the PlayerInfoDto ({"challenges": [...], "preferences": {...},
    "totalPoints": {...}, "categoryPoints": {...}}) or ``None`` on
    failure. One request covers every challenge the player has touched.

    Platform host (euw1).
    """
    url = f"{PLATFORM_HOST}/lol/challenges/v1/player-data/{puuid}"
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, dict):
        return None
    return body


async def get_summoner_by_puuid(puuid: str) -> dict | None:
    """Summoner-v4 basics: summonerLevel, profileIconId, revisionDate.

    Platform host (euw1).
    """
    url = f"{PLATFORM_HOST}/lol/summoner/v4/summoners/by-puuid/{puuid}"
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, dict):
        return None
    return body


_APEX_LEAGUE_PATHS = {
    "challenger": "challengerleagues",
    "grandmaster": "grandmasterleagues",
    "master": "masterleagues",
}


async def get_apex_league(tier: str, queue: str = "RANKED_SOLO_5x5") -> dict | None:
    """League-v4 apex league list for a queue: the whole Challenger /
    Grandmaster / Master ladder in one response.

    ``tier`` is one of "challenger" / "grandmaster" / "master" (KeyError
    on anything else — caller bug, not an API condition). Returns the
    LeagueListDTO ({"entries": [...], "tier", "name", ...}) or ``None``.
    The min leaguePoints across entries is the observable promotion
    cutoff — see cogs/profile_updater.py.

    Platform host (euw1).
    """
    url = f"{PLATFORM_HOST}/lol/league/v4/{_APEX_LEAGUE_PATHS[tier]}/by-queue/{queue}"
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, dict):
        return None
    return body


async def get_clash_tournaments() -> list[dict] | None:
    """Clash-v1 tournament schedule: all upcoming + active tournaments.

    One TournamentDto per tournament ({"id", "themeId", "nameKey",
    "nameKeySecondary", "schedule": [{registrationTime, startTime,
    cancelled}]}) — live-validated 2026-08-08. Past tournaments drop off
    the response; the clash_tournaments table keeps them.

    Platform host (euw1).
    """
    url = f"{PLATFORM_HOST}/lol/clash/v1/tournaments"
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, list):
        return None
    return body


async def get_clash_players(puuid: str) -> list[dict] | None:
    """Clash-v1 active registrations for a player.

    Returns the list of PlayerDto rows — one per active Clash team the
    player is registered on ({"teamId", "position", "role"} per the
    schema), usually ``[]`` (live-validated 2026-08-08: every tracked
    account returned 200 + empty outside a Clash window). ``None`` means
    the request failed, NOT "not registered".

    Platform host (euw1).
    """
    url = f"{PLATFORM_HOST}/lol/clash/v1/players/by-puuid/{puuid}"
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, list):
        return None
    return body


async def get_clash_team(team_id: str) -> dict | None:
    """Clash-v1 team by id: roster, name, tier, captain.

    TeamDto per the schema ({"id", "tournamentId", "name", "iconId",
    "tier", "captain", "abbreviation", "players": [...]}) — could not be
    live-validated (no tracked player was registered during the 2026-08-08
    pass), so the caller archives it verbatim and extracts defensively.
    404 is a real answer (team disbanded between the player fetch and this
    one) — handled quietly.

    Platform host (euw1).
    """
    url = f"{PLATFORM_HOST}/lol/clash/v1/teams/{team_id}"
    status, body = await _get_json(url, quiet_404=True)
    if status != 200 or not isinstance(body, dict):
        return None
    return body


async def get_challenges_config() -> list[dict] | None:
    """Challenges-v1 static catalogue: every challenge's definition.

    One ChallengeConfigInfoDto per challenge ({"id", "localizedNames",
    "state", "thresholds", "leaderboard"} — live 2026-08-08: 405 entries;
    the documented ``tracking``/``startTimestamp`` fields were absent and
    ``endTimestamp`` present on one entry, so all are treated optional).
    This is what makes player_challenges' bare ids interpretable.

    Platform host (euw1). ~1 MB response, so callers fetch it rarely.
    """
    url = f"{PLATFORM_HOST}/lol/challenges/v1/challenges/config"
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, list):
        return None
    return body


async def get_challenges_percentiles() -> dict | None:
    """Challenges-v1 percentile map: challengeId -> {tier: percentile}.

    Keys are challenge ids AS STRINGS (JSON object keys), values map tier
    names (IRON..CHALLENGER) to the population fraction at that tier —
    live-validated 2026-08-08, 405 entries matching the config catalogue.

    Platform host (euw1).
    """
    url = f"{PLATFORM_HOST}/lol/challenges/v1/challenges/percentiles"
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, dict):
        return None
    return body


async def get_champion_rotation() -> dict | None:
    """Champion-v3 free-to-play rotation.

    Riot's documented shape is {"freeChampionIds",
    "freeChampionIdsForNewPlayers", "maxNewPlayerLevel"} but the LIVE
    response (2026-08-08, EUW) is {"sr": [ids], "newplayer": [ids]} —
    consumers must handle both (cogs/game_data_updater.py does, and
    archives the payload verbatim either way).

    Platform host (euw1).
    """
    url = f"{PLATFORM_HOST}/lol/platform/v3/champion-rotations"
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, dict):
        return None
    return body


async def get_platform_status() -> dict | None:
    """Lol-status-v4 platform data for EUW: incidents + maintenances.

    PlatformDataDto ({"id", "name", "locales", "maintenances": [...],
    "incidents": [...]}) — live-validated 2026-08-08 (both lists empty,
    the steady state). Entry fields are snake_case (status-v4 quirk):
    ``maintenance_status``, ``incident_severity``, ``created_at`` etc.
    Riot documents this endpoint as not counting against the app rate
    limit, but it goes through the shared budget anyway — 24 requests a
    day is noise and one code path is one code path.

    Platform host (euw1).
    """
    url = f"{PLATFORM_HOST}/lol/status/v4/platform-data"
    status, body = await _get_json(url)
    if status != 200 or not isinstance(body, dict):
        return None
    return body
