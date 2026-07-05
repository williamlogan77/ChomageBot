# API runner / DB caller split — engineering assessment

William's question: *"mayhaps we have an api runner, and then a db caller
instead of wrapping it all into one bot?"*

Short answer up front: **the split is the right end-state architecture, but
it is not the highest-value next change.** The code seam (Phase 1 below) is
cheap and worth doing soon; the actual second process (Phase 2) should wait
for a concrete trigger. Two smaller changes — already in flight or doable
today — deliver most of the stated motivation (external devs, fewer
unnecessary API calls) at a fraction of the work. Details in §7.

Written 2026-07-05 against branch `feat/ranked5s-board`.

---

## 1. Current architecture

One Python process does everything: Riot ingestion, Postgres writes,
Postgres reads, chart rendering, Discord presentation, and its own
self-maintenance.

```
                    Proxmox host pve (192.168.0.3)
┌───────────────────────────────────────────────┐  ┌──────────────────────┐
│ LXC 103 "chomagebot"                          │  │ LXC 105 "chomage-db" │
│  cron */2m: scripts/deploy.sh (git pull main) │  │  Postgres 15         │
│  docker compose: ONE service ("myapp")        │  │  192.168.0.5:5432    │
│  ┌──────────────────────────────────────────┐ │  └──────────▲───────────┘
│  │ ONE process: discord.py bot (main.py)    │ │             │
│  │                                          │ │  DATABASE_URL
│  │ INGESTION (Riot → Postgres)              │─┼─────────────┘
│  │  post_ranks        120s  league-v4 ─┐    │ │
│  │  post_ranks_5s     120s  league-v4 ─┤115s│ │      Riot dev key
│  │  stream_matches      5m  match-v5   │TTL │─┼──► 20/1s + 100/120s
│  │  /backfill_all     manual match-v5  │    │ │   ONE in-process limiter
│  │  check_name        account-v1 ──────┘    │ │   (utils/riot_client.py)
│  │  /add_player       account-v1  (pantheon,│ │   ...except pantheon,
│  │  /kda, streak ping match-v5 ~21 calls ea)│ │   which bypasses it
│  │                                          │ │
│  │ PRESENTATION (Postgres → Discord)        │ │
│  │  board renders, charts (pandas/mpl),     │ │
│  │  panel views, slash commands             │ │
│  │                                          │ │
│  │ SELF-MAINTENANCE                         │ │
│  │  auto_reload  (30s mtime poll, hot swap) │ │
│  │  heartbeat    (5m watchdog, cog reload)  │ │
│  └──────────────────────────────────────────┘ │
└───────────────────────────────────────────────┘
```

Data flows today:

- **League entries** — `cogs/league_table_updater.py` (`FetchFromRiot.post_ranks`,
  120s) and `cogs/ranked5s_table_updater.py` (`Ranked5sBoard.post_ranks_5s`,
  120s, window-gated) each poll `get_league_entries()` per tracked player
  (~20). A 115s TTL cache in `utils/riot_client.py` lets the second board
  reuse the first's responses. Snapshots land in `league_history` **only
  when wins/losses change**; the boards themselves render from the live
  API response held in memory, not from the DB.
- **Matches** — `cogs/backfill.py`: `stream_matches` (5 min, last 5 IDs per
  player) and `/backfill_all` (one-shot, paginated) both funnel through
  `_backfill_player` → `_insert_matches` into `match_stats`. Idempotent by
  design. Raw-payload capture (`match_raw` JSONB) is being added on this
  same path right now.
- **On-demand Riot calls from presentation paths** — `/kda` and the
  loss-streak ping both call `utils/riot_stats.fetch_recent_kd`, which burns
  **~21 match-v5 calls per invocation** to compute K/D/A totals that are
  already sitting in `match_stats`.
- **Account-v1** — `/add_player` (`cogs/league_user_updater.py`) and the
  per-cycle name re-sync (`FetchFromRiot.check_name`) go through
  `bot.lolapi` (pantheon), which has its own internal handling and does
  **not** share the `riot_client` budget.
- **Deploy** — cron pulls `main` every 2 min; `cogs/auto_reload.py`
  hot-reloads changed `cogs/*.py` and `utils/*.py` (nothing else — the
  known utils gap bit PR #50). `cogs/heartbeat.py` watchdogs the four
  `@tasks.loop`s via in-process `*_last_fired` timestamps and reloads the
  owning cog when one freezes.

The one prod incident that motivates all of this: the position backfill run
as a **second process** shared the key but not the in-process limiter,
blew the 100/120s budget, and froze `post_ranks`. The limiter is only a
guarantee while *everything* using the key lives in one process.

## 2. The proposed split

Two processes, one repo, one image, two compose services in LXC 103.
(To be explicit: this is a second **app** process. It is *not* a bundled
database service — that's been ruled out; Postgres stays in CT 105.)

**API runner (`ingest`)** — owns the Riot key and the limiter. No Discord
connection, no discord.py import. Loops: entries poll (120s, both queues in
one pass), match stream (5 min), job consumer (backfills, account
resolution). Writes Postgres. Crash-only: if its own main loop stalls, it
exits nonzero and Docker's `restart: always` brings it back.

**DB caller (the bot)** — everything Discord. Reads Postgres, renders
boards/charts, serves slash commands. **Zero Riot calls**; `riot_key`
disappears from its environment entirely.

### What moves where

| Today | Function/loop | Destination |
|---|---|---|
| `utils/riot_client.py` | limiter, `get_league_entries` / `get_match_ids` / `get_match`, entries TTL cache | runner (whole module) |
| `utils/riot_stats.py` | `fetch_recent_kd` | retired — see `/kda` note below |
| `cogs/league_table_updater.py` | `fetch_users_rank`, `fetch_ranks_from_riot`, `update_table` (history insert), `check_name` (name re-sync) | runner |
| `cogs/league_table_updater.py` | board render + post, position arrows, `get_last_five_games`, streak detection + ping (already pure DB + Discord) | bot (stays) |
| `cogs/ranked5s_table_updater.py` | `_fetch_5s_ranks`, `_pick_5s_entry` (queueType discovery), `_record_history`, `is_ranked5s_tracking()` fetch gating | runner |
| `cogs/ranked5s_table_updater.py` | `_render_board`, `_get_board_channel`, `/set_ranked5s_channel`, `/ranked5s_status` | bot (stays) |
| `cogs/backfill.py` | `stream_matches` body, `_do_backfill`, `_backfill_player`, `_insert_matches`, `_participant_position`, match_raw capture | runner |
| `cogs/backfill.py` | `/backfill_all`, `/backfill_cancel`, `/backfill_status` | bot — as job enqueuer/reader (below) |
| `cogs/kda.py` | `/kda` | bot — retargeted at `match_stats` (pure DB read) |
| `cogs/league_user_updater.py` | `/add_player`'s account-v1 resolve | runner, via job row (below) |
| `main.py` | `pantheon.Pantheon` client (`bot.lolapi`) | deleted — account-v1 is a ~10-line aiohttp call inside the runner, under the shared limiter for the first time |
| `cogs/match_analysis.py`, `ranked_graphing.py`, `team_generator.py`, `sync.py`, `usage_logger.py` | — | bot, untouched (already zero Riot) |
| `utils/queue_windows.py`, `utils/rank_sorting_class.py`, `utils/db.py` | — | shared (pure modules, imported by both) |

### New shared state (additive schema)

- **`league_current`** — `(puuid, queue) → tier, division, lp, wins,
  losses, fetched_at`, upserted by the runner every cycle. Required: today
  the boards render from the in-memory API response, and `league_history`
  only gets rows on W/L change — a pure-DB bot would otherwise never see a
  newly tracked player until their first result, and gets no freshness
  stamp. `fetched_at` also gives the board an honest "data as of" line.
- **`ingest_jobs`** — `id, kind, payload jsonb, status, progress jsonb,
  requested_by, created_at, finished_at`.
- **`service_heartbeat`** — `service, beat_at, detail` (or reuse
  `bot_config`, which already has the key/value + `updated_at` shape).

### Commands in the bot, work in the runner

Slash commands must respond within Discord's interaction window, but the
Riot work now lives in another process. Mechanism: the bot inserts an
`ingest_jobs` row and issues `NOTIFY ingest_jobs`; the runner holds a
dedicated `LISTEN` connection (psycopg3 supports async notifies) with a
few-second poll as fallback so a dropped notify can't strand a job.
Precedent already exists — `bot_config.ranked5s_channel_id` is exactly this
DB-mediated config pattern.

- **`/backfill_all`** → insert `kind='backfill', payload={count,
  all_history}`, reply "queued". The runner updates `progress` jsonb as it
  goes (replacing the in-memory `Backfill._progress` dict), so
  `/backfill_status` becomes a row read and — bonus — progress now survives
  bot restarts. `/backfill_cancel` flips `status='cancel_requested'`;
  the runner checks between players (already resumable/idempotent).
- **`/add_player`** → `kind='resolve_account', payload={name, tag}`. The
  bot defers, polls the job row for ~5s, and edits in the result — account-v1
  is one call, so this resolves near-instantly in practice. (Pragmatic
  alternative: keep this single interactive call in the bot. It's a
  couple of calls a month; the cost is that the prod bot still holds a key
  and the "zero Riot calls" invariant gets an asterisk. Recommend the job
  row: the invariant is worth more than 5 seconds of latency on a rare
  admin command.)
- **queueType discovery** (Ranked 5s) → `_pick_5s_entry`'s heuristic runs in
  the runner; on discovery it writes `bot_config.ranked5s_queue_type_discovered`
  so `/ranked5s_status` in the bot can still report it. Pinning stays an
  env var — on the runner now.
- **Name re-sync** (`check_name`) → folded into the runner's entries cycle,
  writing `league_players.league_username` as today.
- **`/refresh_ranks` / `/refresh_ranked5s`** → re-render from
  `league_current` (at most ~120s stale) — or additionally enqueue a
  `kind='poll_now'` job if true freshness matters.

## 3. What it buys

1. **A single owner of the rate budget — structurally, not procedurally.**
   Today "everything goes through the shared limiter" is a convention that
   a second process silently breaks (the backfill freeze). Post-split, the
   key exists *only* in the runner's environment: a future one-off script
   physically cannot repeat the incident; its author is forced through
   `ingest_jobs` instead. It also finally brings account-v1 (pantheon,
   currently outside the limiter) under the same budget.
2. **Restart independence, both directions.** Bot restarts (image rebuild,
   gateway trouble, a broken cog) no longer interrupt ingestion — no more
   data gaps while the bot flaps. Runner restarts don't drop the panel,
   views, or slash commands. Notably, the `tasks.loop` disconnect-freeze
   class of bug can no longer stall data collection: the runner has no
   Discord gateway and its loops are plain asyncio.
3. **External contributors never need a Riot key.** A dev bot pointed at a
   read-only Postgres role gets a fully working bot — boards, charts, every
   command — because no bot code path calls Riot. Today a keyless dev bot
   limps: the board loops error every cycle and `/kda` fails outright.
4. **A real testing seam.** The ingest module has no discord imports —
   testable against a scratch Postgres with recorded Riot fixtures, no
   Discord token, no gateway. Today `fetch_users_rank` can't be exercised
   without constructing half a bot.
5. Minor: the runner's watchdog story gets *simpler* than the bot's
   (crash-and-restart vs. the careful cog-reload dance in
   `heartbeat.py`/`loop_restart.py`), because there is no long-lived
   session to preserve.

What it does **not** buy: API capacity. Same dev key, same 20/1s +
100/120s. Steady-state spend (~20 entries calls/120s + ~21 stream calls/5
min) is identical; the split changes ownership of the budget, not its size.

## 4. What it costs

1. **A second service to deploy, monitor, and restart.** New compose
   service in LXC 103 (same image, different `command:` — no new
   Dockerfile). Two log streams; "why is the board stale" now starts with
   *which process* instead of *which cog*.
2. **Staleness detection goes cross-process.** Today `heartbeat.py` reads
   `post_ranks_last_fired` on a sibling cog and can *fix* a freeze by
   reloading the extension. Post-split it can only *observe*: the runner
   upserts `service_heartbeat` every cycle; the bot's heartbeat cog gains a
   check that posts an admin-channel alert when `beat_at` goes stale. The
   bot cannot restart the runner (no docker socket in the container, and we
   shouldn't mount one) — so the runner must be trustworthy on its own:
   internal stall-watchdog that exits the process, `restart: always`
   does the rest.
3. **The deploy story forks.** `auto_reload` is a discord.py cog — it does
   nothing for the runner. `scripts/deploy.sh` gains a step: after a pull
   that touched `ingest/` (or shared utils), `docker compose restart
   ingest`. This is honestly *less* fragile than hot reload — the
   auto_reload utils gap and the pool-survival dance in `utils/db.py`
   exist precisely because in-place reload is hard — and a runner restart
   is cheap: no gateway session, no slash-command re-sync, and the stream/
   backfill are already idempotent-resumable. But it is a second path to
   keep working.
4. **Schema and protocol surface.** Three new tables and a job protocol
   between two processes, replacing direct method calls. Every future
   "bot command that needs fresh Riot data" feature now costs a job
   round-trip instead of an `await`.
5. **Scale honesty.** This is a ~20-player, one-guild, friends bot. Every
   cost above is paid permanently, on every deploy and every incident,
   whether or not the hazards it guards against ever recur.

## 5. Failure modes

| Scenario | Symptom | Detection | Mitigation |
|---|---|---|---|
| Runner down (crash, OOM) | Boards freeze at last data; charts + commands keep working from DB | `service_heartbeat` stale → bot posts admin alert; `docker compose ps` | `restart: always`; idempotent resume (stream/backfill pre-filter + `ON CONFLICT`); board shows `fetched_at` age |
| Runner alive but frozen (event-loop stall) | Same as down, process looks healthy | Same heartbeat row (that's why it's a DB write, not a liveness probe) | Internal watchdog task: no beat in N min → `sys.exit(1)` → Docker restarts. No session to preserve, so crash-only is safe |
| DB (CT 105) down | Everything: bot loops error, commands fail, runner cycles fail | Both services log loudly; boards freeze | Same blast radius as today. Bot loops already self-restart with backoff (`restart_loop_later`); runner retries with backoff; CT 105 is `onboot` |
| Bot down | No boards/commands; **ingestion continues** (the headline win) | Discord presence, container logs | `restart: always`; on return the board re-renders from `league_current` — no data gap to backfill |
| Both down | Full outage | As today | As today — no worse |
| Rate-starved (429 storm) | Runner cycles slow, data staleness grows; bot presentation **unaffected** | Runner 429 warnings; limiter stats can ride along in heartbeat `detail` | Limiter already honours `Retry-After` + jitter; starvation can no longer freeze presentation, and no in-repo code path outside the runner can consume budget |
| Two runner instances (deploy overlap, manual run) | Doubled spend → 429s — the old incident in new clothes | 429 logs | `pg_advisory_lock` taken at runner startup; the second instance logs and exits |

## 6. Incremental path

**Phase 0 — now (done / in flight).** `match_raw` JSONB capture at ingest;
read-only Postgres role for dev bots. No further action; noted because it
already delivers a large share of the external-dev value.

**Phase 1 — code seam (low risk, do soon).** Create an ingest package with
**no discord imports**: entries polling (both queues, one pass), match
stream/backfill, account-v1 resolution; `riot_client` moves inside. The
cogs shrink to thin wrappers that call it and keep all rendering/posting.
Same single process, byte-identical behaviour, no ops change. This is
where `fetch_users_rank` / `_fetch_5s_ranks` / `_backfill_player` become
testable, and it makes Phase 2 a wiring change instead of a rewrite.
One repo-specific trap: `auto_reload` watches only `./cogs/*.py` and
`./utils/*.py` (non-recursive) — an `ingest/` top-level package would
silently not hot-reload (the exact gap that bit PR #50). Either name the
modules `utils/ingest_*.py` or add the new directory to AutoReload's watch
list in the same PR.

**Phase 2 — process split (wait for a trigger).** Add the `ingest` compose
service (same image, `command: python3 -m ingest`); move `riot_key` to
runner-only env. Add `league_current`, `ingest_jobs`, `service_heartbeat`.
Bot loops become pure DB readers on their existing cadence; heartbeat cog
gains the staleness alert; `deploy.sh` gains restart-on-change for the
runner; runner takes the advisory lock. Delete Riot imports from all cogs.

**Phase 3 — optional cleanup.** Retire the 115s `_entries_cache` in
`riot_client` (one fetch loop feeding a table makes it dead code); drop
`utils/riot_stats.py` once `/kda` reads `match_stats`; remove the pantheon
dependency (its last remaining use is account-v1).

## 7. Recommendation

**Do Phase 1 soon. Don't do Phase 2 yet. Do two cheaper things first.**

Weighing William's actual motivations:

- *External devs shouldn't need a key* — Phase 0's read-only role gets a
  dev bot 90% working today: every chart, every DB-backed command. What
  still errors without a key is the board loops and `/kda`. Full
  key-freedom needs the bot to stop calling Riot — but that's Phase 1's
  seam plus small retargeting, not necessarily a second process.
- *Avoid unnecessary API calls* — the biggest sink in the codebase is not
  architectural, it's `/kda` and the streak ping spending ~21 match-v5
  calls per invocation on numbers already in `match_stats` (kept ≤5 min
  fresh by the stream). Retargeting those at the DB is a small,
  self-contained change with immediate effect and needs no split at all.

So the sequence that actually serves the goals:

1. **Now:** retarget `/kda` + streak-ping K/D at `match_stats`. Biggest
   API-spend win available, trivial risk.
2. **Soon:** Phase 1 seam. Cheap, enables tests, and makes the eventual
   split a config change. Fold the account-v1 calls into the shared
   limiter while at it.
3. **Later, on a trigger:** Phase 2. Legitimate triggers: the next
   backfill-scale job that would tempt a second process; a second
   guild/consumer of the data; applying for a production Riot key (higher
   budget makes an always-on ingester more valuable); or the first
   external contributor actually showing up.

The honest core of it: the split's *unique* payoffs are structural key
ownership and restart independence. Both are insurance. The incident that
motivates the insurance (limiter starvation by a second process) has
already been mitigated procedurally — the standing rule is that backfills
route through the bot's limiter — and Phase 1 makes the safe path also the
easy path. For a friends-scale bot with one operator, a permanent second
service, a job protocol, and a cross-process health story are a real tax
to pay for insurance against a hazard we've already fenced. Build the
seam now so the split is a Tuesday afternoon when a trigger arrives — and
spend the saved effort on the things that move the stated goals today.
