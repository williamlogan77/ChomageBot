import discord
from discord import app_commands
from discord.ext import commands
from utils.autocomplete import DiscordAttachedLeagueNames
from utils.db import aconnect
from utils.riot_stats import fetch_recent_kd

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

        async with aconnect(self.bot.db_path) as db:
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

        kills, deaths, assists, wins, games = await fetch_recent_kd(puuid, count=MATCH_COUNT)

        if games == 0:
            await ctx.followup.send(f"Couldn't fetch ranked matches for {league_name}")
            return

        avg_k = kills / games
        avg_d = deaths / games
        avg_a = assists / games
        if deaths > 0:
            kd = f"{kills / deaths:.2f}"
            kda = f"{(kills + assists) / deaths:.2f}"
        else:
            kd = "perfect (0 deaths)"
            kda = "perfect (0 deaths)"

        msg = (
            f"**{league_name}** — last {games} ranked games "
            f"({wins}W / {games - wins}L)\n"
            f"K/D: **{kd}**  |  KDA: **{kda}**\n"
            f"Per game avg: **{avg_k:.1f} / {avg_d:.1f} / {avg_a:.1f}**"
        )
        await ctx.followup.send(msg)


async def setup(bot: commands.Bot):
    await bot.add_cog(KdaCog(bot))
