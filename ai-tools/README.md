# ai-tools

把 [sub2api](https://github.com/Wei-Shaw/sub2api) 类网关一键配置到三个主流 AI 编码 CLI:

- [codex CLI](https://github.com/openai/codex)
- [Claude Code](https://docs.anthropic.com/claude-code)
- [opencode](https://opencode.ai)

## 它做什么

1. 给定 base URL + API key,**真实地** ping 网关的:
   - `GET /v1/models` (Bearer + x-api-key 两种鉴权)
   - `POST /v1/chat/completions`
   - `POST /v1/responses`
   - `POST /v1/messages`
2. 根据探测结果按"协议 -> 工具"的映射生成各自的标准配置:
   - Anthropic Messages 协议 -> Claude Code (`~/.claude/settings.json`)、opencode anthropic provider
   - OpenAI Chat/Responses 协议 -> codex (`~/.codex/config.toml`)、opencode openai-compatible provider
3. opencode 配置默认**合并到已有 `opencode.jsonc` / `opencode.json`**,保留注释,且按 baseURL 去重不会重复追加。

## 用法

```bash
# 1) 预览到当前目录, 看一眼生成内容
python3 gen_configs.py --base-url https://your-gw.example.com --api-key sk-xxx

# 2) 确认无误后写入标准路径
python3 gen_configs.py --base-url https://your-gw.example.com --api-key sk-xxx --install

# 3) 只生成某些工具
python3 gen_configs.py --tools opencode --install
python3 gen_configs.py --tools codex,claude-code --install

# 4) 自定义 opencode provider 名 / 只要 anthropic
python3 gen_configs.py --tools opencode \
  --opencode-only anthropic \
  --opencode-anthropic-name claude-sub2api \
  --install
```

## 选项一览

| 选项 | 说明 |
|---|---|
| `--base-url URL` | 网关地址, 带不带 `/v1` 都行 |
| `--api-key KEY` | API Key |
| `--install` | 写入标准路径, 否则只预览到 `--out-dir` |
| `--out-dir DIR` | 预览模式输出目录 (默认 `.`) |
| `--tools T1,T2` | `codex` / `claude-code` / `opencode` / `all` |
| `--opencode-only` | `anthropic` / `openai` / `both` (默认 both) |
| `--opencode-anthropic-name` | opencode 中 Anthropic provider 的 key 名 |
| `--opencode-openai-name` | opencode 中 OpenAI-compatible provider 的 key 名 |

## 文件输出位置 (`--install` 模式)

| 工具 | 路径 |
|---|---|
| codex | `~/.codex/config.toml` + `~/.codex/codex.env` (key 通过 env 注入) |
| Claude Code | `~/.claude/settings.json` (合并 `env` 段) |
| opencode | `~/.config/opencode/opencode.jsonc` 优先,不存在则用 `opencode.json` |

## 依赖

- Python 3.10+
- `requests`
