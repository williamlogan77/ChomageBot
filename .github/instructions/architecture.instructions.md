---
applyTo: "**/*.py"
---

# ChomageBot Architecture

## Folder Structure

- `Bot/` - Main application code
  - `main.py` - Entry point, bot initialization, database setup
  - `cogs/` - Discord.py cog modules (commands, tasks)
  - `utils/` - Helper classes (db_utils, rank_sorting_class, autocomplete)
  - `db/` - Database schema (setup.sql) and SQLite file

## Module Organization

- **Main Bot Class**: `MyDiscordBot` extends `discord.ext.commands.Bot`
- **Cog Pattern**: Features separated into loadable extensions (`league_table_updater.py`, `team_generator.py`, etc.)
- **Utilities**: `DButils` for database operations, `Ranker` for rank comparisons, autocomplete transformers

## Layer Dependencies

```
main.py (Bot initialization)
  ↓
cogs/ (Command handlers, tasks)
  ↓
utils/ (Database, calculations, autocomplete)
```

## Communication Patterns

- **Async/await**: All Discord and database operations use asyncio
- **Task Loops**: `@tasks.loop()` decorator for periodic Riot API polling
- **App Commands**: `@app_commands.command()` for Discord slash commands
- **Cog Loading**: Dynamic cog discovery via `glob.glob("./cogs/*.py")`

## External Services

- **Riot API**: pantheon library wrapper, rate limit handling in cogs
- **Discord Gateway**: discord.py handles WebSocket connection, events
