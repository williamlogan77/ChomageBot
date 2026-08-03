"""Discord OAuth2 login — the dashboard's only authentication.

Flow (authorization code grant):
  1. /login redirects to Discord's authorize page with our client_id,
     redirect_uri, scopes (identify + guilds) and a random signed state.
  2. The user approves; Discord redirects back to /auth/callback with a
     one-time code.
  3. We exchange code + client_secret for an access token (server-side —
     the secret never reaches the browser).
  4. With the token we ask Discord who the user is (/users/@me) and what
     guilds they're in (/users/@me/guilds). Members of guild_id get a
     session; everyone else is rejected.
  5. The session is a signed cookie (itsdangerous) holding id + name.
     No passwords, no Discord tokens stored — the access token is used
     for those two lookups and discarded.

Admin writes are gated separately by admin_user_ids (see config.py).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app import config

DISCORD_API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
SCOPES = "identify guilds"

SESSION_COOKIE = "session"
SESSION_MAX_AGE = 7 * 24 * 3600  # re-login weekly
STATE_COOKIE = "oauth_state"


@dataclass(frozen=True)
class SessionUser:
    user_id: int
    name: str

    @property
    def is_admin(self) -> bool:
        return self.user_id in config.admin_user_ids()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.session_secret())


# ------------------------------------------------------------ oauth flow


def new_state() -> str:
    return secrets.token_urlsafe(24)


def authorize_url(state: str) -> str:
    query = urlencode(
        {
            "client_id": config.discord_client_id(),
            "response_type": "code",
            "redirect_uri": config.oauth_redirect_uri(),
            "scope": SCOPES,
            "state": state,
            "prompt": "none",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


async def exchange_code(code: str) -> str | None:
    """One-time code -> access token, or None on failure."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": config.discord_client_id(),
                "client_secret": config.discord_client_secret(),
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.oauth_redirect_uri(),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if response.status_code != 200:
        return None
    return response.json().get("access_token")


async def fetch_member_user(access_token: str) -> SessionUser | None:
    """The logged-in user, or None if they're not in the guild."""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        me = await client.get(f"{DISCORD_API}/users/@me")
        guilds = await client.get(f"{DISCORD_API}/users/@me/guilds")
    if me.status_code != 200 or guilds.status_code != 200:
        return None
    if not any(int(g["id"]) == config.guild_id() for g in guilds.json()):
        return None
    payload = me.json()
    name = payload.get("global_name") or payload["username"]
    return SessionUser(user_id=int(payload["id"]), name=name)


# -------------------------------------------------------------- sessions


def session_cookie_value(user: SessionUser) -> str:
    return _serializer().dumps({"id": user.user_id, "name": user.name})


def user_from_cookie(value: str | None) -> SessionUser | None:
    if not value:
        return None
    try:
        payload = _serializer().loads(value, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    return SessionUser(user_id=int(payload["id"]), name=payload["name"])
