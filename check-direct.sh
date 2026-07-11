#!/usr/bin/env bash
# =============================================================================
# 测试服务器是否能直连 OpenAI / Anthropic API
# =============================================================================
# 部署 sub2api 前先跑这个：用于判断哪些账号可以直连，哪些账号应绑定账户代理
# 用法：bash check-direct.sh
#
# 测试什么：
# - api.openai.com / api.anthropic.com 根路径（API 调用入口）
# - auth.openai.com 的 OAuth discovery 端点（OAuth 流程实际用的子路径）
# - chatgpt.com / claude.ai 仅做参考（被 CF 拦不影响 sub2api 业务）
# =============================================================================

set +e

T=8       # 单次请求超时秒数
PASS=0
FAIL=0

ok() { printf "  \033[32m✓\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
ng() { printf "  \033[31m✗\033[0m %s\n" "$1"; FAIL=$((FAIL+1)); }

probe() {
    local url=$1
    local label=$2
    local ok_codes=$3
    local code
    code=$(curl -s --max-time $T -o /dev/null -w "%{http_code}" "$url" 2>/dev/null)
    if echo " $ok_codes " | grep -q " $code "; then
        ok "$label  → HTTP $code"
        return 0
    else
        ng "$label  → HTTP $code (期望 $ok_codes)"
        return 1
    fi
}

# 检测响应是不是 Cloudflare 浏览器挑战页（IDC 被拦的标志）
is_cf_challenge() {
    local url=$1
    curl -s --max-time $T -I "$url" 2>/dev/null | grep -qi 'cf-mitigated: challenge'
}

echo "========================================"
echo "  服务器直连 AI API 可达性测试"
echo "========================================"
echo

echo "===== 关键 API 端点（sub2api 真正会访问的）====="

# 1. API 入口
probe "https://api.openai.com/v1/models"   "api.openai.com/v1/models   "  "200 401"
probe "https://api.anthropic.com/v1/models" "api.anthropic.com/v1/models"  "200 401 405"

# 2. OAuth discovery 端点（OAuth 流程的真实入口）
probe "https://auth.openai.com/.well-known/openid-configuration" "auth.openai.com (OAuth discovery)" "200"

KEY_FAIL=$FAIL

echo
echo "===== Web 前端（参考；403 是 CF 拦 IDC，不影响 sub2api 业务） ====="
for u_label in \
  "https://chatgpt.com/|chatgpt.com   " \
  "https://claude.ai/|claude.ai     " \
  "https://auth.openai.com/|auth.openai.com (root)"; do
    url="${u_label%%|*}"
    label="${u_label##*|}"
    code=$(curl -s --max-time $T -o /dev/null -w "%{http_code}" "$url" 2>/dev/null)
    if [ "$code" = "200" ] || [ "$code" = "302" ] || [ "$code" = "307" ]; then
        printf "  \033[32m✓\033[0m %s  → HTTP %s（直连成功）\n" "$label" "$code"
    elif is_cf_challenge "$url"; then
        printf "  \033[33m!\033[0m %s  → HTTP %s（CF 拦 IDC）\n" "$label" "$code"
    else
        printf "  \033[33m?\033[0m %s  → HTTP %s\n" "$label" "$code"
    fi
done

echo
echo "===== 出口 IP ====="
ip=$(curl -s --max-time $T https://api.ipify.org 2>/dev/null)
[ -n "$ip" ] && echo "  出口 IP: $ip" || echo "  无法获取"

echo
echo "========================================"
if [ "$KEY_FAIL" = "0" ]; then
    echo -e "\033[32m关键 API 直连全部可达\033[0m"
    echo
    echo "允许直连的账号可以不绑定代理。"
    echo "OAuth / 多账号场景仍建议使用 sub2api 账户级 HTTPS/SOCKS5 代理，"
    echo "避免所有账号共享并暴露宿主机公网 IP。"
    exit 0
else
    echo -e "\033[31m有 $KEY_FAIL/3 个关键端点不可直达\033[0m"
    echo
    echo "受影响的账号必须在 sub2api 中绑定可用的标准 HTTP/HTTPS/SOCKS5 代理。"
    echo "不要依赖容器级 HTTP_PROXY，也不要在代理失败后回退直连。"
    exit 1
fi
