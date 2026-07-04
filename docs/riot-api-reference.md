# Riot API reference (for this repo)

Working notes for maintaining the bot's Riot integration. Facts current as of
**2026-07-04**.

## Hosts and routing

Riot splits its API across two kinds of hosts, and picking the wrong one 404s:

| Host | Routing | APIs used here | Wrapper (Bot/utils/riot_client.py) |
|---|---|---|---|
| `https://euw1.api.riotgames.com` | platform (EUW) | league-v4 | `get_league_entries(puuid)` |
| `https://europe.api.riotgames.com` | regional | match-v5 | `get_match_ids(puuid, count, queue, start)`, `get_match(match_id)` |

All Riot HTTP goes through `Bot/utils/riot_client.py` — do **not** add HTTP
calls elsewhere. Reasons:

- **Rate limits are per API key, shared across everything**: developer tier is
  **20 requests / 1 s** and **100 requests / 120 s**. `riot_client` enforces
  both windows with a single process-wide limiter (`_wait_for_slot`); a second
  limiter elsewhere would silently blow the budget. (This has bitten before —
  a standalone backfill process starved the live loops.)
- 429s are retried internally (honours `Retry-After` + jitter, up to
  `MAX_RETRIES = 2`).
- **Entries TTL cache**: `get_league_entries` caches responses for **100 s**
  per puuid. One league-v4 response lists *all* of a player's ranked queues,
  so the solo board and the Ranked 5s board (both on 120 s loops) share one
  fetch per player per cycle. Pass `fresh=True` to bypass.

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

**Ranked 5s: the queueType string is NOT yet documented as of 2026-07-04**
(Riot's OpenAPI enum still only lists the values above).
`cogs/ranked5s_table_updater.py` auto-discovers it at runtime: any `RANKED_*`
string not in the known set is treated as the 5s ladder and logged with a
warning. Once the string shows up in the logs, pin it via the
`ranked5s_queue_type` env var in `.env` so the heuristic stops guessing.

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

## Sources

- Riot dev blog — [/dev: The Return of Ranked 5s](https://www.leagueoflegends.com/en-us/news/dev/dev-the-return-of-ranked-5s/)
- Community schedule/FAQ — [wards.lol/ranked5s](https://wards.lol/ranked5s/)
- CommunityDragon queue list (has 710) — [queues.json](https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/queues.json)
- Riot static queue list (710 missing as of 2026-07-04) — [queues.json](https://static.developer.riotgames.com/docs/lol/queues.json)
- league-v4 / match-v5 API reference — [developer.riotgames.com/apis](https://developer.riotgames.com/apis)
