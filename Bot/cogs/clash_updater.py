"""Clash (clash-v1) snapshots: tournament schedule + tracked registrations.

Capture-remainder companion to cogs/mastery_updater.py, same shape: a
``@tasks.loop(hours=24)`` sweep through the shared riot_client budget.
Per sweep it costs 1 request for the tournament list plus 1 per tracked
account for registrations, plus 1 per distinct team discovered — teams
only exist during a Clash registration window, so that last term is
almost always 0. ~22 requests a DAY at the current roster; noise.

Stored:
  - ``clash_tournaments``   — one row per tournament, upserted. Riot's
    response only lists upcoming/active tournaments; finished ones drop
    off, our rows persist, so history accumulates here for free.
  - ``clash_registrations`` — one row per (account, team). Team ids are
    per-tournament, so rows never need deleting: the accumulated set is
    the player's Clash career as observed. Empty response = not
    registered = nothing written (the overwhelmingly common case).
  - ``clash_teams``         — roster/name/tier for each team a tracked
    player was seen on. Couldn't be live-validated (nobody registered
    during the 2026-08-08 pass) so extraction is defensive and ``raw``
    is the authority.

Daily cadence is deliberate: tournaments change rarely and registrations
only matter on Clash weekends. A same-day roster shuffle can be missed —
acceptable; the match itself still lands via the all-queue match ingest.

The first iteration runs on cog load, so a fresh deploy populates the
tournament schedule immediately.
"""

import datetime as dt
import logging

from discord.ext import commands, tasks
from psycopg.types.json import Jsonb
from utils import db
from utils.loop_restart import restart_loop_later
from utils.riot_client import get_clash_players, get_clash_team, get_clash_tournaments

log = logging.getLogger(__name__)

SWEEP_HOURS = 24

_TOURNAMENT_UPSERT_SQL = (
    "INSERT INTO clash_tournaments "
    "(tournament_id, theme_id, name_key, name_key_secondary, schedule, raw, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, now()) "
    "ON CONFLICT (tournament_id) DO UPDATE SET "
    "  theme_id = EXCLUDED.theme_id, "
    "  name_key = EXCLUDED.name_key, "
    "  name_key_secondary = EXCLUDED.name_key_secondary, "
    "  schedule = EXCLUDED.schedule, "
    "  raw = EXCLUDED.raw, "
    "  updated_at = now()"
)

_REGISTRATION_UPSERT_SQL = (
    "INSERT INTO clash_registrations "
    "(puuid, team_id, position, role, raw, last_seen_at) "
    "VALUES (%s, %s, %s, %s, %s, now()) "
    "ON CONFLICT (puuid, team_id) DO UPDATE SET "
    "  position = EXCLUDED.position, "
    "  role = EXCLUDED.role, "
    "  raw = EXCLUDED.raw, "
    "  last_seen_at = now()"
)

_TEAM_UPSERT_SQL = (
    "INSERT INTO clash_teams "
    "(team_id, tournament_id, name, abbreviation, icon_id, tier, captain, "
    " players, raw, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
    "ON CONFLICT (team_id) DO UPDATE SET "
    "  tournament_id = EXCLUDED.tournament_id, "
    "  name = EXCLUDED.name, "
    "  abbreviation = EXCLUDED.abbreviation, "
    "  icon_id = EXCLUDED.icon_id, "
    "  tier = EXCLUDED.tier, "
    "  captain = EXCLUDED.captain, "
    "  players = EXCLUDED.players, "
    "  raw = EXCLUDED.raw, "
    "  updated_at = now()"
)


class ClashUpdater(commands.Cog):
    """Daily clash-v1 snapshot: tournaments + tracked players' teams."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        self.clash_sweep_last_fired: dt.datetime | None = None
        self.clash_sweep.start()

    def cog_unload(self) -> None:
        # discord.py does NOT cancel @tasks.loop tasks on cog unload.
        self.clash_sweep.cancel()

    @tasks.loop(hours=SWEEP_HOURS)
    async def clash_sweep(self) -> None:
        tournaments = await self._sweep_tournaments()
        registrations, team_ids = await self._sweep_registrations()
        teams = await self._sweep_teams(team_ids)
        self.clash_sweep_last_fired = dt.datetime.now()
        self.bot.logging.info(
            f"Clash sweep: {tournaments} tournaments, "
            f"{registrations} registrations, {teams} teams upserted"
        )

    async def _sweep_tournaments(self) -> int:
        tournaments = await get_clash_tournaments()
        if tournaments is None:
            self.bot.logging.warning("Clash tournaments fetch failed")
            return 0
        written = 0
        for tournament in tournaments:
            # Per-item isolation: one malformed tournament costs exactly
            # that tournament, never the rest of the sweep.
            try:
                tournament_id = tournament.get("id")
                if tournament_id is None:
                    continue
                await db.execute(
                    _TOURNAMENT_UPSERT_SQL,
                    (
                        int(tournament_id),
                        tournament.get("themeId"),
                        tournament.get("nameKey"),
                        tournament.get("nameKeySecondary"),
                        Jsonb(tournament.get("schedule") or []),
                        Jsonb(tournament),
                    ),
                )
                written += 1
            except Exception as exc:
                self.bot.logging.error(f"Clash tournament upsert failed: {exc!r}")
        return written

    async def _sweep_registrations(self) -> tuple[int, set[str]]:
        """Snapshot every tracked account's active Clash registrations.

        Returns (rows written, distinct team ids seen) — the team ids
        feed the roster fetch. [] from Riot means "not registered" and
        writes nothing; None means the request failed and is only logged.
        """
        rows = await db.fetchall(
            "SELECT DISTINCT puuid FROM league_players WHERE puuid IS NOT NULL AND puuid != ''"
        )
        written = 0
        team_ids: set[str] = set()
        for (puuid,) in rows:
            registrations = await get_clash_players(puuid)
            if registrations is None:
                self.bot.logging.warning(f"Clash players fetch failed for {puuid[:8]}...")
                continue
            for registration in registrations:
                try:
                    team_id = registration.get("teamId")
                    if not team_id:
                        continue
                    await db.execute(
                        _REGISTRATION_UPSERT_SQL,
                        (
                            puuid,
                            str(team_id),
                            registration.get("position"),
                            registration.get("role"),
                            Jsonb(registration),
                        ),
                    )
                    written += 1
                    team_ids.add(str(team_id))
                except Exception as exc:
                    self.bot.logging.error(
                        f"Clash registration upsert failed for {puuid[:8]}...: {exc!r}"
                    )
        return written, team_ids

    async def _sweep_teams(self, team_ids: set[str]) -> int:
        """Fetch + upsert the roster for each team seen this sweep."""
        written = 0
        for team_id in sorted(team_ids):
            try:
                team = await get_clash_team(team_id)
                if team is None:
                    # 404 (disbanded since the player fetch) or transient
                    # failure — either way, next sweep re-discovers it if
                    # it still exists.
                    self.bot.logging.warning(f"Clash team fetch returned nothing for {team_id}")
                    continue
                await db.execute(
                    _TEAM_UPSERT_SQL,
                    (
                        team_id,
                        team.get("tournamentId"),
                        team.get("name"),
                        team.get("abbreviation"),
                        team.get("iconId"),
                        team.get("tier"),
                        team.get("captain"),
                        Jsonb(team.get("players") or []),
                        Jsonb(team),
                    ),
                )
                written += 1
            except Exception as exc:
                self.bot.logging.error(f"Clash team upsert failed for {team_id}: {exc!r}")
        return written

    @clash_sweep.before_loop
    async def before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    @clash_sweep.error
    async def clash_sweep_error(self, exc: BaseException) -> None:
        """Auto-restart the sweep on unhandled error (default @tasks.loop
        behaviour is log + stop). Detached restart — see utils/loop_restart."""
        self.bot.logging.error(f"clash_sweep errored: {exc!r}, restarting in 60s")
        restart_loop_later(
            self.clash_sweep,
            name="clash_sweep",
            log=self.bot.logging,
            still_active=lambda: self.bot.get_cog("ClashUpdater") is self,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ClashUpdater(bot))
