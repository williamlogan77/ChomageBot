---
applyTo: "Bot/**/*.{py,sh}"
---

# Commands & Scripts

## Build & Deploy

- **Build**: `docker compose build` - Builds Python 3.11 image with dependencies
- **Run**: `docker compose up` - Starts bot container with volume mounts and auto-restart
- **Entry Point**: `startup.sh` → creates `tmp/` directory, runs graph generation, starts bot

## Bot Management

- **Sync Commands**: `/sync` command in `chommage_admin` channel - Updates slash commands to Discord
- **Manual Start**: `python3 -u main.py` (from Bot/ directory with .env in parent)

## Database

- **Initialization**: Automatic on first run via `setup_db()` in main.py
- **Schema**: `Bot/db/setup.sql` creates tables with indexes
- **Location**: `./db/database.sqlite` relative to Bot/ directory

## Development Scripts

- **Graph Generation**: `python3 utils/create_ranked_graph.py` - Creates rank visualization (runs on startup)
- Located in `Bot/utils/` directory

## Slash Commands (Discord)

- `/select_teams` - Generate balanced teams from selected users (team_generator.py)
- `/sync` - Sync bot commands to Discord server (sync.py cog)
- Additional commands in league_table_updater.py and league_user_updater.py cogs

## Prerequisites

- Python 3.11
- Docker and Docker Compose
- `.env` file with `token` and `riot_key`
