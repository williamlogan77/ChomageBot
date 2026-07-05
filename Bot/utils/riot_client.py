"""Global rate-limited client for Riot's APIs.

Riot's developer-tier limits apply to the *application key* as a whole — not
per endpoint and not per process module. Spreading enforcement across cogs
would silently exceed the limit when, say, the rank refresh loop and a
/kda invocation collide. All Riot API requests in the codebase should go
through the public functions in this module so the budget is shared.

Limits (developer tier):
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

PLATFORM_HOST = "https://euw1.api.riotgames.com"  # league entries
REGION_HOST = "https://europe.api.riotgames.com"  # match-v5

RANKED_SOLO_QUEUE_ID = 420
RANKED_5S_QUEUE_ID = 710  # match-v5 queue for the weekend Ranked 5s queue

# League entries are consumed by two board cogs (solo + ranked 5s) on
# independent 120s loops. A response lists ALL of a player's ranked queues,
# so a short TTL cache lets the second consumer reuse the first's response
# instead of doubling API spend. TTL sits just below the loops' 120s period
# so each cycle still gets fresh data while tolerating the loops drifting
# out of phase. ~20 tracked players → no eviction needed.
_ENTRIES_TTL_SECONDS = 115.0
_entries_cache: dict[str, tuple[float, list[dict]]] = {}

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
                return

        # Release the lock while sleeping so other coroutines can re-check.
        log.debug(f"Riot rate limit reached, waiting {wait:.2f}s")
        await asyncio.sleep(wait)


async def _get_json(url: str, params: dict | None = None) -> tuple[int, list | dict | None]:
    """Rate-limited GET returning (status, parsed JSON or None).

    Internal — callers use the endpoint-specific wrappers below so they get
    typed return values instead of a bare ``list | dict``.

    On 429 honours ``Retry-After`` with small jitter and retries internally.
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

    return (429, None)


async def get_league_entries(puuid: str, *, fresh: bool = False) -> list[dict] | None:
    """Ranked league entries for a player.

    Returns the list of league entries (one per ranked queue type the player
    has participated in), or ``None`` if the request failed. Responses are
    cached for ``_ENTRIES_TTL_SECONDS``; pass ``fresh=True`` to bypass.
    """
    cached = _entries_cache.get(puuid)
    if not fresh and cached is not None:
        age = time.monotonic() - cached[0]
        if age < _ENTRIES_TTL_SECONDS:
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
    queue: int = RANKED_SOLO_QUEUE_ID,
    start: int = 0,
) -> list[str] | None:
    """Recent match IDs for a player, newest first.

    ``queue`` defaults to ranked solo/duo (420). ``start`` is the offset into
    the player's match history (0 = newest). Match-V5 returns up to 100 per
    page; paginate by incrementing ``start``. A short response (< requested
    count) signals end of history.

    Match-V5 uses the regional host (europe), not the platform host (euw1).
    """
    url = f"{REGION_HOST}/lol/match/v5/matches/by-puuid/{puuid}/ids"
    status, body = await _get_json(url, params={"queue": queue, "count": count, "start": start})
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
