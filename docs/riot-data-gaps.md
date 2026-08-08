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
| challenges-v1 config (challenge id → names/thresholds) | — | **not stored** — static metadata, fetchable anytime (or CommunityDragon); revisit if a display needs names offline |
| tft-* APIs | TFT matches/league | **not captured** — no signal anyone plays TFT; one client function away if that changes |
| clash-v1, lol-status, featured-games | — | **not worth it** (unchanged) |
| VAL-\* (Valorant) | — | **out of scope per user**; needs a production key with Valorant approval anyway. The `feat/valorant-board` branch's op.gg workaround stands |

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

## Running the full-depth capture (all via Discord, by design)

Every API-hitting backfill runs **inside the bot** as a slash command, so
it shares the process-wide rate limiter with the live boards — a separate
process would compete for the same key blindly (that's how post_ranks got
starved once). All are background tasks: the bot keeps serving, progress
posts to the invoking channel periodically, `/backfill_status` answers on
demand, `/backfill_cancel` stops cleanly, and a `bot_config` resume
marker + per-row commits mean a bot restart **auto-resumes** the run.

Order of operations after deploying this branch:

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

Sweeps (mastery/profile/challenges/cutoffs) need nothing: their loops run
on cog load and every 12h.

### Cost estimates (key budget: 20/1s, 100/120s)

| Run | Requests | Wall clock |
|---|---|---|
| Deep walk, ids pages | ~1 per 100 matches per player | folded into below |
| Deep walk, new matches | 2 per new match (details + timeline) | at ~40 req/min residual: ~10k new matches ≈ **8–9 h** |
| Timeline heal for the ~9k already-stored matches | 1 per match | paced 30/min ≈ **5 h** |
| Steady state after | stream +1 timeline per new game; sweeps ~85 req/day total | noise |

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
