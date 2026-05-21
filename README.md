# clash-sub2api-deploy

在一台 Linux 服务器上用 **mihomo (clash)** 提供出海代理，再用 **sub2api** 把自己的 ChatGPT Plus / Claude Max 账号转成统一 OpenAI / Anthropic 兼容的 API 接口。

适合场景：

- 自己有 ChatGPT Plus / Claude Pro/Max 账号，想用编程 SDK / Cline / Cursor 等以 API 方式调用
- 服务器在境内或在被 OpenAI/Anthropic 拒绝的 IDC 区域，需要先经过代理出海
- 想要 AI 流量优先走美国节点、其他流量走日本节点；美国不可用时自动 fallback

## 架构

```
客户端 → :8080 (sub2api 容器)
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
                 ├─ 1st: 🇺🇸 美国节点 (url-test, 多订阅合并)
                 ├─ 2nd: 🇯🇵 日本节点 (url-test, 多订阅合并)
                 └─ 3rd: DIRECT (兜底)
                 ▼
              真实出站节点 → OpenAI / Anthropic
```

## 仓库内容

| 文件 | 用途 |
|---|---|
| `DEPLOY.md` | **完整部署手册**，从一台空机器到 sub2api 可对外服务的所有步骤 |
| `mixin.yaml.example` | 路由策略 / 双订阅合并 / fallback 的 clash mixin 配置模板 |
| `docker-compose.override.yml.example` | 让 sub2api 容器走宿主 clash 代理的 compose override |
| `smoke.sh` | 部署后端到端冒烟测试，5 步全自动验证 |
| `.env.example` | sub2api 部署脚本生成的环境变量模板（凭证全部用占位符） |

## 快速使用

新机器上想复刻这套环境：

1. 完整阅读 [DEPLOY.md](./DEPLOY.md)
2. 把 `mixin.yaml.example` 中所有 `REPLACE_WITH_*` / `YOUR-*` 改成实际值（订阅 URL、自己生成的代理认证密码）
3. 把 `docker-compose.override.yml.example` 中 `REPLACE_WITH_PROXY_PASS` 改为同一密码
4. 按 DEPLOY.md 步骤 1-6 操作
5. 跑 `bash smoke.sh`，全绿即完成

## 关键设计要点

- **AI 流量优先美国，整组失联才 fallback 日本**：基于 mihomo `fallback` proxy-group + 内层 `url-test`，前者负责整组级切换，后者在组内自动选最快节点
- **两订阅合并**：订阅 1 通过 `clashsub` 管理（决定基础 config），订阅 2 通过 `proxy-providers` 注入（每小时自动刷新）；JP/US 分组同时从两边按 emoji + 关键字 filter 拉节点
- **sub2api 容器走宿主代理**：`extra_hosts: host.docker.internal:host-gateway` + `HTTP_PROXY` env，注意 mihomo 必须 `allow-lan: true` 并配 `authentication`
- **Cloudflare 反 IDC 不影响 sub2api**：`claude.ai` / `chatgpt.com` 网页版被 CF 拦 403 是 IDC IP 黑名单，但 `api.openai.com` / `api.anthropic.com` API 端点对 IDC IP 友好。sub2api 调的是 API 不调网页

详细原理、坑点和故障排查，全在 `DEPLOY.md`。

## 已知坑

1. mihomo `fallback` 不支持延迟阈值，只能整组失联触发切换
2. mixin 同 key 出现两次会被空值覆盖（YAML 行为），改 mixin 用 `edit` 不要 `write` 整体重写
3. `allow-lan: true` 务必同时配 `authentication`，否则 7890 暴露公网无密码
4. install.sh 末尾 `exec $SHELL` 在脚本中会挂住，包一层 `timeout 180 bash install.sh`
5. 订阅自带的"流量信息节点"会污染 url-test，需要用 filter 排除

## 相关项目

- [mihomo](https://github.com/MetaCubeX/mihomo) — clash 内核
- [clash-for-linux-install](https://github.com/nelvko/clash-for-linux-install) — 一键 mihomo 安装脚本
- [sub2api](https://github.com/Wei-Shaw/sub2api) — Plus/Max 账号转 API 网关

## 许可

MIT。仅供个人学习研究使用，请遵守相关服务条款。
