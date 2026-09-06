#!/usr/bin/env python3
"""通过 OpenAI Images API（ChatGPT 图片通道）批量生成小绿书竖版信息图。

与 gemini_generate_images_browser.py 的关系：
  - 产物一致：`infographic.png` / `chart_N.png`，3:4 竖版
  - 输入一致：项目子目录下的 charts_config JSON
  - 通道不同：本脚本直接调 OpenAI Images API（gpt-image-1.5），
    不需要 Chrome CDP、不需要 Google 登录态，可并行/后台运行。

配置文件格式（兼容 gemini_charts_config.json 的数组格式）：

    [
      {"prompt": "...", "filename": "infographic.png", "model": "gpt-image-1.5"},
      {"prompt": "...", "filename": "chart_1.png"}
    ]

或带 style 字段的对象格式：

    {"style": "blackboard-gold", "charts": [{"prompt": "...", "filename": "..."}]}

常用命令：

    # 批量生成
    python3 openai_generate_images.py generate --config <dir>/openai_charts_config.json --output-dir <dir>

    # 只重新生成缺失/指定的图（逗号分隔文件名）
    python3 openai_generate_images.py generate --config <dir>/openai_charts_config.json --output-dir <dir> --only chart_3.png,chart_5.png

    # 跳过已存在的文件
    python3 openai_generate_images.py generate --config <dir>/openai_charts_config.json --output-dir <dir> --skip-existing

网络说明：api.openai.com 在部分网络下会被 DNS 污染拦截。脚本会依次尝试
「环境 HTTPS_PROXY → 系统代理（macOS scutil）→ 直连」，自动选通的一条。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 3:4 竖版；gpt-image-1 / 1.5 支持的尺寸之一
DEFAULT_SIZE = "1024x1536"
DEFAULT_MODEL = "gpt-image-1.5"
DEFAULT_QUALITY = "high"
API_PATH = "/images/generations"
TIMEOUT_S = 600


# --------------------------------------------------------------------------- #
# 网络出口探测
# --------------------------------------------------------------------------- #
def _system_proxy() -> str | None:
    """读取 macOS 系统代理（scutil --proxy），返回 http://host:port 或 None。"""
    try:
        out = subprocess.run(
            ["scutil", "--proxy"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return None
    enabled = re.search(r"HTTPSEnable\s*:\s*(\d)", out)
    host = re.search(r"HTTPSProxy\s*:\s*(\S+)", out)
    port = re.search(r"HTTPSPort\s*:\s*(\d+)", out)
    if enabled and enabled.group(1) == "1" and host and port:
        return f"http://{host.group(1)}:{port.group(1)}"
    return None


def candidate_proxies(explicit: str | None) -> list[str | None]:
    """返回按优先级排列的代理候选；None 表示直连（始终放最后作为兜底）。"""
    seen: list[str | None] = []
    for p in (explicit, os.environ.get("HTTPS_PROXY"), os.environ.get("https_proxy"),
              _system_proxy()):
        if p and p not in seen:
            seen.append(p)
    seen.append(None)
    return seen


def _probe(proxy: str | None) -> bool:
    """用一次轻量请求验证该出口能否连通 OpenAI。"""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": proxy, "http": proxy}) if proxy
        else urllib.request.ProxyHandler({})
    )
    req = urllib.request.Request(
        f"{api_base()}/models",
        headers={"Authorization": f"Bearer {api_key()}"},
        method="GET",
    )
    try:
        with opener.open(req, timeout=25) as resp:
            return resp.status == 200
    except Exception:
        return False


def resolve_proxy(explicit: str | None) -> str | None:
    """挑选第一个可用出口；全部不可用时返回 None（让调用方报明确错误）。"""
    for p in candidate_proxies(explicit):
        label = p or "直连"
        print(f"   · 探测出口 {label} ...", end=" ", flush=True)
        if _probe(p):
            print("可用")
            return p
        print("不可达")
    return None


# --------------------------------------------------------------------------- #
# API 基础
# --------------------------------------------------------------------------- #
def api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("缺少环境变量 OPENAI_API_KEY")
    return key


def api_base() -> str:
    base = (
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_BASE")
        or "https://api.openai.com"
    ).rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def generate_one(
    prompt: str,
    out_path: Path,
    proxy: str | None,
    model: str = DEFAULT_MODEL,
    size: str = DEFAULT_SIZE,
    quality: str = DEFAULT_QUALITY,
    retries: int = 2,
) -> bool:
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "n": 1,
    }
    body = json.dumps(payload).encode("utf-8")
    url = f"{api_base()}{API_PATH}"

    for attempt in range(1, retries + 1):
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy, "http": proxy}) if proxy
            else urllib.request.ProxyHandler({})
        )
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with opener.open(req, timeout=TIMEOUT_S) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode("utf-8", errors="replace")
            print(f"   ✗ HTTP {e.code}: {detail}")
            if attempt < retries and e.code in (429, 500, 502, 503, 504):
                wait = 20 * attempt
                print(f"   ↻ {wait}s 后重试（{attempt}/{retries}）")
                time.sleep(wait)
                continue
            return False
        except Exception as e:
            print(f"   ✗ 请求失败: {e}")
            if attempt < retries:
                time.sleep(15 * attempt)
                continue
            return False

        try:
            data = json.loads(raw)
            item = data["data"][0]
        except Exception:
            print(f"   ✗ 响应解析失败: {raw[:300]!r}")
            return False

        if item.get("b64_json"):
            blob = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            dl = urllib.request.Request(item["url"])
            dl_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"https": proxy, "http": proxy}) if proxy
                else urllib.request.ProxyHandler({})
            )
            with dl_opener.open(dl, timeout=180) as r:
                blob = r.read()
        else:
            print("   ✗ 响应中无图片数据")
            return False

        out_path.write_bytes(blob)
        print(f"   ✓ {out_path.name}  {len(blob) / 1024:.0f} KB")
        return True

    return False


# --------------------------------------------------------------------------- #
# 配置加载
# --------------------------------------------------------------------------- #
def load_charts(config_path: Path) -> list[dict]:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        style = raw.get("style")
        charts = raw.get("charts", [])
        for c in charts:
            c.setdefault("style", style)
        return charts
    return raw


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="OpenAI Images API 批量生成信息图")
    ap.add_argument("command", choices=["generate"])
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--output-dir", required=True, type=Path)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--size", default=DEFAULT_SIZE)
    ap.add_argument("--quality", default=DEFAULT_QUALITY,
                    choices=["low", "medium", "high", "auto"])
    ap.add_argument("--proxy", default=None,
                    help="显式指定出口代理，如 http://127.0.0.1:7897；默认自动探测")
    ap.add_argument("--only", default=None,
                    help="只生成指定文件，逗号分隔，如 chart_3.png,chart_5.png")
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--retries", type=int, default=2)
    args = ap.parse_args(argv)

    charts = load_charts(args.config)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    only = {s.strip() for s in args.only.split(",")} if args.only else None
    todo = [
        c for c in charts
        if (only is None or c["filename"] in only)
        and not (args.skip_existing and (args.output_dir / c["filename"]).exists())
    ]
    if not todo:
        print("没有需要生成的图片（全部已存在或被 --only 过滤）")
        return 0

    print(f"通道: OpenAI Images API · 模型 {args.model} · {args.size} · quality={args.quality}")
    proxy = resolve_proxy(args.proxy)
    print(f"出口: {proxy or '直连'}\n")

    ok, failed = 0, []
    for i, chart in enumerate(todo, 1):
        name = chart["filename"]
        print(f"[{i}/{len(todo)}] {name}")
        if generate_one(
            chart["prompt"],
            args.output_dir / name,
            proxy,
            model=chart.get("model", args.model),
            size=chart.get("size", args.size),
            quality=chart.get("quality", args.quality),
            retries=args.retries,
        ):
            ok += 1
        else:
            failed.append(name)

    print(f"\n完成 {ok}/{len(todo)}")
    if failed:
        print("失败: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
