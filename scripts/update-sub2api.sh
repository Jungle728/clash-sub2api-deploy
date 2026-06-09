#!/usr/bin/env bash
# Update the sub2api Docker image and recreate only the sub2api service.
# Intended for cron; safe to run manually from any working directory.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${COMPOSE_DIR:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
LOG_DIR="${LOG_DIR:-$COMPOSE_DIR/logs}"
BACKUP_DIR="${BACKUP_DIR:-$COMPOSE_DIR/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
SERVICE="${SERVICE:-sub2api}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-sub2api-postgres}"
HEALTH_CONTAINER="${HEALTH_CONTAINER:-sub2api}"

mkdir -p "$LOG_DIR" "$BACKUP_DIR"
LOG_FILE="$LOG_DIR/sub2api-auto-update.log"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S%z')" "$*" | tee -a "$LOG_FILE"
}

fail() {
  log "ERROR: $*"
  exit 1
}

cd "$COMPOSE_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

POSTGRES_USER="${POSTGRES_USER:-sub2api}"
POSTGRES_DB="${POSTGRES_DB:-sub2api}"
SERVER_PORT="${SERVER_PORT:-8080}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:${SERVER_PORT}/health}"

log "Starting sub2api update in $COMPOSE_DIR"

if docker ps --format '{{.Names}}' | grep -qx "$POSTGRES_CONTAINER"; then
  backup_file="$BACKUP_DIR/sub2api-db-$(date '+%Y%m%d-%H%M%S').sql.gz"
  log "Backing up PostgreSQL to $backup_file"
  docker exec "$POSTGRES_CONTAINER" pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" \
    | gzip > "$backup_file"
else
  log "PostgreSQL container $POSTGRES_CONTAINER is not running; skipping backup"
fi

before_image="$(docker image inspect weishaw/sub2api:latest --format '{{.Id}}' 2>/dev/null || true)"
log "Pulling latest sub2api image"
docker compose pull "$SERVICE" 2>&1 | tee -a "$LOG_FILE"
after_image="$(docker image inspect weishaw/sub2api:latest --format '{{.Id}}' 2>/dev/null || true)"

if [[ -n "$before_image" && "$before_image" == "$after_image" ]]; then
  log "Image unchanged: $after_image"
else
  log "Image changed: ${before_image:-none} -> ${after_image:-unknown}"
fi

log "Recreating $SERVICE service"
docker compose up -d "$SERVICE" 2>&1 | tee -a "$LOG_FILE"

log "Waiting for $HEALTH_CONTAINER container health"
for _ in $(seq 1 60); do
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$HEALTH_CONTAINER" 2>/dev/null || true)"
  if [[ "$status" == "healthy" || "$status" == "running" ]]; then
    log "Container status: $status"
    break
  fi
  sleep 2
done

status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$HEALTH_CONTAINER" 2>/dev/null || true)"
[[ "$status" == "healthy" || "$status" == "running" ]] || {
  docker compose logs --tail=120 "$SERVICE" 2>&1 | tee -a "$LOG_FILE"
  fail "$HEALTH_CONTAINER did not become healthy; final status: ${status:-unknown}"
}

log "Checking $HEALTH_URL"
curl -fsS --max-time 10 "$HEALTH_URL" >> "$LOG_FILE"
printf '\n' >> "$LOG_FILE"

find "$BACKUP_DIR" -type f -name 'sub2api-db-*.sql.gz' -mtime +"$BACKUP_RETENTION_DAYS" -delete

log "sub2api update completed"
