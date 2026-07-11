# sub2api 账户级代理部署手册（v3）

本文从一台空 Linux 服务器开始，部署 sub2api、PostgreSQL、Redis 和可选的 Caddy HTTPS 入口。上游出口由 **sub2api 账户级代理**控制：需要海外出口的 OAuth 账号绑定远端 HTTPS/SOCKS5 代理，明确允许直连的账号保持未绑定。

仓库不再运行 mihomo，也不向 sub2api 容器注入 `HTTP_PROXY` / `HTTPS_PROXY`。这样账号路由、出口策略和失败行为都由 sub2api 自己管理。

## 1. 请求链路

```text
客户端
  -> https://api.example.com:443
  -> Caddy（TLS 证书、续期、解密）
  -> Docker 私网 http://sub2api:8080
  -> API Key / 分组 / 模型路由
  -> 选择上游账号并读取 proxy_id
       -> 已绑定：HTTPS/SOCKS5 账户代理 -> OpenAI/Anthropic
       -> 未绑定：宿主机直接出口 -> 对应平台 API
```

响应按原路返回。Caddy 只处理客户端入口 HTTPS，不参与上游账号选择。账户代理只处理 sub2api 到上游平台的连接，不处理客户端到 Caddy 的连接。

## 2. 准备清单

必须准备：

- 一台支持 Docker 的 Linux 服务器
- sub2api 所需的账号和 OAuth 凭据
- 至少一个标准 HTTP、HTTPS 或 SOCKS5 代理（需要隔离出口时）
- 可选 API 域名，例如 `api.example.com`
- 远端代理固定域名或 IP，例如 `proxy.example.com`

推荐最低资源：1 GB 内存、10 GB 可用磁盘。数据库、Redis、日志和备份均保存在部署目录下，迁移时可整体复制。

### 代理要求

sub2api 接受：

```text
http://USER:PASSWORD@HOST:PORT
https://USER:PASSWORD@HOST:PORT
socks5://USER:PASSWORD@HOST:PORT
socks5h://USER:PASSWORD@HOST:PORT
```

VLESS、VMess、Trojan、Hysteria2、Reality 分享链接不是标准应用代理，不能直接导入。使用 3x-ui 时，在实际出口节点新建 `HTTP` 或 `Mixed (SOCKS/HTTP)` 入站即可；该入站默认从该节点直接出网。

公网 SOCKS5 不加密认证和代理握手。优先使用 HTTPS 代理；若使用 SOCKS5，必须在云安全组和本机防火墙中只允许 sub2api 服务器 IP。

## 3. 安装 Docker

以下以 Debian/Ubuntu 为例：

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git openssl
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

重新登录后检查：

```bash
docker version
docker compose version
```

## 4. 获取部署文件

```bash
git clone https://github.com/Jungle728/clash-sub2api-deploy.git ~/sub2api-deploy
cd ~/sub2api-deploy

curl -sSL https://raw.githubusercontent.com/Wei-Shaw/sub2api/main/deploy/docker-compose.local.yml \
  -o docker-compose.yml
```

`docker-compose.yml` 使用上游官方版本，本仓库不跟踪它，避免长期落后。仓库只维护部署覆盖模板和运维脚本。

## 5. 配置环境变量

```bash
cp .env.example .env
chmod 600 .env

for k in POSTGRES_PASSWORD JWT_SECRET TOTP_ENCRYPTION_KEY; do
  sed -i "s|^${k}=.*|${k}=$(openssl rand -hex 32)|" .env
done

grep -n 'REPLACE_WITH' .env
```

最后一条应无输出。重点字段：

- `POSTGRES_PASSWORD`：数据库密码
- `JWT_SECRET`：登录会话签名密钥，重启后必须保持不变
- `TOTP_ENCRYPTION_KEY`：2FA 密钥加密材料，迁移时必须保留
- `SERVER_PORT`：宿主机本地管理端口
- `BIND_HOST`：启用 Caddy 后应为 `127.0.0.1`
- `UPDATE_PROXY_URL`：仅控制 GitHub/价格更新，不等于账户代理；允许直连时留空

不要在 `.env` 中配置容器级 `HTTP_PROXY` 或 `HTTPS_PROXY`。它们不能替代账号绑定，还会让健康检查、更新任务和故障定位变得含混。

## 6. 应用资源覆盖配置

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
docker compose config
```

模板只包含低内存参数、日志限制和 OpenAI WebSocket 会话设置，不包含代理地址。账户代理保存在 sub2api 数据库中。

## 7. 启动基础服务

```bash
docker compose up -d
docker compose ps
docker compose logs --tail=100 sub2api
```

预期服务：

- `sub2api`
- `sub2api-postgres`
- `sub2api-redis`

读取首启管理员密码：

```bash
docker compose logs sub2api 2>&1 | grep -i 'admin password'
```

本地健康检查：

```bash
source ./.env
curl -fsS "http://127.0.0.1:${SERVER_PORT:-8080}/health"
```

## 8. 配置 Caddy HTTPS

将 API 域名的 DNS A/AAAA 记录指向 sub2api 服务器，并放行 `80/443`。然后：

```bash
cp docker-compose.https.yml.example docker-compose.https.yml
cp Caddyfile.example Caddyfile
```

把 `Caddyfile` 中的 `YOUR-API-DOMAIN.example.com` 改为实际域名，并在 `.env` 中设置：

```dotenv
BIND_HOST=127.0.0.1
```

启动：

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.https.yml \
  up -d
```

验证：

```bash
curl -fsS https://api.example.com/health
ss -tln | grep "${SERVER_PORT:-8080}"
```

宿主机端口应只监听 `127.0.0.1`。公网客户端使用 `https://api.example.com`，不会直接访问容器端口 `8080`。

### TLS 终止是什么意思

客户端先与 Caddy 建立 HTTPS。Caddy 提供证书并解密请求，再通过同机 Docker 私网以 HTTP 转发到 `sub2api:8080`。sub2api 返回响应后，Caddy 重新通过 HTTPS 发给客户端。这不会改变 API 内容或账号路由，只是把证书管理从 sub2api 中分离出来。

## 9. 在 3x-ui 出口节点创建标准代理

若 3x-ui 就运行在目标海外出口服务器，不需要让 sub2api 解析 VLESS 链接。直接在该节点添加标准代理入站。

### 推荐：HTTPS 代理

在 3x-ui 的 Inbounds 页面新增：

- Protocol：`HTTP`
- Listen：`0.0.0.0`
- Port：未占用高位端口
- Account：独立强用户名和密码
- Transport：RAW/TCP
- Security：TLS
- Certificate：与代理域名匹配的有效证书
- Sniffing：关闭

在 sub2api 中使用：

```text
https://USER:PASSWORD@proxy.example.com:PORT
```

### 备选：Mixed SOCKS5

在 3x-ui 中选择 `Mixed (SOCKS/HTTP)`，启用 `password` 认证并关闭 UDP。随后在 sub2api 中使用：

```text
socks5://USER:PASSWORD@proxy.example.com:PORT
```

防火墙必须只允许 sub2api 服务器 IP：

```bash
sudo ufw insert 1 allow proto tcp from SUB2API_SERVER_IP to any port PROXY_PORT
sudo ufw deny PROXY_PORT/tcp
sudo ufw status numbered
```

云平台安全组也应配置相同的 `/32` 来源限制。不要把标准代理开放给 `0.0.0.0/0`。

## 10. 在 sub2api 中绑定账号

登录管理后台：

1. 打开代理管理并添加标准代理 URL。
2. 运行代理连通性测试，确认状态为 active。
3. 打开需要海外出口的 OAuth 账号并选择该代理。
4. 对允许直连的其他平台账号保持代理为空。
5. 不配置“代理失败后直连”的 fallback；代理故障时应 fail closed。

账号是否实际走代理取决于账号记录中的 `proxy_id`，不是容器环境变量。sub2api 会在每个请求中先按平台、模型、分组、额度和状态选择账号，再读取该账号的代理。

## 11. 验证实际链路

### 基础冒烟检查

```bash
bash smoke.sh
```

脚本会检查：

- PostgreSQL、Redis 和 sub2api 是否健康
- 本地 `/health` 是否返回 200
- Compose 和容器中是否残留 mihomo 或非空全局代理变量
- 数据库中 active 代理和账号绑定数量

未绑定代理的账号可能是有意直连，脚本只报告，不会擅自判定失败。

### 从客户端产生一次真实请求

使用 Codex、Claude Code 或 SDK 调用一次 API，然后检查：

```bash
docker compose logs --since 10m sub2api | grep 'http request completed'
docker exec sub2api netstat -tn
```

日志中的 `account_id` 应对应已绑定代理的账号，并返回 `200`。TCP 连接应指向账户代理服务器；明确允许直连的账号可能同时产生其他公网连接。

验证 HTTPS 账户代理本身时，OpenAI models 端点返回 `401` 即表示代理链路和 TLS 均正常：

```bash
curl --proxy 'https://USER:PASSWORD@proxy.example.com:PORT' \
  -o /dev/null -w '%{http_code}\n' \
  https://api.openai.com/v1/models
```

不要在共享终端、Issue 或日志中粘贴真实凭据。测试完成后清理包含代理密码的 shell 历史。

## 12. 客户端接入

仓库的 `ai-tools/` 可探测网关协议并生成客户端配置：

```bash
python3 ai-tools/gen_configs.py \
  --base-url https://api.example.com \
  --api-key REPLACE_WITH_API_KEY \
  --out-dir /tmp/sub2api-client-preview
```

预览确认后再增加 `--install`。生成文件可能包含 API Key，不要放入 Git。

## 13. 日常运维

### 查看状态和日志

```bash
docker compose ps
docker compose logs -f sub2api
docker compose logs -f postgres
docker compose logs -f redis
docker compose logs -f caddy
```

### 每小时检查更新

```bash
bash scripts/install-auto-update-cron.sh
crontab -l | grep 'clash-sub2api-deploy auto update'
```

`scripts/update-sub2api.sh` 会拉取 `weishaw/sub2api:latest`。运行中容器已使用最新镜像时，脚本跳过备份和重建；镜像发生变化时，流程为：

1. 使用 `pg_dump` 备份 PostgreSQL 到 `backups/`。
2. 只重建 `sub2api` 服务。
3. 等待容器健康。
4. 请求本地 `/health`。
5. 清理超过保留天数的旧备份。

手动执行：

```bash
bash scripts/update-sub2api.sh
tail -n 100 logs/sub2api-auto-update.log
```

### 手动备份

```bash
source ./.env
docker exec sub2api-postgres \
  pg_dump -U "${POSTGRES_USER:-sub2api}" "${POSTGRES_DB:-sub2api}" \
  | gzip > "backups/sub2api-db-$(date +%Y%m%d-%H%M%S).sql.gz"
```

备份文件包含账号、代理和凭据数据，权限应限制为仅管理员读取。

## 14. 从旧 mihomo 架构迁移

迁移顺序不能颠倒：

1. 在远端出口服务器创建标准 HTTPS/SOCKS5 代理。
2. 在 sub2api 中添加代理并测试。
3. 将所有需要代理的 OAuth 账号绑定到代理。
4. 产生真实请求并确认返回 200。
5. 从 Compose 覆盖文件删除 mihomo 服务及 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`。
6. 删除 `depends_on.mihomo`，并清空指向 mihomo 的 `UPDATE_PROXY_URL`。
7. 重建 sub2api，再次验证账户代理请求。
8. 最后停止并移除 mihomo 容器。

验证配置中不再存在 mihomo：

```bash
docker compose config --services
docker inspect sub2api --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -i proxy
```

允许保留旧配置目录作为短期回滚备份，但不得提交其中的订阅、节点或认证信息。

## 15. 迁移到新机器

### 整体迁移

旧机器：

```bash
cd ~/sub2api-deploy
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.https.yml \
  down
cd ~
tar czf sub2api-deploy-backup.tar.gz sub2api-deploy/
```

新机器安装 Docker 后解压并启动：

```bash
tar xzf sub2api-deploy-backup.tar.gz
cd sub2api-deploy
docker compose up -d
bash smoke.sh
```

必须保留 `.env`、`data/`、`postgres_data/` 和 `redis_data/`。若启用 Caddy，还应保留实际 `Caddyfile`、`caddy_data/` 和 `caddy_config/`。

迁移后检查远端代理防火墙：来源 IP 已变化时，需要把旧服务器 IP 替换为新服务器 IP，否则账户代理会拒绝连接。

## 16. 故障排查

### sub2api healthy，但模型请求失败

容器健康只验证 `/health`。依次检查：

1. 账号状态、额度和 OAuth 是否有效。
2. 账号是否绑定正确的 `proxy_id`。
3. 代理状态是否 active。
4. 远端安全组是否允许当前 sub2api 服务器 IP。
5. 代理失败时是否错误地回退直连。

### 添加 VLESS 链接提示格式不支持

这是预期行为。VLESS 是隧道协议，不是 sub2api 支持的应用代理 URL。请在实际出口服务器上新增 HTTP/HTTPS/Mixed 入站，然后向 sub2api 填写标准代理地址。

### HTTPS 代理连接失败

- 确认证书域名与代理主机名一致。
- 检查代理端口和安全组来源限制。
- 密码含 `@`, `:`, `/`, `#`, `%` 时需要 URL 编码；优先使用足够长的字母数字密码。
- 用 `curl --proxy` 测试 OpenAI models 端点，预期返回 401。

### OAuth 账号仍在直连

检查账号本身的 `proxy_id`。全局 `HTTP_PROXY` 不是账户代理的替代方案。数据库里 `platform=active` 也不等于该账号当前在业务分组中使用，应结合账号绑定和真实请求日志判断。

### Caddy HTTPS 正常，但本地 8080 不可公网访问

这是正确状态。客户端应访问 Caddy 的 443；`sub2api:8080` 只供 Docker 私网使用，宿主映射端口应限制在 `127.0.0.1`。

### 自动更新没有重建

当拉取后的镜像 ID 与运行中容器镜像一致时，脚本会主动跳过备份和重建。查看 `logs/sub2api-auto-update.log` 确认 `No update needed`。

## 17. 安全检查

提交或分享配置前确认：

```bash
git status --short
git diff --cached
```

以下内容绝不能进入 Git：

- `.env`、真实 API Key、管理员密码
- 实际代理 URL、用户名和密码
- VLESS/VMess/Trojan 分享链接和 UUID
- PostgreSQL/Redis 数据目录
- Caddy 证书和私钥
- 日志、数据库备份和生成的客户端配置

已泄露的 UUID、代理密码或 API Key 必须轮换，不能仅从最新提交中删除；Git 历史和外部日志可能仍保留旧值。

## 修订历史

- **2026-07-11 v3.0**：移除 mihomo 运行依赖，改为 Caddy HTTPS 入口 + sub2api 账户级代理；补充 3x-ui 标准代理、TLS 链路、迁移和防直连验证。
- **2026-07-09 v2.5**：自动更新改为每小时检查；无新镜像时跳过数据库备份和服务重建。
- **2026-05-27 v2.2**：增加 Caddy HTTPS 反向代理模板。
