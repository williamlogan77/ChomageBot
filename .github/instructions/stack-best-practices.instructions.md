---
applyTo: "**/*.py"
---

# Stack Best Practices

## Python Idioms

- Use type hints: `def __init__(self, db_path: str) -> None`
- Async/await for all I/O operations (aiosqlite, discord.py, pantheon)
- Context managers: `async with sqa.connect(self.db_path) as db:`

## Discord.py Patterns

- **Cogs**: Organize commands into classes extending `commands.Cog`
- **Setup function**: Each cog file has `async def setup(bot)` for loading
- **App commands**: Use `@app_commands.command()` for slash commands, not text-based prefix commands
- **Interactions**: Response via `interaction.response.send_message()`, not `ctx.send()`
- **Views/Components**: Use `discord.ui.View` and `discord.ui.UserSelect` for interactive elements

## Error Handling

- **Riot API**: Catch `RateLimit`, `Timeout`, `ServerError` from pantheon, implement backoff
- Example: `await asyncio.sleep(int(limited.timeToWait))` on rate limit

## Database Patterns

- Use aiosqlite for async SQLite operations
- Execute with parameterized queries: `execute("SELECT * FROM users WHERE user_id = ?", (user_id,))`
- `execute_fetchall()` for reading, `commit()` after writes

## Dependency Injection

- Pass `bot` instance to cog constructors: `def __init__(self, bot: MyDiscordBot)`
- Bot stores shared resources: `self.bot.dbutils`, `self.bot.lolapi`, `self.bot.logging`

## Logging

- Use Python logging module: `self.bot.logging.info()`, `.warning()`, `.error()`
- Setup in bot's `setup_hook()`: `discord.utils.setup_logging()`
