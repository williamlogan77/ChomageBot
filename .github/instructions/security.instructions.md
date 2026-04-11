---
applyTo: "**/*.py"
---

# Security & Configuration

## Environment Variables

- **Required**: `.env` file in parent directory (`../Bot` relative to docker context)
  - `token` - Discord bot token
  - `riot_key` - Riot Games API key
- **Loading**: `load_dotenv("../.env")` in main.py
- **Access**: `os.environ.get("token")`, `os.environ.get("riot_key")`

## Secrets Management

- ✅ Store tokens in `.env` file (gitignored)
- ❌ Never commit `.env` to version control
- ❌ Avoid hardcoding server IDs, prefer environment variables

## Authentication Flow

- **Discord**: Bot token authentication via `bot.start(token)`
- **Riot API**: API key passed to pantheon library: `pantheon.Pantheon("euw1", os.environ.get("riot_key"))`

## Docker Security

- Environment variables passed via `env_file` in docker-compose.yml
- Volume mounts for code and database: `./Bot:/Bot`
- Container runs as root (consider adding USER directive for production)

## API Security

- **Rate Limiting**: Implement retry logic for Riot API (see league_table_updater.py)
- **Input Validation**: Discord slash commands have type checking via app_commands
- **SQL Injection**: Use parameterized queries exclusively

## Compliance

- Data stored: Discord user IDs, usernames, League of Legends PUUIDs
- Consider GDPR implications for user data (right to deletion, data portability)
- No sensitive game data or personal information beyond public profiles
