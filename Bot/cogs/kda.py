import os

import aiohttp
import aiosqlite as sqa
import discord
from discord import app_commands
from discord.ext import commands
from utils.autocomplete import DiscordAttachedLeagueNames

# Match-V5 uses regional routing (europe), distinct from the platform routing
# (euw1) used for league entries.
REGION_ROUTE = "europe"
RANKED_SOLO_QUEUE_ID = 420
MATCH_COUNT = 20


class KdaCog(commands.Cog):
    """/kda <user> <league_name> — recent K/D summary for a tracked player."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")

    @app_commands.command(
        name="kda",
        description="Average K/D over a tracked player's recent ranked games",
    )
    @app_commands.describe(
        user="The discord account attached",
        league_name="The league account",
    )
    async def kda(
        self,
        ctx: discord.Interaction,
        user: discord.User,
        league_name: app_commands.Transform[str, DiscordAttachedLeagueNames],
    ):
        await ctx.response.defer()

        async with sqa.connect(self.bot.db_path) as db:
            row = await (
                await db.execute(
                    "SELECT puuid FROM league_players WHERE league_username = ?",
                    (league_name,),
                )
            ).fetchone()

        if not row or not row[0]:
            await ctx.followup.send(f"{league_name} not found in the database")
            return
        puuid = row[0]

        riot_key = os.environ.get("riot_key")
        if not riot_key:
            await ctx.followup.send("Riot API key not configured")
            return
        headers = {"X-Riot-Token": riot_key}

        kills_total = 0
        deaths_total = 0
        assists_total = 0
        wins = 0
        games_counted = 0

        async with aiohttp.ClientSession(headers=headers) as session:
            ids_url = (
                f"https://{REGION_ROUTE}.api.riotgames.com"
                f"/lol/match/v5/matches/by-puuid/{puuid}/ids"
            )
            params = {"queue": RANKED_SOLO_QUEUE_ID, "count": MATCH_COUNT}
            async with session.get(ids_url, params=params) as r:
                if r.status != 200:
                    body = await r.text()
                    await ctx.followup.send(
                        f"Riot match-IDs lookup failed: {r.status} {body[:200]}"
                    )
                    return
                match_ids = await r.json()

            if not match_ids:
                await ctx.followup.send(f"No recent ranked matches for {league_name}")
                return

            for match_id in match_ids:
                match_url = (
                    f"https://{REGION_ROUTE}.api.riotgames.com" f"/lol/match/v5/matches/{match_id}"
                )
                async with session.get(match_url) as r:
                    if r.status != 200:
                        continue
                    match = await r.json()
                for p in match["info"]["participants"]:
                    if p["puuid"] == puuid:
                        kills_total += p["kills"]
                        deaths_total += p["deaths"]
                        assists_total += p["assists"]
                        wins += 1 if p["win"] else 0
                        games_counted += 1
                        break

        if games_counted == 0:
            await ctx.followup.send(f"Couldn't compute K/D for {league_name}")
            return

        avg_k = kills_total / games_counted
        avg_d = deaths_total / games_counted
        avg_a = assists_total / games_counted
        if deaths_total > 0:
            kd = f"{kills_total / deaths_total:.2f}"
            kda = f"{(kills_total + assists_total) / deaths_total:.2f}"
        else:
            kd = "perfect (0 deaths)"
            kda = "perfect (0 deaths)"

        msg = (
            f"**{league_name}** — last {games_counted} ranked games "
            f"({wins}W / {games_counted - wins}L)\n"
            f"K/D: **{kd}**  |  KDA: **{kda}**\n"
            f"Per game avg: **{avg_k:.1f} / {avg_d:.1f} / {avg_a:.1f}**"
        )
        await ctx.followup.send(msg)


async def setup(bot: commands.Bot):
    await bot.add_cog(KdaCog(bot))
