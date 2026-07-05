from utils import db
from utils.riot_client import RANKED_SOLO_QUEUE_ID


async def fetch_recent_kd(puuid: str, count: int = 20) -> tuple[int, int, int, int, int]:
    """Totals across a player's last N ranked solo/duo matches.

    Returns ``(kills, deaths, assists, wins, games_counted)``; callers treat
    ``games_counted == 0`` as "no data".

    Reads match_stats instead of Riot — this used to burn ~N+1 Match-V5
    calls per invocation for numbers the stream already ingests. The 5-min
    stream means a game finished moments ago may not be counted yet; at
    N=20 that skews nothing worth an API round-trip.
    """
    rows = await db.fetchall(
        "SELECT kills, deaths, assists, win FROM match_stats "
        "WHERE puuid = %s AND queue_id = %s "
        "ORDER BY game_start DESC LIMIT %s",
        (puuid, RANKED_SOLO_QUEUE_ID, count),
    )
    kills = sum(row[0] for row in rows)
    deaths = sum(row[1] for row in rows)
    assists = sum(row[2] for row in rows)
    wins = sum(1 for row in rows if row[3])
    return (kills, deaths, assists, wins, len(rows))
