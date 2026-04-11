---
applyTo: "**/*.py"
---

# ChomageBot Overview

## Purpose

Private Discord bot tracking League of Legends rankings for a friend group, deployed on AWS EC2.

## Business Domain

- **Primary Function**: Monitor and display League of Legends player rankings
- **Data Collection**: Riot API integration (via pantheon library) for player stats, rankings, match history
- **Discord Integration**: Slash commands for user management, team generation, rank visualization
- **Persistence**: SQLite database tracking Discord users, League players, historical rankings

## Key Workflows

1. **User Sync**: On bot connect, fetch Discord server members/channels → store in SQLite
2. **Rank Updates**: Periodic tasks query Riot API → update league_history table
3. **Team Selection**: Slash command `/select_teams` allows interactive user selection via dropdowns
4. **Player Linking**: Users attach League accounts to Discord profiles via slash commands

## User Types

- Discord server members (friends group)
- Bot administrator (owner, deployment manager)

## Integration Points

- Riot Games API (via pantheon library)
- Discord API (via discord.py)
- SQLite database (aiosqlite for async operations)
- Docker for containerization and deployment

## Architecture

Bot uses discord.py cogs pattern for command organization. Main entry in `Bot/main.py`, features split into `cogs/` modules.
