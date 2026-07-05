"""Leaderboard for the 2026 limited-test "Ranked 5s" weekend queue.

Mirrors cogs/league_table_updater.py (the solo-queue board) but:
  - only polls while the queue window is live (utils/queue_windows.py) —
    Fri/Sat/Sun 20:00-01:00 Europe/Paris plus a 2h post-close tail;
  - writes/reads league_history rows tagged queue = 'RANKED_5S';
  - reads its board channel from bot_config (key ranked5s_channel_id)
    instead of a hardcoded channel ID;
  - the league-v4 queueType string for Ranked 5s is not documented yet,
    so the entry picker discovers it at runtime (see _pick_5s_entry) and
    can be pinned via the ranked5s_queue_type env var.

Deliberate differences from the solo board:
  - no minimum-games filter for now — the weekend ladder is small;
  - no loss-streak ping — it's a premade-5 queue, less mock-worthy;
  - no summoner-name re-sync — the solo cog already refreshes
    league_players.league_username every 120s for the same players.

get_league_entries() responses are TTL-cached (100s) in utils/riot_client,
so this loop reuses the solo board's fetches: near-zero extra API spend.
"""

import datetime as dt
import os

import discord
from discord import app_commands
from discord.ext import commands, tasks
from main import MyDiscordBot
from utils import db
from utils.loop_restart import restart_loop_later
from utils.queue_windows import is_ranked5s_open, is_ranked5s_tracking, next_window_open
from utils.rank_sorting_class import Ranker
from utils.riot_client import get_league_entries

# Canonical internal tag for Ranked 5s rows in league_history.queue.
# Decoupled from whatever league-v4 queueType string Riot ends up using.
QUEUE_KEY = "RANKED_5S"

# bot_config key holding the board channel ID (set via /set_ranked5s_channel).
CHANNEL_CONFIG_KEY = "ranked5s_channel_id"

# Env var to pin the league-v4 queueType string once discovered.
QUEUE_TYPE_ENV = "ranked5s_queue_type"

# Every documented league-v4 queueType that is NOT Ranked 5s. The
# heuristic picker treats any other RANKED_* string as the 5s ladder.
KNOWN_NON_5S = {
    "RANKED_SOLO_5x5",
    "RANKED_FLEX_SR",
    "RANKED_FLEX_TT",
    "RANKED_TFT",
    "RANKED_TFT_TURBO",
    "RANKED_TFT_DOUBLE_UP",
}


class Ranked5sBoard(commands.Cog):
    def __init__(self, bot: MyDiscordBot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        self.post_ranks_5s.start()
        self.previous_ranks: dict = {}
        self.last_updated_by: list[str] = []
        self.previous_positions: dict[str, int] = {}
        # Watchdog input, same contract as FetchFromRiot.post_ranks_last_fired
        # (cogs/heartbeat.py reloads us if this goes stale).
        self.post_ranks_5s_last_fired: dt.datetime | None = None
        # Log each unknown queueType / the missing-channel notice once per
        # cog instance, not once per loop tick.
        self._seen_queue_types: set[str] = set()
        self._warned_no_channel = False

    def cog_unload(self) -> None:
        # discord.py does NOT cancel @tasks.loop tasks on cog unload —
        # without this, every hot reload (auto_reload / heartbeat) leaves
        # the old instance's loop running next to the new one's.
        self.post_ranks_5s.cancel()

    # ------------------------------------------------------------------ fetch

    async def _fetch_tracked_players(self) -> list[tuple]:
        """(puuid, league_username, display_name, discord_user_id) per player."""
        return await db.fetchall(
            """SELECT lp.puuid,
                    lp.league_username,
                    CASE
                        WHEN COALESCE(u.nickname, '') = '' THEN u.discord_tag
                        ELSE u.nickname
                    END,
                    lp.discord_user_id
                FROM league_players lp
                    LEFT JOIN users u ON u.user_id = lp.discord_user_id
                WHERE lp.puuid IS NOT NULL"""
        )

    def _pick_5s_entry(self, entries: list[dict]) -> dict | None:
        """Pick the Ranked 5s entry out of a player's league-v4 entries.

        Riot has not documented the queueType string for Ranked 5s yet.
        If the ranked5s_queue_type env var is set we match it exactly;
        otherwise any RANKED_* queueType we don't recognise is assumed to
        be the 5s ladder, and its string is logged (once per process) so
        it can be pinned in .env.
        """
        pinned = os.environ.get(QUEUE_TYPE_ENV)
        for entry in entries:
            queue_type = entry.get("queueType", "")
            if pinned:
                if queue_type == pinned:
                    return entry
                continue
            if "RANKED" in queue_type and queue_type not in KNOWN_NON_5S:
                if queue_type not in self._seen_queue_types:
                    self._seen_queue_types.add(queue_type)
                    self.bot.logging.warning(
                        f"discovered candidate Ranked 5s queueType '{queue_type}' — "
                        f"pin it by setting {QUEUE_TYPE_ENV}={queue_type} in .env"
                    )
                return entry
        return None

    async def _fetch_5s_ranks(self, players: list[tuple]) -> dict:
        """Current Ranked 5s standings keyed by league_username."""
        ranks: dict = {}
        seen: set[str] = set()
        for puuid, league_name, display_name, discord_user_id in players:
            if puuid in seen:
                continue
            seen.add(puuid)

            entries = await get_league_entries(puuid)
            if entries is None:
                self.bot.logging.error(f"Failed to fetch 5s rank for {display_name}")
                continue

            entry = self._pick_5s_entry(entries)
            if entry is None:
                continue  # not on the 5s ladder (yet)

            games = entry["wins"] + entry["losses"]
            if games <= 0:
                continue  # placement edge case: no games, nothing to rank

            try:
                sorted_rank = Ranker(entry["tier"], entry["rank"], entry["leaguePoints"])
            except KeyError:
                # Defence against a tier/division string Ranker doesn't know
                # (new Riot tier, UNRANKED oddity) — skip the player rather
                # than crash the whole board.
                self.bot.logging.warning(
                    f"Unsortable 5s tier/rank {entry['tier']}/{entry['rank']} "
                    f"for {league_name}, skipping"
                )
                continue

            entry = dict(entry)
            entry["puuid"] = puuid
            entry["user_id"] = discord_user_id
            entry["summonerName"] = league_name
            entry["sorted_rank"] = sorted_rank
            entry["GamesPlayed"] = games
            entry["WinRate"] = (entry["wins"] / games) * 100
            ranks[league_name] = entry
        return ranks

    # ---------------------------------------------------------------- history

    async def _record_history(self, entry: dict) -> None:
        """Insert a league_history snapshot when wins/losses changed.

        Ranked 5s rows are always keyed by the real puuid — the legacy
        encrypted-summoner-ID dual-key union the solo board needs for its
        pre-2024 rows does not apply here, so plain puuid equality is enough.
        """
        last = await db.fetchone(
            "SELECT wins, losses FROM league_history "
            "WHERE puuid = %s AND queue = %s "
            "ORDER BY id DESC LIMIT 1",
            (entry["puuid"], QUEUE_KEY),
        )
        if last is not None and last == (entry["wins"], entry["losses"]):
            return
        self.bot.logging.info(
            f"5s history insert for {entry['summonerName']}: "
            f"{entry['tier']} {entry['rank']} {entry['leaguePoints']}lp "
            f"{entry['wins']}W/{entry['losses']}L"
        )
        await db.execute(
            "INSERT INTO league_history (puuid, lp, division, tier, wins, losses, queue) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                entry["puuid"],
                int(entry["leaguePoints"]),
                entry["rank"],
                entry["tier"],
                int(entry["wins"]),
                int(entry["losses"]),
                QUEUE_KEY,
            ),
        )

    async def _get_last_five_games(self, puuid: str) -> str:
        """Green/red squares for the player's last 5 games on the 5s ladder."""
        rows = await db.fetchall(
            "SELECT wins, losses FROM league_history "
            "WHERE puuid = %s AND queue = %s "
            "AND wins IS NOT NULL AND losses IS NOT NULL "
            "ORDER BY id DESC LIMIT 6",
            (puuid, QUEUE_KEY),
        )
        if not rows:
            return ""
        sequence = []
        for i in range(len(rows) - 1):
            newer_w, newer_l = rows[i]
            older_w, older_l = rows[i + 1]
            delta_w = max(newer_w - older_w, 0)
            delta_l = max(newer_l - older_l, 0)
            sequence.extend(["\U0001f7e9"] * delta_w + ["\U0001f7e5"] * delta_l)
        # Implicit (0,0) baseline — always valid on this ladder while the
        # oldest stored row is within the first few games of the test.
        oldest_w, oldest_l = rows[-1]
        if oldest_w + oldest_l <= 5:
            sequence.extend(["\U0001f7e9"] * oldest_w + ["\U0001f7e5"] * oldest_l)
        # Cap to last 5; reverse so the newest game is on the right.
        return "".join(reversed(sequence[:5]))

    # ---------------------------------------------------------------- channel

    async def _get_board_channel(self) -> discord.abc.Messageable | None:
        """Resolve the board channel from bot_config, or None if unset."""
        row = await db.fetchone(
            "SELECT value FROM bot_config WHERE key = %s", (CHANNEL_CONFIG_KEY,)
        )
        if row is None:
            if not self._warned_no_channel:
                self._warned_no_channel = True
                self.bot.logging.info(
                    "Ranked 5s board channel not configured — run /set_ranked5s_channel "
                    "(history is still being recorded)"
                )
            return None
        channel_id = int(row[0])
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException as exc:
                self.bot.logging.error(f"Ranked 5s channel {channel_id} unavailable: {exc!r}")
                return None
        return channel

    # ----------------------------------------------------------------- render

    async def _render_board(self, sorted_results: list[dict]) -> str:
        header = "**Ranked 5s** — weekend ladder"
        if not is_ranked5s_open():
            next_open = next_window_open()
            if next_open is not None:
                header += f" (queue closed — opens {next_open.strftime('%A')})"
            else:
                header += " (queue closed — test window over)"

        output_list = [header]
        current_positions: dict[str, int] = {}
        for index, posting in enumerate(sorted_results):
            current_pos = index + 1
            current_positions[posting["summonerName"]] = current_pos
            prev_pos = self.previous_positions.get(posting["summonerName"])
            # \U00002B06\U0000FE0F = up arrow, \U00002B07\U0000FE0F = down arrow
            if prev_pos is None or prev_pos == current_pos:
                position_arrow = ""
            elif current_pos < prev_pos:
                position_arrow = "\U00002b06\U0000fe0f "
            else:
                position_arrow = "\U00002b07\U0000fe0f "

            updated_flag = " \U0001f6a9" if posting["summonerName"] in self.last_updated_by else ""
            last_five = await self._get_last_five_games(posting["puuid"])
            last_five_line = f"Last 5: {last_five}\n" if last_five else ""
            # Apex tiers have no real division (league-v4 reports rank "I").
            if posting["tier"].title() in ("Master", "Grandmaster", "Challenger"):
                rank_line = f"Rank: {posting['tier'].title()} {posting['leaguePoints']}lp"
            else:
                rank_line = (
                    f"Rank: {posting['tier'].title()} {posting['rank']} "
                    f"{posting['leaguePoints']}lp"
                )
            post = (
                position_arrow
                + str(current_pos)
                + ". "
                + posting["summonerName"]
                + f" - <@{posting['user_id']}>"
                + updated_flag
                + "\n"
                + rank_line
                + "\n"
                + "Played: "
                + str(posting["GamesPlayed"])
                + " games with a "
                + str("{:.2f}".format(posting["WinRate"]))
                + "% winrate"
                + "\n"
                + last_five_line
            )
            output_list.append(post)

        # Remember positions for next cycle's arrow comparisons.
        self.previous_positions = current_positions
        return "\n".join(output_list)

    # ------------------------------------------------------------------- loop

    async def _run_cycle(self, force: bool = False) -> None:
        """One full fetch -> history -> post pass. ``force`` reposts even
        when nothing changed (used by /refresh_ranked5s)."""
        players = await self._fetch_tracked_players()
        ranked_dict = await self._fetch_5s_ranks(players)

        # Record history + collect the flag list even if no channel is set.
        updated_users: list[str] = []
        for user, entry in ranked_dict.items():
            previous = self.previous_ranks.get(user)
            if previous is not None and entry["leaguePoints"] != previous["leaguePoints"]:
                updated_users.append(user)
            await self._record_history(entry)
        if updated_users:
            self.last_updated_by = updated_users

        changed = (ranked_dict != self.previous_ranks) or (not self.previous_ranks)
        self.previous_ranks = ranked_dict
        if not (changed or force):
            return
        if not ranked_dict:
            # Nobody placed on the 5s ladder yet — leave the channel alone.
            return

        channel = await self._get_board_channel()
        if channel is None:
            return

        self.bot.logging.info("Posting Ranked 5s board")
        sorted_results = sorted(ranked_dict.values(), key=lambda d: d["sorted_rank"], reverse=True)
        to_send = await self._render_board(sorted_results)

        try:
            async for message in channel.history():
                await message.delete()
        except discord.errors.Forbidden:
            self.bot.logging.warning("Missing permissions to delete messages, skipping cleanup")

        # Ping-free board: silent send, no mentions resolved.
        await channel.send(
            to_send,
            silent=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tasks.loop(seconds=120)
    async def post_ranks_5s(self):
        await self.bot.wait_until_ready()
        # Watchdog input: set on EVERY tick (including out-of-window skips)
        # so the heartbeat cog can tell "loop frozen" apart from "queue shut".
        self.post_ranks_5s_last_fired = dt.datetime.now()
        if not is_ranked5s_tracking():
            self.bot.logging.debug("Ranked 5s queue not in tracking window, skipping tick")
            return
        await self._run_cycle()

    @post_ranks_5s.error
    async def post_ranks_5s_error(self, exc: BaseException) -> None:
        """Auto-restart post_ranks_5s on unhandled error.

        Default @tasks.loop behaviour on exception is to log + stop the
        loop, which would freeze the board until manual recovery. The
        restart must run detached — this callback executes inside the
        dying loop task, where is_running() is still True (see
        utils/loop_restart.py).
        """
        self.bot.logging.error(f"post_ranks_5s errored: {exc!r}, restarting in 60s")
        restart_loop_later(
            self.post_ranks_5s,
            name="post_ranks_5s",
            log=self.bot.logging,
            still_active=lambda: self.bot.get_cog("Ranked5sBoard") is self,
        )

    # --------------------------------------------------------------- commands

    @app_commands.command(
        name="set_ranked5s_channel",
        description="Set the channel the Ranked 5s leaderboard posts into",
    )
    @app_commands.describe(channel="The channel for the Ranked 5s board")
    async def set_ranked5s_channel(self, ctx: discord.Interaction, channel: discord.TextChannel):
        await ctx.response.defer(ephemeral=True)
        await db.execute(
            "INSERT INTO bot_config (key, value, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (CHANNEL_CONFIG_KEY, str(channel.id)),
        )
        self._warned_no_channel = False  # re-arm the unset-channel notice
        self.bot.logging.info(f"Ranked 5s board channel set to {channel.id} by {ctx.user}")
        await ctx.followup.send(f"Ranked 5s board will post in {channel.mention}", ephemeral=True)

    @app_commands.command(
        name="refresh_ranked5s",
        description="Manually refresh the Ranked 5s leaderboard",
    )
    async def refresh_ranked5s(self, ctx: discord.Interaction):
        await ctx.response.defer(ephemeral=True)
        msg = await ctx.followup.send("Refreshing Ranked 5s board...", wait=True, ephemeral=True)
        # force=True bypasses the tracking gate so the board can be smoke-
        # tested outside the weekend window too.
        await self._run_cycle(force=True)
        note = "" if is_ranked5s_tracking() else " (queue currently outside its window)"
        await msg.edit(content=f"Refreshed the Ranked 5s leaderboard{note}")

    @app_commands.command(
        name="ranked5s_status",
        description="Show Ranked 5s queue window and tracking status",
    )
    async def ranked5s_status(self, ctx: discord.Interaction):
        await ctx.response.defer(ephemeral=True)
        pinned = os.environ.get(QUEUE_TYPE_ENV)
        if pinned:
            queue_type_line = f"queueType: pinned to `{pinned}`"
        elif self._seen_queue_types:
            queue_type_line = f"queueType: discovered {sorted(self._seen_queue_types)} (not pinned)"
        else:
            queue_type_line = "queueType: not discovered yet"
        row = await db.fetchone(
            "SELECT COUNT(DISTINCT puuid) FROM league_history WHERE queue = %s",
            (QUEUE_KEY,),
        )
        ladder_count = row[0] if row else 0
        next_open = next_window_open()
        next_open_line = (
            next_open.strftime("%A %Y-%m-%d %H:%M %Z") if next_open else "never (test over)"
        )
        await ctx.followup.send(
            f"Queue open: {'yes' if is_ranked5s_open() else 'no'}\n"
            f"Tracking: {'yes' if is_ranked5s_tracking() else 'no'}\n"
            f"Next window opens: {next_open_line}\n"
            f"{queue_type_line}\n"
            f"Players on ladder: {ladder_count}",
            ephemeral=True,
        )


async def setup(bot: MyDiscordBot):
    await bot.add_cog(Ranked5sBoard(bot))
