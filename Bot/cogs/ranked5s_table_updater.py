"""Leaderboard for the 2026 limited-test "Ranked 5s" weekend queue.

Shares its rendering/history core with the solo-queue board
(cogs/league_table_updater.py) via utils/leaderboard.py, but:
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

import discord
from discord import app_commands
from discord.ext import commands, tasks
from main import MyDiscordBot
from utils import config, db, leaderboard
from utils.loop_restart import restart_loop_later
from utils.queue_windows import is_ranked5s_open, is_ranked5s_tracking, next_window_open
from utils.rank_sorting_class import Ranker
from utils.riot_client import RANKED_5S_QUEUE_ID, get_league_entries

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
        # Last match-derived fallback standings (used while Riot's league
        # API exposes no 5s ladder) — change detection for reposts.
        self.previous_fallback: list[dict] = []

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
        pinned = config.ranked5s_queue_type()
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
        if await leaderboard.record_history_snapshot(entry, QUEUE_KEY):
            self.bot.logging.info(
                f"5s history insert for {entry['summonerName']}: "
                f"{entry['tier']} {entry['rank']} {entry['leaguePoints']}lp "
                f"{entry['wins']}W/{entry['losses']}L"
            )

    async def _get_last_five_games(self, puuid: str) -> str:
        """Green/red squares for the player's last 5 games on the 5s ladder."""
        rows = await leaderboard.fetch_history_wl(puuid, QUEUE_KEY, 6)
        return leaderboard.build_last_five(rows)

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

    async def _render_board(self, sorted_results: list[dict]) -> list[str]:
        """Board blocks (header + one block per entry) for wipe_and_post."""
        header = "**Ranked 5s** — weekend ladder"
        if not is_ranked5s_open():
            next_open = next_window_open()
            if next_open is not None:
                header += f" (queue closed — opens {next_open.strftime('%A')})"
            else:
                header += " (queue closed — test window over)"

        # previous_positions feeds next cycle's arrow comparisons. Unlike
        # the solo board, apex entries here keep the " games " wording.
        entries, self.previous_positions = await leaderboard.render_board_entries(
            sorted_results,
            self.previous_positions,
            self.last_updated_by,
            self._get_last_five_games,
        )
        return [header] + entries

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

        if not ranked_dict:
            # League entries only exist for players who've completed
            # placements (RANKED_PREMADE_5x5, shipped by Riot 2026-07-05).
            # With nobody placed yet, the board is match-derived standings.
            await self._post_fallback_board(force)
            return

        changed = (ranked_dict != self.previous_ranks) or (not self.previous_ranks)
        self.previous_ranks = ranked_dict

        # Hybrid: tracked players with 5s games but no entry yet (still in
        # placements) appear in a compact section under the ranked board —
        # otherwise they vanish the moment the first friend gets a rank.
        standings = await self._fetch_match_standings()
        ranked_puuids = {entry["puuid"] for entry in ranked_dict.values()}
        unplaced = [s for s in standings if s["puuid"] not in ranked_puuids]
        unplaced_changed = unplaced != self.previous_fallback
        self.previous_fallback = unplaced

        if not (changed or unplaced_changed or force):
            return

        channel = await self._get_board_channel()
        if channel is None:
            return

        self.bot.logging.info("Posting Ranked 5s board")
        sorted_results = sorted(ranked_dict.values(), key=lambda d: d["sorted_rank"], reverse=True)
        blocks = await self._render_board(sorted_results)
        if unplaced:
            blocks.append("-# In placements (no rank yet) — match record:")
            for entry in unplaced:
                blocks.append(
                    f"{entry['summonerName']} - <@{entry['user_id']}>: "
                    f"{entry['wins']}W / {entry['losses']}L"
                )
        # Ping-free board: silent sends, no mentions resolved.
        await leaderboard.wipe_and_post(channel, blocks, self.bot.logging)

    # ------------------------------------------------- match-derived fallback

    async def _fetch_match_standings(self) -> list[dict]:
        """Standings from our own queue-710 match results (match_stats).

        Wins/losses/winrate per tracked player, best record first. The
        ingestion stream records every 5s game (cogs/backfill.py fetches
        queue 710 alongside solo), so this is complete even though no
        rank/LP exists to show.
        """
        rows = await db.fetchall(
            """SELECT lp.puuid,
                    lp.league_username,
                    lp.discord_user_id,
                    COUNT(*) FILTER (WHERE ms.win = 1) AS wins,
                    COUNT(*) AS games
                FROM match_stats ms
                    JOIN league_players lp ON lp.puuid = ms.puuid
                WHERE ms.queue_id = %s
                GROUP BY lp.puuid, lp.league_username, lp.discord_user_id""",
            (RANKED_5S_QUEUE_ID,),
        )
        standings = [
            {
                "puuid": puuid,
                "summonerName": name,
                "user_id": user_id,
                "wins": wins,
                "losses": games - wins,
                "GamesPlayed": games,
                "WinRate": (wins / games) * 100 if games else 0.0,
            }
            for puuid, name, user_id, wins, games in rows
        ]
        standings.sort(key=lambda d: (d["wins"], d["WinRate"], d["GamesPlayed"]), reverse=True)
        return standings

    async def _post_fallback_board(self, force: bool) -> None:
        standings = await self._fetch_match_standings()
        changed = standings != self.previous_fallback
        self.previous_fallback = standings
        if not standings or not (changed or force):
            return

        channel = await self._get_board_channel()
        if channel is None:
            return

        header = "**Ranked 5s** — weekend ladder"
        if not is_ranked5s_open():
            next_open = next_window_open()
            if next_open is not None:
                header += f" (queue closed — opens {next_open.strftime('%A')})"
            else:
                header += " (queue closed — test window over)"
        note = (
            "-# Riot's API doesn't expose 5s ranks yet — standings from "
            "match results, sorted by wins."
        )

        lines = [header, note]
        for index, entry in enumerate(standings):
            wins_flags = await db.fetchall(
                "SELECT win FROM match_stats "
                "WHERE puuid = %s AND queue_id = %s "
                "ORDER BY game_start DESC LIMIT 5",
                (entry["puuid"], RANKED_5S_QUEUE_ID),
            )
            last_five = leaderboard.build_last_five_from_wins([w[0] for w in wins_flags])
            lines.append(
                f"{index + 1}. {entry['summonerName']} - <@{entry['user_id']}>\n"
                f"Record: {entry['wins']}W / {entry['losses']}L "
                f"({entry['WinRate']:.2f}% winrate)\n"
                f"Last 5: {last_five}\n"
            )

        self.bot.logging.info("Posting Ranked 5s board (match-derived fallback)")
        await leaderboard.wipe_and_post(channel, lines, self.bot.logging)

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
        pinned = config.ranked5s_queue_type()
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
        row = await db.fetchone(
            "SELECT COUNT(DISTINCT ms.puuid) FROM match_stats ms "
            "JOIN league_players lp ON lp.puuid = ms.puuid WHERE ms.queue_id = %s",
            (RANKED_5S_QUEUE_ID,),
        )
        match_count = row[0] if row else 0
        next_open = next_window_open()
        next_open_line = (
            next_open.strftime("%A %Y-%m-%d %H:%M %Z") if next_open else "never (test over)"
        )
        await ctx.followup.send(
            f"Queue open: {'yes' if is_ranked5s_open() else 'no'}\n"
            f"Tracking: {'yes' if is_ranked5s_tracking() else 'no'}\n"
            f"Next window opens: {next_open_line}\n"
            f"{queue_type_line}\n"
            f"Players with rank entries: {ladder_count} "
            f"(Riot API exposes no 5s ladder yet — board falls back to match results)\n"
            f"Players with recorded 5s matches: {match_count}",
            ephemeral=True,
        )


async def setup(bot: MyDiscordBot):
    await bot.add_cog(Ranked5sBoard(bot))
