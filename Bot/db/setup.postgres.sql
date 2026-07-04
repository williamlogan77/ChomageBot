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
create table if not exists league_history (
    id BIGINT generated always as identity primary key,
    puuid TEXT not null,
    timestamp TIMESTAMPTZ not null default now(),
    lp INTEGER,
    division TEXT,
    tier TEXT,
    wins INTEGER,
    losses INTEGER,
    queue TEXT not null default 'RANKED_SOLO_5x5'
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
    primary key (match_id, puuid)
);
create index if not exists idx_match_stats_puuid_time on match_stats (puuid, game_start desc);

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
