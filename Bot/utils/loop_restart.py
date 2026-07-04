"""Detached restart for @tasks.loop error handlers.

A loop's error callback runs INSIDE the dying loop task — discord.py
awaits it before re-raising (see discord/ext/tasks Loop._loop) — so
``is_running()`` is still True there and a direct ``start()`` is
silently skipped: the loop just dies until the heartbeat watchdog
notices. Scheduling the restart on a detached task lets the loop task
finish first, so the ``start()`` actually takes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable


def restart_loop_later(
    loop,
    *,
    name: str,
    log: logging.Logger,
    delay: float = 60.0,
    still_active: Callable[[], bool] | None = None,
) -> None:
    """Restart ``loop`` after ``delay`` seconds, from outside its own task.

    ``still_active`` guards the reload race: if the parent cog was
    reloaded during the delay (heartbeat / auto_reload), restarting the
    OLD instance's loop would run two boards side by side — the new
    instance already started its own loop, so we bail out instead.
    """

    async def _restart() -> None:
        await asyncio.sleep(delay)
        if still_active is not None and not still_active():
            log.info(f"{name}: cog replaced during backoff, skipping restart")
            return
        if loop.is_running():
            return
        try:
            loop.start()
            log.info(f"{name}: restarted after error backoff")
        except RuntimeError as exc:
            log.warning(f"{name}: restart failed: {exc}")

    asyncio.create_task(_restart())
