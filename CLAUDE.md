# CLAUDE.md

本仓库用于部署和运维 sub2api。通用贡献规则见 `AGENTS.md`，完整操作见 `DEPLOY.md`。

## 当前架构

客户端通过 Caddy HTTPS 访问 `sub2api:8080`。sub2api 按平台、分组和状态选择账号，再读取账号的 `proxy_id`：已绑定账号通过远端 HTTP/HTTPS/SOCKS5 代理访问上游，未绑定账号按策略直连。不要重新引入 mihomo 或容器级 `HTTP_PROXY` / `HTTPS_PROXY`。

## 验证

```bash
bash -n check-direct.sh smoke.sh scripts/*.sh
python3 -m py_compile ai-tools/gen_configs.py
docker compose config
bash smoke.sh
```

业务链路变更必须额外产生一次真实请求，结合 sub2api 日志、账号 `proxy_id` 和容器连接验证。`healthy` 只表示本地健康端点正常。

## 敏感数据

不要读取、打印或提交 `.env`、代理凭据、OAuth 数据、数据库内容、证书、日志或备份。修改 `.example` 模板时只使用 `example.com` 和 `REPLACE_WITH_*`。实际 `docker-compose.yml`、override、Caddyfile 和数据目录都属于运行态文件。
