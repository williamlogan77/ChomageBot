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
    division INTEGER,
    tier TEXT
);
create unique index if not exists discord_events_event_id_uindex on discord_events (event_id);
create unique index if not exists users_user_id_uindex on users (user_id);