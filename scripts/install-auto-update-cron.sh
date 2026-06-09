#!/usr/bin/env bash
# Install a daily 00:00 cron job for scripts/update-sub2api.sh.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
UPDATE_SCRIPT="$COMPOSE_DIR/scripts/update-sub2api.sh"
CRON_MARKER="# clash-sub2api-deploy auto update"
CRON_LINE="0 0 * * * cd $COMPOSE_DIR && $UPDATE_SCRIPT >> $COMPOSE_DIR/logs/sub2api-auto-update.cron.log 2>&1 $CRON_MARKER"

mkdir -p "$COMPOSE_DIR/logs"
chmod +x "$UPDATE_SCRIPT"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

crontab -l 2>/dev/null | grep -vF "$CRON_MARKER" > "$tmp" || true
printf '%s\n' "$CRON_LINE" >> "$tmp"
crontab "$tmp"

echo "Installed cron job:"
echo "$CRON_LINE"
