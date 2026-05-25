"""List every role in the bot's guild(s) — diagnostic helper.

Reads the bot token from the BOT_TOKEN environment variable (or
DISCORD_TOKEN, matching the bot's own config) and dumps each guild's
role list to stdout. Useful when an `app_commands.check` is rejecting
invocations and we need the EXACT role name (case + unicode) the
guild has.

Usage (from inside container 103, after `source .env` or equivalent):
    python3 scripts/list_guild_roles.py

Read-only. Does not write to the DB. Does not start the gateway —
uses Discord's REST API directly.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://discord.com/api/v10"


def _token() -> str:
    for env_var in ("BOT_TOKEN", "DISCORD_TOKEN", "TOKEN"):
        tok = os.environ.get(env_var)
        if tok:
            return tok
    sys.exit(
        "ERROR: no BOT_TOKEN / DISCORD_TOKEN / TOKEN env var. "
        "Source your .env first: `set -a; source .env; set +a`"
    )


def _request(path: str, token: str) -> object:
    req = urllib.request.Request(
        f"{API}{path}",
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "ChomageBot-RoleLister/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    token = _token()
    try:
        guilds = _request("/users/@me/guilds", token)
    except urllib.error.HTTPError as exc:
        print(f"ERROR: {exc.code} listing guilds: {exc.read().decode('utf-8')[:200]}")
        return 1

    print(f"Bot is in {len(guilds)} guild(s):\n")
    for g in guilds:
        gid, gname = g["id"], g["name"]
        print(f"=== {gname}  (id={gid}) ===")
        try:
            roles = _request(f"/guilds/{gid}/roles", token)
        except urllib.error.HTTPError as exc:
            print(f"  ERROR fetching roles: {exc.code} {exc.read().decode('utf-8')[:200]}")
            continue
        # Sort by position descending (highest = listed first in Discord UI).
        roles.sort(key=lambda r: r["position"], reverse=True)
        for r in roles:
            # `repr` exposes unicode quirks (curly quotes, NBSP, em-dash, ZWSP).
            print(f"  {repr(r['name']):<40}  (id={r['id']})")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
