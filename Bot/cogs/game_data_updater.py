"""Game-wide static data snapshots: challenges catalogue + free rotation.

Capture-remainder companion to cogs/mastery_updater.py, same shape: a
``@tasks.loop(hours=24)`` sweep through the shared riot_client budget.
Exactly 3 requests a DAY regardless of roster size (challenges config +
challenges percentiles + champion rotation) — the cheapest sweep in the
bot.

Stored:
  - ``challenge_config``    — one row per challenge: en_GB name and
    descriptions, state, thresholds, tier percentiles, raw DTO. This is
    the decoder ring that makes player_challenges' bare challenge ids
    interpretable offline (no CommunityDragon dependency at read time).
    Daily refresh is generous — the catalogue moves once a patch — but
    a single ~1 MB request is cheaper than being clever about patch
    detection.
  - ``champion_rotations``  — one row per OBSERVED rotation. The daily
    fetch dedupes against the latest stored row (same rotation just
    bumps last_seen_at), so the table grows ~1 row a week and
    first_seen_at/last_seen_at bound each rotation's window. Live shape
    (2026-08-08) is {"sr": [...], "newplayer": [...]}; the documented
    freeChampionIds shape is handled too, and raw archives whichever
    arrived.

The first iteration runs on cog load, so a fresh deploy populates both
tables immediately.
"""

import datetime as dt
import logging

from discord.ext import commands, tasks
from psycopg.types.json import Jsonb
from utils import db
from utils.loop_restart import restart_loop_later
from utils.riot_client import (
    get_challenges_config,
    get_challenges_percentiles,
    get_champion_rotation,
)

log = logging.getLogger(__name__)

SWEEP_HOURS = 24

# EUW-appropriate locale for the extracted name/description columns; every
# locale still lands in raw.
_PREFERRED_LOCALES = ("en_GB", "en_US")

_CHALLENGE_CONFIG_UPSERT_SQL = (
    "INSERT INTO challenge_config "
    "(challenge_id, name, short_description, description, state, tracking, "
    " start_timestamp, end_timestamp, leaderboard, thresholds, percentiles, "
    " raw, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
    "ON CONFLICT (challenge_id) DO UPDATE SET "
    "  name = EXCLUDED.name, "
    "  short_description = EXCLUDED.short_description, "
    "  description = EXCLUDED.description, "
    "  state = EXCLUDED.state, "
    "  tracking = EXCLUDED.tracking, "
    "  start_timestamp = EXCLUDED.start_timestamp, "
    "  end_timestamp = EXCLUDED.end_timestamp, "
    "  leaderboard = EXCLUDED.leaderboard, "
    "  thresholds = EXCLUDED.thresholds, "
    "  percentiles = COALESCE(EXCLUDED.percentiles, challenge_config.percentiles), "
    "  raw = EXCLUDED.raw, "
    "  updated_at = now()"
)


def _epoch_ms_to_ts(value) -> dt.datetime | None:
    """Riot epoch-millis -> tz-aware datetime; None on missing/zero."""
    if isinstance(value, int) and value > 0:
        return dt.datetime.fromtimestamp(value / 1000.0, tz=dt.UTC)
    return None


def _localized(entry: dict, field: str) -> str | None:
    """Pull ``field`` from the first preferred locale that has it non-empty."""
    names = entry.get("localizedNames") or {}
    for locale in _PREFERRED_LOCALES:
        value = (names.get(locale) or {}).get(field)
        if value:
            return value
    return None


def _rotation_ids(rotation: dict, live_key: str, documented_key: str) -> list[int] | None:
    """Champion-id list from whichever response shape arrived.

    Sorted so equality against the stored latest row is order-insensitive
    (Riot's ordering is not contractual). None when neither key exists —
    distinct from [] (an explicit empty rotation).
    """
    for key in (live_key, documented_key):
        value = rotation.get(key)
        if isinstance(value, list):
            return sorted(v for v in value if isinstance(v, int))
    return None


class GameDataUpdater(commands.Cog):
    """Daily challenges-catalogue + free-rotation snapshot."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        self.game_data_sweep_last_fired: dt.datetime | None = None
        self.game_data_sweep.start()

    def cog_unload(self) -> None:
        # discord.py does NOT cancel @tasks.loop tasks on cog unload.
        self.game_data_sweep.cancel()

    @tasks.loop(hours=SWEEP_HOURS)
    async def game_data_sweep(self) -> None:
        challenges = await self._sweep_challenge_config()
        rotation = await self._sweep_rotation()
        self.game_data_sweep_last_fired = dt.datetime.now()
        self.bot.logging.info(
            f"Game-data sweep: {challenges} challenge configs upserted, rotation {rotation}"
        )

    async def _sweep_challenge_config(self) -> int:
        config = await get_challenges_config()
        if config is None:
            self.bot.logging.warning("Challenges config fetch failed")
            return 0
        # Percentiles are a separate endpoint; a failure there degrades to
        # NULL percentiles (the upsert COALESCE keeps any earlier values)
        # rather than skipping the whole catalogue refresh.
        percentiles = await get_challenges_percentiles()
        if percentiles is None:
            self.bot.logging.warning("Challenges percentiles fetch failed — keeping stored values")
            percentiles = {}

        params = []
        for entry in config:
            challenge_id = entry.get("id")
            if challenge_id is None:
                continue
            pct = percentiles.get(str(challenge_id))
            params.append(
                (
                    int(challenge_id),
                    _localized(entry, "name"),
                    _localized(entry, "shortDescription"),
                    _localized(entry, "description"),
                    entry.get("state"),
                    entry.get("tracking"),
                    _epoch_ms_to_ts(entry.get("startTimestamp")),
                    _epoch_ms_to_ts(entry.get("endTimestamp")),
                    entry.get("leaderboard"),
                    Jsonb(entry.get("thresholds") or {}),
                    Jsonb(pct) if pct is not None else None,
                    Jsonb(entry),
                )
            )
        if params:
            await db.executemany(_CHALLENGE_CONFIG_UPSERT_SQL, params)
        return len(params)

    async def _sweep_rotation(self) -> str:
        """Fetch the rotation; insert a row only when it changed.

        Returns a short outcome word for the sweep log line.
        """
        rotation = await get_champion_rotation()
        if rotation is None:
            self.bot.logging.warning("Champion rotation fetch failed")
            return "fetch failed"
        free_ids = _rotation_ids(rotation, "sr", "freeChampionIds")
        new_player_ids = _rotation_ids(rotation, "newplayer", "freeChampionIdsForNewPlayers")
        if free_ids is None and new_player_ids is None:
            # Unrecognised shape: archive it anyway (raw is the point of
            # this table) so the data is kept while the extractor catches up.
            self.bot.logging.warning(
                f"Champion rotation shape unrecognised (keys: {sorted(rotation.keys())}) "
                "— archiving raw with NULL id columns"
            )

        latest = await db.fetchone(
            "SELECT id, free_champion_ids, new_player_ids FROM champion_rotations "
            "ORDER BY id DESC LIMIT 1"
        )
        if latest is not None and latest[1] == free_ids and latest[2] == new_player_ids:
            await db.execute(
                "UPDATE champion_rotations SET last_seen_at = now() WHERE id = %s",
                (latest[0],),
            )
            return "unchanged"
        await db.execute(
            "INSERT INTO champion_rotations "
            "(free_champion_ids, new_player_ids, max_new_player_level, raw) "
            "VALUES (%s, %s, %s, %s)",
            (
                Jsonb(free_ids) if free_ids is not None else None,
                Jsonb(new_player_ids) if new_player_ids is not None else None,
                rotation.get("maxNewPlayerLevel"),
                Jsonb(rotation),
            ),
        )
        return "new rotation stored"

    @game_data_sweep.before_loop
    async def before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    @game_data_sweep.error
    async def game_data_sweep_error(self, exc: BaseException) -> None:
        """Auto-restart the sweep on unhandled error (default @tasks.loop
        behaviour is log + stop). Detached restart — see utils/loop_restart."""
        self.bot.logging.error(f"game_data_sweep errored: {exc!r}, restarting in 60s")
        restart_loop_later(
            self.game_data_sweep,
            name="game_data_sweep",
            log=self.bot.logging,
            still_active=lambda: self.bot.get_cog("GameDataUpdater") is self,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(GameDataUpdater(bot))
