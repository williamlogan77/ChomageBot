"""
Script to update PUUIDs in the database using the Riot Account API
"""

import asyncio
import os
import sys

import aiohttp
from dotenv import load_dotenv

# Make Bot/ importable (`from utils import db`) when this script is run
# directly (python utils/update_puuids.py from the Bot/ working dir).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import db  # noqa: E402

# Load environment variables
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(env_path)


async def update_puuids():
    """Update all PUUIDs in the database using the Account API"""
    riot_api_key = os.environ.get("riot_key")

    if not riot_api_key:
        print("ERROR: riot_key not found in environment variables!")
        return

    print(f"Riot API Key loaded: {riot_api_key[:10]}...{riot_api_key[-4:]}")

    # Fetch all players
    players = await db.fetchall("SELECT discord_user_id, league_username, tag FROM league_players")

    print(f"\nFound {len(players)} players to update")
    print("-" * 60)

    updated = 0
    failed = 0

    for discord_user_id, game_name, tag_line in players:
        try:
            # Call Account API to get PUUID
            # Use europe routing for EUW players
            url = f"https://europe.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
            headers = {"X-Riot-Token": riot_api_key}

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        account_data = await response.json()
                        new_puuid = account_data["puuid"]

                        # Update database
                        await db.execute(
                            "UPDATE league_players SET puuid = %s WHERE discord_user_id = %s AND league_username = %s AND tag = %s",
                            (new_puuid, discord_user_id, game_name, tag_line),
                        )

                        print(f"✓ Updated {game_name}#{tag_line}")
                        updated += 1
                    elif response.status == 429:
                        retry_after = int(response.headers.get("Retry-After", 10))
                        print(f"⚠ Rate limited, waiting {retry_after} seconds...")
                        await asyncio.sleep(retry_after)
                        # Retry this one
                        continue
                    else:
                        error_text = await response.text()
                        print(
                            f"✗ Failed {game_name}#{tag_line}: HTTP {response.status} - {error_text}"
                        )
                        failed += 1

            # Small delay to avoid rate limits
            await asyncio.sleep(0.1)

        except Exception as e:
            print(f"✗ Error updating {game_name}#{tag_line}: {e}")
            failed += 1

    print("-" * 60)
    print("\nUpdate complete!")
    print(f"  Updated: {updated}")
    print(f"  Failed: {failed}")
    print(f"  Total: {len(players)}")

    await db.close()


if __name__ == "__main__":
    # psycopg's async support needs a selector event loop on Windows —
    # only relevant when running this standalone script locally.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(update_puuids())
