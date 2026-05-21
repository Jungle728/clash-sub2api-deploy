# sub2api + Clash 部署手册

一台新机器上，从零到 sub2api 可对外提供 API 转发服务的完整流程。本文档假定 Ubuntu 24.04 (`noble`)、x86_64、普通用户（带 sudo）。其他发行版按对应包管理器调整。

## 架构

```
客户端 → :8080 (sub2api 容器)
          │
          ├─ POST /v1/responses 等 OpenAI/Claude 网关请求
          │
          └─ 出站调用 OpenAI/Anthropic API
                 │  (HTTP_PROXY env)
                 ▼
              host.docker.internal:7890
                 │  (mihomo mixed-port, 带 Basic Auth)
                 │
                 │  rule 命中 *.openai.com / *.anthropic.com / claude.ai 等
                 ▼
              🤖 AI 优选 (fallback)
                 ├─ 1st: 🇺🇸 美国节点 (url-test, 13 个节点, 两订阅合并)
                 ├─ 2nd: 🇯🇵 日本节点 (url-test, 11 个节点, 两订阅合并)
                 └─ 3rd: DIRECT (兜底)
                 ▼
              真实出站节点 → OpenAI / Anthropic
```

**核心设计**

- **AI 流量优先美国节点**，整个美国节点组都不可用时才 fallback 日本，再不行直连
- **非 AI 流量**（包括 sub2api 容器本身的更新检查、pricing 拉取等）走 `🌐 全局出口`，默认日本，更稳定
- **两订阅合并**：订阅 1 用 `clashsub` 管理，订阅 2 以 `proxy-providers` 形式注入，自动每小时刷新
- **Cloudflare 拦截 IDC IP** 是已知现象：`claude.ai` / `chatgpt.com` 网页前端会返回 403，但 `api.openai.com` / `api.anthropic.com` API 端点不受影响。sub2api 调的是 API，所以可正常工作

---

## 必需信息（迁移时准备）

| 项 | 当前值 | 说明 |
|---|---|---|
| 订阅 1 URL | `https://YOUR-SUB1-PROVIDER/PATH/TOKEN` | 次元/SDK DNS 机场，由 `clashsub` 管理 |
| 订阅 2 URL | `https://YOUR-SUB2-PROVIDER/api/v1/client/subscribe?token=YOUR-SUB2-TOKEN` | 三毛机场，作为 proxy-provider |
| clash 仓库 | `https://github.com/nelvko/clash-for-linux-install` | 一键安装脚本 |
| sub2api 镜像 | `weishaw/sub2api:latest` | docker hub 公共镜像 |
| sub2api 部署脚本 | `https://raw.githubusercontent.com/Wei-Shaw/sub2api/main/deploy/docker-deploy.sh` | 自动生成密钥 |

---

## 部署步骤

### 1. 系统依赖

```bash
sudo apt-get update
sudo apt-get install -y curl unzip xz-utils
# 装 docker（官方源）
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release; echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"   # 新 shell 生效
```

验证：

```bash
docker --version              # 期望 27+ 或更高
docker compose version
```

### 2. 安装 mihomo (clash) + 加载第一订阅

```bash
git clone --branch master --depth 1 https://gh-proxy.org/https://github.com/nelvko/clash-for-linux-install.git
cd clash-for-linux-install

# 把订阅 URL 写进 .env，install.sh 会自动加为 id=1 并启用
sed -i 's|^CLASH_CONFIG_URL=.*|CLASH_CONFIG_URL=https://YOUR-SUB1-PROVIDER/PATH/TOKEN|' .env

bash install.sh
```

`install.sh` 装完会替换当前 shell（`exec $SHELL -i`），自动 source 上 `clashctl` 命令。如在脚本/CI 中执行，加 `timeout 180 bash install.sh` 防止挂住。

校验：

```bash
ss -tunl | grep 7890       # 应看到 mixed-port 监听
clashstatus                 # 应显示 mihomo 进程
```

### 3. 写 mixin.yaml（核心配置）

把 [附录 A](#附录-a-完整-mixinyaml) 的内容覆盖写入 `~/clashctl/resources/mixin.yaml`。这一步是整个部署的核心，决定了：

- 路由规则（AI 域名走美国，其他走日本）
- 两订阅合并 + 细分组
- AI 优选 fallback 策略
- `allow-lan: true` + `authentication`（让 docker 容器能用 mihomo 代理）
- `tun.enable: true`（让宿主全部流量走代理）

> **重要**：第 14 行的密码 `sub2api:REPLACE_WITH_PROXY_PASS` 是 mihomo 的代理认证。新机器部署时**必须重新生成一份**：
>
> ```bash
> openssl rand -hex 12
> ```
> 然后把生成的值替换到 mixin.yaml 第 14 行 + sub2api 的 `docker-compose.override.yml`（见步骤 5）。

应用 mixin：

```bash
source ~/clashctl/scripts/cmd/clashctl.sh
_merge_config_restart
sleep 3
ss -tunl | grep 7890       # 应该是 *:7890 而不是 127.0.0.1:7890
```

### 4. 部署 sub2api 基础

```bash
mkdir -p ~/sub2api-deploy && cd ~/sub2api-deploy
curl -sSL https://raw.githubusercontent.com/Wei-Shaw/sub2api/main/deploy/docker-deploy.sh | bash
```

脚本会下载 `docker-compose.yml`、生成 `.env`（含随机的 POSTGRES_PASSWORD / JWT_SECRET / TOTP_ENCRYPTION_KEY）、创建数据目录。**记下脚本输出的三个密钥**，它们也保存在 `.env` 文件里（chmod 600）。

### 5. 让 sub2api 容器走 clash

写 `~/sub2api-deploy/docker-compose.override.yml`，让 docker compose 自动合并到主配置：

```bash
PROXY_PASS="$(openssl rand -hex 12)"   # 新机器请新生成
echo "$PROXY_PASS" > /tmp/proxy_pass    # 临时记录，下一步要用

cat > ~/sub2api-deploy/docker-compose.override.yml <<EOF
# 让 sub2api 容器走宿主机的 clash 代理（监听 0.0.0.0:7890）
# host.docker.internal 通过 extra_hosts 解析到 host-gateway（docker 20.10+）
services:
  sub2api:
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      - HTTP_PROXY=http://sub2api:${PROXY_PASS}@host.docker.internal:7890
      - HTTPS_PROXY=http://sub2api:${PROXY_PASS}@host.docker.internal:7890
      - NO_PROXY=localhost,127.0.0.1,postgres,redis,host.docker.internal
      - http_proxy=http://sub2api:${PROXY_PASS}@host.docker.internal:7890
      - https_proxy=http://sub2api:${PROXY_PASS}@host.docker.internal:7890
      - no_proxy=localhost,127.0.0.1,postgres,redis,host.docker.internal
      - UPDATE_PROXY_URL=http://sub2api:${PROXY_PASS}@host.docker.internal:7890
EOF
```

**记得**把同样的 `$PROXY_PASS` 替换到 mixin.yaml 第 14 行 `sub2api:xxxxx`，然后再跑一次 `_merge_config_restart`。

启动：

```bash
cd ~/sub2api-deploy
docker compose pull
docker compose up -d
docker compose logs -f sub2api | grep -i 'admin password\|started'
```

第一次启动会输出一行类似：

```
Generated admin password (one-time): REPLACE_WITH_GENERATED_ADMIN_PWD
```

**这个密码只显示一次，立即保存**。它对应的账号是 `admin@sub2api.local`。

### 6. 验证链路

```bash
# 容器内出口 IP（应该是日本/美国 VPS IP，不是机器自己的公网 IP）
docker exec sub2api curl -s --max-time 8 https://api.ipify.org

# AI API 端点可达（404/421 是预期，表示路径正确但根 URL 没响应内容）
docker exec sub2api curl -s -o /dev/null -w "anthropic %{http_code}\n" https://api.anthropic.com/
docker exec sub2api curl -s -o /dev/null -w "openai    %{http_code}\n" https://api.openai.com/

# Web UI
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/health   # 期望 200
```

**预期结果**：

| 测试 | 期望 | 含义 |
|---|---|---|
| `api.ipify.org` | 返回非本机的国外 IP | mihomo 代理生效 |
| `api.anthropic.com` | HTTP 404 | 端点可达，根路径正常响应 |
| `api.openai.com` | HTTP 421 | 端点可达，区域重定向 |
| `claude.ai` | HTTP 403 | **预期**，CF 拦截 IDC IP，**不影响 sub2api API 转发** |
| `:8080/health` | HTTP 200 | sub2api 健康 |

---

## 路由策略详解

mihomo 处理流量按规则顺序匹配。当前生效的关键规则（mixin.yaml 中 `rules.prefix` + 订阅 + `rules.suffix`）：

```
rules:
  prefix:
    - DOMAIN,api64.ipify.org,DIRECT             # 用于 clashui
    - DOMAIN-SUFFIX,anthropic.com,🤖 AI 优选
    - DOMAIN-SUFFIX,claude.ai,🤖 AI 优选
    - DOMAIN-SUFFIX,openai.com,🤖 AI 优选
    - DOMAIN-SUFFIX,chatgpt.com,🤖 AI 优选
    - ... (其他 AI 域名)
  (订阅自带规则被插在中间，几乎用不到)
  suffix:
    - MATCH,🌐 全局出口                          # 兜底，所有未匹配走日本
```

**分组层级**：

| 分组 | 类型 | 候选 | 当前默认 |
|---|---|---|---|
| 🤖 AI 优选 | fallback | 美国节点 → 日本节点 → DIRECT | 美国（健康时） |
| 🌐 全局出口 | select | AI 优选 / 日本 / 美国 / 4 个细分组 | 日本节点 |
| 🇯🇵 日本节点（合并） | url-test | 两订阅 11 个日本节点 | 自动选最快 |
| 🇺🇸 美国节点（合并） | url-test | 两订阅 13 个美国节点 | 自动选最快 |
| 🇯🇵 日本-订阅1 / 订阅2 | url-test | 各订阅独立的日本节点 | 调试/手动切换用 |
| 🇺🇸 美国-订阅1 / 订阅2 | url-test | 各订阅独立的美国节点 | 调试/手动切换用 |

**fallback 行为说明**

mihomo 的 `fallback` 类型按列表顺序探测，**只判定可达性，不判定延迟阈值**。我们把 `🇺🇸 美国节点` 整个 url-test 组作为 fallback 的第一个候选——意味着：

- 单个美国节点延迟波动 → url-test 在该组内自动切换到更快的美国节点（不会跨国跳到日本）
- 整个美国节点组都不通（健康检查 60s 探测一次失败）→ fallback 切到 `🇯🇵 日本节点`
- 日本也不通 → 直连兜底（不至于完全断网）

恢复时也是自动的：fallback 会重新检查首选项，可达后切回美国。

---

## 凭证清单（迁移时必备）

| 凭证 | 当前值 | 来源 | 必须保留 |
|---|---|---|---|
| mihomo 代理认证 | `sub2api:REPLACE_WITH_PROXY_PASS` | 自定义 | 是，docker-compose.override.yml 也要同步 |
| sub2api Web UI 密码 | `REPLACE_WITH_GENERATED_ADMIN_PWD`<br>(账号 `admin@sub2api.local`) | 首启日志，**只显示一次** | **是，丢了只能进 DB 重置** |
| POSTGRES_PASSWORD | `REPLACE_WITH_POSTGRES_PASSWORD` | `~/sub2api-deploy/.env` 自动生成 | **是，丢了 DB 文件就读不出来** |
| JWT_SECRET | `REPLACE_WITH_JWT_SECRET` | 同上 | 是，丢了所有用户被强制重新登录 |
| TOTP_ENCRYPTION_KEY | `REPLACE_WITH_TOTP_KEY` | 同上 | 是，丢了所有 2FA 失效 |

> **⚠️ 这些值是当前实例的真实凭证。新机器部署应**该重新生成新值（见步骤 1）。本文是给"恢复同一套实例"做参考。

---

## 日常运维

### clash

```bash
clashon                     # 开启代理（含系统代理 env）
clashoff                    # 关闭
clashstatus                 # 看内核进程
clashlog                    # 实时日志
clashui                     # 显示 Web 控制台地址 + 密钥
clashmixin                  # 看当前 mixin
clashmixin -e               # 编辑 mixin（vim），保存自动 merge + restart
clashmixin -r               # 看运行时 runtime.yaml（merge 后）
clashsub ls                 # 列出订阅（仅订阅1）
clashsub update             # 刷新订阅1（订阅2 由 mihomo 每小时自动刷新）
clashtun on / off           # 开关 tun 模式（需要 sudo，重启）
```

Web 面板：`http://<server>:9090/ui`（外网放行 9090；secret 在 `~/clashctl/resources/runtime.yaml` 第一行 `secret:`）

### sub2api

```bash
cd ~/sub2api-deploy
docker compose ps
docker compose logs -f sub2api
docker compose restart sub2api          # 重启
docker compose down                     # 停止所有（保留数据卷）
docker compose up -d                    # 启动
docker compose pull && docker compose up -d   # 升级镜像
```

Web 面板：`http://<server>:8080`

### 切换出口分组（无缝）

打开 `http://<server>:9090/ui` → 「代理」→ 找到 `🤖 AI 优选` 或 `🌐 全局出口` Selector → 点击候选项即时切换。

或命令行：

```bash
SECRET=$(~/clashctl/bin/yq '.secret // ""' ~/clashctl/resources/runtime.yaml)
ENC=$(printf '%s' "🌐 全局出口" | python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.stdin.read()))')
curl -s --noproxy '*' -X PUT \
  -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"name":"🇺🇸 美国节点"}' \
  "http://127.0.0.1:9090/proxies/$ENC"
```

切换时已建立的 TCP 连接继续走旧节点直到自然关闭，新连接走新节点——**对 sub2api 进行中的请求无影响**。

---

## 故障排查

### sub2api 返回 503 "Service temporarily unavailable"

日志找 `account_select_failed` 关键字。两种情况：

1. `error="no available accounts"` —— 你的 API key 所在的 group 里没有绑定可用账号。去 Web UI「账号管理」编辑账号，勾选目标分组保存。
2. `excluded_account_count: N` —— 有账号但都被排除了（可能 401/403 失效、限速冷却、或被手动 schedulable=false）。

直接查 DB：

```bash
docker exec sub2api-postgres psql -U sub2api -d sub2api -c \
  "SELECT a.id, a.name, a.platform, a.status, ag.group_id, g.name AS grp \
   FROM accounts a LEFT JOIN account_groups ag ON ag.account_id=a.id \
   LEFT JOIN groups g ON g.id=ag.group_id WHERE a.deleted_at IS NULL;"
```

确认账号有 `group_id`（不是 NULL），且 `status=active`。

### 容器内 wget/curl 返回 502 / 000

- **502** = mihomo 代理收到客户端请求但上游拒绝（常见于 Cloudflare 反 IDC，针对 web 前端）。换一个出口节点再试，或者确认目标 URL 是不是 API 端点（API 通常放行）。
- **000** = 容器到 mihomo 的连接根本建不起来。检查：
  - `ss -tunl | grep 7890` 是否 `*:7890`（不是 `127.0.0.1:7890`） → 检查 mixin 的 `allow-lan: true`
  - `docker exec sub2api getent hosts host.docker.internal` 是否解析到 docker0 IP（`172.17.0.1` 或类似）
  - `docker compose config | grep PROXY` 确认环境变量注入正确

### mihomo 启动报 "use or proxies missing"

mixin.yaml 里某个 proxy-group 写了 `use-existing-proxies: true` 但既没有 `proxies:` 列表，也没有 `use:` 引用 provider。修法：

- 想包含订阅 1 的所有节点 → 用 `include-all-proxies: true`
- 想包含 provider 节点 → 用 `use: [smjc]`
- 想全部包含 → 用 `include-all: true`

### mihomo 启动报 "test failed"，旧进程继续跑

`_merge_config_restart` 校验失败时会回滚到旧 runtime，但**它不会回滚 mixin.yaml 文件**——你写错的内容还在 mixin 里，下次重启还是会失败。修 mixin 后再跑一次。

### sub2api 的 "Cloudflare 验证页"问题

- `claude.ai` / `chatgpt.com` 网页版被 CF 拦 → 这是 IDC IP 在 CF 黑名单里，换"住宅 IP 解锁机场"才能解决，sub2api 不需要这种链路
- `console.anthropic.com` OAuth 登录受影响 → 临时用 `clash` Web 面板把 `🌐 全局出口` 切到一个能过 CF 的节点，登完再切回来

---

## 迁移到新机器

### 备份这些（旧机器）

```bash
# 1. clash 配置和订阅
tar czf clashctl-backup.tar.gz \
  ~/clashctl/.env \
  ~/clashctl/resources/mixin.yaml \
  ~/clashctl/resources/profiles.yaml \
  ~/clashctl/resources/profiles/ \
  ~/clashctl/resources/providers/

# 2. sub2api 部署目录（含 .env / DB / Redis）
docker compose -f ~/sub2api-deploy/docker-compose.yml down
tar czf sub2api-backup.tar.gz ~/sub2api-deploy/
```

### 在新机器恢复

```bash
# 1. 走步骤 1（装系统依赖、docker）
# 2. 装 clash（步骤 2）
# 3. 替换配置：
tar xzf clashctl-backup.tar.gz -C /
source ~/clashctl/scripts/cmd/clashctl.sh
_merge_config_restart

# 4. 还原 sub2api：
tar xzf sub2api-backup.tar.gz -C /
cd ~/sub2api-deploy
docker compose up -d
```

数据库（postgres）的密码已经在 `.env` 里，配套的 `postgres_data/` 也一起拷过来，直接能起。

### 完全重建（不要旧数据）

跳过备份还原，按完整 6 步走，sub2api 首启会重建 admin 账号 + 数据库。把订阅 URL 和 mixin.yaml 复制过来即可。

---

## 已知局限和坑

1. **mihomo fallback 不支持延迟阈值**：只能"整组 down 了才切下一组"，无法配"美国延迟 > 500ms 切日本"。如果美国 url-test 内全部节点都很慢但 TCP 还能连，fallback 不会触发。如需，要装第三方插件或前置一个高延迟检测脚本。

2. **mixin 的 `proxy-groups.override` 和 `proxy-groups.suffix` 不能并存空行**：YAML 里同 key 出现两次，后者会覆盖前者（实测踩过坑，"override 是空的"导致订阅自带的 `SDK DNS` 组没被改写）。整文件改用 `edit` 微调，避免 `write` 整体重写。

3. **`allow-lan: true` 必须和 `authentication` 同时存在**：否则 mihomo 7890 会暴露在公网无密码，任何人都能拿来当代理跳板。当前 auth 凭证 `sub2api:REPLACE_WITH_PROXY_PASS`，新部署务必换。

4. **install.sh 末尾会 `exec $SHELL`**：在非交互环境（脚本/CI）会挂住；包一层 `timeout 180 bash install.sh` 即可。

5. **CF 反 IDC 仅影响 Web 入口**：sub2api 走 `api.openai.com` / `api.anthropic.com` 都没问题。如果将来 sub2api 增加了"自动 OAuth 登录"流程（要访问 `console.anthropic.com` web 表单），可能在 CF 验证那一步卡住——届时手动切节点或改 OAuth refresh_token 模式。

6. **订阅 1 的"信息节点"会污染分组**：第一订阅的 proxies 列表里有"剩余流量"、"套餐到期"等占位项目（无效 proxy），mihomo 的 url-test 会把它们也算进去测试失败。当前 filter 用 `日本|美国` 等地理关键字过滤，避开它们；但如果机场升级或换机场，命名风格变了，要重新校准 filter 正则。

---

## 附录 A: 完整 mixin.yaml

> 路径：`~/clashctl/resources/mixin.yaml`（同时建议提交到 `~/code/clash-for-linux-install/resources/mixin.yaml` 备份）。
>
> 第 14 行 `sub2api:REPLACE_WITH_PROXY_PASS` 是当前实例的代理认证；新部署请用 `openssl rand -hex 12` 重新生成，并同步替换 `~/sub2api-deploy/docker-compose.override.yml` 中的同名密码。

```yaml
_custom:
  system-proxy:
    enable: true

mixed-port: 7890

external-controller: "0.0.0.0:9090"
external-ui: dist
external-ui-url: https://github.com/Zephyruso/zashboard/releases/latest/download/dist.zip
secret:

allow-lan: true # 允许局域网/容器访问 mixed-port，需配 authentication 才安全
authentication:
  - "sub2api:REPLACE_WITH_PROXY_PASS" # 用户验证（clashon 会自动填充）

rules:
  prefix:
    - DOMAIN,api64.ipify.org,DIRECT
    # ===== AI 服务强制走 JP/US =====
    - DOMAIN-SUFFIX,anthropic.com,🤖 AI 优选
    - DOMAIN-SUFFIX,claude.ai,🤖 AI 优选
    - DOMAIN-KEYWORD,claude,🤖 AI 优选
    - DOMAIN-SUFFIX,openai.com,🤖 AI 优选
    - DOMAIN-SUFFIX,chatgpt.com,🤖 AI 优选
    - DOMAIN-SUFFIX,oaistatic.com,🤖 AI 优选
    - DOMAIN-SUFFIX,oaiusercontent.com,🤖 AI 优选
    - DOMAIN-KEYWORD,openai,🤖 AI 优选
    - DOMAIN-KEYWORD,chatgpt,🤖 AI 优选
    - DOMAIN-SUFFIX,gemini.google.com,🤖 AI 优选
    - DOMAIN-SUFFIX,bard.google.com,🤖 AI 优选
    - DOMAIN-SUFFIX,generativelanguage.googleapis.com,🤖 AI 优选
  suffix:
    - MATCH,🌐 全局出口

# 第二订阅作为节点池注入；mihomo 每小时自动刷新
proxy-providers:
  smjc:
    type: http
    url: "https://YOUR-SUB2-PROVIDER/api/v1/client/subscribe?token=YOUR-SUB2-TOKEN"
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

proxies:
  prefix:
  suffix:
  override:

proxy-groups:
  override:
    - name: SDK DNS
      type: select
      proxies:
        - 🌐 全局出口
        - 🇯🇵 日本节点
        - 🇺🇸 美国节点
        - 🇯🇵 日本-订阅1
        - 🇯🇵 日本-订阅2
        - 🇺🇸 美国-订阅1
        - 🇺🇸 美国-订阅2
        - DIRECT
  prefix:
    # AI 优选：fallback，美国整体不可用才掉到日本
    - name: 🤖 AI 优选
      type: fallback
      url: https://www.gstatic.com/generate_204
      interval: 60
      timeout: 5000
      proxies:
        - 🇺🇸 美国节点
        - 🇯🇵 日本节点
        - DIRECT

    # 总出口：默认走日本组（更稳定），AI 流量由 fallback 处理
    - name: 🌐 全局出口
      type: select
      proxies:
        - 🤖 AI 优选
        - 🇯🇵 日本节点
        - 🇺🇸 美国节点
        - 🇯🇵 日本-订阅1
        - 🇯🇵 日本-订阅2
        - 🇺🇸 美国-订阅1
        - 🇺🇸 美国-订阅2

    # 日本节点合并组（两订阅）
    - name: 🇯🇵 日本节点
      type: url-test
      url: https://www.gstatic.com/generate_204
      interval: 300
      tolerance: 50
      use-existing-proxies: true
      include-all: true
      filter: "(?i)日本|jp\\b|japan|tokyo|osaka|🇯🇵"

    # 美国节点合并组（两订阅）
    - name: 🇺🇸 美国节点
      type: url-test
      url: https://www.gstatic.com/generate_204
      interval: 300
      tolerance: 50
      use-existing-proxies: true
      include-all: true
      filter: "(?i)美国|us\\b|usa|united states|america|los angeles|seattle|san jose|silicon|kansas|🇺🇸"

    # 仅订阅1的日本节点
    - name: 🇯🇵 日本-订阅1
      type: url-test
      url: https://www.gstatic.com/generate_204
      interval: 300
      tolerance: 50
      include-all-proxies: true
      filter: "(?i)日本|jp\\b|japan|tokyo|osaka|🇯🇵"

    # 仅订阅2的日本节点
    - name: 🇯🇵 日本-订阅2
      type: url-test
      url: https://www.gstatic.com/generate_204
      interval: 300
      tolerance: 50
      use:
        - smjc
      filter: "(?i)日本|jp\\b|japan|tokyo|osaka|🇯🇵"

    # 仅订阅1的美国节点
    - name: 🇺🇸 美国-订阅1
      type: url-test
      url: https://www.gstatic.com/generate_204
      interval: 300
      tolerance: 50
      include-all-proxies: true
      filter: "(?i)美国|us\\b|usa|united states|america|los angeles|seattle|san jose|silicon|🇺🇸"

    # 仅订阅2的美国节点
    - name: 🇺🇸 美国-订阅2
      type: url-test
      url: https://www.gstatic.com/generate_204
      interval: 300
      tolerance: 50
      use:
        - smjc
      filter: "(?i)美国|us\\b|usa|united states|america|los angeles|seattle|san jose|silicon|kansas|堪萨斯|🇺🇸"
  suffix:

# tun 配置（让宿主所有程序的流量都走代理；docker 容器走 HTTP_PROXY env，不依赖 tun）
tun:
  enable: true
  stack: system
  auto-route: true
  auto-redir: true
  auto-redirect: true
  auto-detect-interface: true
  dns-hijack:
    - any:53
    - tcp://any:53
  strict-route: true
  route-exclude-address:
    - 1.1.1.1/32
    - 127.0.0.1/32
  exclude-interface:
    - docker0
    - podman0

dns:
  enable: true
  listen: 0.0.0.0:1053
  enhanced-mode: fake-ip
  default-nameserver:
    - 223.5.5.5
    - 119.29.29.29
  proxy-server-nameserver:
    - 223.5.5.5
    - 119.29.29.29
    - 1.1.1.1
    - 8.8.8.8
  nameserver:
    - 114.114.114.114
    - 8.8.8.8
```

---

## 附录 B: docker-compose.override.yml

> 路径：`~/sub2api-deploy/docker-compose.override.yml`
>
> 与 `~/sub2api-deploy/docker-compose.yml`（部署脚本下载的官方版）自动合并。第 8-13 行的密码必须和 mixin.yaml 第 14 行 `authentication` 一致。

```yaml
# 让 sub2api 容器走宿主机的 clash 代理（监听 0.0.0.0:7890）
# host.docker.internal 通过 extra_hosts 解析到 host-gateway（docker 20.10+）
services:
  sub2api:
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      - HTTP_PROXY=http://sub2api:REPLACE_WITH_PROXY_PASS@host.docker.internal:7890
      - HTTPS_PROXY=http://sub2api:REPLACE_WITH_PROXY_PASS@host.docker.internal:7890
      - NO_PROXY=localhost,127.0.0.1,postgres,redis,host.docker.internal
      - http_proxy=http://sub2api:REPLACE_WITH_PROXY_PASS@host.docker.internal:7890
      - https_proxy=http://sub2api:REPLACE_WITH_PROXY_PASS@host.docker.internal:7890
      - no_proxy=localhost,127.0.0.1,postgres,redis,host.docker.internal
      - UPDATE_PROXY_URL=http://sub2api:REPLACE_WITH_PROXY_PASS@host.docker.internal:7890
```

---

## 附录 C: 端到端冒烟测试

启动后跑这个 bash 脚本，全部通过即认为部署成功：

```bash
#!/usr/bin/env bash
set -e
PROXY_PASS="REPLACE_WITH_PROXY_PASS"
SUB2API_URL="http://127.0.0.1:8080"

echo "[1/5] mihomo 监听..."
ss -tunl | grep -q '\*:7890' && echo "  OK *:7890 (allow-lan)" || { echo "  FAIL: not *:7890"; exit 1; }

echo "[2/5] 宿主走代理出口..."
ip=$(curl -s --max-time 8 -x "http://sub2api:$PROXY_PASS@127.0.0.1:7890" https://api.ipify.org)
[[ -n "$ip" ]] && echo "  OK exit=$ip" || { echo "  FAIL"; exit 1; }

echo "[3/5] 容器走代理出口..."
ip=$(docker exec sub2api curl -s --max-time 8 https://api.ipify.org)
[[ -n "$ip" ]] && echo "  OK exit=$ip" || { echo "  FAIL"; exit 1; }

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
```

保存为 `~/sub2api-deploy/smoke.sh && chmod +x ~/sub2api-deploy/smoke.sh`，部署后跑一次。

---

## 修订历史

- 2026-05-21 v1：初版。clash + 双订阅 + sub2api + AI 优选 fallback。
