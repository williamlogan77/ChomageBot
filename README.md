# ChomageBot

ChomageBot is a small discord bot that I personally host on an AWS EC2 instance that is deployed to a private server for me and my friends. It primarily tracks rankings in the game League of Legends and has implemented slash commands for customisation of users.

The bot is fully encapsulated using docker and should be deployable to any environment.


# Getting started

1. Clone the repository (`git clone https://github.com/williamlogan77/ChomageBot.git`)

2. Create a `.env` file that contains a discord token (named `token`), a riot token (named `riot_key`), and a `DATABASE_URL` pointing at your Postgres server. The bot does not bundle a database — in production Postgres runs in its own LXC on the Proxmox host (see `docs/postgres-migration-runbook.md`); for local development any Postgres works, e.g. `docker run -d -e POSTGRES_USER=chomage -e POSTGRES_PASSWORD=chomage -e POSTGRES_DB=chomage -p 5432:5432 postgres:16-alpine`.

3. Build the container with `docker compose build` and run the container with `docker compose up`. The bot applies the schema (`Bot/db/setup.postgres.sql`) automatically on boot.

4. If you have data in an old SQLite file, copy it into Postgres once with `python scripts/migrate_sqlite_to_postgres.py` (idempotent; prints per-table row counts; `--sqlite` / `--database-url` to override the defaults).

5. If changes are made in cogs, run the `/sync` command in the `chommage_admin` channel to update these to the bot

# To-do

* Migrate from SQLite to PostGresSQL (WIP on a local branch)
  * Use AWS RDS for persistent data store?
* Implement front-end dashboard/control
* Add in persistent view for management of bot
* Find new hosting environment (free EC2 instance almost up)
* Implement local caching using a temp db/redis for calls to riot API
* Use AWS lambda + gatway API to modularise some of the code
  * This would require AWS cdk to maintain

# Notes

If you have come across this repository, please feel free to rework/update features and suggest new implementations.


