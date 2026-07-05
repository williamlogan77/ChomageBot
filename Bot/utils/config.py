"""Central runtime configuration.

Every environment variable the bot reads lives behind a getter here — one
place to see the whole config surface and one place to add the next value.
Loads the repo-root ``.env`` on import (the same file main.py always used),
so standalone scripts that import any utils module get the same view.

Required values raise :class:`MissingConfigError` with a hint instead of
falling back to baked-in defaults — no credentials live in code.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

# Bot/utils/config.py -> repo root .env (sits next to docker-compose.yml).
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(_ENV_PATH)


class MissingConfigError(RuntimeError):
    """A required env var is absent — the .env is incomplete."""


def _require(name: str, hint: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingConfigError(f"{name} is not set — add it to .env ({hint})")
    return value


def database_url() -> str:
    """Postgres DSN. Prod: the chomage-db LXC; dev: your own container.

    See docs/db-access.md and .env.example — there is deliberately no
    default with credentials in it.
    """
    return _require(
        "DATABASE_URL",
        "e.g. postgresql://<user>:<password>@<host>:5432/chomage — see docs/db-access.md",
    )


def discord_token() -> str:
    return _require("token", "the Discord bot token")


def guild_id() -> int:
    return int(_require("guild_id", "the Discord server id the bot lives in"))


def riot_api_key() -> str | None:
    """Riot API key; None is tolerated so DB-only dev setups can run."""
    return os.environ.get("riot_key")


def ranked5s_queue_type() -> str | None:
    """Pinned league-v4 queueType for Ranked 5s (unset = auto-discover)."""
    return os.environ.get("ranked5s_queue_type")
