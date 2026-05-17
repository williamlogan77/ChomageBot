from utils.riot_client import get_json

# Match-V5 uses regional routing (europe), distinct from the platform routing
# (euw1) used for league entries.
REGION_ROUTE = "europe"
RANKED_SOLO_QUEUE_ID = 420


async def fetch_recent_kd(puuid: str, count: int = 20) -> tuple[int, int, int, int, int]:
    """Totals across a player's last N ranked solo/duo matches.

    Returns ``(kills, deaths, assists, wins, games_counted)``. On any API
    failure returns ``(0, 0, 0, 0, 0)``; callers should treat
    ``games_counted == 0`` as "no data".

    Each call hits Riot's Match-V5 ~N+1 times. Rate-limited via the shared
    :mod:`utils.riot_client`, so safe to call from anywhere — concurrent
    callers serialise behind the global limiter rather than racing.
    """
    ids_url = (
        f"https://{REGION_ROUTE}.api.riotgames.com" f"/lol/match/v5/matches/by-puuid/{puuid}/ids"
    )
    status, match_ids = await get_json(
        ids_url, params={"queue": RANKED_SOLO_QUEUE_ID, "count": count}
    )
    if status != 200 or not match_ids:
        return (0, 0, 0, 0, 0)

    kills = 0
    deaths = 0
    assists = 0
    wins = 0
    games = 0
    for match_id in match_ids:
        match_url = f"https://{REGION_ROUTE}.api.riotgames.com" f"/lol/match/v5/matches/{match_id}"
        status, match = await get_json(match_url)
        if status != 200 or match is None:
            continue
        for p in match["info"]["participants"]:
            if p["puuid"] == puuid:
                kills += p["kills"]
                deaths += p["deaths"]
                assists += p["assists"]
                wins += 1 if p["win"] else 0
                games += 1
                break

    return (kills, deaths, assists, wins, games)
