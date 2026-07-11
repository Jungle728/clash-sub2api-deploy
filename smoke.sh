#!/usr/bin/env bash
# Validate the Caddy/sub2api deployment without printing credentials or IPs.
# Usage: bash smoke.sh
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

SUB2API_CONTAINER="${SUB2API_CONTAINER:-sub2api}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-sub2api-postgres}"
REDIS_CONTAINER="${REDIS_CONTAINER:-sub2api-redis}"
POSTGRES_USER="${POSTGRES_USER:-sub2api}"
POSTGRES_DB="${POSTGRES_DB:-sub2api}"
SERVER_PORT="${SERVER_PORT:-8080}"
LOCAL_HEALTH_URL="${LOCAL_HEALTH_URL:-http://127.0.0.1:${SERVER_PORT}/health}"

fail() {
  printf '  FAIL: %s\n' "$*" >&2
  exit 1
}

container_status() {
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null || true
}

printf '[1/5] Compose services and containers...\n'
services="$(docker compose config --services)"
grep -qx 'sub2api' <<< "$services" || fail 'sub2api service is missing'
grep -qx 'postgres' <<< "$services" || fail 'postgres service is missing'
grep -qx 'redis' <<< "$services" || fail 'redis service is missing'
if grep -qi 'mihomo' <<< "$services" || docker ps --format '{{.Names}}' | grep -qx 'sub2api-mihomo'; then
  fail 'mihomo is still configured or running'
fi
printf '  OK: no mihomo service or container\n'

for container in "$SUB2API_CONTAINER" "$POSTGRES_CONTAINER" "$REDIS_CONTAINER"; do
  status="$(container_status "$container")"
  [[ "$status" == 'healthy' || "$status" == 'running' ]] \
    || fail "$container status is ${status:-missing}"
done
printf '  OK: core containers are running\n'

printf '[2/5] Container-level proxy isolation...\n'
global_proxy_keys="$(
  docker inspect "$SUB2API_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | awk -F= '$1 == "HTTP_PROXY" || $1 == "HTTPS_PROXY" || $1 == "http_proxy" || $1 == "https_proxy" {if (length($2) > 0) print $1}'
)"
[[ -z "$global_proxy_keys" ]] || fail "non-empty global proxy variables found: $global_proxy_keys"
printf '  OK: account routing is not overridden by global HTTP_PROXY\n'

printf '[3/5] Local health endpoint...\n'
health_code="$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "$LOCAL_HEALTH_URL")"
[[ "$health_code" == '200' ]] || fail "$LOCAL_HEALTH_URL returned HTTP $health_code"
printf '  OK: %s returned 200\n' "$LOCAL_HEALTH_URL"

printf '[4/5] Account proxy configuration...\n'
summary="$(
  docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F $'\t' -c \
    "SELECT
       (SELECT COUNT(*) FROM proxies WHERE status='active' AND deleted_at IS NULL),
       (SELECT COUNT(*) FROM accounts WHERE status='active' AND proxy_id IS NOT NULL),
       (SELECT COUNT(*) FROM accounts WHERE status='active' AND proxy_id IS NULL);"
)"
IFS=$'\t' read -r active_proxies bound_accounts direct_accounts <<< "$summary"
printf '  Active proxies: %s\n' "$active_proxies"
printf '  Active accounts with proxy: %s\n' "$bound_accounts"
printf '  Active accounts configured for direct access: %s\n' "$direct_accounts"
if [[ "$active_proxies" == '0' ]]; then
  printf '  WARN: no active account proxy; this is valid only for a direct-only deployment\n'
fi

printf '[5/5] Public endpoint and recent traffic...\n'
if [[ -n "${PUBLIC_BASE_URL:-}" ]]; then
  public_code="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "${PUBLIC_BASE_URL%/}/health")"
  [[ "$public_code" == '200' ]] || fail "$PUBLIC_BASE_URL/health returned HTTP $public_code"
  printf '  OK: public /health returned 200\n'
else
  printf '  SKIP: set PUBLIC_BASE_URL=https://api.example.com to test Caddy\n'
fi

recent_success="$(docker logs --since 1h "$SUB2API_CONTAINER" 2>&1 | grep -c '"status_code": 200' || true)"
printf '  Recent successful requests (last hour): %s\n' "$recent_success"

printf '\nDeployment smoke check passed.\n'
