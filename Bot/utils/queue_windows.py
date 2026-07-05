"""Ranked 5s queue schedule helpers.

The 2026 "Ranked 5s" limited-test queue is only open on weekends:
Friday, Saturday and Sunday, 20:00 until 01:00 the next day, in the
*server's local time*. For EUW that is Europe/Paris (20:00-01:00 CEST
during the whole test window). Sunday's window therefore ends Monday
01:00, and a game started just before close finishes after it.

Everything here is a pure function over datetimes — no discord, no db,
no network — so the schedule logic is trivially unit-testable:

    is_ranked5s_open()        queue is open right now
    is_ranked5s_tracking()    open OR within the 2h post-close tail
    next_window_open()        next opening datetime (None after the test ends)

All functions accept an optional ``now`` for testing: tz-aware datetimes
are converted to Europe/Paris; naive datetimes are *assumed* to already
be Europe/Paris wall time.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

RANKED5S_TZ = ZoneInfo("Europe/Paris")

# datetime.weekday(): Monday == 0 ... Sunday == 6.
OPEN_WEEKDAYS = (4, 5, 6)  # Friday, Saturday, Sunday
OPEN_TIME = dt.time(20, 0)
CLOSE_TIME = dt.time(1, 0)  # 01:00 the day AFTER the open date

# Keep tracking for a while after close: league-v4 entries / LP settle
# after the window, and games in flight at 01:00 finish + award LP well
# past it. 2h comfortably covers a game started at 00:59.
TRACKING_TAIL = dt.timedelta(hours=2)

# Limited-test bounds (dates of window *opens*, Europe/Paris). The queue
# exists June 26 - September 6 2026; the final window opens Sunday
# Sept 6 and runs into Monday Sept 7. Riot may extend the test — bump
# TEST_END if they do.
TEST_START = dt.date(2026, 6, 26)
TEST_END = dt.date(2026, 9, 6)


def _localize(now: dt.datetime | None) -> dt.datetime:
    """Coerce ``now`` to an aware Europe/Paris datetime (default: now)."""
    if now is None:
        return dt.datetime.now(RANKED5S_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=RANKED5S_TZ)
    return now.astimezone(RANKED5S_TZ)


def _window_for_open_date(open_date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """(open, close) datetimes for the window that opens on ``open_date``.

    Close is 01:00 on the following day — this is the midnight crossing.
    """
    open_dt = dt.datetime.combine(open_date, OPEN_TIME, tzinfo=RANKED5S_TZ)
    close_dt = dt.datetime.combine(open_date + dt.timedelta(days=1), CLOSE_TIME, tzinfo=RANKED5S_TZ)
    return open_dt, close_dt


def _is_window_open_date(day: dt.date) -> bool:
    """True if a window opens on ``day`` (right weekday, inside the test)."""
    return day.weekday() in OPEN_WEEKDAYS and TEST_START <= day <= TEST_END


def _candidate_open_dates(local_now: dt.datetime) -> tuple[dt.date, dt.date]:
    """The only two dates whose window could contain ``local_now``.

    Because a window spans at most 20:00 -> next-day 03:00 (with tail),
    only "yesterday's" and "today's" windows can be live.
    """
    today = local_now.date()
    return (today - dt.timedelta(days=1), today)


def _in_live_window(now: dt.datetime | None, tail: dt.timedelta) -> bool:
    """Is ``now`` inside any test-bounded window, extended ``tail`` past close?"""
    local = _localize(now)
    for day in _candidate_open_dates(local):
        if not _is_window_open_date(day):
            continue
        open_dt, close_dt = _window_for_open_date(day)
        if open_dt <= local < close_dt + tail:
            return True
    return False


def is_ranked5s_open(now: dt.datetime | None = None) -> bool:
    """Is the Ranked 5s queue open right now?"""
    return _in_live_window(now, dt.timedelta(0))


def is_ranked5s_tracking(now: dt.datetime | None = None) -> bool:
    """Should the board be polling right now? (open, or the 2h tail after close)

    Always False outside the TEST_START..TEST_END limited-test bounds —
    the bounds apply to the window's *open* date, so the final Sunday
    window is still tracked through Monday 03:00.
    """
    return _in_live_window(now, TRACKING_TAIL)


def next_window_open(now: dt.datetime | None = None) -> dt.datetime | None:
    """The next datetime the queue opens, strictly after ``now``.

    Returns None once the limited test is over (no window opens after
    TEST_END). Intended for logging / status output.
    """
    local = _localize(now)
    day = max(local.date(), TEST_START)
    while day <= TEST_END:
        if day.weekday() in OPEN_WEEKDAYS:
            open_dt, _ = _window_for_open_date(day)
            if open_dt > local:
                return open_dt
        day += dt.timedelta(days=1)
    return None
