import re
from typing import Iterable
import aiosqlite as sqa

from typing import Any, Iterable

from discord import CategoryChannel
###============================================================================

class DButils:
    def __init__(self, db_path):
        self.db_path = db_path

    EXECUTE = "execute"
    EXECUTE_MANY = "executemany"
    EXECUTE_FETCH = "execute_fetchall"

    async def execute_query(self, method_name, statement: str, params: Iterable[Any]):
        async with sqa.connect(self.db_path) as db:
            query_func = getattr(db, method_name)
            result = await query_func(statement, params)
            if db.in_transaction:
                await db.commit()
            return result   # This will only return on a fetch all

###============================================================================

    async def add_members_to_db(self, members) -> None:
        statement = "REPLACE INTO users (user_id, nickname, discord_tag) VALUES (?, ?, ?)"
        member_data = [
            (member.id, member.display_name, member.name)
            for member in members
        ]
        await self.execute_query(self.EXECUTE_MANY, statement, member_data)


    async def add_channels_to_db(self, channels) -> None:
        statement = "REPLACE INTO discord_channels (channel_id, name, type) VALUES (?, ?, ?)"
        channel_data = [
            (channel.id, channel.name, channel.type)
            for channel in channels if isinstance(channel, CategoryChannel)
        ]
        await self.execute_query(self.EXECUTE_MANY, statement, channel_data)


    async def get_id_from_username(self, name) -> str:
        statement = "SELECT puuid FROM league_players WHERE league_username = ?"
        return await self.execute_query(self.EXECUTE_FETCH, statement, name)[0][0]


    async def get_username_from_id(self, puuid) -> str:
        statement = "SELECT league_username FROM league_players WHERE puuid = ?"
        return await self.execute_query(self.EXECUTE_FETCH, statement, puuid)[0][0]


    async def get_recent(self, player, number) -> Iterable[tuple]:
        if player == "":
            sql = (
                "SELECT * FROM league_history ORDER BY timestamp DESC LIMIT ?",
                (number,),
            )
        else:
            sql = (
                "SELECT * FROM league_history WHERE puuid = (SELECT puuid FROM league_players WHERE league_username = ?) ORDER BY timestamp DESC LIMIT ?",
                (player, number),
            )
        async with sqa.connect(self.bot.db_path) as connection:
            recent_matches = await connection.execute_fetchall(*sql)
        return recent_matches
