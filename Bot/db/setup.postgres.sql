-- Postgres schema for ChomageBot. Applied on every boot by main.py via
-- utils/db.apply_schema — every statement is IF NOT EXISTS so this is
-- idempotent. Data arrives from the old sqlite file via
-- scripts/migrate_sqlite_to_postgres.py.
--
-- Identifiers are deliberately unquoted (lower-case folded) so queries can
-- reference them without quoting: the legacy mixed-case sqlite column
-- league_players."leagueId" is plain leagueid here, and unquoted
-- ``leagueId`` in queries folds to it.

create table if not exists discord_channels (
    channel_id BIGINT not null primary key,
    name TEXT not null,
    type TEXT
);

create table if not exists users (
    user_id BIGINT not null primary key,
    nickname TEXT,
    discord_tag TEXT
);

create table if not exists discord_events (
    event_id BIGINT generated always as identity primary key,
    timestamp TIMESTAMPTZ not null default now(),
    user_id BIGINT not null references users on update cascade on delete restrict,
    channel_id BIGINT not null references discord_channels on update cascade on delete restrict,
    type TEXT not null,
    metadata TEXT
);

-- leagueid: legacy encrypted summoner ID, still the PK because pre-2024
-- league_history rows are keyed by it (newer rows use the real puuid —
-- queries check both, see get_last_five_games).
create table if not exists league_players (
    discord_user_id BIGINT not null,
    leagueid TEXT not null primary key,
    league_username TEXT not null,
    puuid TEXT,
    tag TEXT
);

-- queue: league-v4 queueType this snapshot belongs to. Pre-existing rows
-- migrate as RANKED_SOLO_5x5; the Ranked 5s board writes RANKED_5S
-- (canonical internal constant — see cogs/ranked5s_table_updater.py).
-- Since 2026-08 every other ranked queue in the entries response (flex
-- as RANKED_FLEX_SR, anything Riot adds later) is snapshotted too,
-- tagged with Riot's own queueType string (cogs/league_table_updater.py).
-- raw: the complete league-v4 entry this snapshot was extracted from,
-- archived verbatim (same rationale as match_raw below: hotStreak,
-- miniSeries, the real queueType string etc. stay queryable without a
-- re-fetch). NULL on rows from before the column existed — league-v4 has
-- no history endpoint, so those snapshots are unrecoverable by design.
create table if not exists league_history (
    id BIGINT generated always as identity primary key,
    puuid TEXT not null,
    timestamp TIMESTAMPTZ not null default now(),
    lp INTEGER,
    division TEXT,
    tier TEXT,
    wins INTEGER,
    losses INTEGER,
    queue TEXT not null default 'RANKED_SOLO_5x5',
    raw JSONB
);
create index if not exists idx_league_history_puuid_id on league_history (puuid, id desc);
create index if not exists idx_league_history_queue on league_history (queue);

-- Composite PK on (match_id, puuid) so when two tracked players share a
-- game, BOTH rows survive the backfill's upsert (single-column PK on
-- match_id silently dropped the second player's row and broke duo /
-- head-to-head detection).
create table if not exists match_stats (
    match_id TEXT not null,
    puuid TEXT not null,
    game_start TIMESTAMPTZ not null,
    queue_id INTEGER not null,
    champion TEXT not null,
    win SMALLINT not null,
    kills INTEGER not null,
    deaths INTEGER not null,
    assists INTEGER not null,
    duration_sec INTEGER not null,
    patch_version TEXT,
    -- Riot Match-V5 teamPosition (TOP/JUNGLE/MIDDLE/BOTTOM/UTILITY), the
    -- actual role played that game. Empty "" on remakes / very old matches;
    -- NULL on rows inserted before this column existed.
    position TEXT,
    -- Riot Match-V5 teamId (100 blue / 200 red). Two tracked players with
    -- the same match_id AND team_id played together (duo detection for the
    -- board's Last 5). NULL on rows from before this column existed —
    -- backfillable from match_raw payloads with a single UPDATE, no API.
    team_id SMALLINT,
    primary key (match_id, puuid)
);
create index if not exists idx_match_stats_puuid_time on match_stats (puuid, game_start desc);
-- Existing installs predate the column; CREATE IF NOT EXISTS won't add it.
alter table match_stats add column if not exists team_id SMALLINT;

-- Rich per-participant Match-V5 detail (2026-08). All nullable; ingested
-- going forward by cogs/backfill.py and backfillable for historical rows
-- from the archived match_raw payloads with
-- scripts/backfill_match_detail_from_raw.py (pure SQL, zero API spend).
-- NULL means "not yet backfilled", except the challenges-derived trio
-- (solo_kills, kill_participation, team_damage_pct) and the ping columns,
-- which are also NULL on payloads too old to carry those fields.
alter table match_stats add column if not exists damage_to_champs INTEGER;   -- totalDamageDealtToChampions
alter table match_stats add column if not exists damage_taken INTEGER;       -- totalDamageTaken
alter table match_stats add column if not exists gold_earned INTEGER;
alter table match_stats add column if not exists cs INTEGER;                 -- totalMinionsKilled + neutralMinionsKilled
alter table match_stats add column if not exists vision_score INTEGER;
alter table match_stats add column if not exists wards_placed INTEGER;
alter table match_stats add column if not exists wards_killed INTEGER;
alter table match_stats add column if not exists control_wards INTEGER;      -- detectorWardsPlaced
alter table match_stats add column if not exists pings_total INTEGER;        -- sum of all *Pings counters
alter table match_stats add column if not exists pings_missing INTEGER;      -- enemyMissingPings (the "?" ping)
alter table match_stats add column if not exists largest_multi_kill SMALLINT;
alter table match_stats add column if not exists penta_kills SMALLINT;
alter table match_stats add column if not exists time_dead_sec INTEGER;      -- totalTimeSpentDead
alter table match_stats add column if not exists turret_takedowns SMALLINT;
alter table match_stats add column if not exists objectives_stolen SMALLINT;
alter table match_stats add column if not exists first_blood SMALLINT;       -- firstBloodKill, 0/1
alter table match_stats add column if not exists early_surrender SMALLINT;   -- gameEndedInEarlySurrender (remake), 0/1
alter table match_stats add column if not exists solo_kills SMALLINT;        -- challenges.soloKills
alter table match_stats add column if not exists kill_participation REAL;    -- challenges.killParticipation, 0..1
alter table match_stats add column if not exists team_damage_pct REAL;       -- challenges.teamDamagePercentage, 0..1

-- Complete Match-V5 JSON payload, archived verbatim at ingest. One row per
-- MATCH (not per participant — tracked players often share a game; the
-- per-player extract lives in match_stats). Exists so adding a new stat
-- later is a JSONB query or one-off UPDATE against payload instead of a
-- full Riot re-fetch backfill (the position column needed one; never again).
-- Old matches ingested before this table existed are healed lazily: the
-- backfill pre-filter re-fetches any match missing from here (see
-- cogs/backfill.py), so a manual /backfill_all all_history=True fills it.
create table if not exists match_raw (
    match_id TEXT not null primary key,
    fetched_at TIMESTAMPTZ not null default now(),
    payload JSONB not null
);

-- Complete Match-V5 TIMELINE payload, archived verbatim (2026-08,
-- capture-everything pass). One row per MATCH, same rationale and
-- lifecycle as match_raw above; fetched best-effort right after the
-- match payload at ingest, and healed for historical matches by the
-- /backfill_timelines command (timelines are NOT recoverable from
-- match_raw — they're a separate endpoint — so the heal is API-based).
-- A match with no row either predates capture or 404'd (timelines age
-- out of Riot's window before match details do).
-- Storage note: timelines are the biggest payloads the bot stores
-- (~100-250 KB JSONB per match after compression, vs ~25-45 KB for
-- match_raw) — see docs/riot-data-gaps.md before running a full heal.
create table if not exists match_timeline_raw (
    match_id TEXT not null primary key,
    fetched_at TIMESTAMPTZ not null default now(),
    payload JSONB not null
);

-- Challenges-v1 snapshots (capture-everything pass): per-challenge
-- progress, one row per (account, challenge), upserted by
-- cogs/profile_updater.py twice a day. Current-state like
-- champion_mastery — percentile/level/value move both ways but the
-- history isn't interesting enough to keep.
create table if not exists player_challenges (
    puuid TEXT not null,
    challenge_id BIGINT not null,
    level TEXT,
    value DOUBLE PRECISION,
    percentile REAL,
    position INTEGER,
    players_in_level INTEGER,
    achieved_time TIMESTAMPTZ,
    updated_at TIMESTAMPTZ not null default now(),
    primary key (puuid, challenge_id)
);

-- One row per account: challenges-v1 totals + everything else in the
-- PlayerInfoDto kept verbatim (category_points, preferences incl. the
-- equipped title/banner; raw is the complete response) so nothing from
-- the endpoint is dropped.
create table if not exists player_challenge_summary (
    puuid TEXT not null primary key,
    total_level TEXT,
    total_current INTEGER,
    total_max INTEGER,
    total_percentile REAL,
    category_points JSONB,
    preferences JSONB,
    raw JSONB,
    updated_at TIMESTAMPTZ not null default now()
);

-- Solo-queue apex promotion cutoffs (league-v4 challenger/grandmaster
-- ladders): min LP currently seated + ladder size, upserted by
-- cogs/profile_updater.py. Feeds "distance to Challenger" gag stats.
create table if not exists ladder_cutoffs (
    queue TEXT not null,
    tier TEXT not null,
    cutoff_lp INTEGER,
    players INTEGER,
    updated_at TIMESTAMPTZ not null default now(),
    primary key (queue, tier)
);

-- Summoner-v4 basics (capture-everything pass), upserted by
-- cogs/profile_updater.py alongside the challenge sweep. revision_date
-- is Riot's "profile last modified" timestamp.
create table if not exists summoner_profile (
    puuid TEXT not null primary key,
    summoner_level INTEGER,
    profile_icon_id INTEGER,
    revision_date TIMESTAMPTZ,
    updated_at TIMESTAMPTZ not null default now()
);

-- Audit log of slash commands + button clicks + select-menu picks.
create table if not exists command_usage (
    id BIGINT generated always as identity primary key,
    timestamp TIMESTAMPTZ not null default now(),
    command_name TEXT not null,
    user_id TEXT,
    guild_id TEXT,
    interaction_type TEXT not null
);
create index if not exists idx_command_usage_time_cmd on command_usage (timestamp desc, command_name);

-- Small key/value store for runtime bot configuration set via slash
-- commands (e.g. ranked5s_channel_id). Avoids hardcoding channel IDs.
create table if not exists bot_config (
    key TEXT not null primary key,
    value TEXT not null,
    updated_at TIMESTAMPTZ not null default now()
);

-- Weekly awards ceremony winners (cogs/weekly_awards.py): one row per
-- (week, award, winner) — ties write several rows. week_start is the
-- Monday (Europe/London) the awarded week began. detail carries the
-- award-specific evidence (scoreline, duo record, per-account LP deltas)
-- so the trophy cabinet re-renders without recomputing old weeks.
create table if not exists weekly_awards (
    id BIGINT generated always as identity primary key,
    week_start DATE not null,
    award TEXT not null,
    discord_user_id BIGINT not null,
    display_name TEXT,
    value NUMERIC,
    detail JSONB,
    created_at TIMESTAMPTZ not null default now(),
    unique (week_start, award, discord_user_id)
);

-- Per-week award controls, written by the DASHBOARD (grants for the
-- chomage_dash role are documented in the dashboard README) and read by
-- the Monday ceremony (cogs/weekly_awards.py + utils/awards.py):
--   forced_winner     overrides the computed winner for that week's
--                     award — honored only while they still qualify as
--                     a candidate, posted with a "chosen by management"
--                     note;
--   excluded_user_ids recomputes the award as if those users didn't
--                     play that week ("on holiday");
--   chosen_metric     re-points the award at a different weekly measure
--                     for that week (utils/awards.METRICS key — most
--                     deaths, most time dead, lowest winrate, ...);
--   chosen_scope      picks which queues count that week
--                     (utils/awards.SCOPES key — all/ranked/aram/...).
-- NULL metric/scope fall back to the award's built-in metric and the
-- awards_scope bot_config default. Precedence at ceremony time:
-- exclusion > forced_winner > chosen metric/scope > defaults.
-- One row per (week, award); week_start is the Monday (Europe/London)
-- the awarded week began, same convention as weekly_awards. Per-award
-- enable/disable and taglines live in bot_config
-- (award_<key>_enabled / award_<key>_tagline), not here.
create table if not exists award_overrides (
    week_start DATE not null,
    award_key TEXT not null,
    forced_winner BIGINT,
    excluded_user_ids BIGINT[],
    chosen_metric TEXT,
    chosen_scope TEXT,
    primary key (week_start, award_key)
);
-- Existing installs predate the measure columns (2026-08 picker).
alter table award_overrides add column if not exists chosen_metric TEXT;
alter table award_overrides add column if not exists chosen_scope TEXT;

-- Dashboard-managed display aliases. users.nickname is re-upserted from
-- the guild on every boot (main.py member sync), so a user-chosen name
-- needs a store that sync never touches. Written by the dashboard
-- (grants in the dashboard README); the bot reads it when rendering
-- award display names so both surfaces call people the same thing.
create table if not exists user_aliases (
    user_id BIGINT not null primary key,
    alias TEXT not null,
    set_by BIGINT,
    updated_at TIMESTAMPTZ not null default now()
);

-- Live (spectator-v5) games for tracked players (cogs/live_games.py):
-- one row per (game, tracked player), upserted each poll while the game
-- runs and pruned by staleness once it ends. payload is the raw
-- spectator response (participants, bans, clock) — the dashboard's
-- /live page renders straight from it.
create table if not exists live_games (
    game_id BIGINT not null,
    puuid TEXT not null,
    queue_id INTEGER,
    game_start TIMESTAMPTZ,
    payload JSONB,
    seen_at TIMESTAMPTZ not null default now(),
    primary key (game_id, puuid)
);

-- Champion mastery snapshots (champion-mastery-v4), one row per
-- (account, champion), upserted by cogs/mastery_updater.py twice a day.
-- Current-state table, not a history: points only ever grow, so the
-- upsert keeps the latest values and updated_at says how fresh they are.
-- champion_name comes from Data Dragon at snapshot time (championId is
-- what Riot returns); NULL when the ddragon lookup failed — the upsert
-- never overwrites a known name with NULL. raw is the complete mastery
-- entry verbatim (milestone grades, nextSeasonMilestone requirements —
-- everything the extract skips).
create table if not exists champion_mastery (
    puuid TEXT not null,
    champion_id INTEGER not null,
    champion_name TEXT,
    level INTEGER not null,
    points INTEGER not null,
    points_since_last_level INTEGER,
    points_until_next_level INTEGER,
    tokens_earned INTEGER,
    milestone INTEGER,
    last_play_time TIMESTAMPTZ,
    raw JSONB,
    updated_at TIMESTAMPTZ not null default now(),
    primary key (puuid, champion_id)
);
create index if not exists idx_champion_mastery_points on champion_mastery (points desc);

-- Clash tournament schedule (clash-v1, capture-remainder pass 2026-08):
-- one row per tournament, upserted daily by cogs/clash_updater.py.
-- Tournaments drop off Riot's response once finished — rows here persist,
-- so the table accumulates the full schedule history. schedule is the
-- phase list ([{registrationTime, startTime, cancelled}]); raw the whole
-- TournamentDto verbatim.
create table if not exists clash_tournaments (
    tournament_id BIGINT not null primary key,
    theme_id INTEGER,
    name_key TEXT,
    name_key_secondary TEXT,
    schedule JSONB,
    raw JSONB,
    first_seen_at TIMESTAMPTZ not null default now(),
    updated_at TIMESTAMPTZ not null default now()
);

-- A tracked player's Clash team registration as seen by the daily sweep
-- (clash-v1 /players/by-puuid). Rows are never deleted: team ids are
-- per-tournament, so accumulated rows ARE the history of Clash teams
-- played on. Outside a registration window the endpoint returns [] and
-- nothing is written.
create table if not exists clash_registrations (
    puuid TEXT not null,
    team_id TEXT not null,
    position TEXT,
    role TEXT,
    raw JSONB,
    first_seen_at TIMESTAMPTZ not null default now(),
    last_seen_at TIMESTAMPTZ not null default now(),
    primary key (puuid, team_id)
);

-- Clash team rosters (clash-v1 /teams/{id}), fetched once per distinct
-- team id discovered in clash_registrations each sweep. captain is
-- whatever identifier Riot returns (documented as an encrypted summoner
-- id); players is the roster array verbatim; raw the whole TeamDto —
-- the endpoint couldn't be live-validated (nobody registered during the
-- 2026-08-08 pass) so extraction is defensive and raw is the authority.
create table if not exists clash_teams (
    team_id TEXT not null primary key,
    tournament_id BIGINT,
    name TEXT,
    abbreviation TEXT,
    icon_id INTEGER,
    tier INTEGER,
    captain TEXT,
    players JSONB,
    raw JSONB,
    first_seen_at TIMESTAMPTZ not null default now(),
    updated_at TIMESTAMPTZ not null default now()
);

-- Challenges-v1 static catalogue (config + percentiles merged), keyed by
-- challengeId — the decoder ring for player_challenges' bare ids.
-- Upserted daily by cogs/game_data_updater.py. name/descriptions are the
-- en_GB localisation (en_US fallback); thresholds maps level name ->
-- required value; percentiles maps tier -> population fraction (from the
-- separate /percentiles endpoint); raw is the ChallengeConfigInfoDto
-- verbatim (all 28 locales included).
create table if not exists challenge_config (
    challenge_id BIGINT not null primary key,
    name TEXT,
    short_description TEXT,
    description TEXT,
    state TEXT,
    tracking TEXT,
    start_timestamp TIMESTAMPTZ,
    end_timestamp TIMESTAMPTZ,
    leaderboard BOOLEAN,
    thresholds JSONB,
    percentiles JSONB,
    raw JSONB,
    updated_at TIMESTAMPTZ not null default now()
);

-- Free champion rotation (champion-v3): one row per OBSERVED rotation,
-- inserted by cogs/game_data_updater.py when the fetched rotation
-- differs from the latest stored row (same rotation just bumps
-- last_seen_at) — so history accumulates at ~1 row/week without a
-- unique-key gymnastic. Live shape 2026-08-08 is {"sr": [...],
-- "newplayer": [...]}; the documented freeChampionIds shape is also
-- handled. Champion ids resolve to names via champion_mastery or ddragon.
create table if not exists champion_rotations (
    id BIGINT generated always as identity primary key,
    free_champion_ids JSONB,
    new_player_ids JSONB,
    max_new_player_level INTEGER,
    raw JSONB,
    first_seen_at TIMESTAMPTZ not null default now(),
    last_seen_at TIMESTAMPTZ not null default now()
);

-- lol-status-v4 events affecting EUW: one row per incident/maintenance
-- Riot publishes, upserted hourly by cogs/status_updater.py WHILE the
-- entry is live. Steady-state "all fine" polls write nothing — this
-- table only ever holds the interesting moments ("EUW died mid-Clash").
-- status-v4 field names are snake_case (Riot quirk); timestamps arrive
-- as strings and parse best-effort (NULL + raw when unparseable).
create table if not exists lol_status_events (
    status_id BIGINT not null,
    kind TEXT not null,             -- 'incident' | 'maintenance'
    status TEXT,                    -- maintenance_status: scheduled/in_progress/complete
    severity TEXT,                  -- incident_severity: info/warning/critical
    title TEXT,                     -- en_GB/en_US title extract
    platforms JSONB,
    created_at TIMESTAMPTZ,
    updated_at_riot TIMESTAMPTZ,
    archive_at TIMESTAMPTZ,
    raw JSONB,
    first_seen_at TIMESTAMPTZ not null default now(),
    last_seen_at TIMESTAMPTZ not null default now(),
    primary key (status_id, kind)
);

-- Auto-detected season/split boundaries (utils/seasons.py): one row per
-- ladder reset, derived from league_history games totals shrinking.
-- started_at is the first post-reset snapshot observed — Riot's actual
-- reset moment is a little earlier, bounded by how fast the first
-- tracked player re-placed. Consumers (e.g. the ranked graphs) read
-- MAX(started_at) as "the current ladder began here".
create table if not exists seasons (
    id BIGINT generated always as identity primary key,
    started_at TIMESTAMPTZ not null unique,
    detected_at TIMESTAMPTZ not null default now()
);
