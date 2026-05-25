-- Persist journal mode = WAL on this DB. Avoids "database is locked"
-- contention between concurrent writers (e.g. backfill writing match_stats
-- while post_ranks writes league_history). Persistent in the file once set;
-- this line keeps fresh DBs (from a clean container build) on WAL too.
pragma journal_mode = wal;

create table if not exists discord_channels (
    channel_id INTEGER not null primary key,
    name TEXT not null,
    type TEXT
);
create unique index if not exists discord_channels_channel_id_uindex on discord_channels (channel_id);
create table if not exists users (
    user_id INTEGER not null primary key,
    nickname TEXT,
    discord_tag TEXT
);
create table if not exists discord_events (
    event_id INTEGER not null constraint discord_events_pk primary key autoincrement,
    timestamp DATETIME not null DEFAULT CURRENT_TIMESTAMP,
    user_id INTEGER not null references users on update cascade on delete restrict,
    channel_id INTEGER not null references discord_channels on update cascade on delete restrict,
    type TEXT not null,
    metadata TEXT
);
create table if not exists league_players (
    discord_user_id INTEGER not null,
    puuid TEXT not null primary key,
    league_username TEXT not null
);
create table if not exists league_history (
    id INTEGER not null primary key autoincrement,
    puuid TEXT not null,
    timestamp DATETIME not null DEFAULT CURRENT_TIMESTAMP,
    lp INTEGER,
    division TEXT,
    tier TEXT
);
-- Composite PK on (match_id, puuid) so when two tracked players share a
-- game, BOTH rows survive the backfill's INSERT OR IGNORE. A single-column
-- PK on match_id alone silently dropped the second player's row and broke
-- duo / head-to-head detection. Existing DBs get migrated via
-- scripts/migrate_match_stats_composite_pk.py — `if not exists` here only
-- covers fresh installs.
create table if not exists match_stats (
    match_id TEXT not null,
    puuid TEXT not null,
    game_start DATETIME not null,
    queue_id INTEGER not null,
    champion TEXT not null,
    win INTEGER not null,
    kills INTEGER not null,
    deaths INTEGER not null,
    assists INTEGER not null,
    duration_sec INTEGER not null,
    patch_version TEXT,
    primary key (match_id, puuid)
);
create index if not exists idx_match_stats_puuid_time on match_stats (puuid, game_start DESC);
create unique index if not exists discord_events_event_id_uindex on discord_events (event_id);
create unique index if not exists users_user_id_uindex on users (user_id);
-- Audit log of slash commands + button clicks + select-menu picks. Used to
-- surface which features actually get used, so we can prune the unused ones.
-- Written by the `usage_logger` cog via the on_interaction event. Indexed
-- on (timestamp, command_name) for the typical "top commands in last N days"
-- query.
create table if not exists command_usage (
    id INTEGER not null primary key autoincrement,
    timestamp DATETIME not null DEFAULT CURRENT_TIMESTAMP,
    command_name TEXT not null,
    user_id TEXT,
    guild_id TEXT,
    interaction_type TEXT not null
);
create index if not exists idx_command_usage_time_cmd on command_usage (timestamp DESC, command_name);