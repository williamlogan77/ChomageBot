import re
from typing import Iterable
import aiosqlite as sqa


class APIutils:
    def __init__(self, riot_api_key):
        self.riot_api_key = riot_api_key
        self._headers = {"X-Riot-Token": riot_api_key}


###============================================================================

    async def get_league_v4_entries(self, puuid: str)
        #GET /lol/league/v4/entries/by-puuid
        # Direct PUUID endpoint - much simpler!
        # Endpoint: GET /lol/league/v4/entries/by-puuid/{encryptedPUUID}
        # Note: League API uses platform routing (euw1), not regional routing (europe)
        league_v4_url = f"https://euw1.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        async with aiohttp.ClientSession() as session:
            async with session.get(league_v4_url, headers=_headers) as response:
                if response.status == 200:
                    response_json = await response.json()
                elif response.status == 429:
                    # Rate limited
                    retry_after = int(response.headers.get('Retry-After', 10))
                    self.bot.logging.warning(f"Rate limited, waiting {retry_after} seconds")
                    await asyncio.sleep(retry_after)
                else:
                    error_text = await response.text()
                    raise Exception(f"HTTP {response.status}: {error_text}")





