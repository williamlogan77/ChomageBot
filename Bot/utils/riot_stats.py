from utils.riot_client import get_match, get_match_ids


async def fetch_recent_kd(puuid: str, count: int = 20) -> tuple[int, int, int, int, int]:
    """Totals across a player's last N ranked solo/duo matches.

    Returns ``(kills, deaths, assists, wins, games_counted)``. On any API
    failure returns ``(0, 0, 0, 0, 0)``; callers should treat
    ``games_counted == 0`` as "no data".

    Each call hits Riot's Match-V5 ~N+1 times via the shared rate-limited
    client. Safe to call concurrently — callers serialise behind the global
    limiter rather than racing.
    """
    match_ids = await get_match_ids(puuid, count=count)
    if not match_ids:
        return (0, 0, 0, 0, 0)

    kills = 0
    deaths = 0
    assists = 0
    wins = 0
    games = 0
    for match_id in match_ids:
        match = await get_match(match_id)
        if match is None:
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
