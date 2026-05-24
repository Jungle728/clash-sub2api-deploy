#!/usr/bin/env bash
# 部署后端到端冒烟测试，全部通过即认为环境正常。
# 用法：bash smoke.sh
set -e
PROXY_PASS="REPLACE_WITH_PROXY_PASS"   # 必须与 mihomo config authentication 一致
SUB2API_PORT=$(grep -E '^SERVER_PORT=' "$(dirname "$0")/.env" 2>/dev/null | cut -d= -f2)
SUB2API_URL="http://127.0.0.1:${SUB2API_PORT:-8080}"

echo "[1/4] mihomo 容器健康..."
docker ps --filter name=sub2api-mihomo --filter health=healthy -q | grep -q . \
  && echo "  OK" || { echo "  FAIL: mihomo 容器不存在或不健康"; exit 1; }

echo "[2/4] sub2api 容器走代理出口..."
ip=$(docker exec sub2api curl -s --max-time 8 https://api.ipify.org)
[[ -n "$ip" ]] && echo "  OK exit=$ip" || { echo "  FAIL: 检查 docker-compose.override.yml 中的 HTTP_PROXY env"; exit 1; }

echo "[3/4] AI API 端点可达..."
for u in https://api.anthropic.com/ https://api.openai.com/; do
  c=$(docker exec sub2api curl -s --max-time 8 -o /dev/null -w "%{http_code}" "$u")
  [[ "$c" =~ ^(200|404|421)$ ]] && echo "  OK $u -> $c" || { echo "  FAIL $u -> $c"; exit 1; }
done

echo "[4/4] sub2api 健康..."
c=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" $SUB2API_URL/health)
[[ "$c" == "200" ]] && echo "  OK $SUB2API_URL/health 200" || { echo "  FAIL $SUB2API_URL/health $c"; exit 1; }

echo
echo "✅ 部署正常"
