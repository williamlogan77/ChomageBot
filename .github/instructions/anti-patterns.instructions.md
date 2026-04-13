---
applyTo: "**/*.py"
---

# Anti-Patterns to Avoid

## Security Issues

- **Hardcoded Server ID**: `serverid=667751882260742164` in main.py - should use environment variable
- **Logging Sensitive Data**: Avoid logging user tokens, API keys directly
- **SQL Injection Risk**: Always use parameterized queries, never string formatting: ❌ `f"SELECT * FROM users WHERE id = {user_id}"` ✅ `execute("SELECT * FROM users WHERE id = ?", (user_id,))`

## Code Smells

- **Unused Imports**: Remove dead code (e.g., sys import in main.py if not used)
- **Commented Code**: Clean up large commented blocks in main.py (lines 118-133)
- **Global State**: Avoid module-level mutable state, use bot instance attributes

## Discord.py Specific

- **Blocking Operations**: Never use synchronous I/O in async functions (e.g., `sqlite3` instead of `aiosqlite`)
- **Missing Error Handlers**: Always handle Interaction failures with try/except
- **Guild ID Hardcoding**: Use configuration for deployment flexibility

## API Usage

- **Rate Limit Ignorance**: Must handle Riot API rate limits (see league_table_updater.py for correct pattern)
- **No Retry Logic**: Implement exponential backoff for external API calls
- **Missing Timeouts**: Add timeout parameters to HTTP requests

## Database

- **Missing Indexes**: Ensure indexed columns for frequent queries (already done in setup.sql)
- **No Connection Pooling**: For SQLite, use context managers to avoid leaks
