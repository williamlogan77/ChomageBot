#!/bin/bash
# One-shot, idempotent: create the read-only `chomage_ro` role on the
# chomage-db LXC (CT 105) for external developers, allow it from the LAN
# (which includes WireGuard clients — CT 102 masquerades them as
# 192.168.0.4), and park the generated credentials root-only on CT 105.
#
# Run ON THE PROXMOX HOST (pve):
#   ssh root@192.168.0.3 'bash -s' < scripts/setup_db_readonly.sh
#
# Prints everything EXCEPT the password (stored in
# /root/chomage-db-readonly.txt inside CT 105, chmod 600).

set -euo pipefail

CT=105
HBA=/etc/postgresql/15/main/pg_hba.conf
RO_PW=$(openssl rand -hex 16)

if pct exec $CT -- su - postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='chomage_ro';\"" | grep -q 1; then
  echo "role chomage_ro already exists — leaving its password unchanged"
else
  pct exec $CT -- su - postgres -c "psql -v ON_ERROR_STOP=1 \
    -c \"CREATE ROLE chomage_ro LOGIN PASSWORD '$RO_PW';\" \
    -c \"GRANT CONNECT ON DATABASE chomage TO chomage_ro;\"" >/dev/null
  pct exec $CT -- bash -c "umask 077; printf 'role: chomage_ro\npassword: %s\ndsn: postgresql://chomage_ro:%s@192.168.0.5:5432/chomage\n' '$RO_PW' '$RO_PW' > /root/chomage-db-readonly.txt"
  echo "role created; credentials in /root/chomage-db-readonly.txt on CT $CT"
fi

# SELECT-only on everything current and future (future covered via the
# bot role's default privileges — the bot creates tables at boot).
pct exec $CT -- su - postgres -c "psql -v ON_ERROR_STOP=1 -d chomage \
  -c \"GRANT USAGE ON SCHEMA public TO chomage_ro;\" \
  -c \"GRANT SELECT ON ALL TABLES IN SCHEMA public TO chomage_ro;\" \
  -c \"ALTER DEFAULT PRIVILEGES FOR ROLE chomage IN SCHEMA public GRANT SELECT ON TABLES TO chomage_ro;\"" >/dev/null
echo "grants applied"

pct exec $CT -- bash -c "grep -q 'chomage_ro' $HBA || echo 'host chomage chomage_ro 192.168.0.0/24 scram-sha-256' >> $HBA; systemctl reload postgresql"
echo "pg_hba rule present + postgres reloaded"

# Verify: SELECT allowed, INSERT denied — over the LAN address so the
# listen/hba path is what's actually being tested.
PW=$(pct exec $CT -- bash -c "grep '^password:' /root/chomage-db-readonly.txt | cut -d' ' -f2")
echo -n "SELECT as chomage_ro: users count = "
pct exec $CT -- bash -c "PGPASSWORD='$PW' psql -h 192.168.0.5 -U chomage_ro -d chomage -tAc 'SELECT count(*) FROM users;'"
echo -n "INSERT as chomage_ro (must fail): "
pct exec $CT -- bash -c "PGPASSWORD='$PW' psql -h 192.168.0.5 -U chomage_ro -d chomage -tAc 'INSERT INTO users (user_id) VALUES (1);' 2>&1 | head -1"
echo "done"
