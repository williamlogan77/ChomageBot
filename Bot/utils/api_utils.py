import re
import logging

import aiohttp

from typing import Iterable
log = logging.getLogger(__name__)

# Based somewhat on Pantheon API requests
# https://github.com/Canisback/pantheon/blob/master/pantheon/pantheon.py

class APIutils:
    def __init__(self, riot_api_key):
        self.headers = {"X-Riot-Token": riot_api_key}

    BASE_URL_LOL = "https://euw1.api.riotgames.com/lol/"
    BASE_URL_RIOT = "https://europe.api.riotgames.com/riot/" #Need europe for riot Oddge

###============================================================================

    # Retry function, taken directly from aiohttp docs
    # https://docs.aiohttp.org/en/stable/client_middleware_cookbook.html
    # Moved rate limit code here also
    async def retry_middleware(
        self,
        request: aiohttp.ClientRequest,
        handler: aiohttp.ClientHandlerType
    ) -> aiohttp.ClientResponse:
        for _ in range(3):  # Try up to 3 times
            response = await handler(request)
            log.info(f"Request: {response.url}, Status: {response.status}")
            if response.ok: # response is 200 (400 or below)
                return response
            elif response.status == 429:
                # Rate limited
                retry_after = int(response.headers.get('Retry-After', 10))
                log.warning(f"Rate limited, waiting {retry_after} seconds")
                await asyncio.sleep(retry_after)
            else:
                error_text = await response.text()
                log.error(f"HTTP {response.status}: {error_text}")
                raise Exception(f"HTTP {response.status}: {error_text}")

    async def fetch(self, url):
        async with aiohttp.ClientSession() as session:
            async with session.get(
                    url,
                    headers=self.headers,
                    middlewares=[self.retry_middleware]) as response:
                return await response.json()

###============================================================================

    async def get_league_queues_entries(self, puuid: str):
        # GET /lol/league/v4/entries/by-puuid
        # Endpoint: GET /lol/league/v4/entries/by-puuid/{encryptedPUUID}
        # Note: League API uses platform routing (euw1), not regional routing (europe)
        endpoint = f"league/v4/entries/by-puuid/{puuid}"
        return await self.fetch(self.BASE_URL_LOL + endpoint)


    async def get_account_by_puuid(self, puuid: str):
        # GET /riot/account/v1/accounts/by-puuid/{puuid}
        # Returns the result of https://developer.riotgames.com/apis#account-v1/GET_getByPuuid
        endpoint = f"account/v1/accounts/by-puuid/{puuid}"
        return await self.fetch(self.BASE_URL_RIOT + endpoint)


    async def get_account_by_riotid(self, league_name, tag_Line):
        # GET /riot/account/v1/accounts/by-riot-id/{gameName}/{tagLine}
        # Returns the result of https://developer.riotgames.com/apis#account-v1/GET_getByRiotId
        endpoint = f"account/v1/accounts/by-riot-id/{league_name}/{tag_Line}"
        return await self.fetch(self.BASE_URL_RIOT + endpoint)
