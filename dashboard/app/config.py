"""Env-driven configuration — every setting the dashboard needs.

Mirrors the bot's utils/config.py philosophy: env reads live here, no
hardcoded credentials, missing required values fail loudly at startup.
"""

from __future__ import annotations

import os


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required env var {name} is not set")
    return value


def database_url() -> str:
    return _require("DATABASE_URL")


def discord_client_id() -> str:
    return _require("discord_client_id")


def discord_client_secret() -> str:
    return _require("discord_client_secret")


def oauth_redirect_uri() -> str:
    """Must byte-match a Redirect URI registered in the Discord portal."""
    return _require("oauth_redirect_uri")


def session_secret() -> str:
    """Signs the session cookie. Generate once: openssl rand -hex 32."""
    return _require("session_secret")


def guild_id() -> int:
    """Only members of this guild may log in."""
    return int(_require("guild_id"))


def admin_user_ids() -> set[int]:
    """Discord user ids allowed to change settings (comma-separated).

    Everyone in the guild can view; only admins can write.
    """
    raw = os.environ.get("admin_user_ids", "")
    return {int(part) for part in raw.split(",") if part.strip()}
