"""Champion mastery snapshots (champion-mastery-v4) for every tracked player.

A ``@tasks.loop(hours=12)`` pulls each tracked account's full mastery list
through the shared riot_client budget (~1 request per account, so a whole
sweep costs less than one board cycle) and upserts it into the
``champion_mastery`` table — current-state, one row per (puuid, champion).
Points only ever grow, so an upsert plus ``updated_at`` is enough; no
history table (LP-style time series can be derived later from match_stats
games if anyone ever wants "points gained this week").

Champion names: mastery responses carry only ``championId``. The loop
resolves ids to names via Data Dragon (Riot's static CDN — no API key, no
rate budget) once per sweep, best-effort: if the lookup fails the rows are
written with NULL names and the upsert's COALESCE keeps any name learned
on a previous sweep.

The first iteration runs on cog load, so a fresh deploy populates the
table immediately instead of waiting half a day.
"""

import datetime as dt
import logging

import aiohttp
from discord.ext import commands, tasks
from psycopg.types.json import Jsonb
from utils import db
from utils.loop_restart import restart_loop_later
from utils.riot_client import get_champion_mastery

log = logging.getLogger(__name__)

SWEEP_HOURS = 12

# Data Dragon: version list, then the champion catalogue for that version.
# Static CDN endpoints — deliberately NOT routed through riot_client (no
# key, no rate limit, different host).
_DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
_DDRAGON_CHAMPIONS_URL = (
    "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
)

_UPSERT_SQL = (
    "INSERT INTO champion_mastery "
    "(puuid, champion_id, champion_name, level, points, "
    " points_since_last_level, points_until_next_level, tokens_earned, "
    " milestone, last_play_time, raw, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
    "ON CONFLICT (puuid, champion_id) DO UPDATE SET "
    "  champion_name = COALESCE(EXCLUDED.champion_name, champion_mastery.champion_name), "
    "  level = EXCLUDED.level, "
    "  points = EXCLUDED.points, "
    "  points_since_last_level = EXCLUDED.points_since_last_level, "
    "  points_until_next_level = EXCLUDED.points_until_next_level, "
    "  tokens_earned = EXCLUDED.tokens_earned, "
    "  milestone = EXCLUDED.milestone, "
    "  last_play_time = EXCLUDED.last_play_time, "
    "  raw = EXCLUDED.raw, "
    "  updated_at = now()"
)


async def _fetch_champion_names() -> dict[int, str]:
    """championId -> display name via Data Dragon; {} on any failure.

    Uses ddragon's ``name`` ("Kai'Sa", "Wukong") rather than the ``id``
    slug ("Kaisa", "MonkeyKing") so stored names read like the client.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(_DDRAGON_VERSIONS_URL, timeout=aiohttp.ClientTimeout(30)) as r:
                if r.status != 200:
                    log.warning(f"ddragon versions fetch failed: HTTP {r.status}")
                    return {}
                versions = await r.json()
            latest = versions[0]
            url = _DDRAGON_CHAMPIONS_URL.format(version=latest)
            async with session.get(url, timeout=aiohttp.ClientTimeout(30)) as r:
                if r.status != 200:
                    log.warning(f"ddragon champions fetch failed: HTTP {r.status}")
                    return {}
                catalogue = await r.json()
        return {
            int(champ["key"]): champ["name"]
            for champ in catalogue.get("data", {}).values()
            if str(champ.get("key", "")).isdigit()
        }
    except Exception as exc:
        log.warning(f"ddragon champion-name lookup failed: {exc!r}")
        return {}


class MasteryUpdater(commands.Cog):
    """Twice-daily champion-mastery snapshot for every tracked account."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        self.mastery_sweep_last_fired: dt.datetime | None = None
        self.mastery_sweep.start()

    def cog_unload(self) -> None:
        # discord.py does NOT cancel @tasks.loop tasks on cog unload —
        # without this, every hot reload leaves the old instance's loop
        # running next to the new one's.
        self.mastery_sweep.cancel()

    @tasks.loop(hours=SWEEP_HOURS)
    async def mastery_sweep(self) -> None:
        rows = await db.fetchall(
            "SELECT DISTINCT puuid FROM league_players WHERE puuid IS NOT NULL AND puuid != ''"
        )
        if not rows:
            return
        names = await _fetch_champion_names()

        accounts_ok = 0
        champs_written = 0
        for (puuid,) in rows:
            masteries = await get_champion_mastery(puuid)
            if masteries is None:
                self.bot.logging.warning(f"Mastery fetch failed for {puuid[:8]}...")
                continue
            params = []
            for entry in masteries:
                champion_id = entry.get("championId")
                if champion_id is None:
                    continue
                last_play = entry.get("lastPlayTime")
                last_play_ts = (
                    dt.datetime.fromtimestamp(last_play / 1000.0, tz=dt.UTC)
                    if isinstance(last_play, int)
                    else None
                )
                params.append(
                    (
                        puuid,
                        int(champion_id),
                        names.get(int(champion_id)),
                        entry.get("championLevel", 0),
                        entry.get("championPoints", 0),
                        entry.get("championPointsSinceLastLevel"),
                        entry.get("championPointsUntilNextLevel"),
                        entry.get("tokensEarned"),
                        entry.get("championSeasonMilestone"),
                        last_play_ts,
                        Jsonb(entry),
                    )
                )
            if params:
                await db.executemany(_UPSERT_SQL, params)
                accounts_ok += 1
                champs_written += len(params)

        self.mastery_sweep_last_fired = dt.datetime.now()
        self.bot.logging.info(
            f"Mastery sweep: {accounts_ok}/{len(rows)} accounts, "
            f"{champs_written} champion rows upserted"
        )

    @mastery_sweep.before_loop
    async def before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    @mastery_sweep.error
    async def mastery_sweep_error(self, exc: BaseException) -> None:
        """Auto-restart the sweep on unhandled error.

        Default @tasks.loop behaviour on exception is log + stop, which
        would silently end mastery snapshots until the next full deploy.
        Restart must run detached — this callback runs inside the dying
        loop task (see utils/loop_restart.py).
        """
        self.bot.logging.error(f"mastery_sweep errored: {exc!r}, restarting in 60s")
        restart_loop_later(
            self.mastery_sweep,
            name="mastery_sweep",
            log=self.bot.logging,
            still_active=lambda: self.bot.get_cog("MasteryUpdater") is self,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MasteryUpdater(bot))
