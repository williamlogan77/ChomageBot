import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite as sqa


class LeagueUsers(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="add_player",
                          description="Add user to the table")
    async def temp_name(self, ctx: discord.Interaction, league_name: str,
                        user: discord.User):

        puuid = self.bot.lolapi.summoner.by_name("EUW1", league_name)["puuid"]
        print(puuid)
        async with sqa.connect(self.bot.db_path) as db:  # type: ignore
            await db.execute(
                "REPLACE INTO league_players (discord_user_id, puuid, league_username) VALUES (?, ?, ?)",
                (ctx.user.id, puuid, league_name))
            await db.commit()
        print("put into db")
        return


async def setup(bot: commands.Bot):
    await bot.add_cog(LeagueUsers(bot))
