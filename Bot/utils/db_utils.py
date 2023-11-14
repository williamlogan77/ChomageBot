import re
from typing import Iterable
import aiosqlite as sqa


class DButils:
    def __init__(self, db_path):
        self.db_path = db_path

    async def get_id_from_username(self, name) -> str:
        async with sqa.connect(self.db_path) as connection:
            puuid = await connection.execute_fetchall(
                "SELECT puuid FROM league_players WHERE league_username = ?", (name,)
            )
        return puuid[0][0]

    async def get_username_from_id(self, puuid) -> str:
        async with sqa.connect(self.db_path) as connection:
            name = await connection.execute_fetchall(
                "SELECT league_username FROM league_players WHERE puuid = ?", (puuid)
            )
        return name[0][0]

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
