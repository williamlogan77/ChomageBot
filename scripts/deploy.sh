#!/bin/bash
# Runs inside Proxmox LXC 103 (chomagebot).
# Pulls latest main. Cog hot-reload is handled in-process by the bot's
# auto_reload cog (Bot/cogs/auto_reload.py) — it watches cog file mtimes
# and reloads anything that changes. No restart, no signals.
#
# Self-installs its own cron entry so re-running after a container rebuild
# is a single command.

set -euo pipefail

REPO="/root/ChomageBot"
LOG="/var/log/chomagebot-deploy.log"
CRON="*/2 * * * * $REPO/scripts/deploy.sh >> $LOG 2>&1"

if ! crontab -l 2>/dev/null | grep -qF "$REPO/scripts/deploy.sh"; then
  echo "[$(date)] Installing cron entry"
  (crontab -l 2>/dev/null; echo "$CRON") | crontab -
fi

cd "$REPO"
git fetch --quiet origin main

BEHIND=$(git rev-list --count HEAD..origin/main)
if [ "$BEHIND" -eq 0 ]; then
  exit 0
fi

echo "[$(date)] Pulling $BEHIND new commit(s) from origin/main"
git pull --ff-only origin main
echo "[$(date)] Pull complete — auto_reload cog will pick up cog changes within ${POLL_SECONDS:-30}s"
