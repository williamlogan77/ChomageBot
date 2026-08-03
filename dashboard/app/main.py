"""Chomage dashboard — FastAPI app.

Read views over the bot's Postgres (seasons, weekly awards, tracked
players, bot_config) plus a control primitive: editing bot_config keys,
which the bot already polls at runtime (e.g. ranked5s_channel_id). Auth
is Discord OAuth2 only — see app/auth.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth, db

log = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="Chomage dashboard")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.on_event("shutdown")
async def shutdown() -> None:
    await db.close()


def current_user(request: Request) -> auth.SessionUser | None:
    return auth.user_from_cookie(request.cookies.get(auth.SESSION_COOKIE))


# ----------------------------------------------------------------- auth


@app.get("/login")
async def login() -> RedirectResponse:
    state = auth.new_state()
    response = RedirectResponse(auth.authorize_url(state))
    # State round-trips via a short-lived cookie and must match on the
    # callback — standard CSRF protection for the OAuth redirect.
    response.set_cookie(auth.STATE_COOKIE, state, max_age=600, httponly=True)
    return response


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    if not code or not state or state != request.cookies.get(auth.STATE_COOKIE):
        return RedirectResponse("/?error=state")
    token = await auth.exchange_code(code)
    user = await auth.fetch_member_user(token) if token else None
    if user is None:
        log.warning("OAuth login rejected (bad code exchange or not a guild member)")
        return RedirectResponse("/?error=denied")
    log.info(f"Login: {user.name} ({user.user_id}), admin={user.is_admin}")
    response = RedirectResponse("/")
    response.delete_cookie(auth.STATE_COOKIE)
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.session_cookie_value(user),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/")
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


# ----------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = current_user(request)
    if user is None:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": request.query_params.get("error")}
        )

    seasons = await db.fetchall(
        "SELECT started_at, detected_at FROM seasons ORDER BY started_at DESC"
    )
    awards = await db.fetchall(
        "SELECT week_start, award, display_name, value FROM weekly_awards "
        "WHERE week_start = (SELECT MAX(week_start) FROM weekly_awards) ORDER BY id"
    )
    players = await db.fetchall(
        "SELECT league_username, discord_user_id FROM league_players "
        "WHERE puuid IS NOT NULL ORDER BY league_username"
    )
    bot_config = await db.fetchall("SELECT key, value, updated_at FROM bot_config ORDER BY key")
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "user": user,
            "seasons": seasons,
            "awards": awards,
            "players": players,
            "bot_config": bot_config,
        },
    )


@app.get("/healthz")
async def healthz() -> dict:
    await db.fetchone("SELECT 1")
    return {"ok": True}


# --------------------------------------------------------------- control


@app.post("/config")
async def set_config(request: Request, key: str = Form(...), value: str = Form(...)):
    """Upsert a bot_config key — the bot polls this table at runtime."""
    user = current_user(request)
    if user is None:
        return RedirectResponse("/", status_code=303)
    if not user.is_admin:
        return RedirectResponse("/?error=admin", status_code=303)
    await db.execute(
        "INSERT INTO bot_config (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key.strip(), value.strip()),
    )
    log.info(f"bot_config[{key.strip()}] set by {user.name} ({user.user_id})")
    return RedirectResponse("/", status_code=303)
