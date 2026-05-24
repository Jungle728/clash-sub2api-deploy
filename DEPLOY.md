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

## 必需信息（迁移时准备）

| 项 | 占位符 | 说明 |
|---|---|---|
| 订阅 URL | `https://YOUR-SUBSCRIPTION-PROVIDER/...` | 机场订阅地址，作为 mihomo proxy-provider |
| 代理认证密码 | `REPLACE_WITH_PROXY_PASS` | mihomo 7890 的 Basic Auth；新部署用 `openssl rand -hex 12` 生成 |
| sub2api 镜像 | `weishaw/sub2api:latest` | docker hub 公共镜像 |
| sub2api 部署脚本 | `https://raw.githubusercontent.com/Wei-Shaw/sub2api/main/deploy/docker-deploy.sh` | 自动生成 db / jwt 等密钥 |
| mihomo 镜像 | `metacubex/mihomo:latest` | docker hub 公共镜像 |

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

### 2. 部署 sub2api 基础

```bash
mkdir -p ~/sub2api-deploy && cd ~/sub2api-deploy
curl -sSL https://raw.githubusercontent.com/Wei-Shaw/sub2api/main/deploy/docker-deploy.sh | bash
```

脚本会下载 `docker-compose.yml`、生成 `.env`（含随机的 POSTGRES_PASSWORD / JWT_SECRET / TOTP_ENCRYPTION_KEY）、创建数据目录。**记下脚本输出的三个密钥**（也保存在 `.env` chmod 600）。

### 3. 写 mihomo 配置

```bash
mkdir -p ~/sub2api-deploy/mihomo
# 把 mihomo/config.yaml.example 改实际值后保存为 mihomo/config.yaml
# 见附录 A
```

**关键替换**：

```bash
# 生成代理认证密码
PROXY_PASS=$(openssl rand -hex 12)
echo "PROXY_PASS=$PROXY_PASS"   # 保存好，稍后 docker-compose.override.yml 也要用
```

把 `mihomo/config.yaml` 中两处替换：
- `REPLACE_WITH_PROXY_PASS` → 你刚生成的 `$PROXY_PASS`
- `https://YOUR-SUBSCRIPTION-PROVIDER/...` → 你的实际订阅 URL

### 4. 写 docker-compose.override.yml

把附录 B 内容写到 `~/sub2api-deploy/docker-compose.override.yml`，并把 `REPLACE_WITH_PROXY_PASS` 替换成同一个 `$PROXY_PASS`。

> **关键点**：`mihomo/config.yaml` 的 `authentication` 字段密码必须和 `docker-compose.override.yml` 的 `HTTP_PROXY` URL 中的密码**一字不差**。

### 5. （可选）改对外端口

默认 `8080` 是常见 Web 端口，扫描器最爱光顾。建议换成 `49152-65535` 范围的高位端口：

```bash
sed -i 's/^SERVER_PORT=.*/SERVER_PORT=65432/' ~/sub2api-deploy/.env
```

### 6. 启动

```bash
cd ~/sub2api-deploy
docker compose pull          # 拉所有镜像
docker compose up -d         # 启动 mihomo + sub2api + postgres + redis
docker compose ps             # 应该看到 4 个容器都 healthy
docker compose logs sub2api 2>&1 | grep -i 'admin password'
```

**第一次启动会输出一行**：

```
Generated admin password (one-time): xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**这个密码只显示一次，立即保存**。账号 `admin@sub2api.local`。

### 7. 验证链路

```bash
bash ~/sub2api-deploy/smoke.sh
```

应输出 5 项全 OK。或手动测：

```bash
# 容器内出口 IP（应该是订阅节点的国外 IP）
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

## 故障排查

### sub2api 返回 503 "Service temporarily unavailable"

日志找 `account_select_failed`：

- `error="no available accounts"` → API key 所在 group 没有可用账号。Web UI 编辑账号绑分组
- `Token revoked (401)` → OAuth token 失效。Web UI 重新走授权
- `proxy connection refused` → mihomo 容器挂了。`docker compose ps` 检查 mihomo 状态，看 `docker logs sub2api-mihomo`

### sub2api OAuth 报"未设置代理"

sub2api 调上游 OAuth 不复用容器的 `HTTP_PROXY` env，每个账号必须**显式绑 proxy 记录**。在 Web UI：

1. 代理管理 / Proxies → 新建：host=`mihomo`, port=`7890`, username=`sub2api`, password=`<你的 PROXY_PASS>`
2. 账号管理 → 编辑账号 → proxy 字段选刚才创建的代理

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

### 备份

```bash
cd ~
docker compose -f sub2api-deploy/docker-compose.yml down
tar czf clash-sub2api-backup.tar.gz sub2api-deploy/
```

`sub2api-deploy/` 已经包含所有需要的：mihomo 配置、所有 db 数据、所有凭证、override 文件。

### 还原

```bash
# 在新机器上：装 docker（步骤 1），然后
tar xzf clash-sub2api-backup.tar.gz -C ~/
cd ~/sub2api-deploy
docker compose up -d
bash smoke.sh   # 5/5 全绿即完成
```

---

## 已知坑

1. **mihomo `fallback` 不支持延迟阈值**：只能整组失联才切下一组。需要"延迟超过阈值切换"的话要装第三方插件
2. **mihomo 改 config.yaml 必须 `docker compose restart mihomo`**：mihomo 不监听文件变化，热加载只能通过 API
3. **`allow-lan: true` 在容器内是必需的**：mihomo 默认只监听 127.0.0.1，sub2api 容器会连不到。配合 `authentication` 仍然安全（端口不暴露宿主）
4. **busybox wget 即使设了 NO_PROXY=localhost 也会走代理**：导致 docker healthcheck 假阴性。`-Y off` 修复
5. **订阅"信息节点"会污染 url-test**：用 `exclude-filter` 排除"流量"、"过期"、"套餐"等关键字
6. **sub2api 账号必须绑 proxy_id**：sub2api 不复用容器 HTTP_PROXY，每个账号要在 Web UI 显式选代理
7. **mihomo 第一次拉订阅是直连**：不走自己的 7890。如果服务器在 GFW 后面、订阅 URL 又被墙，mihomo 起不来。要么用 ip 直连的订阅 URL，要么暂时走代理拉一次再切回直连

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

- **2026-05-24 v2**: 全容器化部署。mihomo 容器化（替代宿主 nohup），订阅以 proxy-provider 自动刷新（替代 clashsub），sub2api 容器通过 docker 网络服务名找代理（替代 host.docker.internal）
- **2026-05-21 v1**: 初版。clash 装宿主 + nohup，sub2api 通过 host.docker.internal 走代理。已废弃但配置仍保留在仓库 `mixin.yaml.example`
