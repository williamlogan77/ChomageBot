---
applyTo: "Bot/db/setup.sql"
---

# Data Models

## Core Entities

### users

- **Purpose**: Discord server members
- **Key Fields**: user_id (PK), nickname, discord_tag
- **Relationships**: Referenced by discord_events, league_players

### league_players

- **Purpose**: Link Discord users to League of Legends accounts
- **Key Fields**: puuid (PK, Riot Games unique ID), discord_user_id, league_username
- **Relationships**: References users, linked to league_history

### league_history

- **Purpose**: Track player rank changes over time
- **Key Fields**: id (PK), puuid, timestamp, lp (League Points), division, tier
- **Relationships**: References league_players via puuid

### discord_channels

- **Purpose**: Cache Discord server channels
- **Key Fields**: channel_id (PK), name, type
- **Usage**: Referenced by discord_events

### discord_events

- **Purpose**: Log bot activities (currently unused but schema exists)
- **Key Fields**: event_id (PK), timestamp, user_id, channel_id, type, metadata

## Domain Objects

- **Ranker** class (`utils/rank_sorting_class.py`): Comparable rank representation, converts tier/division/LP to numeric score
- Supports leagues: Iron → Bronze → Silver → Gold → Platinum → Emerald → Diamond → Master

## Validation

- PUUID from Riot API is primary identifier for League players
- Timestamps default to CURRENT_TIMESTAMP
- Foreign key constraints with cascade on update, restrict on delete
