#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sub2api 网关配置生成器

功能:
  1. 探测网关支持的协议: OpenAI Chat / OpenAI Responses / Anthropic Messages
  2. 为以下工具生成可直接使用的配置:
       - codex CLI       -> ~/.codex/config.toml (+ codex.env)
       - Claude Code     -> ~/.claude/settings.json
       - opencode        -> ~/.config/opencode/opencode.json

用法:
  python gen_configs.py                                  # 交互式, 写到当前目录预览
  python gen_configs.py --base-url URL --api-key KEY     # 非交互
  python gen_configs.py --install                        # 直接写入标准配置路径
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests


# ---------- 工具函数 ----------

def normalize_base(url: str) -> str:
    """规范化 base url: 去末尾斜杠, 去末尾的 /v1 (我们内部统一拼)"""
    url = url.strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


def safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


# ---------- 探测 ----------

def probe(base: str, api_key: str) -> dict:
    """探测网关支持哪些协议. base 不含 /v1."""
    result = {
        "openai_chat": False,
        "openai_responses": False,
        "anthropic": False,
        "openai_models": [],
        "anthropic_models": [],
    }

    print(f"\n=== 探测 {base} ===")

    # 1) OpenAI 风格 /v1/models (Bearer)
    try:
        r = requests.get(
            base + "/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        data = safe_json(r) or {}
        if r.ok and isinstance(data.get("data"), list):
            ids = [m.get("id") for m in data["data"] if isinstance(m, dict) and m.get("id")]
            result["openai_models"] = ids
            print(f"  ✔ /v1/models (Bearer) -> {len(ids)} 个模型")
        else:
            print(f"  · /v1/models (Bearer) 状态={r.status_code}")
    except Exception as e:
        print(f"  · /v1/models (Bearer) 异常: {e}")

    # 2) Anthropic 风格 /v1/models (x-api-key)
    try:
        r = requests.get(
            base + "/v1/models",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            timeout=10,
        )
        data = safe_json(r) or {}
        if r.ok and isinstance(data.get("data"), list):
            ids = [m.get("id") for m in data["data"] if isinstance(m, dict) and m.get("id")]
            # 启发式: 名字含 claude 的优先认为是 anthropic 模型
            claude_ids = [m for m in ids if "claude" in m.lower()]
            result["anthropic_models"] = claude_ids or ids
            print(f"  ✔ /v1/models (x-api-key) -> {len(result['anthropic_models'])} 个模型")
        else:
            print(f"  · /v1/models (x-api-key) 状态={r.status_code}")
    except Exception as e:
        print(f"  · /v1/models (x-api-key) 异常: {e}")

    # 3) OpenAI Chat Completions
    chat_model = result["openai_models"][0] if result["openai_models"] else "gpt-4o-mini"
    try:
        r = requests.post(
            base + "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": chat_model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            },
            timeout=20,
        )
        if r.ok:
            result["openai_chat"] = True
            print(f"  ✔ /v1/chat/completions 可用 (model={chat_model})")
        else:
            print(f"  · /v1/chat/completions 状态={r.status_code} body={r.text[:160]}")
    except Exception as e:
        print(f"  · /v1/chat/completions 异常: {e}")

    # 4) OpenAI Responses API
    try:
        r = requests.post(
            base + "/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": chat_model,
                "input": "ping",
                "max_output_tokens": 16,
                "stream": False,
            },
            timeout=20,
        )
        if r.ok:
            result["openai_responses"] = True
            print(f"  ✔ /v1/responses 可用")
        else:
            print(f"  · /v1/responses 状态={r.status_code}")
    except Exception as e:
        print(f"  · /v1/responses 异常: {e}")

    # 5) Anthropic Messages
    a_model = (
        result["anthropic_models"][0]
        if result["anthropic_models"]
        else "claude-3-5-sonnet-20241022"
    )
    try:
        r = requests.post(
            base + "/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": a_model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=20,
        )
        if r.ok:
            result["anthropic"] = True
            print(f"  ✔ /v1/messages 可用 (model={a_model})")
        else:
            print(f"  · /v1/messages 状态={r.status_code} body={r.text[:160]}")
    except Exception as e:
        print(f"  · /v1/messages 异常: {e}")

    return result


# ---------- 生成: codex CLI ----------

def gen_codex(base: str, api_key: str, p: dict, out: Path, install: bool) -> None:
    if not (p["openai_chat"] or p["openai_responses"]):
        print("  ⚠ 跳过 codex: 网关不支持 OpenAI Chat / Responses")
        return

    wire_api = "responses" if p["openai_responses"] else "chat"

    models = p["openai_models"]
    default_model = next(
        (m for m in models if m.lower().startswith(("gpt-5", "gpt-4", "o1", "o3", "o4"))),
        models[0] if models else "gpt-4o",
    )

    env_key_name = "SUB2API_KEY"
    toml = f'''# Codex CLI 配置 (sub2api 网关) — 自动生成
model = "{default_model}"
model_provider = "sub2api"

[model_providers.sub2api]
name = "sub2api"
base_url = "{base}/v1"
env_key = "{env_key_name}"
wire_api = "{wire_api}"

# sub2api 网关默认不支持 image_generation, 开启会触发 403
# 如果你的网关支持其它内置工具但有问题, 可在这里加:
#   computer_use = false
#   browser_use = false
#   in_app_browser = false
[features]
image_generation = false
'''

    target_dir = Path.home() / ".codex" if install else out
    target_dir.mkdir(parents=True, exist_ok=True)

    cfg_path = target_dir / "config.toml"
    if install and cfg_path.exists():
        backup = cfg_path.with_suffix(".toml.bak")
        backup.write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  · 已备份原 config.toml -> {backup.name}")
    cfg_path.write_text(toml, encoding="utf-8")
    cfg_path.chmod(0o600)

    env_path = target_dir / "codex.env"
    env_path.write_text(f'export {env_key_name}="{api_key}"\n', encoding="utf-8")
    env_path.chmod(0o600)

    print(f"  ✔ codex: {cfg_path}")
    print(f"          {env_path}  (使用前 source 一下)")
    print(f"          默认模型 = {default_model}, wire_api = {wire_api}")


# ---------- 生成: Claude Code ----------

def gen_claude_code(base: str, api_key: str, p: dict, out: Path, install: bool) -> None:
    if not p["anthropic"]:
        print("  ⚠ 跳过 Claude Code: 网关不支持 /v1/messages")
        return

    env_block = {
        "ANTHROPIC_BASE_URL": base,
        "ANTHROPIC_AUTH_TOKEN": api_key,
    }

    target_dir = Path.home() / ".claude" if install else out
    target_dir.mkdir(parents=True, exist_ok=True)
    name = "settings.json" if install else "claude_code.settings.json"
    path = target_dir / name

    settings: dict[str, Any] = {"env": env_block}
    if install and path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                existing.setdefault("env", {}).update(env_block)
                settings = existing
        except Exception as e:
            print(f"  ⚠ 已有 settings.json 解析失败 ({e}), 将覆盖")

    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(f"  ✔ Claude Code: {path}")
    if not install:
        print(f"          → 把 env 段合并进 ~/.claude/settings.json, 或直接 export 两个变量")


# ---------- 生成: opencode ----------

def _build_opencode_providers(
    base: str,
    api_key: str,
    p: dict,
    name_anthropic: str = "sub2api-anthropic",
    name_openai: str = "sub2api-openai",
) -> dict[str, Any]:
    providers: dict[str, Any] = {}
    if p["openai_chat"] or p["openai_responses"]:
        ids = p["openai_models"][:50] or ["gpt-4o"]
        providers[name_openai] = {
            "npm": "@ai-sdk/openai-compatible",
            "name": "sub2api (OpenAI)",
            "options": {
                "baseURL": base + "/v1",
                "headers": {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            },
            "models": {m: {"name": m} for m in ids},
        }

    if p["anthropic"]:
        ids = p["anthropic_models"][:50] or ["claude-3-5-sonnet-20241022"]
        providers[name_anthropic] = {
            "npm": "@ai-sdk/anthropic",
            "name": "sub2api (Anthropic)",
            "options": {
                "baseURL": base + "/v1",
                "headers": {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            },
            "models": {m: {"name": m} for m in ids},
        }
    return providers


def gen_opencode(
    base: str,
    api_key: str,
    p: dict,
    out: Path,
    install: bool,
    name_anthropic: str = "sub2api-anthropic",
    name_openai: str = "sub2api-openai",
    skip_openai: bool = False,
    skip_anthropic: bool = False,
) -> None:
    providers = _build_opencode_providers(
        base, api_key, p, name_anthropic=name_anthropic, name_openai=name_openai
    )
    if skip_openai:
        providers.pop(name_openai, None)
    if skip_anthropic:
        providers.pop(name_anthropic, None)
    if not providers:
        print("  ⚠ 跳过 opencode: 没有可用 provider")
        return

    if install:
        # 安装模式: 优先合并到已有的 opencode.jsonc 或 opencode.json
        cfg_dir = Path.home() / ".config" / "opencode"
        cfg_dir.mkdir(parents=True, exist_ok=True)

        existing_jsonc = cfg_dir / "opencode.jsonc"
        existing_json = cfg_dir / "opencode.json"

        if existing_jsonc.exists():
            _merge_opencode_jsonc(existing_jsonc, providers)
        elif existing_json.exists():
            _merge_opencode_json(existing_json, providers)
        else:
            existing_json.write_text(
                json.dumps(
                    {"$schema": "https://opencode.ai/config.json", "provider": providers},
                    indent=2,
                    ensure_ascii=False,
                ) + "\n",
                encoding="utf-8",
            )
            existing_json.chmod(0o600)
            print(f"  ✔ opencode: {existing_json} (新建)")
    else:
        # 预览模式: 单独输出一份 opencode.json
        out.mkdir(parents=True, exist_ok=True)
        path = out / "opencode.json"
        path.write_text(
            json.dumps(
                {"$schema": "https://opencode.ai/config.json", "provider": providers},
                indent=2,
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        print(f"  ✔ opencode: {path}")


def _extract_existing_baseurls(text: str) -> set[str]:
    """从 jsonc 文本里粗略提取所有 \"baseURL\": \"...\" 的值, 用于去重."""
    import re
    return set(re.findall(r'"baseURL"\s*:\s*"([^"]+)"', text))


def _filter_dup_providers(
    providers: dict[str, Any], existing_baseurls: set[str]
) -> tuple[dict[str, Any], list[str]]:
    keep, skipped = {}, []
    for k, v in providers.items():
        url = v.get("options", {}).get("baseURL")
        if url and url in existing_baseurls:
            skipped.append(f"{k} (baseURL 已存在)")
        else:
            keep[k] = v
    return keep, skipped


def _merge_opencode_json(path: Path, providers: dict[str, Any]) -> None:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ⚠ 解析 {path} 失败 ({e}), 跳过")
        return
    if not isinstance(existing, dict):
        print(f"  ⚠ {path} 顶层不是对象, 跳过")
        return
    existing.setdefault("provider", {})
    existing_urls = {
        (v or {}).get("options", {}).get("baseURL")
        for v in existing["provider"].values()
    }
    existing_urls.discard(None)
    providers, skipped = _filter_dup_providers(providers, existing_urls)
    for s in skipped:
        print(f"  ↷ 跳过 {s}")
    if not providers:
        print(f"  · {path} 无新 provider 需要写入")
        return
    added, replaced = [], []
    for k, v in providers.items():
        (replaced if k in existing["provider"] else added).append(k)
        existing["provider"][k] = v
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(f"  ✔ opencode: {path}")
    if added:
        print(f"          新增 provider: {', '.join(added)}")
    if replaced:
        print(f"          覆盖 provider: {', '.join(replaced)}")


def _merge_opencode_jsonc(path: Path, providers: dict[str, Any]) -> None:
    """保留注释地把 provider 追加进 jsonc. 用文本插入, 不解析整个 jsonc."""
    text = path.read_text(encoding="utf-8")

    # baseURL 去重
    existing_urls = _extract_existing_baseurls(text)
    providers, skipped = _filter_dup_providers(providers, existing_urls)
    for s in skipped:
        print(f"  ↷ 跳过 {s}")
    if not providers:
        print(f"  · {path} 无新 provider 需要追加")
        return

    # 备份
    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_text(text, encoding="utf-8")

    # 用最稳的策略: 找最后一个 "}" 之前的位置插入
    # 但更精确: 找 "provider" 对象的 } 配对
    # 简化做法: 假设结构是规范的, 在文件最后一个 "}" 之前找到倒数第二个 "}" (即 provider 块结束)
    closing_provider = _find_provider_close(text)
    if closing_provider is None:
        print(f"  ⚠ 无法解析 {path} 结构 (找不到 provider 块), 跳过 (已备份到 {backup.name})")
        return

    # 渲染新 provider 片段
    snippet_parts = []
    for k, v in providers.items():
        body = json.dumps(v, indent=2, ensure_ascii=False)
        # 把整块缩进 4 个空格 (provider 对象内层)
        indented = "\n".join("    " + line if line else line for line in body.splitlines())
        snippet_parts.append(f'    "{k}": ' + indented.lstrip())

    snippet = ",\n".join(snippet_parts)

    # 在 closing_provider 之前插入 (要确保前面有逗号)
    before = text[:closing_provider].rstrip()
    after = text[closing_provider:]

    # before 末尾如果不是 { 或 , 就加逗号
    needs_comma = before and before[-1] not in ("{", ",")
    insertion = (",\n" if needs_comma else "\n") + snippet + "\n  "

    new_text = before + insertion + after
    path.write_text(new_text, encoding="utf-8")
    path.chmod(0o600)
    print(f"  ✔ opencode: {path}  (已备份原文件到 {backup.name})")
    print(f"          追加 provider: {', '.join(providers.keys())}")


def _find_provider_close(text: str) -> int | None:
    """找到 \"provider\": { ... } 这个对象的右括号 } 的位置 (字符下标)."""
    import re
    m = re.search(r'"provider"\s*:\s*\{', text)
    if not m:
        return None
    i = m.end()  # 指向 { 之后
    depth = 1
    in_str = False
    esc = False
    while i < len(text):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
            elif c == "/" and i + 1 < len(text) and text[i + 1] == "/":
                # 跳过行注释
                nl = text.find("\n", i)
                if nl == -1:
                    return None
                i = nl
        i += 1
    return None


# ---------- 主流程 ----------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="sub2api 网关 -> codex / Claude Code / opencode 配置生成器",
    )
    ap.add_argument("--base-url", help="API Base URL (例: https://gw.example.com 或带 /v1 都行)")
    ap.add_argument("--api-key", help="API Key")
    ap.add_argument(
        "--install",
        action="store_true",
        help="直接写入标准路径 (~/.codex, ~/.claude, ~/.config/opencode)",
    )
    ap.add_argument("--out-dir", default=".", help="预览模式输出目录 (默认当前目录)")
    ap.add_argument(
        "--tools",
        default="all",
        help="逗号分隔, 指定要生成哪些工具的配置: codex, claude-code, opencode, all (默认 all)",
    )
    ap.add_argument(
        "--opencode-anthropic-name",
        default="sub2api-anthropic",
        help="opencode 中 Anthropic provider 的 key 名 (默认 sub2api-anthropic)",
    )
    ap.add_argument(
        "--opencode-openai-name",
        default="sub2api-openai",
        help="opencode 中 OpenAI-compatible provider 的 key 名 (默认 sub2api-openai)",
    )
    ap.add_argument(
        "--opencode-only",
        choices=["anthropic", "openai", "both"],
        default="both",
        help="opencode 只生成某一种 provider (默认 both)",
    )
    args = ap.parse_args()

    # 解析 --tools
    valid_tools = {"codex", "claude-code", "opencode"}
    raw = [t.strip().lower() for t in args.tools.split(",") if t.strip()]
    if not raw or "all" in raw:
        selected = valid_tools
    else:
        selected = set(raw)
        unknown = selected - valid_tools
        if unknown:
            print(f"❌ 未知工具: {', '.join(sorted(unknown))}")
            print(f"   可选: {', '.join(sorted(valid_tools))}, all")
            return 1

    base_url = args.base_url or input("API Base URL: ").strip()
    api_key = args.api_key or input("API Key: ").strip()
    if not base_url or not api_key:
        print("❌ base_url 和 api_key 不能为空")
        return 1

    base = normalize_base(base_url)
    out = Path(args.out_dir).resolve()

    p = probe(base, api_key)

    if not (p["openai_chat"] or p["openai_responses"] or p["anthropic"]):
        print("\n❌ 没有发现任何可用端点, 中止")
        return 2

    print(f"\n=== 生成配置 ({'安装' if args.install else '预览到 ' + str(out)}) ===")
    print(f"目标工具: {', '.join(sorted(selected))}")
    if "codex" in selected:
        gen_codex(base, api_key, p, out, args.install)
    if "claude-code" in selected:
        gen_claude_code(base, api_key, p, out, args.install)
    if "opencode" in selected:
        gen_opencode(
            base,
            api_key,
            p,
            out,
            args.install,
            name_anthropic=args.opencode_anthropic_name,
            name_openai=args.opencode_openai_name,
            skip_openai=(args.opencode_only == "anthropic"),
            skip_anthropic=(args.opencode_only == "openai"),
        )

    print("\n=== 完成 ===")
    if not args.install:
        print("确认无误后, 重新运行加 --install 即可写入标准路径")
    return 0


if __name__ == "__main__":
    sys.exit(main())
