# Postgres cutover runbook — dedicated LXC on Proxmox

One-time migration of the live bot (LXC 103 `chomagebot`, repo at
`/root/ChomageBot`, deployed by the cron in `scripts/deploy.sh`) from the
bind-mounted SQLite file to a **Postgres server in its own LXC** on the same
Proxmox host (`pve`, 192.168.0.3). The bot container reaches it over the
LAN bridge via the `DATABASE_URL` env var — no database service runs inside
LXC 103. Written 2026-07-04.

> **⚠️ Ordering matters.** The deploy cron pulls `origin/main` every 2
> minutes, `Bot/` is bind-mounted into the running container, and
> auto_reload hot-reloads changed cogs/utils in place. If this branch
> lands on `main` while the OLD container is still running, the live bot
> immediately hot-reloads code its image cannot import (`psycopg` isn't
> installed there) and every cog reload fails. **Pause the cron (Phase 0)
> and stop the bot before the code ever reaches LXC 103.**

Expected bot downtime: **~5 minutes** (Phase C: pull + image build +
migrate).

## Phase A — database LXC  ✅ DONE 2026-07-04

Already provisioned:

- **CT 105 `chomage-db`** — Debian 12 (`debian-12-standard_12.12-1`),
  1 GB RAM / 1 core / 8 GB on local-lvm, unprivileged, onboot.
- Static **192.168.0.5/24** (below pihole's DHCP pool, which starts at
  .10), gw 192.168.0.1, DNS 192.168.0.2. Avoid 192.168.0.4 — something
  else on the LAN already sits there.
- PostgreSQL **15** (Debian package), `listen_addresses = '*'`, pg_hba
  allows `chomage` from 192.168.0.0/24 (scram-sha-256).
- Role `chomage` + database `chomage` created; the password lives only in
  `/root/ChomageBot/.env` on LXC 103 as part of `DATABASE_URL`
  (added 2026-07-04, inert until the container restarts on new code).
- Verified: authed psql over 192.168.0.5 works; LXC 103 reaches port 5432.

## Phase 0 — pause the deploy cron (LXC 103, before merging anything)

```bash
pct enter 103
# Comment out the deploy entry; re-enabled in Phase D.
crontab -l | sed 's|^\(.*scripts/deploy.sh.*\)$|#\1|' | crontab -
crontab -l   # confirm the deploy line starts with '#'
```

Only merge/push the branch to `main` once this is done.

## Phase B — merge (cron is paused, bot untouched)

With the Phase 0 cron pause in place, merge the branch to `main`. Nothing
pulls it onto LXC 103 automatically anymore.

## Phase C — cutover (downtime starts at the stop)

The pull happens ONLY after the bot is stopped — with the bind-mounted
`Bot/` and auto_reload, pulling under a live container would hot-load
code the old image can't import.

```bash
pct enter 103
cd /root/ChomageBot

docker compose stop myapp              # downtime starts

# Backup the (now quiesced) live SQLite.
cp Bot/db/database.sqlite Bot/db/database.sqlite.bak-$(date +%F)

git pull --ff-only origin main
# DATABASE_URL is already in .env (Phase A) — nothing to add.

docker compose build

# Migrate. Runs in the bot image; scripts/ mounted in. The script resolves
# the sqlite/schema paths itself; DATABASE_URL comes from .env.
docker compose run --rm -v ./scripts:/scripts myapp \
  python3 /scripts/migrate_sqlite_to_postgres.py

# Check the per-table count table it prints — it exits nonzero on any
# mismatch. Do not continue past a failure.

docker compose up -d                   # downtime ends
docker compose logs -f --tail 50 myapp # expect: schema applied, cogs load,
                                       # post_ranks cycle completes
```

## Phase D — re-enable the deploy cron

```bash
crontab -l | sed 's|^#\(.*scripts/deploy.sh.*\)$|\1|' | crontab -
crontab -l   # confirm the deploy line is active again
```

## Verify

- `#league-ranking` board refreshes within ~2 minutes.
- In CT 105: `su - postgres -c "psql -d chomage -c 'select count(*) from league_history;'"`
  matches the migration output (live DB had 9,298 history / 8,989
  match_stats rows on 2026-07-04).
- The SQLite file stays on disk untouched — no longer read or written.

## Ranked 5s board (after cutover)

The 5s board posts into its **own channel**, separate from
`#league-ranking`:

1. Create a text channel for it (e.g. `#ranked-5s`).
2. `/sync` in `#chommage_admin` so the new slash commands register.
3. `/set_ranked5s_channel` pointing at the new channel.
4. First weekend window (Fri/Sat/Sun 20:00–01:00 CEST): watch the logs for
   `discovered candidate Ranked 5s queueType '<X>'`, then pin it by adding
   `ranked5s_queue_type=<X>` to `.env` and `docker compose up -d` to reload
   env. (The board works before pinning via the discovery heuristic;
   pinning just makes it exact.)

## Rollback

Code revert = back on SQLite; the file was never modified.

```bash
cd /root/ChomageBot
git revert -m 1 <merge-commit>   # or revert the squash commit
docker compose up -d --build myapp
# CT 105 can be left running (or: pct stop 105) — data persists either way.
```
