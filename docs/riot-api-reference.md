# Riot API reference (for this repo)

Working notes for maintaining the bot's Riot integration. Facts current as of
**2026-07-04**.

## Hosts and routing

Riot splits its API across two kinds of hosts, and picking the wrong one 404s:

| Host | Routing | APIs used here | Wrapper (Bot/utils/riot_client.py) |
|---|---|---|---|
| `https://euw1.api.riotgames.com` | platform (EUW) | league-v4, spectator-v5, champion-mastery-v4, challenges-v1, summoner-v4 | `get_league_entries(puuid)`, `get_active_game(puuid)`, `get_champion_mastery(puuid)`, `get_player_challenges(puuid)`, `get_summoner_by_puuid(puuid)`, `get_apex_league(tier, queue)` |
| `https://europe.api.riotgames.com` | regional | match-v5, account-v1 | `get_match_ids(puuid, count, queue, start)` (`queue=None` = ALL queues), `get_match(match_id)`, `get_match_timeline(match_id)`, `get_account_by_riot_id` / `get_account_by_puuid` |

All Riot HTTP goes through `Bot/utils/riot_client.py` — do **not** add HTTP
calls elsewhere. Reasons:

- **Rate limits are per API key, shared across everything**: this key's
  actual limits are **20 requests / 1 s** and **100 requests / 120 s** —
  confirmed 2026-08-05 from Riot's own `X-App-Rate-Limit: 100:120,20:1`
  response header (the authoritative source; re-check the same way after
  any key change). The key is a registered/personal key (no 24 h expiry)
  whose numbers happen to match the dev tier. `riot_client` enforces
  both windows with a single process-wide limiter (`_wait_for_slot`); a second
  limiter elsewhere would silently blow the budget. (This has bitten before —
  a standalone backfill process starved the live loops.) The limiter logs
  a line as window usage crosses 70/85/95 of 100.
- 429s are retried internally (honours `Retry-After` + jitter, up to
  `MAX_RETRIES = 2`).
- **Entries TTL cache**: `get_league_entries` caches responses for **130 s**
  per puuid — just above the boards' 120 s loop period, so whichever board
  fetches first serves the other from cache. One league-v4 response lists
  *all* of a player's ranked queues. Pass `fresh=True` to bypass, or
  `allow_stale=True` to accept up to 2 h old data when the caller knows the
  entries can't have changed (player mid-game or idle — LP only moves when
  a game ends; the solo board uses this with an hourly full sweep).

The API key comes from the `riot_key` env var.

## league-v4: entries by puuid

`GET /lol/league/v4/entries/by-puuid/{puuid}` (platform host) returns a JSON
**list** with one object per ranked queue the player has placed in. Fields the
codebase actually uses:

```json
{
  "queueType": "RANKED_SOLO_5x5",
  "tier": "GOLD",
  "rank": "II",
  "leaguePoints": 57,
  "wins": 34,
  "losses": 30,
  "puuid": "..."
}
```

Notes:

- `tier` is upper-case (`GOLD`); display code calls `.title()`.
  `utils/rank_sorting_class.Ranker` sorts Iron→Master only (no
  Grandmaster/Challenger).
- `rank` is the division (`I`–`IV`); Master+ has no meaningful division and
  the boards special-case it.
- A player with zero games in a queue simply has no entry for that queue.

### queueType values

Documented in Riot's API reference:

- `RANKED_SOLO_5x5` — solo/duo
- `RANKED_FLEX_SR` — flex (Summoner's Rift)
- `RANKED_FLEX_TT` — legacy Twisted Treeline
- `RANKED_TFT`, `RANKED_TFT_TURBO`, `RANKED_TFT_DOUBLE_UP` — TFT

**Ranked 5s: `RANKED_PREMADE_5x5` — confirmed from live entries
2026-07-05.** An entry appears in `entries/by-puuid` only once the player
completes placements; before that the player has 710 matches but no entry.
(Beware: league-exp-v4's queue enum still rejects the string, and the
OpenAPI docs don't list it — live entries are the source of truth. The
cog's auto-discovery heuristic caught it in prod logs.)

`cogs/ranked5s_table_updater.py` renders a **hybrid board**: real ranks for
placed players, plus an "in placements" section from match results
(`match_stats` queue 710, ingested alongside solo by `cogs/backfill.py`).
With no entries at all it falls back to pure match-derived standings. The
pinned string lives in `utils/config.py` (env `ranked5s_queue_type`
overrides; empty value re-enables auto-discovery).

Internally (in `league_history.queue`) Ranked 5s rows are always tagged with
the repo's own constant **`RANKED_5S`**, decoupled from whatever string Riot
ships.

## Queue IDs (match-v5)

`match-v5` identifies queues by numeric `queueId`, unrelated to the league-v4
`queueType` strings:

| queueId | Queue | Constant in `utils/riot_client.py` |
|---|---|---|
| 420 | Ranked Solo/Duo (SR) | `RANKED_SOLO_QUEUE_ID` |
| 440 | Ranked Flex (SR) | — |
| 710 | Ranked 5s (2026 limited test) | `RANKED_5S_QUEUE_ID` |

710 appears in CommunityDragon's queues.json but is **not yet listed** in
Riot's static `queues.json` as of 2026-07-04 (links below).

Usage: `get_match_ids(puuid, queue=710)` filters match history to Ranked 5s;
`GET /lol/match/v5/matches/{id}` responses carry `info.queueId` for filtering
after the fact.

## Ranked 5s: schedule and test window

- Limited-test queue, live **June 26 – September 6 2026** (Riot may extend).
- Open **Friday, Saturday, Sunday, 20:00 → 01:00 next day, in the server's
  local time**. EUW's server time is **Europe/Paris**, so the window is
  20:00–01:00 **CEST** — for UK players that's **19:00–00:00**. Sunday's
  window ends Monday 01:00.
- Premade team of exactly 5, Tournament Draft pick — but ranks are awarded
  **individually**, on a ladder separate from solo/flex.
- Schedule logic lives in `Bot/utils/queue_windows.py`
  (`is_ranked5s_open` / `is_ranked5s_tracking` / `next_window_open`). The
  board keeps polling for a 2 h tail after close so games in flight at 01:00
  and late LP settlement are still captured.

## Raw payload capture (match_raw)

Every Match-V5 payload the bot fetches is archived **verbatim** into the
`match_raw` table (`Bot/db/setup.postgres.sql`):

| column | type | meaning |
|---|---|---|
| `match_id` | TEXT PK | e.g. `EUW1_7371190121` — one row per **match**, not per participant |
| `fetched_at` | TIMESTAMPTZ | when the bot pulled it |
| `payload` | JSONB | the complete `GET /lol/match/v5/matches/{id}` response |

**Why**: `match_stats` extracts a bounded set of columns (core stats plus
the 2026-08 detail columns — see `_DETAIL_COLUMNS` in `cogs/backfill.py` and
`docs/riot-data-gaps.md`). When a new stat is wanted (the `position` column
required a full, rate-limited re-fetch of ~8k matches), it should be a SQL
query or one-off `UPDATE` against `payload`, never another Riot backfill —
`scripts/backfill_match_detail_from_raw.py` is the worked example. Both
ingest paths in `cogs/backfill.py` (5-min stream and `/backfill_all`) write
it with `ON CONFLICT (match_id) DO NOTHING`.

### Extracting a new field with JSONB

The full response shape is `metadata` (the 10 puuids) + `info` (game fields,
`participants[10]`, `teams[2]`). To pull a participant-level field for every
tracked-player row — here `goldEarned` — join on puuid inside the
participants array:

```sql
SELECT ms.match_id,
       ms.puuid,
       (p ->> 'goldEarned')::int                 AS gold_earned,
       (p ->> 'totalDamageDealtToChampions')::int AS champ_damage
FROM match_stats ms
JOIN match_raw mr ON mr.match_id = ms.match_id
CROSS JOIN LATERAL jsonb_array_elements(mr.payload -> 'info' -> 'participants') AS p
WHERE p ->> 'puuid' = ms.puuid;
```

Match-level fields are direct paths: `payload -> 'info' ->> 'gameVersion'`,
`payload -> 'info' -> 'teams' -> 0 -> 'objectives' -> 'baron' ->> 'kills'`.

### Auto-heal for pre-existing matches

Matches ingested before `match_raw` existed have stats rows but no payload.
The backfill pre-filter skips a match only when it is in **both**
`match_stats` (for that puuid) **and** `match_raw`, so:

- the 5-min stream stays cheap — anything fetched after this shipped has
  both rows (it re-fetches at most the last 5 per player once, right after
  deploy);
- a manual `/backfill_all` with `all_history=True` re-fetches exactly the
  old matches missing payloads, through the shared limiter, and the
  `match_stats` conflict no-ops make the re-writes harmless. At the
  developer-tier budget (100 req/120 s) a ~9k-match heal takes roughly
  3 hours of residual budget, sharing with the live boards.

### Storage

A ranked Match-V5 payload is ~60–90 KB as JSON text (10 participants x
~130 fields + ~110 `challenges` fields each). Stored as JSONB it lands in
TOAST with pglz compression: a full-shape synthetic payload measured
**44 KB `pg_column_size`** (77 KB as text); real payloads compress somewhat
better (many zero/repeated values), so figure **~25–45 KB per match** on
disk. For the ~9k matches in the live DB that is **~0.25–0.4 GB**, growing
on the order of tens of MB per month at current tracked-player volume —
comfortable on the DB container's 8 GB rootfs, but worth a
`pg_total_relation_size('match_raw')` glance if the tracked-player list
grows a lot.

## Timelines + capture-everything (2026-08)

`GET /lol/match/v5/matches/{id}/timeline` payloads are archived verbatim
into `match_timeline_raw`, mirroring `match_raw`. Measured on a real
27-minute ranked game: **779 KB as JSON text, 142 KB as JSONB on disk** —
roughly 3× a match payload, the biggest data the bot stores. Both ingest
paths fetch the timeline inline for every NEW match (one extra request);
`/backfill_timelines` heals older matches, paced to ≤ ~60% of the key
budget. Ingest also covers **every queue** now (`get_match_ids` with
`queue=None`), and league entries for flex (or any future ranked queue)
are snapshotted into `league_history` under Riot's own queueType tag.
Full endpoint-by-endpoint status, backfill runbook, depth limits and
storage math: `docs/riot-data-gaps.md`.

## Sources

- Riot dev blog — [/dev: The Return of Ranked 5s](https://www.leagueoflegends.com/en-us/news/dev/dev-the-return-of-ranked-5s/)
- Community schedule/FAQ — [wards.lol/ranked5s](https://wards.lol/ranked5s/)
- CommunityDragon queue list (has 710) — [queues.json](https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/queues.json)
- Riot static queue list (710 missing as of 2026-07-04) — [queues.json](https://static.developer.riotgames.com/docs/lol/queues.json)
- league-v4 / match-v5 API reference — [developer.riotgames.com/apis](https://developer.riotgames.com/apis)
