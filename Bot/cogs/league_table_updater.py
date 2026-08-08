import asyncio
import datetime as dt
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks
from main import MyDiscordBot
from utils import db, leaderboard, seasons
from utils.loop_restart import restart_loop_later
from utils.rank_sorting_class import Ranker
from utils.riot_client import (
    RANKED_SOLO_QUEUE_ID,
    get_account_by_puuid,
    get_league_entries,
)
from utils.riot_stats import fetch_recent_kd

# Want to fetch ranks to post from the database
# want to fetch from rito every 30s

# Channel + threshold for the loss-streak callout. When a tracked player's
# most-recent N consecutive games are all losses, the bot pings them once
# in this channel. The ping rearms after they win again.
STREAK_PING_CHANNEL = 667751882260742167  # #general
STREAK_THRESHOLD = 7

# league_history.queue tag for this board's rows (also the column default).
SOLO_QUEUE = "RANKED_SOLO_5x5"

# Entries only change when a ranked game ENDS (plus rare apex decay), so
# most cycles most players can be served from the stale entries cache.
# A full fresh sweep this often catches decay and anything the change
# signals miss.
ENTRIES_FULL_SWEEP_SECONDS = 3600


class FetchFromRiot(commands.Cog):
    def __init__(self, bot: MyDiscordBot):
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")
        self.post_ranks.start()
        # self.fetch_ranks_from_riot.start()
        self.previous_ranks = {}
        self.min_games_played = 0
        self.streak_pinged: set[str] = set()
        # Arrow state: previous_positions is the pre-reshuffle baseline the
        # arrows diff against, last_positions is last cycle's standings —
        # see leaderboard.render_board_entries.
        self.previous_positions: dict[str, int] = {}
        self.last_positions: dict[str, int] = {}
        # Blocks of the last posted board — the post gate. In-memory, so
        # the first cycle after a hot reload reposts once redundantly.
        self.previous_output: list[str] | None = None
        self.post_ranks_last_fired: dt.datetime | None = None

        self.ranked_dict: dict | None = None
        # Entries-fetch gating state: 0.0 forces a full sweep on the first
        # cycle after every (re)load, which also warms the stale cache.
        self._entries_sweep_at = 0.0
        # (puuid, game_id) pairs — game-level, not just presence: a player
        # who finished game A and is already loading game B never leaves
        # the live set, but pair (puuid, A) vanishing still marks a finish.
        self._prev_live: set[tuple[str, int]] = set()
        # Finishes announced directly by the live-games cog's fast path —
        # authoritative and state-independent, so a sweep or reload wiping
        # _prev_live between transition and fetch can't lose the signal.
        self._pending_finished: set[str] = set()
        self._post_lock = asyncio.Lock()

    def cog_unload(self) -> None:
        # discord.py does NOT cancel @tasks.loop tasks on cog unload —
        # without this, every hot reload (auto_reload / heartbeat) leaves
        # the old instance's loop running next to the new one's.
        self.post_ranks.cancel()

    async def _entries_skip_set(self) -> tuple[set[str], set[str]]:
        """Players whose entries can't have changed since the last fetch.

        Returns ``(skip, force_fresh)`` for fetch_users_rank.

        LP only moves when a ranked game ends, so a player is skippable
        (serve the stale entries cache) unless a game of theirs just
        finished. "Just finished" is detected two ways, belt and braces:
        they vanished from live_games since the previous cycle
        (spectator saw the game end), or a match_stats row whose game
        ENDED recently exists (the stream ingested the finished game —
        keyed on game_start + duration, NOT game_start: a 35-minute game
        starts far outside any recency window by the time it's ingested,
        so filtering on start time would miss almost every real game).
        Players mid-game stay skippable — their LP is frozen until the
        game ends. The hourly full sweep catches apex decay, queue-dodge
        LP penalties (no game is ever created for those) and anything
        the signals miss; every failure path fails open to fetching
        everyone.

        ``force_fresh`` (the pair-diff/fast-path finishers) must BYPASS
        the entries TTL cache, not merely skip the stale allowance: a
        game ending seconds after a normal fetch leaves a sub-TTL cache
        entry holding pre-game-end LP, and serving it would defeat the
        very refresh the fast path triggered. The ingest-lagged
        fresh_match signal doesn't need this — by the time it fires, any
        sub-TTL cache entry postdates the game end.
        """
        # Finish signals are collected BEFORE any branch decision so no
        # path — sweep included — can consume and then discard them: a
        # finisher whose signal lands on a sweep cycle must still bypass
        # the TTL cache, or the sweep's fetch-all serves their ≤130s-old
        # pre-game-end entry.
        pending = self._pending_finished
        self._pending_finished = set()
        live_ok = True
        try:
            live = {
                (row[0], row[1])
                for row in await db.fetchall(
                    "SELECT DISTINCT puuid, game_id FROM live_games "
                    "WHERE seen_at > now() - interval '6 minutes'"
                )
            }
            # Pair-level diff: (puuid, game_id) disappearing marks a finish
            # even when the player is already in their NEXT game.
            just_finished = {puuid for puuid, _ in self._prev_live - live}
            self._prev_live = live
        except Exception:
            # live_games unavailable — the pair diff contributes nothing
            # this cycle, but fast-path announcements are authoritative
            # and must still force a cache bypass.
            just_finished = set()
            live_ok = False
        force_fresh = just_finished | pending

        if time.monotonic() - self._entries_sweep_at >= ENTRIES_FULL_SWEEP_SECONDS:
            self._entries_sweep_at = time.monotonic()
            return set(), force_fresh
        if not live_ok:
            # Without live tracking the gate can't see games end, so
            # skipping anyone would serve stale LP until the (slower)
            # ingest signal. Fail open: fetch everyone, as before gating.
            self.bot.logging.warning("live_games unavailable — entries gating fails open")
            return set(), force_fresh
        try:
            # queue_id filter: since the capture-everything pass the
            # stream ingests EVERY queue, but only ranked games (solo 420,
            # flex 440, 5s 710) can move league entries — a fresh ARAM
            # must not burn a fresh entries fetch.
            fresh_match = {
                row[0]
                for row in await db.fetchall(
                    "SELECT DISTINCT puuid FROM match_stats "
                    "WHERE game_start + make_interval(secs => COALESCE(duration_sec, 0)) "
                    "      > now() - interval '30 minutes' "
                    "AND queue_id IN (420, 440, 710)"
                )
            }
            tracked = {
                row[0]
                for row in await db.fetchall(
                    "SELECT DISTINCT puuid FROM league_players WHERE puuid IS NOT NULL"
                )
            }
        except Exception as exc:
            self.bot.logging.warning(f"Entries gating unavailable, fetching all: {exc!r}")
            return set(), force_fresh
        return tracked - fresh_match - force_fresh, force_fresh

    def note_finished(self, puuids) -> None:
        """Fast-path hook for the live-games cog: these players' games just
        ended, so the next cycle must fresh-fetch them regardless of what
        the pair diff can see."""
        self._pending_finished |= set(puuids)

    async def _live_board_marker(self) -> tuple[set[str], str]:
        """(in-game puuids, marker suffix) for the board render.

        The marker emoji comes from the bot_config key ``live_emoji`` so a
        custom Discord emoji (``<:live:123...>``) can be set from the
        dashboard without a deploy; defaults to 🔴. Empty set/marker when
        live tracking isn't running — the board just renders plain.
        """
        try:
            live = {
                row[0]
                for row in await db.fetchall(
                    "SELECT DISTINCT puuid FROM live_games "
                    "WHERE seen_at > now() - interval '12 minutes'"
                )
            }
        except Exception:
            return set(), ""
        if not live:
            return set(), ""
        row = await db.fetchone("SELECT value FROM bot_config WHERE key = 'live_emoji'")
        marker = row[0] if row and row[0] else "\U0001f534"
        return live, f" {marker}"

    async def fetch_users_rank(
        self,
        users,
        stale_ok: set[str] = frozenset(),
        force_fresh: set[str] = frozenset(),
    ):
        users_ranks = {}
        seen: set[str] = set()
        failed = 0
        served_stale = 0

        # League-entries uses platform routing (euw1), not regional (europe).
        for puuid, name, user_id in users:
            if puuid in seen:
                continue
            seen.add(puuid)
            allow_stale = puuid in stale_ok
            user_rank = await get_league_entries(
                puuid, fresh=puuid in force_fresh, allow_stale=allow_stale
            )
            if allow_stale:
                served_stale += 1
            if user_rank is None:
                failed += 1
                self.bot.logging.error(f"Failed to fetch rank for {name}")
                continue

            # Capture-everything: snapshot every OTHER ranked queue in this
            # response (flex today, whatever Riot adds tomorrow) into
            # league_history while we're already holding it — zero extra
            # API spend, the entries call always returns all queues. Solo
            # keeps its richer change-gate below; RANKED_PREMADE_5x5
            # belongs to the 5s board cog (written under its internal
            # RANKED_5S tag). record_history_snapshot inserts only when
            # W/L moved, so the steady-state cost is one SELECT per entry
            # per cycle. Tagged with Riot's own queueType string
            # (RANKED_FLEX_SR etc.) — consumers filter on their own tags,
            # so nothing leaks into the solo board/awards/graphs.
            for entry in user_rank:
                queue_type = entry.get("queueType")
                if not queue_type or queue_type in (SOLO_QUEUE, "RANKED_PREMADE_5x5"):
                    continue
                try:
                    if await leaderboard.record_history_snapshot(entry, queue_type):
                        self.bot.logging.info(
                            f"{queue_type} history insert for {name}: "
                            f"{entry.get('tier')} {entry.get('rank')} "
                            f"{entry.get('leaguePoints')}lp "
                            f"{entry.get('wins')}W/{entry.get('losses')}L"
                        )
                except Exception as exc:
                    self.bot.logging.error(f"{queue_type} snapshot failed for {name}: {exc!r}")

            fivev5 = list(filter(lambda x: x["queueType"] == "RANKED_SOLO_5x5", user_rank))
            if len(fivev5) > 0:
                fivev5 = fivev5[0]
                fivev5["user_id"] = user_id
                fivev5["discord_name"] = name
                fivev5["sorted_rank"] = Ranker(
                    fivev5["tier"], fivev5["rank"], fivev5["leaguePoints"]
                )
                fivev5["GamesPlayed"] = fivev5["wins"] + fivev5["losses"]
                fivev5["WinRate"] = (fivev5["wins"] / fivev5["GamesPlayed"]) * 100
                fivev5["summonerName"] = await self.get_name(puuid)

                users_ranks[fivev5["summonerName"]] = fivev5
                if fivev5["GamesPlayed"] < self.min_games_played:
                    del users_ranks[fivev5["summonerName"]]
                    continue
            else:
                fivev5 = []

        # One DEBUG summary per cycle replaces the old per-account INFO
        # line (~14 lines every 2 minutes). Failures still log per account
        # at ERROR above; loop liveness is the heartbeat cog's job.
        self.bot.logging.debug(
            f"Rank fetch cycle: {len(seen) - failed}/{len(seen)} accounts, "
            f"{served_stale} served from stale cache, {len(users_ranks)} on board"
        )
        return users_ranks

    # @tasks.loop(seconds=30)
    async def fetch_ranks_from_riot(self):
        rows = await db.fetchall(
            """SELECT puuid,
                    CASE WHEN nickname = '' THEN discord_tag ELSE nickname END,
                    discord_user_id
                FROM league_players
                    LEFT JOIN users ON user_id = discord_user_id"""
        )
        # Fetch current ranks and store them in a dict with updated values.
        # Per-player API failures are handled inside fetch_users_rank
        # (riot_client returns None rather than raising). Players whose
        # entries can't have moved are served from the stale cache.
        skip, force_fresh = await self._entries_skip_set()
        self.ranked_dict = await self.fetch_users_rank(rows, skip, force_fresh)
        return

    async def get_last_five_games(self, puuid):
        """Duo-aware Last 5 + last-played timestamp from match results.

        A game is a duo game when another tracked player has a row for the
        same match on the same team (match_stats holds only tracked
        players). Sourced from match data instead of the old
        league_history diff reconstruction — exact per-game results and
        ordering; a game finished within the last ~5 min (stream interval)
        can show one refresh late. NULL team_id (rows predating the
        column, not yet backfilled) never counts as duo.

        Returns ``(squares, last_played)`` — last_played is the newest
        game_start (same rows, no extra roundtrip), None when the player
        has no recorded games.
        """
        rows = await db.fetchall(
            "SELECT ms.win, EXISTS ("
            "    SELECT 1 FROM match_stats o"
            "    WHERE o.match_id = ms.match_id"
            "      AND o.team_id = ms.team_id"
            "      AND o.puuid <> ms.puuid"
            ") AS duo, ms.game_start "
            "FROM match_stats ms "
            "WHERE ms.puuid = %s AND ms.queue_id = %s "
            "ORDER BY ms.game_start DESC LIMIT 5",
            (puuid, RANKED_SOLO_QUEUE_ID),
        )
        squares = leaderboard.build_last_five_with_duo([(win, duo) for win, duo, _ in rows])
        return squares, rows[0][2] if rows else None

    async def get_recent_streak(self, puuid):
        """Return the count of consecutive losses ending at the most recent game.

        Looks at the last ~15 history rows (both legacy-summonerId-keyed
        and real-puuid-keyed). Returns 0 if the most recent game was a win
        or there's no history — see leaderboard.count_leading_losses for
        the in-cycle ordering caveat (false-negatives over false-positives
        for the ping).
        """
        rows = await leaderboard.fetch_history_wl(puuid, SOLO_QUEUE, 15, legacy_dual_key=True)
        return leaderboard.count_leading_losses(rows)

    async def send_streak_ping(self, user_id: int, streak: int, puuid: str) -> None:
        channel = self.bot.get_channel(STREAK_PING_CHANNEL)
        if channel is None:
            self.bot.logging.warning(
                f"Streak channel {STREAK_PING_CHANNEL} not found, skipping ping"
            )
            return
        # Pull K/D over roughly the streak window (capped at 20). If the
        # ratio is below 1.0, mock with the raw kills/deaths; otherwise
        # the emoji + ping speak for themselves.
        kills, deaths, _assists, _wins, games = await fetch_recent_kd(
            puuid, count=min(max(streak, 5), 20)
        )
        extra = ""
        if games > 0 and deaths > 0 and (kills / deaths) < 1.0:
            extra = f" {kills}/{deaths}"
        self.bot.logging.info(
            f"Streak ping: user {user_id} on a {streak}-loss streak -> "
            f"#general ({STREAK_PING_CHANNEL})"
        )
        # \U0001FAF5 = pointing at viewer, \U0001F602 = face with tears of joy
        await channel.send(f"\U0001faf5\U0001f602 <@{user_id}>{extra}")

    async def get_name(self, puuid):
        row = await db.fetchone(
            "SELECT league_username FROM league_players WHERE puuid = %s",
            (puuid,),
        )
        if row is not None:
            return row[0]
        return "Unknown"

    async def check_name(self, puuid):
        row = await db.fetchone(
            "SELECT puuid, league_username FROM league_players WHERE puuid = %s",
            (puuid,),
        )
        if row is None:
            return
        puuid, stored_name = row

        account = await get_account_by_puuid(puuid)
        if account is None:
            return  # transient account-v1 failure; retry next cycle
        name = account["gameName"]

        if name != stored_name:
            self.bot.logging.info(f"updating {stored_name} to {name}")
            await db.execute(
                "UPDATE league_players SET league_username = %s WHERE puuid = %s",
                (name, puuid),
            )

    @tasks.loop(seconds=120)
    async def post_ranks(self):
        await self.bot.wait_until_ready()
        # Serialise the scheduled loop, /refresh_ranks and the live-games
        # cog's end-of-game fast path — two concurrent runs would race
        # wipe_and_post and double-wipe the board channel.
        async with self._post_lock:
            await self._post_ranks_once()

    async def _post_ranks_once(self):
        await self.fetch_ranks_from_riot()

        # LP-diff bookkeeping stays gated on the fetched ranks moving:
        # check_name is an account-v1 call per player, and the history
        # snapshots / streak ping only have anything to do on a change.
        if (self.ranked_dict != self.previous_ranks) or (not self.previous_ranks):
            updated_users: list[str] = []
            for user in self.ranked_dict.keys():
                await self.check_name(self.ranked_dict[user]["puuid"])

                if self.previous_ranks and user in self.previous_ranks.keys():
                    if (
                        self.ranked_dict[user]["leaguePoints"]
                        != self.previous_ranks[user]["leaguePoints"]
                    ):
                        old = self.previous_ranks[user]
                        new = self.ranked_dict[user]
                        self.bot.logging.info(
                            f"LP change: {user} "
                            f"{old['tier']} {old['rank']} {old['leaguePoints']}lp -> "
                            f"{new['tier']} {new['rank']} {new['leaguePoints']}lp"
                        )
                        await self.update_table(user, self.ranked_dict[user])
                        updated_users.append(user)

            # Streak ping: only check players whose LP changed this cycle so
            # we don't spam Riot's DB on every refresh. Posts once when a
            # player crosses STREAK_THRESHOLD; rearms when their streak breaks.
            for user in updated_users:
                posting_data = self.ranked_dict[user]
                streak = await self.get_recent_streak(posting_data["puuid"])
                if streak >= STREAK_THRESHOLD and user not in self.streak_pinged:
                    await self.send_streak_ping(
                        posting_data["user_id"], streak, posting_data["puuid"]
                    )
                    self.streak_pinged.add(user)
                elif streak < STREAK_THRESHOLD and user in self.streak_pinged:
                    self.streak_pinged.discard(user)

            self.previous_ranks = self.ranked_dict

        # Render every cycle, not just on rank changes: match_stats rows
        # land via the separate ~5-min stream loop, so squares/flag/
        # last-played can change with no LP movement. Post iff the
        # rendered blocks differ from the last posted board.
        to_post = filter(
            lambda x: isinstance(x, dict),
            [data for data in self.ranked_dict.values()],
        )
        # Sort by rank
        sorted_results = sorted(to_post, key=lambda d: d["sorted_rank"], reverse=True)

        # Sort by winrate
        # sorted_results = sorted(to_post,
        #                         key=lambda d: d["WinRate"],
        #                         reverse=True)

        # apex_omits_games_word: this board's apex entries have always
        # read "Played: N with a ..." — see utils/leaderboard.py.
        live_puuids, live_marker = await self._live_board_marker()
        (
            output_list,
            self.previous_positions,
            self.last_positions,
        ) = await leaderboard.render_board_entries(
            sorted_results,
            self.previous_positions,
            self.last_positions,
            self.get_last_five_games,
            apex_omits_games_word=True,
            live_puuids=live_puuids,
            live_marker=live_marker,
        )
        if output_list:
            # Key at the top of the board (William's call) — explains
            # the per-entry squares, which carry no label of their own.
            output_list.insert(
                0,
                "-# Key: \U0001f7e9 solo win · ❎ duo win · \U0001f7e5 solo loss · ❌ duo loss",
            )

        if output_list != self.previous_output:
            self.previous_output = output_list
            self.bot.logging.info("Posting ranks")
            paste = self.bot.get_channel(919981835428179988)
            # Blocks, not a joined string — the board can exceed Discord's
            # 2000-char message cap and gets split on entry boundaries.
            await leaderboard.wipe_and_post(paste, output_list, self.bot.logging)

        # Watchdog input: heartbeat cog reads this and reloads us if it
        # goes stale (typically because a Gateway disconnect left the
        # @tasks.loop in a state where it never fires again).
        self.post_ranks_last_fired = dt.datetime.now()
        return

    @post_ranks.error
    async def post_ranks_error(self, exc: BaseException) -> None:
        """Auto-restart post_ranks on unhandled error.

        Default @tasks.loop behaviour on exception is to log + stop the
        loop. That leaves the leaderboard frozen until manual recovery.
        The restart must run detached — this callback executes inside the
        dying loop task, where is_running() is still True (see
        utils/loop_restart.py).
        """
        self.bot.logging.error(f"post_ranks errored: {exc!r}, restarting in 60s")
        restart_loop_later(
            self.post_ranks,
            name="post_ranks",
            log=self.bot.logging,
            still_active=lambda: self.bot.get_cog("FetchFromRiot") is self,
        )

    @app_commands.command(
        name="set_minimum_games_played",
        description="Set the minimum amount of games played for a user to appear on the leaderboard",
    )
    @app_commands.describe(
        number="The number of games a user must have played to appear on the leaderboard"
    )
    async def min_games_played_setter(self, ctx: discord.Interaction, number: int):
        await ctx.response.defer()
        if not isinstance(number, int) or number > 200:
            await ctx.followup.send("please enter a reasonable number....", ephemeral=True)
        self.bot.logging.info(
            f"Updating minimum number of games played from {self.min_games_played} to {number}"
        )

        await ctx.followup.send(
            f"updating minimum number of games played from {self.min_games_played} to {number}"
        )
        self.min_games_played = number

        return

    @app_commands.command(
        name="refresh_ranks", description="Refreshes the league ranking discord channel"
    )
    async def refresh_ranks(self, ctx: discord.Interaction):
        await ctx.response.defer()
        msg = await ctx.followup.send("Refreshing ranks...", wait=True, ephemeral=True)
        await self.post_ranks()
        await msg.edit(content="Sucessfully refreshed rank leaderboard")

    @app_commands.command(
        name="stop_rank_refresh",
        description="Pauses the league ranking table refreshing",
    )
    async def stop_ranks(self, ctx: discord.Interaction):
        await ctx.response.defer()
        msg = await ctx.followup.send("Stopping...", wait=True, ephemeral=True)
        self.post_ranks.stop()
        await asyncio.sleep(30)
        self.post_ranks.cancel()
        await msg.edit(content="Stopped refreshing of ranks")
        self.bot.logging.info("Stopped the refreshing of ranks posting")

    @app_commands.command(
        name="start_rank_refresh",
        description="Restarts the league ranking table refreshing",
    )
    async def start_ranks(self, ctx: discord.Interaction):
        await ctx.response.defer()
        msg = await ctx.followup.send("Starting...", wait=True, ephemeral=True)
        if self.post_ranks.is_running():
            await msg.edit(content="Already running, cannot start")
        else:
            self.post_ranks.start()
            await msg.edit(content="Started refreshing of ranks")
            self.bot.logging.info("Started rank refresh")

    # Needs updating to grab last match from the table
    async def update_table(self, user, user_stats_dict):
        # Keeps this board's historical except shape (a player's first-ever
        # insert has no prior row and IndexErrors the W/L compare) around
        # the shared history helpers.
        try:
            last_values = await leaderboard.latest_history_wl(user_stats_dict["puuid"], SOLO_QUEUE)
        except Exception as e:
            self.bot.logging.error(f"Failed to read history for {user}: {e!r}")
            return
        shrink_detected = False
        try:
            if last_values[0] == (
                user_stats_dict["wins"],
                user_stats_dict["losses"],
            ):
                return
            prev = last_values[0]
            new_games = user_stats_dict["wins"] + user_stats_dict["losses"]
            if None not in prev and new_games < sum(prev):
                # Games totals never shrink within a split — this account
                # just crossed a ladder reset.
                self.bot.logging.info(
                    f"Games total shrank for {user} ({sum(prev)} -> {new_games}) — "
                    "possible season reset, re-syncing seasons table"
                )
                shrink_detected = True
        except Exception:
            self.bot.logging.info(f"No prior history for {user} — first snapshot")

        self.bot.logging.info(
            f"History snapshot: {user} "
            f"{user_stats_dict['tier']} {user_stats_dict['rank']} "
            f"{user_stats_dict['leaguePoints']}lp "
            f"{user_stats_dict['wins']}W/{user_stats_dict['losses']}L"
        )
        await leaderboard.insert_history_snapshot(user_stats_dict, SOLO_QUEUE)
        if shrink_detected:
            # After the insert so the shrunken snapshot itself is part of
            # the derivation (utils/seasons.py needs it in league_history).
            await seasons.sync_seasons()
        return


async def setup(bot: MyDiscordBot):
    await bot.add_cog(FetchFromRiot(bot))
