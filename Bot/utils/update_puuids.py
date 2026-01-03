"""
Script to update PUUIDs in the database using the Riot Account API
"""
import asyncio
import aiosqlite as sqa
import aiohttp
import os
from dotenv import load_dotenv

# Load environment variables
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
load_dotenv(env_path)

async def update_puuids(db_path: str):
    """Update all PUUIDs in the database using the Account API"""
    riot_api_key = os.environ.get("riot_key")
    
    if not riot_api_key:
        print("ERROR: riot_key not found in environment variables!")
        return
    
    print(f"Riot API Key loaded: {riot_api_key[:10]}...{riot_api_key[-4:]}")
    
    async with sqa.connect(db_path) as db:
        # Fetch all players
        cursor = await db.execute("SELECT discord_user_id, league_username, tag FROM league_players")
        players = await cursor.fetchall()
        
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
                            new_puuid = account_data['puuid']
                            
                            # Update database
                            await db.execute(
                                "UPDATE league_players SET puuid = ? WHERE discord_user_id = ? AND league_username = ? AND tag = ?",
                                (new_puuid, discord_user_id, game_name, tag_line)
                            )
                            await db.commit()
                            
                            print(f"✓ Updated {game_name}#{tag_line}")
                            updated += 1
                        elif response.status == 429:
                            retry_after = int(response.headers.get('Retry-After', 10))
                            print(f"⚠ Rate limited, waiting {retry_after} seconds...")
                            await asyncio.sleep(retry_after)
                            # Retry this one
                            continue
                        else:
                            error_text = await response.text()
                            print(f"✗ Failed {game_name}#{tag_line}: HTTP {response.status} - {error_text}")
                            failed += 1
                
                # Small delay to avoid rate limits
                await asyncio.sleep(0.1)
                
            except Exception as e:
                print(f"✗ Error updating {game_name}#{tag_line}: {e}")
                failed += 1
        
        print("-" * 60)
        print(f"\nUpdate complete!")
        print(f"  Updated: {updated}")
        print(f"  Failed: {failed}")
        print(f"  Total: {len(players)}")

if __name__ == "__main__":
    db_path = "./db/database.sqlite"
    asyncio.run(update_puuids(db_path))
