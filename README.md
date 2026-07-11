# sub2api account-proxy deploy

在 Linux 服务器上用 Docker Compose 运行 [sub2api](https://github.com/Wei-Shaw/sub2api)，通过 **sub2api 账户级代理**为不同上游账号选择独立的 HTTP、HTTPS 或 SOCKS5 出口。公网入口由 Caddy 提供 HTTPS，仓库不再为 sub2api 注入容器级全局代理，也不依赖 mihomo。

适合以下场景：

- 将自己的 ChatGPT Plus、Claude Pro/Max 等账号转换为 OpenAI / Anthropic 兼容 API
- OAuth 账号需要固定海外出口，但部分其他平台 API 允许服务器直连
- 希望在 sub2api 后台按账号设置代理，并在代理失败时阻止静默直连
- 需要把网关接入 Codex CLI、Claude Code 或 opencode

## 架构

```text
Codex / Claude Code / SDK
        |
        | HTTPS :443
        v
      Caddy                         远端标准代理
  (TLS 证书与续期)                 (HTTPS / SOCKS5)
        |                                |
        | Docker 私网 HTTP               | TLS / CONNECT
        v                                v
   sub2api:8080 -- 按账号 proxy_id --> OpenAI / Anthropic
        |
        +--> PostgreSQL
        +--> Redis
        |
        +--> 未绑定代理的指定账号可按策略直连
```

Caddy 只负责公网 HTTPS：客户端的 TLS 连接在 Caddy 结束，随后请求通过同机 Docker 私网转发到 `sub2api:8080`。sub2api 完成鉴权、模型路由和账号调度，再读取所选账号的 `proxy_id`。账户级代理和客户端入口是两条独立链路。

## 仓库内容

| 路径 | 用途 |
|---|---|
| `DEPLOY.md` | 从空服务器到可用网关的完整部署和迁移手册 |
| `.env.example` | sub2api、PostgreSQL 和服务端口变量模板 |
| `docker-compose.override.yml.example` | 低内存参数和 sub2api WebSocket 会话设置 |
| `docker-compose.https.yml.example` | Caddy HTTPS 反向代理服务 |
| `Caddyfile.example` | 公网 API 域名反代模板 |
| `check-direct.sh` | 检查宿主机直连上游的能力和风险 |
| `smoke.sh` | 检查容器、入口健康、全局代理残留及账户代理配置 |
| `scripts/update-sub2api.sh` | 检查镜像更新；有新镜像时备份数据库并重建 sub2api |
| `scripts/install-auto-update-cron.sh` | 安装每小时更新检查任务 |
| `ai-tools/` | 生成 Codex、Claude Code 和 opencode 客户端配置 |

## 快速开始

完整说明见 [DEPLOY.md](./DEPLOY.md)。下面是最短路径：

```bash
git clone https://github.com/Jungle728/clash-sub2api-deploy.git ~/sub2api-deploy
cd ~/sub2api-deploy

# 使用 sub2api 官方 local compose，避免仓库副本落后上游
curl -sSL https://raw.githubusercontent.com/Wei-Shaw/sub2api/main/deploy/docker-compose.local.yml \
  -o docker-compose.yml

cp .env.example .env
chmod 600 .env
for k in POSTGRES_PASSWORD JWT_SECRET TOTP_ENCRYPTION_KEY; do
  sed -i "s|^${k}=.*|${k}=$(openssl rand -hex 32)|" .env
done

cp docker-compose.override.yml.example docker-compose.override.yml
docker compose config
docker compose up -d
bash smoke.sh
```

首启管理员密码可从日志读取：

```bash
docker compose logs sub2api 2>&1 | grep -i 'admin password'
```

## 配置账户级代理

sub2api 支持以下标准格式：

```text
http://USER:PASSWORD@proxy.example.com:8080
https://USER:PASSWORD@proxy.example.com:443
socks5://USER:PASSWORD@proxy.example.com:1080
```

VLESS、VMess、Trojan 或 Reality 分享链接不能直接填入 sub2api。若代理服务器由 3x-ui 管理，应在实际出口节点额外创建带认证的 `HTTP` 或 `Mixed (SOCKS/HTTP)` 入站，并通过云防火墙只允许 sub2api 服务器 IP 访问。

在 sub2api 后台完成以下操作：

1. 添加代理并运行连通性测试。
2. 将需要隔离出口的 OAuth 账号绑定到该代理。
3. 对允许直连的账号保持未绑定状态。
4. 不启用“代理失败后回源直连”，避免意外暴露宿主机 IP。

## 启用 HTTPS

准备域名并将 DNS 指向服务器，然后：

```bash
cp docker-compose.https.yml.example docker-compose.https.yml
cp Caddyfile.example Caddyfile
# 将 Caddyfile 中的 YOUR-API-DOMAIN.example.com 改为真实域名
# 在 .env 中设置 BIND_HOST=127.0.0.1

docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.https.yml \
  up -d
```

公网客户端只访问 `https://api.example.com`。宿主机映射端口应绑定 `127.0.0.1`，PostgreSQL、Redis 和 sub2api 的容器端口不直接暴露公网。

## 自动更新

```bash
bash scripts/install-auto-update-cron.sh
```

cron 每小时检查 `weishaw/sub2api:latest`。镜像未变化时不会备份或重建；发现新镜像后，脚本先将 PostgreSQL 备份到 `backups/`，再只重建 sub2api 并检查 `/health`。

## 客户端配置

```bash
python3 ai-tools/gen_configs.py \
  --base-url https://api.example.com \
  --api-key REPLACE_WITH_API_KEY \
  --out-dir /tmp/sub2api-client-preview
```

确认预览后再使用 `--install`。完整选项见 [ai-tools/README.md](./ai-tools/README.md)。

## 安全原则

- 不提交 `.env`、实际 Compose/Caddy 配置、数据库、日志、备份或代理凭据。
- 账户代理必须使用强认证；公网 SOCKS5 还必须限制来源 IP，优先选择 HTTPS 代理。
- 已发送到聊天、Issue 或日志的代理链接和 UUID 应立即轮换。
- 代理测试成功不代表账号已绑定；需要在账号详情中确认 `proxy_id`。
- “容器 healthy”只说明本地服务正常，真实上游链路应结合业务请求日志验证。

## 相关项目

- [sub2api](https://github.com/Wei-Shaw/sub2api)
- [3x-ui](https://github.com/MHSanaei/3x-ui)
- [Caddy](https://github.com/caddyserver/caddy)

## 许可

MIT。仅供个人学习研究使用，请遵守相关服务条款。
