# clash-sub2api-deploy

在一台 Linux 服务器上用 **mihomo (clash)** 提供出海代理，再用 **sub2api** 把自己的 ChatGPT Plus / Claude Max 账号转成统一 OpenAI / Anthropic 兼容的 API 接口。仓库还附带 [`ai-tools/`](./ai-tools/) 客户端配置生成器，可一键把网关接到 codex CLI / Claude Code / opencode。

适合场景：

- 自己有 ChatGPT Plus / Claude Pro/Max 账号，想用编程 SDK / Cline / Cursor 等以 API 方式调用
- 服务器在境内或在被 OpenAI/Anthropic 拒绝的 IDC 区域，需要先经过代理出海
- AI 流量优先走美国节点，自动 fallback 日本，节点不可达时切换无感

## 架构（v2 全容器化）

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
              JP / US 节点 → AI API (OpenAI/Anthropic)
```

## 仓库内容

| 文件 | 用途 |
|---|---|
| `DEPLOY.md` | **完整部署手册**，从一台空机器到 sub2api 可对外服务的所有步骤 |
| `mihomo/config.yaml.example` | mihomo 容器的完整配置（路由 / 分组 / fallback / 订阅） |
| `docker-compose.override.yml.example` | mihomo 服务定义 + sub2api 走代理的 env 注入 |
| `smoke.sh` | 部署后端到端冒烟测试，4 步全自动验证 |
| `check-direct.sh` | **部署前**测试服务器能否直连 OpenAI/Anthropic API，决定走完整 / 简化部署 |
| `.env.example` | sub2api 部署脚本生成的环境变量模板（凭证全部用占位符） |
| `mixin.yaml.example` | **v1 遗留**：基于宿主 mihomo 安装的 mixin 配置（v2 已不用，仅供参考） |
| `ai-tools/` | 客户端侧工具：探测 sub2api 网关协议并生成 codex / Claude Code / opencode 配置（详见 [`ai-tools/README.md`](./ai-tools/README.md)） |

## 快速使用

新机器上从零开始，完整流程见 [DEPLOY.md](./DEPLOY.md)。最简版：

```bash
# 1. 装 docker（见 DEPLOY.md 步骤 1）

# 2. clone + 下载官方 compose
git clone https://github.com/Jungle728/clash-sub2api-deploy.git ~/sub2api-deploy
cd ~/sub2api-deploy
curl -sSL https://raw.githubusercontent.com/Wei-Shaw/sub2api/main/deploy/docker-compose.local.yml \
  -o docker-compose.yml

# 3. 测试服务器能不能直连 OpenAI/Anthropic（决定走哪种部署）
bash check-direct.sh
# 全绿 → 可选简化部署（跳过 mihomo + 订阅）
# 失败 → 走完整部署（mihomo + 订阅）

# === 完整部署（推荐）===
# 4. 生成密码 + 配 .env / mihomo / override
PROXY_PASS=$(openssl rand -hex 12)
cp .env.example .env && chmod 600 .env
for k in POSTGRES_PASSWORD JWT_SECRET TOTP_ENCRYPTION_KEY; do
  sed -i "s|^${k}=.*|${k}=$(openssl rand -hex 32)|" .env
done

cp mihomo/config.yaml.example mihomo/config.yaml
cp docker-compose.override.yml.example docker-compose.override.yml
sed -i "s|REPLACE_WITH_PROXY_PASS|$PROXY_PASS|g" mihomo/config.yaml docker-compose.override.yml
# 别忘了把 mihomo/config.yaml 里的订阅 URL 占位符也改了

# 5. 启动 + 验证
docker compose up -d
bash smoke.sh
docker compose logs sub2api 2>&1 | grep -i 'admin password'
```

**新机器迁移（保留旧数据）**：从旧机器 `tar` 备份 `~/sub2api-deploy/` 目录传过去，解压后直接 `docker compose up -d` 即可。详见 DEPLOY.md「迁移到新机器」节。

## 客户端配置（ai-tools）

部署完 sub2api 后，可以用 `ai-tools/gen_configs.py` 一键探测网关支持的协议（OpenAI Chat / Responses / Anthropic Messages）并生成对应客户端配置：

```bash
# 预览到当前目录
python3 ai-tools/gen_configs.py --base-url http://YOUR-HOST:65432 --api-key sk-xxx

# 写入标准路径（~/.codex、~/.claude、~/.config/opencode）
python3 ai-tools/gen_configs.py --base-url http://YOUR-HOST:65432 --api-key sk-xxx --install
```

完整选项见 [`ai-tools/README.md`](./ai-tools/README.md)。

## 关键设计要点

- **AI 流量优先美国，整组失联才 fallback 日本**：基于 mihomo `fallback` proxy-group + 内层 `url-test`，前者负责整组级切换，后者在组内自动选最快节点
- **订阅作为 proxy-provider 自动刷新**：每小时 mihomo 自动重新拉订阅，新增/失效节点自动同步，不需要 cron / clashsub
- **mihomo 也容器化，享受 docker 自动重启**：避免 nohup 模式下挂了无人拉起的问题
- **服务间通过 docker 网络 DNS 通信**：sub2api 用 `mihomo:7890` 而不是 `host.docker.internal:7890`，更稳定
- **Cloudflare 反 IDC 不影响 sub2api**：`claude.ai` / `chatgpt.com` 网页版被 CF 拦 403 是 IDC IP 黑名单，但 `api.openai.com` / `api.anthropic.com` API 端点对 IDC IP 友好。sub2api 调的是 API 不调网页

详细原理、坑点和故障排查，全在 `DEPLOY.md`。

## 已知坑

1. mihomo `fallback` 不支持延迟阈值，只能整组失联触发切换
2. mihomo 改 `config.yaml` 后必须 `docker compose restart mihomo` 才生效（mihomo 不监听文件变化）
3. `allow-lan: true` 在容器内是必需的，配合 `authentication` 仍然安全（端口不暴露宿主）
4. busybox wget 即使设了 `NO_PROXY=localhost` 也会走代理 → docker healthcheck 会假阴性报 unhealthy。需要在 healthcheck 里加 `-Y off` 强制不走代理
5. 订阅"信息节点"会污染 url-test，要用 `exclude-filter` 排除"流量"、"过期"等关键字

## 版本历史

- **v2 (current)**: 全容器化部署，mihomo 改为 docker 服务（[本文档](./DEPLOY.md)）
- **v1 (deprecated)**: 宿主装 mihomo + nohup，sub2api 通过 host.docker.internal 走代理（仅保留 `mixin.yaml.example` 作参考）

## 相关项目

- [mihomo](https://github.com/MetaCubeX/mihomo) — clash 内核
- [sub2api](https://github.com/Wei-Shaw/sub2api) — Plus/Max 账号转 API 网关

## 许可

MIT。仅供个人学习研究使用，请遵守相关服务条款。
