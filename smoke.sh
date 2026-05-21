#!/usr/bin/env bash
# 部署后端到端冒烟测试，全部通过即认为环境正常。
# 用法：bash smoke.sh
set -e
PROXY_PASS="REPLACE_WITH_PROXY_PASS"   # 必须与 mixin.yaml authentication 一致
SUB2API_URL="http://127.0.0.1:8080"

echo "[1/5] mihomo 监听 *:7890..."
ss -tunl | grep -q '\*:7890' && echo "  OK *:7890 (allow-lan)" || { echo "  FAIL: not *:7890, 检查 mixin allow-lan"; exit 1; }

echo "[2/5] 宿主走代理出口..."
ip=$(curl -s --max-time 8 -x "http://sub2api:$PROXY_PASS@127.0.0.1:7890" https://api.ipify.org)
[[ -n "$ip" ]] && echo "  OK exit=$ip" || { echo "  FAIL: 检查 PROXY_PASS 与 mixin authentication 是否一致"; exit 1; }

echo "[3/5] 容器走代理出口..."
ip=$(docker exec sub2api curl -s --max-time 8 https://api.ipify.org)
[[ -n "$ip" ]] && echo "  OK exit=$ip" || { echo "  FAIL: 检查 docker-compose.override.yml + extra_hosts"; exit 1; }

echo "[4/5] AI API 端点可达..."
for u in https://api.anthropic.com/ https://api.openai.com/; do
  c=$(docker exec sub2api curl -s --max-time 8 -o /dev/null -w "%{http_code}" "$u")
  [[ "$c" =~ ^(200|404|421)$ ]] && echo "  OK $u -> $c" || { echo "  FAIL $u -> $c"; exit 1; }
done

echo "[5/5] sub2api 健康..."
c=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" $SUB2API_URL/health)
[[ "$c" == "200" ]] && echo "  OK /health 200" || { echo "  FAIL /health $c"; exit 1; }

echo
echo "✅ 部署正常"
