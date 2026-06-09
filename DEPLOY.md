# sub2api + Clash 全容器化部署手册（v2）

一台新机器上，从零到 sub2api 可对外提供 API 转发服务的完整流程。本文档假定 Ubuntu 24.04 (`noble`)、x86_64、普通用户（带 sudo）。其他发行版按对应包管理器调整。

> **v1 vs v2**：v1 把 mihomo 装在宿主（nohup 后台）、sub2api 容器通过 `host.docker.internal` 找它。问题是 mihomo 挂了无人拉起，且不同 docker 网络下 `host.docker.internal` 路由偶发不通。v2 把 mihomo 也容器化，全部走 docker compose，自动重启，迁移更简单。
>
> 旧 v1 配置（`mixin.yaml.example` + `clashsub` 命令）保留在仓库根目录作历史参考，不再推荐使用。

## 架构

```
┌─────────────────────────────────────────────────┐
│ 宿主机                                          │
│   ✗ 不装 mihomo / clashctl                      │
│   ✗ 不依赖 systemd / nohup                      │
│                                                 │
│  ┌──────────── docker compose ───────────────┐ │
│  │                                            │ │
│  │ sub2api-mihomo  ←─┐                        │ │
│  │   (mihomo:latest) │                        │ │
│  │   :7890 (内部)    │ HTTP_PROXY            │ │
│  │   :9090 (内部)    │ mihomo:7890          │ │
│  │                   │                        │ │
│  │ sub2api ──────────┘                        │ │
│  │   :8080 → 宿主 :65432 (可改)               │ │
│  │   ↓                                        │ │
│  │ sub2api-postgres                           │ │
│  │ sub2api-redis                              │ │
│  └────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────┘
                       ↓
              proxy-providers (订阅，自动每小时刷新)
              JP / US 节点 → AI API
```

**核心设计**

- **AI 流量优先美国节点**，美国整组失联才 fallback 日本，再不行直连
- **非 AI 流量**走 `🌐 全局出口`，默认日本（更稳定）
- **订阅以 `proxy-providers` 注入**，mihomo 每小时自动刷新，不需要 cron / clashsub
- **mihomo 容器化享受 `restart: unless-stopped`**，挂了自动拉起
- **服务间走 docker 网络 DNS**：sub2api 用 `mihomo:7890` 而不是 `host.docker.internal:7890`
- **Cloudflare 反 IDC 不影响 API 端点**：`api.openai.com` / `api.anthropic.com` 对 IDC IP 友好

---

## 准备清单（从零部署前）

### 你必须自己准备的

| 项 | 说明 | 怎么获取 |
|---|---|---|
| 一台 Linux 服务器 | x86_64，2 核 / 4GB / 20GB 磁盘起步；境外节点（Azure / 美西 / 日本 IDC 等）最佳 | 任何云厂商；推荐 4GB 内存以上避免 OOM |
| sudo 权限 | 装 docker 需要 | 服务器管理员账号 |
| 一个机场订阅（clash 格式） | sub2api 容器要靠它出海调 OpenAI / Anthropic API | 任何支持 clash 订阅的机场。**测试链接是否可用**：<br>`curl -A "clash-verge/v2.4.0" -I "<你的订阅URL>"` 应返回 `content-type: text/yaml` |
| OpenAI / Anthropic 账号 | sub2api 是中转，自己不带账号；你要有 ChatGPT Plus / Claude Max / Anthropic API 等账号才有用 | 自己注册 |
| 服务器对外开放的端口 | 默认 65432（推荐改高位端口）；客户端调 sub2api 要能连上 | 云厂商安全组 + 服务器 firewall 放行 |

### 仓库 / 脚本会自动生成的（不用提前准备）

| 项 | 默认值 / 生成方式 | 用途 |
|---|---|---|
| 代理认证密码 | `openssl rand -hex 12`（部署时执行） | mihomo 7890 的 Basic Auth |
| POSTGRES_PASSWORD | `openssl rand -hex 32` | sub2api 数据库密码 |
| JWT_SECRET | `openssl rand -hex 32` | 用户登录 token 加密 |
| TOTP_ENCRYPTION_KEY | `openssl rand -hex 32` | 用户 2FA 加密 |
| admin 一次性密码 | sub2api 首次启动日志输出 | Web UI 管理员登录（账号 `admin@sub2api.local`）|

### 镜像和上游资源（部署时自动拉）

| 项 | 来源 |
|---|---|
| sub2api 镜像 | `weishaw/sub2api:latest`（docker hub） |
| mihomo 镜像 | `metacubex/mihomo:latest`（docker hub） |
| postgres 镜像 | `postgres:18-alpine`（docker hub） |
| redis 镜像 | `redis:8-alpine`（docker hub） |
| sub2api 官方 docker-compose | `https://raw.githubusercontent.com/Wei-Shaw/sub2api/main/deploy/docker-compose.local.yml` |
| zashboard mihomo 控制台 | `https://github.com/Zephyruso/zashboard/releases/latest/download/dist.zip`（mihomo 容器自动下载） |

### 可选（视场景）

| 项 | 用途 | 需要时再准备 |
|---|---|---|
| 域名 | 想用 HTTPS 而不是裸 IP 暴露 | DNS 解析到服务器 + Caddy/Nginx 反代 |
| 邮箱（SMTP） | sub2api 给用户发邮件通知 | Web UI 系统设置里配 |
| 备用机场订阅 | 防一个机场挂掉时切换 | 可加多个 proxy-provider |

### 部署前自检

新机器上跑这几条，全 OK 才开始部署：

```bash
# 1. 确认是支持的发行版
. /etc/os-release && echo "$ID $VERSION_ID"   # 期望：ubuntu 22.04+ / 24.04 等

# 2. 确认架构
uname -m   # 期望：x86_64

# 3. 确认订阅 URL 可达（用 mihomo 模拟 UA）
curl -A "clash-verge/v2.4.0" -I --max-time 10 "<你的订阅URL>" | head -5
# 期望：HTTP/x 200 + content-type: text/yaml

# 4. 确认目标端口空闲
ss -tunl | grep -E ':65432' || echo "✅ 65432 空闲"

# 5. 确认能访问 docker hub（镜像拉取）
curl -s --max-time 5 -o /dev/null -w "%{http_code}\n" https://registry-1.docker.io/v2/   # 期望非 000
```

---

## 部署步骤

### 1. 系统依赖（仅 docker）

```bash
sudo apt-get update
sudo apt-get install -y curl

# docker 官方源
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release; echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"   # 新 shell 生效
```

验证：`docker --version && docker compose version`。

### 2. clone 仓库 + 下载 sub2api docker-compose

```bash
git clone https://github.com/Jungle728/clash-sub2api-deploy.git ~/sub2api-deploy
cd ~/sub2api-deploy

# sub2api 官方 docker-compose.yml（不在本仓库，避免和上游不同步）
curl -sSL https://raw.githubusercontent.com/Wei-Shaw/sub2api/main/deploy/docker-compose.local.yml \
  -o docker-compose.yml
```

> **不要**用 `docker-deploy.sh | bash`：它会生成它自己的 `.env`，覆盖我们仓库已有的模板和定制。

### 2.5 测试服务器是否能直连 AI API（路径选择）

不是所有服务器都需要走 mihomo 代理。某些云服务商（部分 Azure / DigitalOcean / Vultr 海外节点）可以直连 OpenAI / Anthropic 的 API 端点。先测一下：

```bash
cd ~/sub2api-deploy
bash check-direct.sh
```

脚本会测试 3 个关键端点：

- `api.openai.com/v1/models` — sub2api 转发上游用
- `api.anthropic.com/v1/models` — 同上
- `auth.openai.com/.well-known/openid-configuration` — OpenAI OAuth 流程用

**根据结果选路径**：

| 结果 | 走哪条路 |
|---|---|
| ✅ 全绿（关键端点直连可达） | 可选 → [简化部署](#简化部署不装-mihomo) 或继续完整流程 |
| ❌ 有失败 | 必须走完整流程，继续[步骤 3](#3-生成代理认证密码) |

> 即使全绿可以走简化路径，**完整部署仍然有价值**：可切换节点、隐藏真实 IP、多账号走不同节点降低风控。简化部署适合"自用 + 接受 IP 暴露"。

### 3. 生成代理认证密码

```bash
PROXY_PASS=$(openssl rand -hex 12)
echo "PROXY_PASS=$PROXY_PASS"   # 保存好，下面三个文件都要用
```

### 4. 配置 .env

```bash
cd ~/sub2api-deploy
cp .env.example .env
chmod 600 .env

# 自动生成 postgres / jwt / totp 三个密钥
for k in POSTGRES_PASSWORD JWT_SECRET TOTP_ENCRYPTION_KEY; do
  val=$(openssl rand -hex 32)
  sed -i "s|^${k}=.*|${k}=${val}|" .env
done

# 改对外端口为高位端口（推荐，避免扫描器；缺省 8080）
sed -i 's/^SERVER_PORT=.*/SERVER_PORT=65432/' .env

# 校验：所有 REPLACE_WITH 占位符都被替换
grep REPLACE_WITH .env && echo "❌ 还有占位符未替换" || echo "✅ .env 配置完成"
```

### 5. 配置 mihomo/config.yaml

```bash
cp mihomo/config.yaml.example mihomo/config.yaml

# 把 PROXY_PASS 替换进 authentication
sed -i "s|REPLACE_WITH_PROXY_PASS|$PROXY_PASS|g" mihomo/config.yaml

# 替换订阅 URL（用你的实际订阅链接）
SUB_URL='https://YOUR-PROVIDER/api/v1/client/subscribe?token=YOUR-TOKEN'   # 改这一行
sed -i "s|https://YOUR-SUBSCRIPTION-PROVIDER/api/v1/client/subscribe?token=YOUR-TOKEN|$SUB_URL|g" mihomo/config.yaml

grep -E '^  - "sub2api:|url:' mihomo/config.yaml | head -3   # 校验
```

### 6. 配置 docker-compose.override.yml

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
sed -i "s|REPLACE_WITH_PROXY_PASS|$PROXY_PASS|g" docker-compose.override.yml

grep REPLACE_WITH docker-compose.override.yml && echo "❌ 还有占位符" || echo "✅"
```

> **重要**：`mihomo/config.yaml` 的 `authentication` 密码必须和 `docker-compose.override.yml` 的 `HTTP_PROXY` URL 密码**完全一致**——都用同一个 `$PROXY_PASS`。

### 7. 启动

```bash
cd ~/sub2api-deploy
docker compose pull          # 拉 4 个镜像
docker compose up -d         # 启动
docker compose ps            # 应看到 4 个容器都 healthy
docker compose logs sub2api 2>&1 | grep -i 'admin password'
```

**第一次启动会输出一行**：

```
Generated admin password (one-time): xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**这个密码只显示一次，立即保存**。账号 `admin@sub2api.local`。

如果想自定义初始密码（避免一次性密码丢失），启动前在 `.env` 里设 `ADMIN_PASSWORD=你想要的密码` 即可。

### 8. 验证链路

```bash
# smoke.sh 自动从 .env 读 SERVER_PORT，并验证全链路
bash ~/sub2api-deploy/smoke.sh
```

应输出 4 项全 OK。或手动测：

```bash
# 容器内出口 IP（应该是订阅节点的国外 IP，不是宿主公网 IP）
docker exec sub2api curl -s https://api.ipify.org

# AI API 端点可达
docker exec sub2api curl -s -o /dev/null -w "%{http_code}\n" https://api.openai.com/   # 期望 421
docker exec sub2api curl -s -o /dev/null -w "%{http_code}\n" https://api.anthropic.com/ # 期望 404

# sub2api Web UI
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:65432/health   # 期望 200
```

### 8. Web UI 配置账号

打开 `http://<server>:65432`，admin 登录，进**账号管理**：

1. **添加 OpenAI / Anthropic 账号**，按提示走 OAuth 授权
2. **添加代理记录**（左侧 `代理 / Proxies`）：
   - name: `mihomo-internal`
   - protocol: `http`
   - host: `mihomo`（**不是 `host.docker.internal`，因为容器在同 docker 网络**）
   - port: `7890`
   - username: `sub2api`
   - password: 你的 `$PROXY_PASS`
3. **编辑每个账号**，把 `proxy` 字段绑到 `mihomo-internal`
4. **新建 group + 把账号加进去**，然后**新建 API key 绑到这个 group**

> **重要**：sub2api 的 OAuth / API 调用走"账号上的 proxy 字段"，不走容器的 HTTP_PROXY env。账号必须显式绑代理才能正常授权和调用。

---

## 路由策略详解

mihomo 处理流量按规则顺序匹配。当前规则：

```yaml
rules:
  - DOMAIN,api64.ipify.org,DIRECT             # mihomo 控制台用
  - DOMAIN-SUFFIX,anthropic.com,🤖 AI 优选
  - DOMAIN-SUFFIX,claude.ai,🤖 AI 优选
  - DOMAIN-SUFFIX,openai.com,🤖 AI 优选
  - DOMAIN-SUFFIX,chatgpt.com,🤖 AI 优选
  - DOMAIN-SUFFIX,auth.openai.com,🤖 AI 优选
  - DOMAIN-SUFFIX,console.anthropic.com,🤖 AI 优选
  # ... (其他 AI 域名)
  - MATCH,🌐 全局出口                           # 兜底
```

**分组层级**：

| 分组 | 类型 | 候选 | 默认 |
|---|---|---|---|
| 🌐 全局出口 | select | AI 优选 / 日本 / 美国 | AI 优选 |
| 🤖 AI 优选 | fallback | 美国节点 → 日本节点 → DIRECT | 美国（健康时） |
| 🇯🇵 日本节点 | url-test | 订阅里的所有日本节点 | 自动选最快 |
| 🇺🇸 美国节点 | url-test | 订阅里的所有美国节点 | 自动选最快 |

**fallback 行为**：mihomo 的 `fallback` 类型按列表顺序探测，**只判定可达性，不判定延迟阈值**。把 `🇺🇸 美国节点` 整个 url-test 组作为第一候选，意味着：

- 单节点延迟波动 → url-test 组内自动切到更快的美国节点（不会跨国跳）
- 整个美国组都不通（健康检查 60s 探测一次失败）→ 切到日本组
- 日本也不通 → 直连兜底（不至于完全断网）
- 美国恢复后会自动切回

---

## 凭证清单（迁移时必备）

| 凭证 | 来源 | 必须保留 |
|---|---|---|
| 代理认证密码 | 自定义 `openssl rand -hex 12` | 是，mihomo config + override 两处必须一致 |
| sub2api Web UI 管理员密码 | 首启日志，**只显示一次** | **是，丢了只能进 DB 重置** |
| POSTGRES_PASSWORD | `~/sub2api-deploy/.env` 自动生成 | **是，丢了 DB 文件就读不出来** |
| JWT_SECRET | 同上 | 是，丢了所有用户被强制重新登录 |
| TOTP_ENCRYPTION_KEY | 同上 | 是，丢了所有 2FA 失效 |

---

## 日常运维

### 整套服务

```bash
cd ~/sub2api-deploy
docker compose ps                      # 状态
docker compose logs -f sub2api         # 看 sub2api 日志
docker compose logs -f mihomo          # 看 mihomo 日志
docker compose restart sub2api         # 重启 sub2api（不动 db / mihomo）
docker compose restart mihomo          # 重启 mihomo（改了 config.yaml 后必做）
docker compose down                    # 停止所有（保留数据卷）
docker compose up -d                   # 启动
docker compose pull && docker compose up -d   # 升级所有镜像
bash scripts/update-sub2api.sh                # 只升级 sub2api，并做 DB 备份 + 健康检查
```

### 改 mihomo 配置

```bash
vim ~/sub2api-deploy/mihomo/config.yaml
docker compose restart mihomo          # 必须重启容器，mihomo 不监听文件变化
docker compose logs --tail=30 mihomo   # 看是否报错
```

### 切换出口分组（无缝）

mihomo 控制台默认不暴露宿主，要看面板可以临时给 9090 加端口映射，或者通过 `docker exec` 进容器：

```bash
# 切到日本节点
docker exec sub2api-mihomo wget -qO- --header="Authorization: Bearer YOUR_PROXY_PASS" \
  --method=PUT \
  --body-data='{"name":"🇯🇵 日本节点"}' \
  --header="Content-Type: application/json" \
  "http://localhost:9090/proxies/$(printf '%s' '🌐 全局出口' | python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.stdin.read()))')"
```

切换是 mihomo 内部路由切换，已建立的 TCP 连接不会立即断——**对 sub2api 进行中的请求无影响**。

### 加新订阅

编辑 `mihomo/config.yaml` 的 `proxy-providers` 段，加一条：

```yaml
proxy-providers:
  smjc:           # 已有的
    type: http
    url: ...
    ...
  sub2:           # 新加的
    type: http
    url: "https://NEW-PROVIDER/..."
    path: ./providers/sub2.yaml
    interval: 3600
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
      lazy: true
    exclude-filter: "(?i)流量|过期|到期|官网|邮件|节点|订阅|重置|套餐|注意|发送|获取|不推荐"
```

然后让 url-test 分组从两个 provider 拉节点：

```yaml
proxy-groups:
  - name: 🇯🇵 日本节点
    type: url-test
    use:
      - smjc
      - sub2          # 加这行
    filter: "(?i)日本|jp\\b|..."
```

最后 `docker compose restart mihomo`。

---


## 可选：用域名启用 HTTPS

如果要把 sub2api 暴露给公网客户端，推荐用 Caddy 做 HTTPS 反向代理，而不是直接公开明文 `http://IP:65432`。

### 前提

- 一个子域名，例如 `api.example.com`
- DNS `A` 记录已经指向 sub2api 服务器公网 IP
- 云厂商安全组 / 服务器防火墙放行入站 `TCP 80` 和 `TCP 443`
- `80` / `443` 没有被其他进程占用：

```bash
ss -tunlp | grep -E ':(80|443)\b' || echo '80/443 空闲'
```

### 配置 Caddy

```bash
cd ~/sub2api-deploy

# 1. 准备 HTTPS compose 覆盖文件
cp docker-compose.https.yml.example docker-compose.https.yml

# 2. 准备 Caddyfile，并替换为自己的域名
cp Caddyfile.example Caddyfile
sed -i 's/YOUR-API-DOMAIN.example.com/api.example.com/g' Caddyfile

# 3. 收紧 sub2api 明文端口，只允许本机访问
#    Caddy 在 docker 网络内访问 sub2api:8080，不依赖宿主 65432。
grep -q '^BIND_HOST=' .env \
  && sed -i 's/^BIND_HOST=.*/BIND_HOST=127.0.0.1/' .env \
  || echo 'BIND_HOST=127.0.0.1' >> .env

# 4. 启动 HTTPS 反代
#    注意：docker compose 默认只自动读取 docker-compose.yml + docker-compose.override.yml，
#    额外的 HTTPS 文件需要显式用 -f 带上。
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.https.yml \
  up -d
```

证书由 Caddy 自动申请和续期，证书与账号数据保存在 `caddy_data/`、`caddy_config/`。这些目录是运行态数据，不应提交到 Git。

### 验证

```bash
# sub2api 明文端口只监听本机
ss -tunlp | grep 65432
# 期望类似：127.0.0.1:65432

# HTTPS 健康检查
curl -sS -D - https://api.example.com/health -o /tmp/sub2api-health.out
# 期望：HTTP/2 200

# 原完整链路仍应通过
bash smoke.sh
```

如果 Caddy 日志里出现 `Timeout during connect (likely firewall problem)`，通常是 DNS 未指向本机，或云安全组没有放行 `80` / `443`：

```bash
getent hosts api.example.com
curl -s https://api.ipify.org
docker compose -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.https.yml logs --tail=120 caddy
```

---

## 简化部署（不装 mihomo）

仅当 [check-direct.sh](#25-测试服务器是否能直连-ai-api路径选择) 测试**全绿**时可用。

### 适用场景

- 自用、单/少账号
- 服务器是境外节点且能直连 OpenAI/Anthropic
- 接受真实 IP 暴露给上游（不在意被风控的概率提升）

### 部署步骤

```bash
# 1. 装 docker（步骤 1）
# 2. clone 仓库 + 下载 sub2api docker-compose（步骤 2）
git clone https://github.com/Jungle728/clash-sub2api-deploy.git ~/sub2api-deploy
cd ~/sub2api-deploy
curl -sSL https://raw.githubusercontent.com/Wei-Shaw/sub2api/main/deploy/docker-compose.local.yml \
  -o docker-compose.yml

# 3. 配 .env（步骤 4，跳过 PROXY_PASS）
cp .env.example .env
chmod 600 .env
for k in POSTGRES_PASSWORD JWT_SECRET TOTP_ENCRYPTION_KEY; do
  sed -i "s|^${k}=.*|${k}=$(openssl rand -hex 32)|" .env
done
sed -i 's/^SERVER_PORT=.*/SERVER_PORT=65432/' .env

# 4. 不创建 docker-compose.override.yml，不创建 mihomo/config.yaml
#    （这两个文件不存在时，docker compose 直接用官方 yml 起 sub2api+postgres+redis 三个容器）

# 5. 启动
docker compose up -d
docker compose logs sub2api 2>&1 | grep -i 'admin password'

# 6. 验证（注意 smoke.sh 不适用，因为它检查 mihomo 容器；用下面的简化版）
sleep 5
curl -s -o /dev/null -w "/health %{http_code}\n" http://127.0.0.1:65432/health   # 期望 200
```

### 后续如果想升级到完整部署

随时可以加上 mihomo——按完整部署流程的步骤 3-6 操作即可，旧数据保留。

### 注意

- **不要** clone 仓库后什么都不删——repo 里的 `mihomo/` 目录和 `docker-compose.override.yml.example` 不会自动生效（需要手动 cp 重命名才会被 compose 加载），所以放着不影响
- **不要** 在 `.env` 里设 `UPDATE_PROXY_URL`——直连场景下保持空即可
- **测试** sub2api 真的能用：在 Web UI 添加一个账号、发起 OAuth，OpenAI 授权页能正常打开就 OK

---

## 故障排查

### sub2api 返回 503 "Service temporarily unavailable"

日志找 `account_select_failed`：

- `error="no available accounts"` → API key 所在 group 没有可用账号。Web UI 编辑账号绑分组
- `Token revoked (401)` → OAuth token 失效。Web UI 重新走授权
- `proxy connection refused` → mihomo 容器挂了。`docker compose ps` 检查 mihomo 状态，看 `docker logs sub2api-mihomo`

### sub2api OAuth 报"未设置代理"

新版本 sub2api 会自动回退到容器 `HTTP_PROXY` env，**通常不会再报这个错**。如果还报：

1. **检查 mihomo 容器是否健康**：`docker compose ps`，应该是 healthy。挂了就 `docker compose up -d mihomo`
2. **确认容器 HTTP_PROXY env 在**：`docker exec sub2api env | grep -i http.*proxy`
3. **手动测代理**：`docker exec sub2api curl -s --max-time 5 https://api.ipify.org`，应返回订阅节点 IP（不是宿主 IP）
4. **如果上面都对，仍报错**：可能是旧版本 sub2api，去 Web UI **代理 / Proxies** 新建一条，host=`mihomo`、port=`7890`、protocol=`http`、username=`sub2api`、password=`<PROXY_PASS>`，然后编辑账号把 proxy 字段绑上

### mihomo 启动报错

```bash
docker logs --tail 50 sub2api-mihomo
```

常见错误：
- `proxy group: ... use or proxies missing` → 某个 group 既没 `proxies:` 也没 `use:` 配置，至少要有一个
- `init provider X: download config: ...` → 订阅 URL 不可达，确认 URL 正确 + 容器能访问外网（mihomo 第一次拉订阅是直连，不走自己的代理）

### 容器 healthcheck 显示 unhealthy 但服务正常

这是 busybox `wget` 的 `NO_PROXY=localhost` 失效问题，已经在 `docker-compose.override.yml` 里覆盖 healthcheck 加了 `-Y off` 修复。如果你看到这个问题，确认 override 文件中 sub2api 服务下的 `healthcheck:` 段存在。

### Cloudflare 拦截 web 入口

`claude.ai` / `chatgpt.com` 网页版被 CF 拦 403 是 IDC IP 黑名单，**不影响 sub2api API 转发**。如果你需要走 web 版（如 OAuth 登录某些步骤要访问 console），临时手动切节点或换支持解锁的机场。

---

## 升级 sub2api

镜像 `weishaw/sub2api:latest` 不定期更新。所有数据都是本地目录映射，更新流程**保留全部数据**。

### 每日自动更新

仓库内置了自动更新脚本：

- `scripts/update-sub2api.sh`：拉取 `weishaw/sub2api:latest`，只重建 `sub2api` 服务；更新前备份 PostgreSQL 到 `backups/`；更新后等待容器健康并请求 `/health`。
- `scripts/install-auto-update-cron.sh`：把更新脚本安装到当前用户 crontab，每天 `00:00` 执行一次。

启用：

```bash
cd ~/sub2api-deploy
bash scripts/install-auto-update-cron.sh
crontab -l | grep 'clash-sub2api-deploy auto update'
```

手动测试：

```bash
bash scripts/update-sub2api.sh
tail -n 80 logs/sub2api-auto-update.log
```

默认保留最近 14 天的数据库备份。需要调整时，在 cron 里给脚本加环境变量，例如：

```cron
BACKUP_RETENTION_DAYS=30 /home/lhl/sub2api-deploy/scripts/update-sub2api.sh
```

### 标准流程

```bash
cd ~/sub2api-deploy

# 1. 备份数据库（建议；新版本如果 schema 迁移失败可回滚）
docker exec sub2api-postgres pg_dump -U sub2api sub2api \
  | gzip > ../sub2api-db-$(date +%Y%m%d-%H%M%S).sql.gz

# 2. 拉新镜像
docker compose pull sub2api

# 3. 重建容器
docker compose up -d sub2api

# 4. 看启动日志
docker compose logs -f sub2api
```

启动日志关注：
- `Database connection successful`
- `Database initialized successfully` / `Database migration completed`
- `Server started on 0.0.0.0:8080`

如果出现 `panic` / `fatal` / `migration failed` 立即回滚。

### 回滚

```bash
sed -i 's|image: weishaw/sub2api:latest|image: weishaw/sub2api:v1.5.0|' docker-compose.yml   # 钉版本
docker compose pull sub2api && docker compose up -d sub2api

# db schema 不兼容时恢复备份
docker compose stop sub2api
gunzip -c ../sub2api-db-XXXXXX.sql.gz | docker exec -i sub2api-postgres psql -U sub2api -d sub2api
docker compose start sub2api
```

### 升级 mihomo / postgres / redis

- **mihomo**: `docker compose pull mihomo && docker compose up -d mihomo`，直接换最新没问题
- **postgres / redis patch 版本**: 同上
- **postgres major 版本**（如 17→18）: **不能直接换 tag**，必须先 `pg_dumpall` 再用新版本导入

---

## 修改对外端口

```bash
cd ~/sub2api-deploy
sed -i 's/^SERVER_PORT=.*/SERVER_PORT=65432/' .env
docker compose up -d sub2api
```

容器内部仍然 `8080`，只改宿主映射。`smoke.sh` 自动从 `.env` 读取，无需改脚本。客户端连接 URL 同步更新到新端口。

如果用反代加 HTTPS，sub2api 可以只监听本机：

```bash
sed -i 's/^BIND_HOST=.*/BIND_HOST=127.0.0.1/' .env
docker compose up -d sub2api
```

---

## 迁移到新机器

### 场景 A：完全重建（不要旧数据，零开始）

直接在新机器走完[部署步骤](#部署步骤)的 1-8。新建 admin 账号、新订阅、新数据库。

### 场景 B：保留旧数据迁移

```bash
# === 旧机器：备份 ===
cd ~
docker compose -f sub2api-deploy/docker-compose.yml down
sudo tar czf clash-sub2api-backup.tar.gz sub2api-deploy/   # sudo 必需，因为 postgres_data 权限敏感
ls -la clash-sub2api-backup.tar.gz   # ~80MB（取决于 usage_logs 量）
# 用 scp / rsync 传到新机器
scp clash-sub2api-backup.tar.gz user@new-server:~/

# === 新机器 ===
# 1. 装 docker（步骤 1）
# 2. 解压备份
sudo tar xzf clash-sub2api-backup.tar.gz -C ~/
sudo chown -R $USER:$USER ~/sub2api-deploy
# postgres_data 内部文件权限保持原样（cp -a / tar 都会保留），不要 chown

# 3. 启动
cd ~/sub2api-deploy
docker compose up -d
bash smoke.sh
```

`sub2api-deploy/` 已经包含所有需要的：mihomo 配置、postgres/redis/sub2api 数据、凭证、override 文件。无需任何配置改动，**所有账号、API key、usage_logs、admin 密码全部保留**。

> 如果迁移后端口冲突（比如旧机器 65432 是空闲的，新机器被占了），改 `.env` 里 `SERVER_PORT` 即可。

### 场景 C：从 GitHub clone 重建 + 仅恢复数据

适合只想保留 sub2api 业务数据（账号、API key、用量），但其他配置全部从仓库模板重生成：

```bash
# === 旧机器：仅备份关键文件 ===
cd ~
sudo tar czf data-only-backup.tar.gz \
  sub2api-deploy/.env \
  sub2api-deploy/data \
  sub2api-deploy/postgres_data \
  sub2api-deploy/redis_data

# === 新机器 ===
# 1. 装 docker
# 2. clone 仓库 + 走配置步骤 2-6（生成新的 mihomo 配置 / override / 端口等）
git clone https://github.com/Jungle728/clash-sub2api-deploy.git ~/sub2api-deploy
cd ~/sub2api-deploy
curl -sSL https://raw.githubusercontent.com/Wei-Shaw/sub2api/main/deploy/docker-compose.local.yml \
  -o docker-compose.yml

# 3. 解压数据（覆盖 .env 和 data 目录）
sudo tar xzf data-only-backup.tar.gz --strip-components=1 -C ~/sub2api-deploy/

# 4. 配置 mihomo + override（步骤 5-6，PROXY_PASS 用 .env 里旧的同一个，确保和数据库里 proxies 表一致）
# ... (按步骤 5-6 操作)

# 5. 启动
docker compose up -d
bash smoke.sh
```

---

## 已知坑

1. **mihomo `fallback` 不支持延迟阈值**：只能整组失联才切下一组。需要"延迟超过阈值切换"的话要装第三方插件
2. **mihomo 改 config.yaml 必须 `docker compose restart mihomo`**：mihomo 不监听文件变化，热加载只能通过 API
3. **`allow-lan: true` 在容器内是必需的**：mihomo 默认只监听 127.0.0.1，sub2api 容器会连不到。配合 `authentication` 仍然安全（端口不暴露宿主）
4. **busybox wget 即使设了 NO_PROXY=localhost 也会走代理**：导致 docker healthcheck 假阴性。`-Y off` 修复
5. **订阅"信息节点"会污染 url-test**：用 `exclude-filter` 排除"流量"、"过期"、"套餐"等关键字
6. **sub2api 的代理优先级**：`accounts.proxy_id`（数据库里账号绑定的代理）> 容器 `HTTP_PROXY` env > 直连。早期版本（<= 2026-05-22）OAuth 路径必须绑 proxy_id 否则报"未设置代理"；新版本（>= 2026-05-23）回退到 env，所以**只要容器 env 配好了，账号不绑也能用**。仍推荐显式绑账号 proxy 做双保险
7. **mihomo 第一次拉订阅是直连**：不走自己的 7890。如果服务器在 GFW 后面、订阅 URL 又被墙，mihomo 起不来。要么用 ip 直连的订阅 URL，要么暂时走代理拉一次再切回直连
8. **postgres_data 权限敏感**：tar 备份必须用 `sudo` 否则会丢权限信息。`docker cp -a` / `tar -p` 会保留 uid（即使宿主 uid 和容器内 70 不一致也无所谓——postgres entrypoint 会自动适配）。**不要**手动 `chown` 整个 postgres_data 目录

---

## 附录 A: 完整 mihomo/config.yaml

> 路径：`~/sub2api-deploy/mihomo/config.yaml`
>
> `authentication` 字段密码必须和 `docker-compose.override.yml` 中 sub2api 的 `HTTP_PROXY` URL 密码一致。新部署用 `openssl rand -hex 12` 生成，全文替换。

```yaml
mixed-port: 7890
external-controller: 0.0.0.0:9090
external-ui: ui
external-ui-url: https://github.com/Zephyruso/zashboard/releases/latest/download/dist.zip

allow-lan: true
bind-address: '*'
authentication:
  - "sub2api:REPLACE_WITH_PROXY_PASS"

mode: rule
log-level: info
unified-delay: true
tcp-concurrent: true

dns:
  enable: true
  listen: 0.0.0.0:1053
  enhanced-mode: redir-host
  default-nameserver:
    - 223.5.5.5
    - 119.29.29.29
  proxy-server-nameserver:
    - 1.1.1.1
    - 8.8.8.8
    - 223.5.5.5
  nameserver:
    - 8.8.8.8
    - 1.1.1.1
    - 223.5.5.5

proxy-providers:
  smjc:
    type: http
    url: "https://YOUR-SUBSCRIPTION-PROVIDER/api/v1/client/subscribe?token=YOUR-TOKEN"
    path: ./providers/smjc.yaml
    interval: 3600
    header:
      User-Agent:
        - "clash-verge/v2.4.0"
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
      lazy: true
    exclude-filter: "(?i)流量|过期|到期|官网|邮件|节点|订阅|重置|套餐|注意|发送|获取|不推荐"

proxy-groups:
  - name: 🌐 全局出口
    type: select
    proxies:
      - 🤖 AI 优选
      - 🇯🇵 日本节点
      - 🇺🇸 美国节点

  - name: 🤖 AI 优选
    type: fallback
    url: https://www.gstatic.com/generate_204
    interval: 60
    timeout: 5000
    proxies:
      - 🇺🇸 美国节点
      - 🇯🇵 日本节点
      - DIRECT

  - name: 🇯🇵 日本节点
    type: url-test
    url: https://www.gstatic.com/generate_204
    interval: 300
    tolerance: 50
    use:
      - smjc
    filter: "(?i)日本|jp\\b|japan|tokyo|osaka|🇯🇵"

  - name: 🇺🇸 美国节点
    type: url-test
    url: https://www.gstatic.com/generate_204
    interval: 300
    tolerance: 50
    use:
      - smjc
    filter: "(?i)美国|us\\b|usa|united states|america|los angeles|seattle|san jose|silicon|kansas|堪萨斯|🇺🇸"

rules:
  - DOMAIN,api64.ipify.org,DIRECT
  - DOMAIN-SUFFIX,anthropic.com,🤖 AI 优选
  - DOMAIN-SUFFIX,claude.ai,🤖 AI 优选
  - DOMAIN-KEYWORD,claude,🤖 AI 优选
  - DOMAIN-SUFFIX,openai.com,🤖 AI 优选
  - DOMAIN-SUFFIX,chatgpt.com,🤖 AI 优选
  - DOMAIN-SUFFIX,oaistatic.com,🤖 AI 优选
  - DOMAIN-SUFFIX,oaiusercontent.com,🤖 AI 优选
  - DOMAIN-KEYWORD,openai,🤖 AI 优选
  - DOMAIN-KEYWORD,chatgpt,🤖 AI 优选
  - DOMAIN-SUFFIX,auth.openai.com,🤖 AI 优选
  - DOMAIN-SUFFIX,auth0.openai.com,🤖 AI 优选
  - DOMAIN-SUFFIX,console.anthropic.com,🤖 AI 优选
  - DOMAIN-SUFFIX,gemini.google.com,🤖 AI 优选
  - DOMAIN-SUFFIX,bard.google.com,🤖 AI 优选
  - DOMAIN-SUFFIX,generativelanguage.googleapis.com,🤖 AI 优选
  - MATCH,🌐 全局出口
```

---

## 附录 B: docker-compose.override.yml

> 路径：`~/sub2api-deploy/docker-compose.override.yml`
>
> 与部署脚本下载的 `docker-compose.yml` 自动合并。第 8-13 行的密码必须和 `mihomo/config.yaml` 第 8 行 `authentication` 一致。

```yaml
services:
  mihomo:
    image: metacubex/mihomo:latest
    container_name: sub2api-mihomo
    restart: unless-stopped
    cap_add:
      - NET_ADMIN
    volumes:
      - ./mihomo:/root/.config/mihomo
    expose:
      - "7890"
      - "9090"
    networks:
      - sub2api-network
    healthcheck:
      test: ["CMD", "wget", "-qO-", "--tries=1", "--timeout=3", "http://localhost:9090"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s

  sub2api:
    environment:
      - HTTP_PROXY=http://sub2api:REPLACE_WITH_PROXY_PASS@mihomo:7890
      - HTTPS_PROXY=http://sub2api:REPLACE_WITH_PROXY_PASS@mihomo:7890
      - NO_PROXY=localhost,127.0.0.1,postgres,redis,mihomo
      - http_proxy=http://sub2api:REPLACE_WITH_PROXY_PASS@mihomo:7890
      - https_proxy=http://sub2api:REPLACE_WITH_PROXY_PASS@mihomo:7890
      - no_proxy=localhost,127.0.0.1,postgres,redis,mihomo
      - UPDATE_PROXY_URL=http://sub2api:REPLACE_WITH_PROXY_PASS@mihomo:7890
    depends_on:
      mihomo:
        condition: service_healthy
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    healthcheck:
      # busybox wget 即使设了 NO_PROXY 也会走代理 → 502。-Y off 强制不走代理
      test: ["CMD", "wget", "-q", "-Y", "off", "-T", "5", "-O", "/dev/null", "http://localhost:8080/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s
```

---

## 附录 C: 端到端冒烟测试

部署后跑这个脚本验证全链路。已包含在仓库 `smoke.sh` 中：

```bash
#!/usr/bin/env bash
# 部署后端到端冒烟测试，全部通过即认为环境正常。
set -e
PROXY_PASS="REPLACE_WITH_PROXY_PASS"   # 必须与 mihomo config authentication 一致
SUB2API_PORT=$(grep -E '^SERVER_PORT=' "$(dirname "$0")/.env" 2>/dev/null | cut -d= -f2)
SUB2API_URL="http://127.0.0.1:${SUB2API_PORT:-8080}"

echo "[1/4] mihomo 容器健康..."
docker ps --filter name=sub2api-mihomo --filter health=healthy -q | grep -q . \
  && echo "  OK" || { echo "  FAIL: mihomo 容器不存在或不健康"; exit 1; }

echo "[2/4] sub2api 容器走代理出口..."
ip=$(docker exec sub2api curl -s --max-time 8 https://api.ipify.org)
[[ -n "$ip" ]] && echo "  OK exit=$ip" || { echo "  FAIL"; exit 1; }

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
```

---

## 修订历史

- **2026-06-09 v2.3**: 新增 `scripts/update-sub2api.sh` 和 `scripts/install-auto-update-cron.sh`，支持每天 00:00 自动拉取 sub2api 最新镜像、备份数据库、重建服务并健康检查。
- **2026-05-27 v2.2**: 新增可选 HTTPS 部署方式：`docker-compose.https.yml.example` + `Caddyfile.example`，支持用 Caddy 自动申请证书并反向代理到 sub2api，同时建议把明文 `65432` 收紧到本机监听。
- **2026-05-24 v2.1**: 重走全部署流程后调整文档：
  - 部署步骤改为"git clone 仓库 → curl 下载 sub2api docker-compose → 从 .env.example 生成 .env"，不再依赖 `docker-deploy.sh`（避免覆盖仓库模板）
  - 新增 .env 自动生成 3 个密钥的命令
  - "迁移到新机器"拆分为 A/B/C 三种场景（全新 / 完整迁移 / 仅迁数据）
  - 修正"已知坑 #6"（sub2api 账号 proxy_id 旧版本必填，新版本可省）
  - "OAuth 报未设置代理"故障排查更新为以容器 env 为主、Web UI 绑定为次
- **2026-05-24 v2**: 全容器化部署。mihomo 容器化（替代宿主 nohup），订阅以 proxy-provider 自动刷新（替代 clashsub），sub2api 容器通过 docker 网络服务名找代理（替代 host.docker.internal）
- **2026-05-21 v1**: 初版。clash 装宿主 + nohup，sub2api 通过 host.docker.internal 走代理。已废弃但配置仍保留在仓库 `mixin.yaml.example`
