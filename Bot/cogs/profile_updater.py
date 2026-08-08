"""Summoner profile + challenges-v1 snapshots for every tracked player.

Capture-everything companion to cogs/mastery_updater.py, same shape: a
``@tasks.loop(hours=12)`` sweep through the shared riot_client budget.
Per tracked account it costs two requests (summoner-v4 basics +
challenges-v1 player-data), plus two per sweep total for the solo-queue
Challenger/Grandmaster ladder cutoffs — ~45 requests a sweep at the
current roster, twice a day. Noise against the 100/120s budget.

Stored current-state, upserted in place:
  - ``summoner_profile``   — level, profile icon, revisionDate.
  - ``player_challenges``  — one row per (account, challenge): level,
    value, percentile, position, playersInLevel, achievedTime.
  - ``player_challenge_summary`` — totals + categoryPoints/preferences
    (equipped title/banner) as JSONB, plus the complete PlayerInfoDto in
    ``raw`` so nothing the endpoint returns is ever dropped.
  - ``ladder_cutoffs``     — min LP in the Challenger/GM solo ladders
    (the observable promotion cutoff, for "distance to Challenger" gags).

History is deliberately not kept (like champion_mastery): these move
slowly and the fun is the current value. If a time series is ever wanted,
copy the league_history snapshot-on-change pattern.
"""

import datetime as dt
import logging

from discord.ext import commands, tasks
from psycopg.types.json import Jsonb
from utils import db
from utils.loop_restart import restart_loop_later
from utils.riot_client import (
    get_apex_league,
    get_player_challenges,
    get_summoner_by_puuid,
)

log = logging.getLogger(__name__)

SWEEP_HOURS = 12

# Queue whose apex cutoffs are worth tracking — the friend group's ladder.
CUTOFF_QUEUE = "RANKED_SOLO_5x5"

_SUMMONER_UPSERT_SQL = (
    "INSERT INTO summoner_profile "
    "(puuid, summoner_level, profile_icon_id, revision_date, updated_at) "
    "VALUES (%s, %s, %s, %s, now()) "
    "ON CONFLICT (puuid) DO UPDATE SET "
    "  summoner_level = EXCLUDED.summoner_level, "
    "  profile_icon_id = EXCLUDED.profile_icon_id, "
    "  revision_date = EXCLUDED.revision_date, "
    "  updated_at = now()"
)

_CHALLENGE_UPSERT_SQL = (
    "INSERT INTO player_challenges "
    "(puuid, challenge_id, level, value, percentile, position, players_in_level, "
    " achieved_time, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now()) "
    "ON CONFLICT (puuid, challenge_id) DO UPDATE SET "
    "  level = EXCLUDED.level, "
    "  value = EXCLUDED.value, "
    "  percentile = EXCLUDED.percentile, "
    "  position = EXCLUDED.position, "
    "  players_in_level = EXCLUDED.players_in_level, "
    "  achieved_time = EXCLUDED.achieved_time, "
    "  updated_at = now()"
)

_SUMMARY_UPSERT_SQL = (
    "INSERT INTO player_challenge_summary "
    "(puuid, total_level, total_current, total_max, total_percentile, "
    " category_points, preferences, raw, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now()) "
    "ON CONFLICT (puuid) DO UPDATE SET "
    "  total_level = EXCLUDED.total_level, "
    "  total_current = EXCLUDED.total_current, "
    "  total_max = EXCLUDED.total_max, "
    "  total_percentile = EXCLUDED.total_percentile, "
    "  category_points = EXCLUDED.category_points, "
    "  preferences = EXCLUDED.preferences, "
    "  raw = EXCLUDED.raw, "
    "  updated_at = now()"
)

_CUTOFF_UPSERT_SQL = (
    "INSERT INTO ladder_cutoffs (queue, tier, cutoff_lp, players, updated_at) "
    "VALUES (%s, %s, %s, %s, now()) "
    "ON CONFLICT (queue, tier) DO UPDATE SET "
    "  cutoff_lp = EXCLUDED.cutoff_lp, "
    "  players = EXCLUDED.players, "
    "  updated_at = now()"
)


def _epoch_ms_to_ts(value) -> dt.datetime | None:
    """Riot epoch-millis -> tz-aware datetime; None on missing/zero.

    achievedTime is 0 for challenges whose level was never achieved —
    1970 would be a lie, so store NULL.
    """
    if isinstance(value, int) and value > 0:
        return dt.datetime.fromtimestamp(value / 1000.0, tz=dt.UTC)
    return None


class ProfileUpdater(commands.Cog):
    """Twice-daily summoner + challenges snapshot for tracked accounts."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        self.profile_sweep_last_fired: dt.datetime | None = None
        self.profile_sweep.start()

    def cog_unload(self) -> None:
        # discord.py does NOT cancel @tasks.loop tasks on cog unload.
        self.profile_sweep.cancel()

    @tasks.loop(hours=SWEEP_HOURS)
    async def profile_sweep(self) -> None:
        rows = await db.fetchall(
            "SELECT DISTINCT puuid FROM league_players WHERE puuid IS NOT NULL AND puuid != ''"
        )
        summoners_ok = 0
        challenges_ok = 0
        for (puuid,) in rows:
            if await self._sweep_summoner(puuid):
                summoners_ok += 1
            if await self._sweep_challenges(puuid):
                challenges_ok += 1

        await self._sweep_cutoffs()

        self.profile_sweep_last_fired = dt.datetime.now()
        self.bot.logging.info(
            f"Profile sweep: {summoners_ok}/{len(rows)} summoner profiles, "
            f"{challenges_ok}/{len(rows)} challenge snapshots"
        )

    async def _sweep_summoner(self, puuid: str) -> bool:
        summoner = await get_summoner_by_puuid(puuid)
        if summoner is None:
            self.bot.logging.warning(f"Summoner fetch failed for {puuid[:8]}...")
            return False
        await db.execute(
            _SUMMONER_UPSERT_SQL,
            (
                puuid,
                summoner.get("summonerLevel"),
                summoner.get("profileIconId"),
                _epoch_ms_to_ts(summoner.get("revisionDate")),
            ),
        )
        return True

    async def _sweep_challenges(self, puuid: str) -> bool:
        data = await get_player_challenges(puuid)
        if data is None:
            self.bot.logging.warning(f"Challenges fetch failed for {puuid[:8]}...")
            return False

        params = []
        for challenge in data.get("challenges") or []:
            challenge_id = challenge.get("challengeId")
            if challenge_id is None:
                continue
            params.append(
                (
                    puuid,
                    int(challenge_id),
                    challenge.get("level"),
                    challenge.get("value"),
                    challenge.get("percentile"),
                    challenge.get("position"),
                    challenge.get("playersInLevel"),
                    _epoch_ms_to_ts(challenge.get("achievedTime")),
                )
            )
        if params:
            await db.executemany(_CHALLENGE_UPSERT_SQL, params)

        totals = data.get("totalPoints") or {}
        await db.execute(
            _SUMMARY_UPSERT_SQL,
            (
                puuid,
                totals.get("level"),
                totals.get("current"),
                totals.get("max"),
                totals.get("percentile"),
                Jsonb(data.get("categoryPoints") or {}),
                Jsonb(data.get("preferences") or {}),
                Jsonb(data),
            ),
        )
        return True

    async def _sweep_cutoffs(self) -> None:
        """Snapshot the solo-queue Challenger/GM promotion cutoffs.

        The observable cutoff is the min LP currently seated in the apex
        league — close enough for a "distance to Challenger" gag stat,
        which is all this feeds.
        """
        for tier in ("challenger", "grandmaster"):
            league = await get_apex_league(tier, CUTOFF_QUEUE)
            if league is None:
                self.bot.logging.warning(f"Apex league fetch failed for {tier}")
                continue
            entries = league.get("entries") or []
            lps = [e.get("leaguePoints") for e in entries if isinstance(e.get("leaguePoints"), int)]
            await db.execute(
                _CUTOFF_UPSERT_SQL,
                (CUTOFF_QUEUE, tier.upper(), min(lps) if lps else None, len(entries)),
            )

    @profile_sweep.before_loop
    async def before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    @profile_sweep.error
    async def profile_sweep_error(self, exc: BaseException) -> None:
        """Auto-restart the sweep on unhandled error (default @tasks.loop
        behaviour is log + stop). Detached restart — see utils/loop_restart."""
        self.bot.logging.error(f"profile_sweep errored: {exc!r}, restarting in 60s")
        restart_loop_later(
            self.profile_sweep,
            name="profile_sweep",
            log=self.bot.logging,
            still_active=lambda: self.bot.get_cog("ProfileUpdater") is self,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ProfileUpdater(bot))
