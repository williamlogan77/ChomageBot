# Riot data: captured vs available (capture-everything pass)

Audit of every Riot API surface relevant to this bot against what the bot
stores, and the runbook for pulling **all data for each player, as far
back as Riot allows**. Facts current as of **2026-08-08**.

Validation: every field name below was checked against the community
OpenAPI schema generated from Riot's API reference AND live-validated on
2026-08-08 against real EUW responses for a tracked player (match, its
timeline, league entries, challenges, summoner, mastery, challenger
ladder — fixtures recorded during the run). Notes from the live pass:

- `baitPings` exists in the published schema but not in current live
  payloads — the extractor treats missing ping fields as 0, so nothing
  breaks either way.
- summoner-v4 no longer returns `id`/`accountId` (only puuid, level,
  icon, revisionDate) — we never stored them.
- A real 27-minute ranked game's timeline is **779 KB as JSON text,
  142 KB as compressed JSONB in Postgres** (measured with
  `pg_column_size`); the match payload itself is 46 KB JSONB.

## Status table

| API | Data | Status |
|---|---|---|
| match-v5 `/matches/{id}` core (K/D/A, champion, win, duration, queue, patch, position, teamId) | per-participant basics | **captured** (since forever) |
| match-v5 full payload archive | everything, verbatim (`match_raw` JSONB) | **captured** |
| match-v5 detail (damage, gold, CS, vision/wards, pings, multikills, time dead, turrets, steals, first blood, remake, challenges' KP / team-dmg% / solo kills) | 20 extracted `match_stats` columns | **implemented** (pass 1) |
| match-v5 **all queues** (ARAM, flex, normals, customs — not just 420/710) | same tables, `queue_id` disambiguates | **implemented** (pass 2 — the ids call simply drops the queue filter; same request count) |
| match-v5 `/timeline` | complete per-minute frames + event stream, verbatim (`match_timeline_raw` JSONB) | **implemented** (pass 2 — fetched inline for every new match; `/backfill_timelines` heals history) |
| league-v4 entries — solo | `league_history` snapshots on LP change | **captured** (LP-over-time already existed) |
| league-v4 entries — **flex + any future ranked queue** | `league_history` rows tagged with Riot's queueType | **implemented** (pass 2 — was fetched and discarded; zero extra API spend) |
| league-v4 entries — hotStreak/veteran/miniSeries etc. | `league_history.raw` JSONB | **implemented** (pass 1 — column existed, writer was missing) |
| league-v4 apex ladders | Challenger/GM solo cutoff LP + ladder size (`ladder_cutoffs`) | **implemented** (pass 2, 2 requests/sweep) |
| champion-mastery-v4 | per-champion level/points/tokens/milestones + full entry in `raw` (`champion_mastery`) | **implemented** (12h sweep, `cogs/mastery_updater.py`) |
| challenges-v1 player-data | per-challenge progress (`player_challenges`) + totals/title/banner + complete payload (`player_challenge_summary.raw`) | **implemented** (12h sweep, `cogs/profile_updater.py`) |
| summoner-v4 | level, profile icon, revisionDate (`summoner_profile`) | **implemented** (same sweep) |
| spectator-v5 | live games incl. raw payload (`live_games`) | **captured** |
| account-v1 | riot id ↔ puuid, rename sync | **captured** |
| challenges-v1 config + percentiles (challenge id → names/descriptions/thresholds/tier percentiles) | `challenge_config`, one row per challenge, en_GB extract + full DTO in `raw` | **implemented** (pass 3 — daily sweep, `cogs/game_data_updater.py`, 2 requests/day) |
| clash-v1 tournaments / registrations / teams | `clash_tournaments` (schedule history), `clash_registrations` (per tracked player), `clash_teams` (rosters) | **implemented** (pass 3 — daily sweep, `cogs/clash_updater.py`, ~22 requests/day) |
| champion-v3 free rotation | `champion_rotations`, one row per observed rotation with first/last-seen window | **implemented** (pass 3 — daily fetch + dedupe in `cogs/game_data_updater.py`, 1 request/day) |
| lol-status-v4 | `lol_status_events` — EUW incidents/maintenances ONLY (steady-state "all fine" polls write nothing) | **implemented** (pass 3 — hourly poll, `cogs/status_updater.py`, 24 requests/day) |
| spectator-v5 featured-games | — | **skipped, documented**: a rotating sample of random high-visibility games platform-wide, zero relation to tracked players (whose live games `live_games` already captures). Storing strangers' games that churn every few minutes has no banter or dashboard value — this is sampling noise, not friend-group data |
| tft-* APIs | TFT matches/league | **excluded per user decision** (2026-08-08): scope is League of Legends (all its queues — ranked/flex/Arena/ARAM/etc., which the all-queue match ingest covers); TFT is a separate title and not wanted |
| VAL-\* (Valorant) | — | **out of scope per user**; needs a production key with Valorant approval anyway. The `feat/valorant-board` branch's op.gg workaround stands |

## Remainder pass (pass 3, 2026-08-08): the endpoints previously skipped

The user overruled the "low value" skips — everything League is captured
now. Three new sweep cogs, same shape as `mastery_updater`, all through
the shared limiter with per-item error isolation and `raw` archives:

| Cog | Cadence | Requests/day | Tables |
|---|---|---|---|
| `cogs/clash_updater.py` | daily | 1 + 1/player + 1/team seen ≈ **22** | `clash_tournaments`, `clash_registrations`, `clash_teams` |
| `cogs/game_data_updater.py` | daily | **3** (config + percentiles + rotation) | `challenge_config`, `champion_rotations` |
| `cogs/status_updater.py` | hourly | **24** (Riot says lol-status doesn't even count against the app limit) | `lol_status_events` |

Total added steady-state spend: **~49 requests/day** (on top of the
existing ~85/day of sweeps — still noise against the 100/120s budget,
whose theoretical ceiling is 72k/day).

Live-validation notes (2026-08-08, real EUW calls):

- **clash-v1 tournaments**: 200, two upcoming Bilgewater cups; DTO shape
  exactly as documented (`schedule` = [{registrationTime, startTime,
  cancelled}]).
- **clash-v1 players by-puuid**: 200 + `[]` for every tracked account
  (nobody registered outside a Clash window — expected). Because nobody
  was registered, **/teams/{id} could not be live-validated**; the cog
  extracts it defensively and `raw` is the authority. First real Clash
  weekend will confirm.
- **challenges config**: 405 entries. The documented `tracking` and
  `startTimestamp` fields were absent from every live entry and
  `endTimestamp` present on exactly one — all treated optional
  (columns exist, NULL when absent). All 28 locales archived in `raw`;
  en_GB extracted into columns.
- **champion rotation**: the LIVE response shape is
  `{"sr": [...], "newplayer": [...]}` — NOT the documented
  `freeChampionIds`/`freeChampionIdsForNewPlayers`/`maxNewPlayerLevel`.
  The cog handles both shapes and archives whichever arrives; this is
  exactly why every sweep keeps `raw`.
- **lol-status**: 200 with zero incidents/maintenances (the steady
  state), so event-entry parsing is coded from the schema (snake_case
  fields, string timestamps) and defensive; steady-state polls write
  nothing by design — the table only holds real events.
- Old puuids from the legacy sqlite 400 with "Exception decrypting"
  against the current key (puuids are key-scoped); validation resolved
  fresh puuids via account-v1. The bot always uses puuids minted by its
  own key, so this only affects ad-hoc scripts.

This pass also extracted the match/timeline ingestion primitives out of
`cogs/backfill.py` into `Bot/utils/match_ingest.py` (verbatim — the cog
now delegates) so the new **standalone deep-backfill runner**
(`scripts/backfill_deep_standalone.py`, see "Running the full-depth
capture" below) shares the exact insert paths instead of copying them.

## How deep "as far back as possible" actually goes

- `/backfill_all all_history=True` paginates the match-ids endpoint
  (`start += 100` until Riot returns a short page) **with no queue filter
  and no startTime filter** — nothing is truncated on our side; the walk
  ends exactly where Riot's data ends. A transient ids-page failure is
  retried once and then aborts that player's walk *loudly* (never
  silently treated as end-of-history); re-running resumes.
- Riot's own floor: the match-ids list only covers games since
  **2021-06-16** (matchlist timestamp epoch, per Riot's docs). In
  practice EUW has served roughly the last **~2 years** of matches;
  expect the walk to end somewhere in that range. Timelines age out
  earlier than match details for the oldest games (404s — counted, not
  fatal).
- Old-payload quirks are handled: missing `gameStartTimestamp`
  (pre-patch-11.20) falls back to `gameCreation`; `gameDuration` in
  milliseconds (same vintage, detected by absent `gameEndTimestamp`) is
  converted; a malformed match costs exactly that match (its raw payload
  still archives) and never the rest of the page. Unknown queues/modes
  are stored like everything else — every consumer (boards, awards,
  charts) pins its own queue_id in SQL, so nothing leaks into ranked views.

### What can NEVER be backfilled (point-in-time endpoints)

Only match-derived data goes deep. These are gone for any moment the bot
wasn't running and snapshotting:

- **LP history** before the bot's snapshots — league-v4 has no history
  endpoint. (The Dec 2024 → Jan 2026 outage gap was already patched with
  sparse U.GG split-end anchors; that's the truthful ceiling.) Same for
  the new flex snapshots: flex LP history starts the day this deploys.
- **Champion mastery / challenges / summoner level / apex cutoffs** as
  of past dates — current-state endpoints; our tables hold "now",
  upserted each sweep.
- **Past live games** (spectator is now-only) and **hotStreak/miniSeries
  flags** for past snapshots (only archived from now on).

## Running the full-depth capture (two sanctioned paths)

The rule was always: never a separate process **on the same key** — it
competes with the live boards blindly (that's how post_ranks got starved
once). Two paths respect that rule:

**A. In-bot (prod key).** Every API-hitting backfill runs inside the bot
as a slash command, sharing the process-wide rate limiter with the live
boards. All are background tasks: the bot keeps serving, progress posts
to the invoking channel periodically, `/backfill_status` answers on
demand, `/backfill_cancel` stops cleanly, and a `bot_config` resume
marker + per-row commits mean a bot restart **auto-resumes** the run.

**B. Standalone on a SPARE key (preferred for the deep walk).**
`scripts/backfill_deep_standalone.py` runs the identical walk + timeline
heal (it imports the same `utils/match_ingest.py` primitives the cog
uses, so inserts are byte-identical and idempotent) in its own process on
its own key — the bot's prod budget is completely untouched. It paces to
~90% of a dev key (18/1s, 90/120s), checkpoints into `bot_config` under
`deep_backfill_standalone_state` (distinct from the in-bot marker, so
both can exist at once), prints progress per player / per 100 timelines
for `docker logs`, and is safe to run while the bot is live (same
per-row commits; concurrent walkers just hit each other's pre-filter).

    docker exec -e riot_key=<SPARE_DEV_KEY> <container> \
        python scripts/backfill_deep_standalone.py --dry-run   # plan + cost estimate
    docker exec -e riot_key=<SPARE_DEV_KEY> <container> \
        python scripts/backfill_deep_standalone.py             # matches, then timelines

`-e` beats the container's `.env` (python-dotenv never overrides existing
env vars). Ctrl-C / restart any time — rerunning resumes. Dev keys expire
daily: on a 403 just mint a new key and rerun with the fresh `-e` value.

Order of operations after deploying this branch (path A shown; for path B
replace steps 2–3 with one standalone run):

1. `/backfill_detail_from_raw` — zero-API SQL fill of the 20 detail
   columns from already-archived payloads (seconds).
2. `/backfill_all all_history=True` — the deep walk: all queues, to the
   end of Riot's retention, healing missing raw payloads and detail
   columns as it goes; every NEW match also pulls its timeline inline.
3. `/backfill_timelines` — heals timelines for matches that were already
   stored before timeline capture (they skip step 2's pre-filter, so
   their timelines arrive here). Paced ~30 req/min (≤ ~60% of budget);
   optional `limit` for a bounded newest-first run.
4. `/backfill_detail_from_raw` again — mops up detail for any rows healed
   in step 2.

Sweeps need nothing: their loops run on cog load and then on their own
cadence — mastery/profile/challenges/cutoffs every 12h; clash and the
challenges-catalogue/rotation daily; the EUW status poll hourly.

### Cost estimates (key budget: 20/1s, 100/120s)

| Run | Requests | Wall clock |
|---|---|---|
| Deep walk, ids pages | ~1 per 100 matches per player | folded into below |
| Deep walk, new matches | 2 per new match (details + timeline) | at ~40 req/min residual: ~10k new matches ≈ **8–9 h** |
| Timeline heal for the ~9k already-stored matches | 1 per match | paced 30/min ≈ **5 h** |
| Steady state after | stream +1 timeline per new game; sweeps ~134 req/day total (~85 from pass 1–2 + ~49 from the remainder pass) | noise |

On the standalone runner's own dev key (90/120s ≈ 45/min sustained) the
same walk finishes ~1.5× faster than the in-bot residual rate — and the
bot's live polling never queues behind it at all.

The deep walk's new-match count is unknown until it runs (all-queues will
surface every ARAM/normal/flex back ~2 years — plausibly 2–4× the ~9k
ranked matches currently stored). It's resumable and restart-safe, so
letting it run across a day or two is fine.

### Storage (measured, not guessed)

- Match payloads: ~25–46 KB JSONB each (existing estimate confirmed).
- **Timelines: ~142 KB JSONB for a 27-min game** — the biggest thing the
  bot stores. 9k matches ≈ ~1.3 GB; if the all-queue deep walk triples
  the match count, timelines alone approach **~4 GB**.
- Everything else added this pass (detail columns, mastery, challenges,
  summoner, cutoffs, flex snapshots): single-digit MB, upserted in place.

**Check the DB container's headroom before the full timeline heal** — the
chomage-db LXC rootfs is 8 GB. `SELECT pg_size_pretty(pg_total_relation_size('match_timeline_raw'));`
mid-run tells you the trajectory; `/backfill_timelines limit:2000` is the
bounded alternative (newest games first), and the crawl can be re-run
anytime to go deeper once the volume is grown.

## Dashboard wishlist reconciliation

`proxmox/dashboard/docs/riot-data-wishlist.md` (appeared after pass 1):

| Wishlist item | Ingestion status |
|---|---|
| 1. Ranked flex snapshots | **satisfied** — `league_history` rows, `queue = 'RANKED_FLEX_SR'` (Riot's own tag), written by the solo board's entries pass at zero extra API cost |
| 2. Match timelines | **satisfied** — table is named `match_timeline_raw` (not `match_timeline`), mirroring `match_raw`; payload verbatim |
| 3. Champion mastery | **satisfied** — `champion_mastery`; note `chest` no longer exists in the API (chests removed), and the full entry is in `raw` |
| 4. Summoner icon + level | **satisfied** — separate `summoner_profile` table keyed by puuid rather than columns on `league_players` (that table's PK is the legacy leagueid; join on puuid) |
| 5. Challenges | **satisfied** — payload-first as requested (`player_challenge_summary.raw` holds the complete PlayerInfoDto) plus per-challenge rows for easy SQL |
| 6. Challenger/GM cutoff | **satisfied** — `ladder_cutoffs` (queue, tier, cutoff_lp, players) |

All of it lands in the same Postgres the dashboard reads. One caveat for
the dashboard: `match_stats` now contains **every queue** — any view that
assumed ranked-only must filter `queue_id IN (420, 710)` (the bot's own
consumers already do).

## Key note

The earlier "dead key" diagnosis was wrong: Riot's API edge (Cloudflare)
403s with `error code: 1010` for `python-urllib`'s User-Agent from this
network — same key works with a browser UA (and via aiohttp in the bot).
If a local script ever sees bare 403s with error 1010, set a browser
User-Agent before blaming the key. The current key in `.env` is a dev key
(20/1s, 100/120s) expiring ~24h from 2026-08-08; the deep-walk commands
work identically on whatever key replaces it, just at that key's rate.
