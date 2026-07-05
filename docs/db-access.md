# Database access — prod layout + external developers

The bot's database is PostgreSQL 15 in its own Proxmox LXC:
**CT 105 `chomage-db`, 192.168.0.5:5432**, database `chomage`.
The schema is applied idempotently by the bot on boot from
`Bot/db/setup.postgres.sql`, so any empty Postgres self-initializes.

| Role | Access | Credentials live |
|---|---|---|
| `chomage` | read-write (the bot) | `DATABASE_URL` in `/root/ChomageBot/.env` on LXC 103 — never share |
| `chomage_ro` | SELECT-only (humans, external devs) | `/root/chomage-db-readonly.txt` on CT 105 (root-only) |

`pg_hba.conf` accepts both roles only from `192.168.0.0/24`
(scram-sha-256). Port 5432 is **not** forwarded to the internet and must
stay that way. One-time setup for `chomage_ro`:
`ssh root@192.168.0.3 'bash -s' < scripts/setup_db_readonly.sh`.

## Path 1 — local development (recommended)

External devs never need the live DB to work on the bot:

```bash
# 1. Throwaway Postgres
docker run -d --name chomage-dev-pg -e POSTGRES_USER=chomage \
  -e POSTGRES_PASSWORD=chomage -e POSTGRES_DB=chomage -p 5432:5432 postgres:16-alpine

# 2. .env
DATABASE_URL=postgresql://chomage:chomage@localhost:5432/chomage

# 3. Seed real-shaped data from the committed SQLite snapshot
python scripts/migrate_sqlite_to_postgres.py \
  --database-url postgresql://chomage:chomage@localhost:5432/chomage
```

The snapshot (`Bot/db/database.sqlite`) is a few months stale but has
~8k history rows — plenty for chart/board work. They can write/wipe
freely, and they don't need a Riot API key unless touching ingestion.

## Path 2 — read-only view of the LIVE db over WireGuard

The WireGuard LXC (CT 102, LAN 192.168.0.4, `wg0` = 10.0.0.1/24, UDP
51820) masquerades VPN clients onto the LAN — they appear as
192.168.0.4, which the pg_hba LAN rule already covers.

Current peer allocations on `wg0` (see `/etc/wireguard/wg0.conf` in CT 102):

| WG IP | device |
|---|---|
| 10.0.0.2 | William — original client (`client.conf`) |
| 10.0.0.3 | William — laptop (`laptop.conf`, added 2026-07-05) |
| 10.0.0.4 | William — phone (`phone.conf` + QR, added 2026-07-05) |

Onboarding a dev as a WG peer (done by William on CT 102):

```bash
pct enter 102
wg genkey | tee /tmp/dev.key | wg pubkey > /tmp/dev.pub   # or dev generates + sends pubkey
# Append to /etc/wireguard/wg0.conf — next free 10.0.0.x (currently .5):
# [Peer]
# PublicKey = <dev.pub>
# AllowedIPs = 10.0.0.5/32
wg syncconf wg0 <(wg-quick strip wg0)
```

Dev-side client config:

```ini
[Interface]
PrivateKey = <dev.key>
Address    = 10.0.0.5/32

[Peer]
PublicKey  = Ior7/UIhWyV6kwQNB2vZmIj3M1GTZy/yqhl0TJKQOFM=
Endpoint   = <your-ddns-hostname>:51820
# Split tunnel: only the DB host routes through the VPN.
AllowedIPs = 192.168.0.5/32
```

Then connect with the read-only DSN from `/root/chomage-db-readonly.txt`:

```bash
psql "postgresql://chomage_ro:<password>@192.168.0.5:5432/chomage"
```

## Security notes

- Read-only is enforced by grants (`SELECT` only, incl. future tables via
  default privileges), not by convention.
- The `chomage` (read-write) DSN stays on LXC 103; never hand it out.
- Prefer split-tunnel `AllowedIPs` for dev peers — full-tunnel
  (0.0.0.0/0) routes their whole internet through your house.
- The data includes Discord IDs/nicknames and Riot puuids of the friend
  group — share access accordingly.
