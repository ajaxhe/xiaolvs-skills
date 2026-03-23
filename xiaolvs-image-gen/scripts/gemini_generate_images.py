#!/usr/bin/env python3
"""
Generate images via Gemini web app (using gemini-webapi).

This script uses your Gemini Pro subscription quota through the web interface,
NOT the Google Cloud API, so no extra token costs.

Authentication: Automatically reads cookies from NotebookLM's Playwright
storage (~/.notebooklm/storage_state.json). No manual cookie copying needed.

Usage:
    # Check authentication (reads from NotebookLM storage automatically)
    python gemini_generate_images.py auth

    # Generate images from config
    python gemini_generate_images.py generate --config <JSON> --output-dir <DIR>

    # Generate single image from prompt file
    python gemini_generate_images.py generate --prompt-file <TXT> --output-dir <DIR> --filename chart.png

Config JSON format:
[
    {
        "prompt": "Generate an image: ...",
        "filename": "chart_1.png",
        "model": "pro"
    },
    ...
]
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Default NotebookLM storage path
DEFAULT_STORAGE_PATH = Path.home() / ".notebooklm" / "storage_state.json"
# Fallback: manual cookie file
COOKIE_FILE = os.path.join(os.path.dirname(__file__), ".gemini_cookies.json")


def load_cookies_from_storage(storage_path: Path | None = None) -> dict | None:
    """Load Gemini cookies from NotebookLM's Playwright storage state.

    Extracts __Secure-1PSID and __Secure-1PSIDTS from the shared Google
    login session that NotebookLM already maintains.
    """
    path = storage_path or DEFAULT_STORAGE_PATH
    if not path.exists():
        return None

    try:
        with open(path, "r") as f:
            state = json.load(f)

        cookies = {}
        for c in state.get("cookies", []):
            if c.get("domain") == ".google.com":
                if c["name"] in ("__Secure-1PSID", "__Secure-1PSIDTS"):
                    cookies[c["name"]] = c["value"]

        if "__Secure-1PSID" in cookies:
            return cookies
        return None
    except (json.JSONDecodeError, KeyError):
        return None


def load_cookies(storage_path: Path | None = None) -> dict | None:
    """Load cookies with fallback chain:
    1. NotebookLM Playwright storage (automatic, no manual steps)
    2. Manual cookie file (.gemini_cookies.json)
    """
    # Try NotebookLM storage first
    cookies = load_cookies_from_storage(storage_path)
    if cookies:
        print(f"🔑 从 NotebookLM storage 自动加载认证 ({storage_path or DEFAULT_STORAGE_PATH})")
        return cookies

    # Fallback to manual cookie file
    if os.path.exists(COOKIE_FILE):
        print(f"🔑 从手动配置加载认证 ({COOKIE_FILE})")
        with open(COOKIE_FILE, "r") as f:
            return json.load(f)

    return None


def save_cookies(cookies: dict):
    """Save cookies to manual cookie file (fallback method)."""
    with open(COOKIE_FILE, "w") as f:
        json.dump(cookies, f, indent=2)
    print(f"✅ Cookies 已保存到 {COOKIE_FILE}")


def _patch_gemini_locale():
    """Patch gemini-webapi to force zh-CN locale.

    Without this patch, Gemini uses the exit IP's geolocation to determine
    the UI language. A Japanese exit IP causes Japanese locale, which leads
    to garbled Chinese text in generated images.

    Two patches applied:
    1. Endpoint.INIT += '?hl=zh-CN' — forces Chinese UI during init
    2. Headers.GEMINI += Accept-Language: zh-CN — tells Google we want Chinese
    """
    from gemini_webapi import constants as _c

    # Patch INIT endpoint to include hl=zh-CN (StrEnum members are immutable,
    # so we patch the internal _value_)
    init_val = _c.Endpoint.INIT._value_
    if "hl=" not in init_val:
        _c.Endpoint.INIT._value_ = init_val + "?hl=zh-CN"
        print("🌐 已 patch Endpoint.INIT → 强制 zh-CN")

    # Patch GENERATE endpoint
    gen_val = _c.Endpoint.GENERATE._value_
    if "hl=" not in gen_val:
        sep = "&" if "?" in gen_val else "?"
        _c.Endpoint.GENERATE._value_ = gen_val + sep + "hl=zh-CN"
        print("🌐 已 patch Endpoint.GENERATE → 强制 zh-CN")

    # Patch BATCH_EXEC endpoint
    batch_val = _c.Endpoint.BATCH_EXEC._value_
    if "hl=" not in batch_val:
        sep = "&" if "?" in batch_val else "?"
        _c.Endpoint.BATCH_EXEC._value_ = batch_val + sep + "hl=zh-CN"
        print("🌐 已 patch Endpoint.BATCH_EXEC → 强制 zh-CN")

    # Patch default headers to include Accept-Language
    headers = _c.Headers.GEMINI.value
    if "Accept-Language" not in headers:
        headers["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8"
        print("🌐 已 patch Headers → Accept-Language: zh-CN")


_nano_banana_patched = False


def _patch_nano_banana_pro():
    """Patch gemini-webapi to activate Nano Banana Pro + temporary chat mode.

    Reverse-engineered from Gemini web app (PR #209, PR #215):
    - inner_req_list[49] = 14   → activates the "Create Images 🍌" tool
    - inner_req_list[17] = [[1]] → sets Pro quality mode for image generation
    - inner_req_list[45] = 1    → enables temporary chat (临时对话) mode,
                                   preventing conversations from polluting
                                   the Gemini web UI chat history

    Implementation: We monkey-patch the module-level `json` reference in
    gemini_webapi.client (which is actually `orjson`) with a proxy that
    intercepts `dumps()` calls to inject our parameters into inner_req_list.
    """
    global _nano_banana_patched
    if _nano_banana_patched:
        return

    import gemini_webapi.client as _client_mod

    real_json = _client_mod.json  # This is actually `orjson`
    real_dumps = real_json.dumps

    def _patched_dumps(obj, *a, **kw):
        if isinstance(obj, list) and len(obj) >= 69:
            # This is inner_req_list — inject our params
            patched_any = False
            if len(obj) > 49 and obj[49] is None:
                obj[49] = 14  # Enable "Create Images 🍌" tool
                patched_any = True
            if len(obj) > 17 and obj[17] is None:
                obj[17] = [[1]]  # Pro quality mode
                patched_any = True
            if len(obj) > 45 and obj[45] is None:
                obj[45] = 1  # Temporary chat (临时对话) — not saved to history
                patched_any = True
            if patched_any:
                print(f"  🍌 [PATCH] inner_req_list injected: idx49={obj[49]}, idx17={obj[17]}, idx45={obj[45]}")
        return real_dumps(obj, *a, **kw)

    # Create a simple proxy object that delegates everything to orjson
    # but intercepts dumps()
    class _JsonProxy:
        """Transparent proxy for orjson that intercepts dumps()."""
        dumps = staticmethod(_patched_dumps)

        def __getattr__(self, name):
            return getattr(real_json, name)

    _client_mod.json = _JsonProxy()
    _nano_banana_patched = True
    print("🍌 已 patch json.dumps → 启用 Nano Banana Pro (idx49=14, idx17=[[1]]) + 临时对话模式 (idx45=1)")


def _patch_retry_patience():
    """Increase retry patience for busy Pro model.

    The default DELAY_FACTOR=5 with retry=5 means the library retries at
    5s, 10s, 15s, 20s, 25s intervals (total ~75s). For busy Pro model,
    we increase DELAY_FACTOR to 15 for intervals of 15s, 30s, 45s, 60s, 75s
    (total ~225s), and increase retry count from 5 to 8.
    """
    from gemini_webapi.utils import decorators as _dec_mod
    if _dec_mod.DELAY_FACTOR < 15:
        _dec_mod.DELAY_FACTOR = 15
        print("⏱️ 已 patch DELAY_FACTOR → 15（增加重试等待间隔）")


def _create_client(cookies: dict):
    """Create a GeminiClient from a cookies dict."""
    _patch_gemini_locale()
    _patch_nano_banana_pro()
    _patch_retry_patience()
    from gemini_webapi import GeminiClient
    # Auto-detect proxy from environment or macOS system preferences
    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
    if not proxy:
        # Try reading macOS system HTTP/HTTPS proxy (more compatible than SOCKS5)
        import subprocess
        for cmd in ["getsecurewebproxy", "getwebproxy"]:
            try:
                result = subprocess.run(
                    ["networksetup", f"-{cmd}", "Wi-Fi"],
                    capture_output=True, text=True, timeout=5
                )
                enabled = False
                server = port = ""
                for line in result.stdout.splitlines():
                    if line.startswith("Enabled: Yes"):
                        enabled = True
                    elif line.startswith("Server:"):
                        server = line.split(":", 1)[1].strip()
                    elif line.startswith("Port:"):
                        port = line.split(":", 1)[1].strip()
                if enabled and server and port:
                    proxy = f"http://{server}:{port}"
                    break
            except Exception:
                pass
    if proxy:
        print(f"🌐 使用代理: {proxy}")
    return GeminiClient(
        secure_1psid=cookies.get("__Secure-1PSID", ""),
        secure_1psidts=cookies.get("__Secure-1PSIDTS", ""),
        proxy=proxy,
    )


async def test_auth(cookies: dict) -> bool:
    """Test if cookies are valid by making a simple request."""
    try:
        client = _create_client(cookies)
        await client.init(auto_close=False, close_delay=0, timeout=600, watchdog_timeout=120)
        response = await client.generate_content("Hello, respond with just 'OK'")
        text = response.candidates[0].text if response.candidates else ""
        await client.close()
        if text:
            print(f"✅ 认证有效，Gemini 回复: {text[:50]}")
            return True
        else:
            print("❌ 认证失败：无回复内容")
            return False
    except Exception as e:
        print(f"❌ 认证失败: {e}")
        return False


async def generate_images(
    cookies: dict,
    charts: list[dict],
    output_dir: str,
    delay_between: int = 10,
    max_retries: int = 3,
):
    """Generate images from a list of chart definitions.

    Args:
        cookies: Gemini web app cookies.
        charts: List of dicts with 'prompt' and 'filename' keys.
        output_dir: Directory to save downloaded PNG files.
        delay_between: Seconds to wait between requests.
        max_retries: Max retry attempts per chart.
    """
    from gemini_webapi import GeminiClient

    os.makedirs(output_dir, exist_ok=True)
    total = len(charts)

    client = _create_client(cookies)
    # Use longer timeouts to handle busy Pro model (thinking/queueing cycles)
    await client.init(auto_close=False, close_delay=0, timeout=600, watchdog_timeout=120)

    print(f"=== 开始生成 {total} 张图片 ===\n")
    results = []

    for i, chart in enumerate(charts):
        prompt = chart["prompt"]
        filename = chart["filename"]
        output_path = os.path.join(output_dir, filename)

        print(f"[{i+1}/{total}] 生成中: {filename}")
        print(f"  提示词前50字: {prompt[:50]}...")

        success = False
        for attempt in range(max_retries):
            if attempt > 0:
                wait = 15 * (2 ** (attempt - 1))
                print(f"  第 {attempt+1} 次重试，等待 {wait}s ...")
                await asyncio.sleep(wait)

            try:
                # Determine model for image generation.
                # Nano Banana Pro is activated via monkey-patch (idx49=14, idx17=[[1]])
                # which is injected into every request's inner_req_list.
                #
                # NOTE: Using Model.G_3_0_PRO header causes hangs/timeouts with
                # image generation (known issue: GitHub #229). The monkey-patch
                # approach with UNSPECIFIED model + idx49/idx17 injection is the
                # only reliable method currently.
                model_arg = chart.get("model", "pro")
                from gemini_webapi.constants import Model
                model_map = {
                    "pro": Model.UNSPECIFIED,  # Nano Banana Pro via monkey-patch only
                    "3.0-pro": Model.G_3_0_PRO,
                    "flash": Model.G_3_0_FLASH,
                    "flash-thinking": Model.G_3_0_FLASH_THINKING,
                    "auto": Model.UNSPECIFIED,
                }
                kwargs = {}
                if model_arg in model_map:
                    kwargs["model"] = model_map[model_arg]
                else:
                    kwargs["model"] = Model.UNSPECIFIED

                response = await client.generate_content(prompt, **kwargs)

                # Check for generated images
                candidate = response.candidates[response.chosen] if response.candidates else None
                if candidate and candidate.generated_images:
                    img = candidate.generated_images[0]
                    saved_path = await img.save(
                        path=output_dir,
                        filename=filename,
                        verbose=True,
                        skip_invalid_filename=True,
                        full_size=True,
                    )
                    if saved_path:
                        print(f"  ✅ 已保存: {saved_path}")
                        results.append({"filename": filename, "status": "success", "path": saved_path})
                        success = True
                        break
                    else:
                        print(f"  ⚠️  图片保存失败，将重试")
                elif candidate and candidate.text:
                    # Model returned text instead of image
                    print(f"  ⚠️  模型返回文本而非图片（可能需要调整提示词）")
                    print(f"  回复前100字: {candidate.text[:100]}")
                    # Still retry - sometimes model needs a different attempt
                else:
                    print(f"  ⚠️  无内容返回，将重试")

            except Exception as e:
                print(f"  ❌ 生成异常: {e}")

        if not success:
            print(f"  ❌ {filename} 多次重试仍失败")
            results.append({"filename": filename, "status": "failed"})

        # Wait between requests to avoid rate limiting
        if i < total - 1:
            print(f"  等待 {delay_between}s ...\n")
            await asyncio.sleep(delay_between)

    await client.close()

    # Summary
    succeeded = sum(1 for r in results if r["status"] == "success")
    print(f"\n=== 完成: {succeeded}/{total} 张图片生成成功 ===")
    for r in results:
        status = "✅" if r["status"] == "success" else "❌"
        print(f"  {status} {r['filename']}")

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Generate images via Gemini web app (uses subscription quota, not API tokens)"
    )
    parser.add_argument(
        "--storage", type=Path, default=None,
        help="Path to NotebookLM storage_state.json (default: ~/.notebooklm/storage_state.json)"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # auth - check authentication (auto-loads from NotebookLM storage)
    auth_parser = subparsers.add_parser("auth", help="Check Gemini authentication (auto-reads from NotebookLM storage)")
    auth_parser.add_argument(
        "--psid", default=None,
        help="(Optional) Manually provide __Secure-1PSID cookie"
    )
    auth_parser.add_argument(
        "--psidts", default=None,
        help="(Optional) Manually provide __Secure-1PSIDTS cookie"
    )

    # generate - generate images
    gen_parser = subparsers.add_parser("generate", help="Generate images from config or prompt file")
    gen_parser.add_argument("--config", help="JSON config file with chart definitions")
    gen_parser.add_argument("--prompt-file", help="Single prompt text file")
    gen_parser.add_argument("--output-dir", required=True, help="Output directory")
    gen_parser.add_argument("--filename", default="output.png", help="Output filename (for --prompt-file mode)")
    gen_parser.add_argument("--delay", type=int, default=10, help="Delay between requests in seconds")
    gen_parser.add_argument("--max-retries", type=int, default=3, help="Max retries per chart")
    gen_parser.add_argument("--model", default="pro", help="Model: pro (default), flash, flash-thinking")

    args = parser.parse_args()

    if args.command == "auth":
        if args.psid:
            # Manual mode: user provides cookies explicitly
            cookies = {
                "__Secure-1PSID": args.psid,
                "__Secure-1PSIDTS": args.psidts or "",
            }
            save_cookies(cookies)
        else:
            # Auto mode: read from NotebookLM storage
            cookies = load_cookies(args.storage)
            if not cookies:
                print("❌ 未找到认证信息")
                print("   请确保已通过 NotebookLM 登录过 Google 账号")
                print(f"   预期路径: {args.storage or DEFAULT_STORAGE_PATH}")
                print("   或手动指定: python gemini_generate_images.py auth --psid <VALUE> --psidts <VALUE>")
                sys.exit(1)
        asyncio.run(test_auth(cookies))

    elif args.command == "generate":
        cookies = load_cookies(args.storage)
        if not cookies:
            print("❌ 未找到认证信息")
            print("   请确保已通过 NotebookLM 登录过 Google 账号")
            print(f"   预期路径: {args.storage or DEFAULT_STORAGE_PATH}")
            sys.exit(1)

        if args.config:
            with open(args.config, "r", encoding="utf-8") as f:
                charts = json.load(f)
        elif args.prompt_file:
            with open(args.prompt_file, "r", encoding="utf-8") as f:
                prompt = f.read().strip()
            charts = [{"prompt": prompt, "filename": args.filename, "model": args.model}]
        else:
            print("❌ 请指定 --config 或 --prompt-file")
            sys.exit(1)

        # Apply model override if specified via CLI
        if args.model != "pro":
            for chart in charts:
                if chart.get("model") in (None, "auto", "pro"):
                    chart["model"] = args.model

        asyncio.run(generate_images(
            cookies, charts, args.output_dir,
            delay_between=args.delay,
            max_retries=args.max_retries,
        ))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
