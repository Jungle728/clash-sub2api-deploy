# Repository Guidelines

## 项目结构

本仓库是 sub2api 的部署与运维工具集，不包含上游应用源码。`README.md` 提供架构概览，`DEPLOY.md` 是完整部署手册；根目录的 `*.example` 是可提交模板，实际 `.env`、Compose 和 Caddy 配置均被忽略。`scripts/` 存放镜像更新和 cron 安装脚本，`ai-tools/` 提供 Codex、Claude Code、opencode 配置生成器。当前架构使用 Caddy HTTPS 入口和 sub2api 账户级 HTTP/HTTPS/SOCKS5 代理，不运行 mihomo。

## 常用命令

```bash
bash -n check-direct.sh smoke.sh scripts/*.sh
python3 -m py_compile ai-tools/gen_configs.py
docker compose config
bash smoke.sh
```

`docker compose config` 需要先按 `DEPLOY.md` 准备本地 `docker-compose.yml`、`.env` 和 override。`smoke.sh` 检查容器、健康端点、全局代理残留及账户代理统计；真实代理路径还应通过一次业务请求和 sub2api 日志确认。

## 编码与命名

Python 使用 4 空格、类型提示和 `snake_case`。Shell 使用 Bash，引用变量；修改状态的脚本采用 `set -Eeuo pipefail`。YAML 使用 2 空格。环境变量采用 `UPPER_SNAKE_CASE`，脚本采用 `kebab-case.sh`，运行态模板以 `.example` 结尾。注释应说明运维原因和失败风险。

## 测试与提交

仓库没有单元测试覆盖率门槛。配置变更至少执行 Shell/Python 语法检查和 `docker compose config`；代理、更新或 HTTPS 变更还要运行 `smoke.sh`。提交历史主要采用 `feat:`, `fix:`, `docs:`, `chore:`，标题保持单一目的。PR 应列出影响范围、迁移要求和实际验证结果。

## 安全要求

不得提交 `.env`、实际代理 URL、API Key、OAuth 数据、数据库、证书、日志、备份或生成的客户端配置。只在模板中使用 `example.com` 和 `REPLACE_WITH_*`。提交前检查 `git diff --cached` 并扫描 UUID、令牌、密码和非示例域名；已泄露凭据必须轮换。
