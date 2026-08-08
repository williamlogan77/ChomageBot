"""EUW service status (lol-status-v4): incidents + maintenances only.

Capture-remainder judgment call, implemented as the lightest possible
snapshot: an hourly poll (24 requests a DAY — Riot documents lol-status
as not even counting against the app limit, but it rides the shared
budget anyway) that writes rows ONLY when Riot has published an incident
or maintenance for EUW. The steady-state "everything is fine" response —
which is what the endpoint returns almost every hour of almost every
day, and was all it had during the 2026-08-08 live pass — is not stored:
a table of "still fine" rows is noise, and uptime history is derivable
from the gaps between event rows anyway.

Why capture it at all: the event rows are the receipts for "remember
when EUW died mid-Clash" banter and explain LP-history flatlines after
the fact. That's cheap enough at ~1 row per real-world outage.

Each live event upserts every poll while Riot lists it, so
first_seen_at/last_seen_at bracket the visible window and the raw column
keeps the final DTO (including the updates[] thread Riot appends to as
an incident develops). Entry fields are snake_case — a status-v4 quirk —
and timestamps arrive as strings, parsed best-effort (raw keeps the
original either way).
"""

import datetime as dt
import logging

from discord.ext import commands, tasks
from psycopg.types.json import Jsonb
from utils import db
from utils.loop_restart import restart_loop_later
from utils.riot_client import get_platform_status

log = logging.getLogger(__name__)

SWEEP_MINUTES = 60

_PREFERRED_LOCALES = ("en_GB", "en_US")

_EVENT_UPSERT_SQL = (
    "INSERT INTO lol_status_events "
    "(status_id, kind, status, severity, title, platforms, created_at, "
    " updated_at_riot, archive_at, raw, last_seen_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
    "ON CONFLICT (status_id, kind) DO UPDATE SET "
    "  status = EXCLUDED.status, "
    "  severity = EXCLUDED.severity, "
    "  title = EXCLUDED.title, "
    "  platforms = EXCLUDED.platforms, "
    "  created_at = EXCLUDED.created_at, "
    "  updated_at_riot = EXCLUDED.updated_at_riot, "
    "  archive_at = EXCLUDED.archive_at, "
    "  raw = EXCLUDED.raw, "
    "  last_seen_at = now()"
)


def _parse_status_ts(value) -> dt.datetime | None:
    """status-v4 timestamp string -> tz-aware datetime, best-effort.

    The endpoint returns ISO-ish strings (couldn't be live-validated —
    EUW had no events during the pass); anything unparseable stores NULL
    and survives verbatim in raw.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def _title(entry: dict) -> str | None:
    """English title from the titles[] content list, any-locale fallback."""
    titles = entry.get("titles") or []
    by_locale = {
        t.get("locale"): t.get("content")
        for t in titles
        if isinstance(t, dict) and t.get("content")
    }
    for locale in _PREFERRED_LOCALES:
        if by_locale.get(locale):
            return by_locale[locale]
    return next(iter(by_locale.values()), None)


class StatusUpdater(commands.Cog):
    """Hourly EUW status poll; writes only real incidents/maintenances."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        self.status_sweep_last_fired: dt.datetime | None = None
        self.status_sweep.start()

    def cog_unload(self) -> None:
        # discord.py does NOT cancel @tasks.loop tasks on cog unload.
        self.status_sweep.cancel()

    @tasks.loop(minutes=SWEEP_MINUTES)
    async def status_sweep(self) -> None:
        data = await get_platform_status()
        if data is None:
            self.bot.logging.warning("lol-status fetch failed")
            return

        written = 0
        for kind, plural in (("maintenance", "maintenances"), ("incident", "incidents")):
            for entry in data.get(plural) or []:
                # Per-item isolation: one malformed entry costs exactly
                # that entry.
                try:
                    if await self._upsert_event(kind, entry):
                        written += 1
                except Exception as exc:
                    self.bot.logging.error(f"lol-status {kind} upsert failed: {exc!r}")

        self.status_sweep_last_fired = dt.datetime.now()
        if written:
            # Quiet when all-clear (the overwhelmingly common poll result);
            # loud when something is actually going on.
            self.bot.logging.info(f"lol-status sweep: {written} live event(s) recorded")

    async def _upsert_event(self, kind: str, entry: dict) -> bool:
        status_id = entry.get("id")
        if status_id is None:
            return False
        await db.execute(
            _EVENT_UPSERT_SQL,
            (
                int(status_id),
                kind,
                entry.get("maintenance_status"),
                entry.get("incident_severity"),
                _title(entry),
                Jsonb(entry.get("platforms") or []),
                _parse_status_ts(entry.get("created_at")),
                _parse_status_ts(entry.get("updated_at")),
                _parse_status_ts(entry.get("archive_at")),
                Jsonb(entry),
            ),
        )
        return True

    @status_sweep.before_loop
    async def before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    @status_sweep.error
    async def status_sweep_error(self, exc: BaseException) -> None:
        """Auto-restart the sweep on unhandled error (default @tasks.loop
        behaviour is log + stop). Detached restart — see utils/loop_restart."""
        self.bot.logging.error(f"status_sweep errored: {exc!r}, restarting in 60s")
        restart_loop_later(
            self.status_sweep,
            name="status_sweep",
            log=self.bot.logging,
            still_active=lambda: self.bot.get_cog("StatusUpdater") is self,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(StatusUpdater(bot))
