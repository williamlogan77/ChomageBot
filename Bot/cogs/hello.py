import discord
from discord.ext import commands
from discord import app_commands
import time


class dealwithconnection(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    # async def on_voice_state_update(self, member, before, after):
    #     if after.afk:
    #         await member.move_to(None)
    #     cur = self.bot.connection.cursor()
    #     if after.name is not None:
    #         event = "leave"
    #     elif after.name is None:
    #         event = "moved"
    #
    #     await cur.execute(f"INSERT INTO discord_events (timestamp, user_id, channel_id, type) VALUES (?, ?)",
    #                       (time.time(), member.id, after.id, "Join"))


async def setup(bot: commands.Bot):
    await bot.add_cog(dealwithconnection(bot))
