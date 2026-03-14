import re
from typing import Iterable
import aiosqlite as sqa

from typing import Any, Iterable

from discord import CategoryChannel

import logging

log = logging.getLogger(__name__)

###============================================================================

class DButils:
    def __init__(self, db_path):
        self.db_path = db_path

    EXECUTE = "execute"
    EXECUTE_MANY = "executemany"
    EXECUTE_FETCH = "execute_fetchall"

    async def execute_query(self, method_name, statement: str, params: Iterable[Any]):
        print_statement = " ".join(line.strip() for line in statement.splitlines())
        log.verbose(f"DB request: {method_name}, {print_statement}, {params}")
        async with sqa.connect(self.db_path) as db:
            query_func = getattr(db, method_name)
            result = await query_func(statement, params)
            if db.in_transaction:
                await db.commit()
            return result   # This will only return on a fetch all

###============================================================================
# Discord users

    async def add_members_to_db(self, members) -> None:
        statement = "REPLACE INTO users (user_id, nickname, discord_tag) VALUES (?, ?, ?)"
        member_data = [
            (member.id, member.display_name, member.name)
            for member in members
        ]
        await self.execute_query(self.EXECUTE_MANY, statement, member_data)

    async def get_members_and_puuid(self) -> list[tuple[Any]]:
        statement = """SELECT puuid,
                        IIF(nickname = '', discord_tag, nickname),
                        discord_user_id
                    FROM (
                        SELECT * FROM league_players
                        LEFT JOIN users ON user_id = discord_user_id
                    )"""
        result = await self.execute_query(self.EXECUTE_FETCH, statement, ())
        return result

###============================================================================
# league_players

    async def get_usernames_and_tags(self) -> list[tuple[Any]]:
        statement = "SELECT league_username, tag FROM league_players"
        result = await self.execute_query(self.EXECUTE_FETCH, statement, ())
        return result

    async def get_id_from_username(self, name) -> str:
        statement = "SELECT puuid FROM league_players WHERE league_username = ?"
        result = await self.execute_query(self.EXECUTE_FETCH, statement, (name,))
        return result[0][0]

    async def get_username_from_id(self, puuid) -> str:
        statement = "SELECT league_username FROM league_players WHERE puuid = ?"
        result = await self.execute_query(self.EXECUTE_FETCH, statement, (puuid,))
        return result[0][0]

    async def get_username_from_discord_id(self, discord_id) -> list[str]:
        statement = "SELECT league_username FROM league_players WHERE discord_user_id = ?"
        usernames = await self.execute_query(self.EXECUTE_FETCH, statement, (discord_id,))
        return [
            str(name[0]) for name in usernames
        ]


    async def updated_username(self, name, puuid) -> None:
        statement = "UPDATE league_players SET league_username = ? WHERE puuid = ?"
        await self.execute_query(self.EXECUTE, statement, (name, puuid))

    async def add_player(self, discord_id, puuid, name, tag) -> None:
        statement = """REPLACE INTO league_players (
                            discord_user_id,
                            puuid,
                            league_username,
                            tag
                        )
                        VALUES (?, ?, ?, ?)"""  #leagueId removed
        await self.execute_query(self.EXECUTE, statement, (discord_id, puuid, name, tag))


    async def delete_player_by_username(self, name) -> None:
        statement = "DELETE FROM league_players WHERE league_username = ?"
        await self.execute_query(self.EXECUTE, statement, (name,))

    async def delete_player_by_discord_id(self, discord_id) -> None:
        statement = "DELETE FROM league_players WHERE discord_user_id = ?"
        await self.execute_query(self.EXECUTE, statement, (discord_id,))

###============================================================================
# League_history

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

###============================================================================

    async def add_channels_to_db(self, channels) -> None:
        statement = "REPLACE INTO discord_channels (channel_id, name, type) VALUES (?, ?, ?)"
        channel_data = [
            (int(channel.id), str(channel.name), str(channel.type))
            for channel in channels if not isinstance(channel, CategoryChannel)
        ]
        await self.execute_query(self.EXECUTE_MANY, statement, channel_data)


    async def get_channel_from_name(self, name: str) -> str:
        statement = "SELECT channel_id FROM discord_channels WHERE name = ?"
        result = await self.execute_query(self.EXECUTE_FETCH, statement, (name,))
        return result[0][0]