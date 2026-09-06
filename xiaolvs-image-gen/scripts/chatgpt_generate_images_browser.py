#!/usr/bin/env python3
"""通过 ChatGPT 网页版（chatgpt.com/images）+ Chrome CDP 批量生成小绿书竖版信息图。

与 gemini_generate_images_browser.py 的关系：
  - 产物一致：`infographic.png` / `chart_N.png`
  - 输入一致：项目子目录下的 charts_config JSON（prompt + filename 数组）
  - 通道不同：本脚本 CDP 驱动 chatgpt.com（走 ChatGPT 订阅配额，gpt-image），
    复用 ~/.chatgpt_browser/chrome_cdp_profile 中的 ChatGPT 登录态。

每张图的流程：
  1. 打开 https://chatgpt.com/images（用户明确要求：不用临时会话模式）
  2. 输入提示词并发送（#prompt-textarea + send-button）
  3. 等待生成完成：真实图片 URL 是 /backend-api/estuary/content?id=file_...；
     提交前快照已有 src，只接受新出现的、宽度 ≥900 的完整渲染图
     （512x512 WebP 是缩略图，不算成果）
  4. 下载原图：统一页面内 fetch（page.evaluate），自动携带 cookies 和
     --proxy-server 代理；context.request 是 Node 侧直连会 TLS 断连
  5. 产物校验（_is_valid_output）：PNG 魔数 + >100KB，不合格直接作废重试
  6. 配额检测（_quota_exhausted）：页面出现「还可生成剩 0 张」等提示时
     标记 status="quota" 并停止本轮，由 --until-done 外层等待重置后续跑

常用命令（必须用带 playwright 的 venv python）：

    PY=/Users/helufan/.workbuddy/binaries/python/envs/default/bin/python

    # 首次登录（打开 CDP Chrome 后立即退出，人工登录，用户确认后再继续——不轮询）
    $PY chatgpt_generate_images_browser.py login

    # 检查登录态
    $PY chatgpt_generate_images_browser.py auth

    # 批量生成（推荐）：--until-done 主循环，撞配额自动等待重置续跑，
    # 直到全部完成，全程无人值守
    $PY -u chatgpt_generate_images_browser.py generate --until-done \
        --config <dir>/chatgpt_charts_config.json --output-dir <dir> \
        --quota-wait-minutes 15 --max-hours 30

    # 单次批量生成（--skip-existing 断点续跑，跳过校验合格的已有文件）
    $PY chatgpt_generate_images_browser.py generate \
        --config <dir>/chatgpt_charts_config.json --output-dir <dir> --skip-existing

注意：
  - CDP 端口 9223（与 Gemini 通道的 9222 错开，可同时运行）
  - WorkBuddy Bash 沙盒会杀 Chrome，启动参数必须含 --no-sandbox --disable-gpu，
    或直接在沙盒外运行
  - 跑了一周以上的 CDP Chrome 可能挂死（/json/version 有响应但
    connect_over_cdp 超时），pkill 重启即可，profile 登录态不受影响
  - ChatGPT 免费版图片配额约 3 张/天，撞配额后 --until-done 会自动等待续跑
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

CHATGPT_STORAGE_DIR = Path.home() / ".chatgpt_browser"
CHATGPT_STORAGE_PATH = CHATGPT_STORAGE_DIR / "storage_state.json"
CDP_PROFILE_DIR = CHATGPT_STORAGE_DIR / "chrome_cdp_profile"

CHATGPT_HOME = "https://chatgpt.com/"
CHATGPT_IMAGES = "https://chatgpt.com/images"

IMAGE_GENERATION_TIMEOUT = 300
POLL_INTERVAL = 3
CDP_PORT = 9223

_chrome_process: subprocess.Popen | None = None


# --------------------------------------------------------------------------- #
# 基础设施（与 Gemini 通道同构）
# --------------------------------------------------------------------------- #
def _proxy_alive(proxy: str) -> bool:
    """TCP 层探测代理端口是否存活（避免选到已失效的代理）。"""
    import socket
    import urllib.parse

    try:
        parsed = urllib.parse.urlparse(proxy)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8080
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect((host, port))
        sock.close()
        return True
    except Exception:
        return False


def _detect_proxy() -> str | None:
    """按优先级探测代理候选：环境变量 → networksetup → scutil；只返回存活的。"""
    candidates: list[str] = []
    env_proxy = (
        os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("HTTP_PROXY")
    )
    if env_proxy:
        candidates.append(env_proxy)

    for cmd in ["getsecurewebproxy", "getwebproxy"]:
        try:
            result = subprocess.run(
                ["networksetup", f"-{cmd}", "Wi-Fi"],
                capture_output=True, text=True, timeout=5,
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
                candidates.append(f"http://{server}:{port}")
        except Exception:
            pass

    # scutil --proxy（覆盖非 Wi-Fi 网卡场景）
    try:
        out = subprocess.run(
            ["scutil", "--proxy"], capture_output=True, text=True, timeout=5
        ).stdout
        import re as _re

        en = _re.search(r"HTTPSEnable\s*:\s*(\d)", out)
        host = _re.search(r"HTTPSProxy\s*:\s*(\S+)", out)
        prt = _re.search(r"HTTPSPort\s*:\s*(\d+)", out)
        if en and en.group(1) == "1" and host and prt:
            candidates.append(f"http://{host.group(1)}:{prt.group(1)}")
    except Exception:
        pass

    seen: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.append(c)
    for c in seen:
        if _proxy_alive(c):
            return c
        print(f"⚠️  代理 {c} 端口不可达，跳过")
    return None


def _find_chrome_executable() -> str | None:
    if platform.system() == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        ]
    elif platform.system() == "Windows":
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
        found = shutil.which("google-chrome") or shutil.which("chromium")
        if found:
            candidates.insert(0, found)
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


async def _launch_real_chrome(port: int = CDP_PORT, url: str | None = None) -> bool:
    """启动带 CDP 的真实 Chrome（独立 profile）。已在运行则直接复用。"""
    global _chrome_process
    import socket

    chrome_path = _find_chrome_executable()
    if not chrome_path:
        print("⚠️  未找到系统 Chrome 浏览器")
        return False

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(1)
        sock.connect(("127.0.0.1", port))
        print(f"✅ Chrome 已在端口 {port} 运行，直接连接")
        return True
    except (ConnectionRefusedError, OSError):
        pass
    finally:
        sock.close()

    os.makedirs(CDP_PROFILE_DIR, exist_ok=True)
    cmd = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={CDP_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        "--disable-popup-blocking",
        "--disable-translate",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        # WorkBuddy Bash 沙盒会拦截 Chrome 沙盒初始化与 GPU 进程
        # （报 "sandbox initialization failed" / "GPU process isn't usable" 后退出），
        # 必须加 --no-sandbox --disable-gpu 才能在沙盒内保活。
        "--no-sandbox",
        "--disable-gpu",
        "--window-size=1440,900",
    ]
    proxy = _detect_proxy()
    if proxy:
        cmd.append(f"--proxy-server={proxy}")
        print(f"🌐 使用代理: {proxy}")
    if url:
        cmd.append(url)

    print(f"🚀 启动 Chrome CDP (profile: {CDP_PROFILE_DIR})")
    _chrome_process = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )

    for _ in range(30):
        await asyncio.sleep(1)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            sock.connect(("127.0.0.1", port))
            sock.close()
            print(f"✅ Chrome CDP 就绪 (port {port})")
            return True
        except (ConnectionRefusedError, OSError):
            pass
        finally:
            sock.close()

    if _chrome_process and _chrome_process.poll() is not None:
        stderr_out = (
            _chrome_process.stderr.read().decode(errors="replace")
            if _chrome_process.stderr else ""
        )
        if stderr_out:
            print(f"❌ Chrome 启动失败:\n{stderr_out[:500]}")
    print("❌ Chrome CDP 启动超时（30s 内端口未就绪）")
    return False


def _cleanup_chrome():
    global _chrome_process
    if _chrome_process:
        try:
            _chrome_process.terminate()
            _chrome_process.wait(timeout=5)
        except Exception:
            try:
                _chrome_process.kill()
            except Exception:
                pass
        _chrome_process = None


async def _connect_cdp(playwright):
    """连接 CDP Chrome，返回 (browser, context)。"""
    launched = await _launch_real_chrome(CDP_PORT)
    if not launched:
        return None, None
    browser = await playwright.chromium.connect_over_cdp(
        f"http://127.0.0.1:{CDP_PORT}"
    )
    if browser.contexts:
        context = browser.contexts[0]
        print("🔗 已连接到 Chrome（复用已有上下文）")
    else:
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        print("🔗 已连接到 Chrome（新建上下文）")
    return browser, context


# --------------------------------------------------------------------------- #
# ChatGPT 页面交互
# --------------------------------------------------------------------------- #
async def _dismiss_dialogs(page):
    """关闭可能出现的弹窗（cookie 横幅、推广弹窗等）。"""
    for selector in [
        'button[data-testid="close-button"]',
        'button[aria-label="Close"]',
        'button[aria-label="关闭"]',
        'div[role="dialog"] button:has-text("OK")',
        'div[role="dialog"] button:has-text("好的")',
        'div[role="dialog"] button:has-text("Got it")',
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=800):
                await btn.click()
                await page.wait_for_timeout(500)
        except Exception:
            continue


async def _check_login_status(page) -> bool:
    """登录判定：能看到输入框且没有显眼的 Log in 按钮。"""
    try:
        composer = page.locator("#prompt-textarea").first
        if await composer.is_visible(timeout=5000):
            return True
    except Exception:
        pass
    try:
        login_btn = page.locator(
            'button:has-text("Log in"), button:has-text("登录"), '
            'a:has-text("Log in"), a:has-text("登录")'
        ).first
        if await login_btn.is_visible(timeout=2000):
            return False
    except Exception:
        pass
    return False


async def _goto_images_page(page) -> bool:
    """打开 https://chatgpt.com/images 并确认输入框就绪。

    用户明确要求：不使用临时会话（temporary chat），
    直接在 /images 页面操作生成并下载图片。
    """
    try:
        await page.goto(CHATGPT_IMAGES, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"  ⚠️  页面加载异常（继续等待）: {e}")
    # 等待输入框出现（Cloudflare 校验可能需要更久）
    for _ in range(40):
        await page.wait_for_timeout(3000)
        await _dismiss_dialogs(page)
        try:
            if await page.locator("#prompt-textarea").first.is_visible(timeout=1000):
                return True
        except Exception:
            pass
    return False


async def _type_prompt(page, prompt: str):
    """向 #prompt-textarea 输入提示词（换行替换为空格防 Enter 触发发送）。"""
    input_el = None
    for selector in ['#prompt-textarea', 'div[contenteditable="true"]']:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=3000):
                input_el = el
                break
        except Exception:
            continue
    if not input_el:
        raise RuntimeError("无法找到 ChatGPT 输入框")

    await input_el.click()
    await page.wait_for_timeout(500)
    await page.keyboard.press("Meta+a")
    await page.keyboard.press("Backspace")
    await page.wait_for_timeout(300)

    safe_prompt = prompt.replace("\n", " ").replace("\r", " ")
    while "  " in safe_prompt:
        safe_prompt = safe_prompt.replace("  ", " ")
    safe_prompt = safe_prompt.strip()
    prompt_len = len(safe_prompt)

    await page.evaluate("""(text) => {
        const el = document.querySelector(
            '#prompt-textarea, div[contenteditable="true"]'
        );
        if (el) {
            el.focus();
            document.execCommand('insertText', false, text);
        }
    }""", safe_prompt)
    await page.wait_for_timeout(1000)

    content = await page.evaluate("""() => {
        const el = document.querySelector('#prompt-textarea');
        return el ? el.innerText.trim().length : 0;
    }""")

    if content >= prompt_len * 0.5:
        print(f"  ✅ 文本已输入（{content} 字符）")
    else:
        print(f"  ⚠️  execCommand 输入不完整（{content}/{prompt_len}），用 keyboard.type 重试...")
        await page.keyboard.press("Meta+a")
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(300)
        chunk_size = 200
        for start in range(0, prompt_len, chunk_size):
            await page.keyboard.type(safe_prompt[start:start + chunk_size], delay=1)
            await page.wait_for_timeout(100)
        await page.wait_for_timeout(500)


async def _submit_prompt(page) -> bool:
    for selector in [
        'button[data-testid="send-button"]',
        '#composer-submit-button',
        'button[aria-label="Send prompt"]',
        'button[aria-label="发送"]',
        'button[aria-label*="Send"]',
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                print("  📤 已点击发送按钮")
                await page.wait_for_timeout(2000)
                return True
        except Exception:
            continue
    await page.keyboard.press("Enter")
    print("  📤 已发送提示词（Enter fallback）")
    await page.wait_for_timeout(2000)
    return True


_FIND_IMAGE_JS = """() => {
    const results = [];
    const imgs = document.querySelectorAll('img');
    for (const img of imgs) {
        const src = img.src || '';
        if (!src) continue;
        if (src.includes('avatar') || src.includes('icon') ||
            src.includes('logo') || src.includes('favicon') ||
            src.includes('emoji') || src.includes('public_content') ||
            src.startsWith('chrome-extension://')) continue;
        // ChatGPT 生成图的真实 URL 形态：
        //   https://chatgpt.com/backend-api/estuary/content?id=file_...
        const isGenerated =
            src.startsWith('blob:') ||
            src.includes('/backend-api/estuary/content') ||
            src.includes('oaiusercontent.com') ||
            src.includes('files.oai') ||
            src.includes('dalle');
        if (!isGenerated) continue;
        const w = img.naturalWidth || img.width || 0;
        const h = img.naturalHeight || img.height || 0;
        if (w < 512 || h < 512) continue;
        results.push({ src, w, h });
    }
    return results;
}"""


_QUOTA_JS = """() => {
    const t = document.body ? document.body.innerText : '';
    const patterns = [
        '还可生成剩 0', '还可生成剩余 0', '剩 0 张',
        '图片生成次数已用完', '已达到图片生成上限', '已达上限',
        'image generation limit', 'limit reached', 'rate limit',
        'too many requests', 'usage limit',
    ];
    const lower = t.toLowerCase();
    for (const p of patterns) { if (lower.includes(p.toLowerCase())) return true; }
    return false;
}"""


async def _quota_exhausted(page) -> bool:
    """检测页面是否提示图片配额已用完（免费版常见：还可生成剩 0 张）。"""
    try:
        return bool(await page.evaluate(_QUOTA_JS))
    except Exception:
        return False


def _is_valid_output(path: str, min_bytes: int = 100000) -> bool:
    """校验产物：真实 PNG（魔数）且体积达标，过滤 512x512 WebP 缩略图。"""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < min_bytes:
            return False
        with open(path, "rb") as f:
            return f.read(8) == b"\x89PNG\r\n\x1a\n"
    except Exception:
        return False


async def _is_generating(page) -> bool:
    """检测是否仍在流式生成中（停止按钮可见即仍在生成）。"""
    for selector in [
        'button[data-testid="stop-button"]',
        'button[aria-label="Stop generating"]',
        'button[aria-label="停止生成"]',
        'button[aria-label*="Stop"]',
    ]:
        try:
            if await page.locator(selector).first.is_visible(timeout=500):
                return True
        except Exception:
            continue
    return False


async def _wait_for_image(
    page, timeout: int = IMAGE_GENERATION_TIMEOUT, exclude_srcs: set | None = None
) -> list[dict]:
    """等待【新】生成图片出现且稳定。

    exclude_srcs：提交提示词前页面上已存在的图片 src 集合（缩略图/历史图），
    只接受不在该集合中的新 src，避免误抓上一张图的缩略图（512x512 WebP）。
    多张候选时返回按面积降序的列表（调用方取 [0] 即最大渲染图）。
    """
    exclude_srcs = exclude_srcs or set()
    print(f"  ⏳ 等待图片生成（最长 {timeout}s）...")
    start = time.time()
    await page.wait_for_timeout(5000)
    last_src = None
    stable_rounds = 0

    while time.time() - start < timeout:
        generating = await _is_generating(page)
        images = await page.evaluate(_FIND_IMAGE_JS)
        new_images = [im for im in images if im["src"] not in exclude_srcs]

        # 只考虑宽度 >=900 的完整渲染图（缩略图为 512x512 WebP）
        big_images = [im for im in new_images if im["w"] >= 900]
        if big_images:
            # 取面积最大的候选（完整渲染图优先于缩略图）
            best = max(big_images, key=lambda im: im["w"] * im["h"])
            current_src = best["src"]
            if not generating and current_src == last_src:
                stable_rounds += 1
                if stable_rounds >= 2:
                    print(f"  ✅ 检测到生成图片（{best['w']}x{best['h']}）")
                    return sorted(
                        big_images, key=lambda im: im["w"] * im["h"], reverse=True
                    )
            else:
                stable_rounds = 0
            last_src = current_src

        elapsed = int(time.time() - start)
        status = "生成中" if generating else "等待图片出现"
        print(f"  ... {status}（{elapsed}s）", flush=True)
        await page.wait_for_timeout(POLL_INTERVAL * 1000)

    if new_images:
        print("  ⚠️  超时，仅有小尺寸候选，返回最大者兜底")
        return sorted(new_images, key=lambda im: im["w"] * im["h"], reverse=True)
    return []


async def _download_image(page, context, src: str, output_path: str) -> bool:
    """下载图片原图。

    统一走页面内 fetch（page.evaluate）：浏览器网络栈自动携带 cookies
    和 --proxy-server 代理；context.request 是 Node 侧直连，不走代理会
    TLS 断连，且同源 estuary URL 页面内 fetch 天然带会话。
    """
    try:
        data_url = await page.evaluate("""async (src) => {
            const resp = await fetch(src, { credentials: 'include' });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            const blob = await resp.blob();
            return await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onloadend = () => resolve(reader.result);
                reader.onerror = reject;
                reader.readAsDataURL(blob);
            });
        }""", src)
        if data_url and "," in data_url:
            blob = base64.b64decode(data_url.split(",", 1)[1])
            with open(output_path, "wb") as f:
                f.write(blob)
            return len(blob) > 10000
    except Exception as e:
        print(f"  ⚠️  页面内下载失败: {e}")
    return False


async def _save_storage(context):
    try:
        CHATGPT_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(CHATGPT_STORAGE_PATH))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 命令：login / auth / generate
# --------------------------------------------------------------------------- #
async def interactive_login():
    """打开 CDP Chrome 到 chatgpt.com 后立即返回（Chrome 保留运行）。

    不做轮询检测——由用户登录完成后人工确认，再运行 auth/generate。
    这样比后台轮询更省 token、也更可靠。
    """
    print("🔐 登录 ChatGPT 账号")
    print("=" * 50)
    print("已打开一个 Chrome 窗口（chatgpt.com），请在其中登录。")
    print("登录完成后回复确认，再运行 auth / generate 命令。")
    print("=" * 50)

    launched = await _launch_real_chrome(CDP_PORT, url=CHATGPT_HOME)
    if not launched:
        return False
    print("✅ Chrome 已打开 chatgpt.com，等待人工登录（本脚本已退出，Chrome 保留运行）")
    return True


async def test_auth() -> bool:
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser, context = await _connect_cdp(p)
            if not context:
                return False
            page = await context.new_page()
            ok = await _goto_images_page(page)
            if ok:
                print("✅ ChatGPT 认证有效")
                await _save_storage(context)
            else:
                print("❌ 未能打开 /images 页面（未登录或被拦截）")
            await page.screenshot(path="/tmp/chatgpt_auth_check.png")
            print("📸 调试截图: /tmp/chatgpt_auth_check.png")
            return ok
    finally:
        pass  # 不 kill 用户可能正在使用的 Chrome


async def generate_images(
    charts: list[dict],
    output_dir: str,
    delay_between: int = 15,
    max_retries: int = 2,
):
    from playwright.async_api import async_playwright

    os.makedirs(output_dir, exist_ok=True)
    total = len(charts)

    async with async_playwright() as p:
        browser, context = await _connect_cdp(p)
        if not context:
            print("❌ 无法连接 CDP Chrome")
            return []

        async def ensure_page():
            """返回可用的 chatgpt 页面；页面/浏览器被关闭时自动重连重建。"""
            nonlocal browser, context
            try:
                # 优先复用已打开的 chatgpt 页面
                for pg in context.pages:
                    if "chatgpt.com" in pg.url:
                        return pg
                return await context.new_page()
            except Exception:
                print("  ↻ 页面/浏览器已关闭，重新连接 CDP ...")
                browser, context = await _connect_cdp(p)
                if not context:
                    raise RuntimeError("CDP 重连失败")
                return await context.new_page()

        page = await ensure_page()

        # 先确认登录态
        ready = await _goto_images_page(page)
        if not ready:
            print("❌ 无法打开 chatgpt.com/images，请先运行 login 命令登录")
            await page.screenshot(path="/tmp/chatgpt_not_ready.png")
            print("📸 调试截图: /tmp/chatgpt_not_ready.png")
            return []

        print(f"\n=== 开始生成 {total} 张图片（ChatGPT 网页 CDP 模式）===\n")
        results = []
        quota_stop = False

        for i, chart in enumerate(charts):
            prompt = chart["prompt"]
            filename = chart["filename"]
            output_path = os.path.join(output_dir, filename)

            print(f"\n{'='*50}")
            print(f"[{i+1}/{total}] 生成中: {filename}")
            print(f"  提示词前50字: {prompt[:50]}...")

            success = False
            for attempt in range(max_retries):
                if attempt > 0:
                    wait = 20 * attempt
                    print(f"  第 {attempt+1} 次尝试，等待 {wait}s ...")
                    await asyncio.sleep(wait)

                try:
                    page = await ensure_page()

                    # 每张图重新打开 /images 页面（ fresh composer）
                    if i > 0 or attempt > 0:
                        ready = await _goto_images_page(page)
                        if not ready:
                            print("  ⚠️  /images 页面打开失败，重试中...")
                            continue

                    # 提交前快照页面已有图片 src（历史图/缩略图），
                    # 之后只接受新出现的 src，防止误抓上一张的缩略图
                    try:
                        before_srcs = {
                            im["src"] for im in await page.evaluate(_FIND_IMAGE_JS)
                        }
                    except Exception:
                        before_srcs = set()

                    await _type_prompt(page, prompt)
                    await page.wait_for_timeout(500)
                    await _submit_prompt(page)

                    images = await _wait_for_image(page, exclude_srcs=before_srcs)

                    if images:
                        src = images[0]["src"]
                        downloaded = await _download_image(page, context, src, output_path)

                        if downloaded and _is_valid_output(output_path):
                            file_size = Path(output_path).stat().st_size
                            print(f"  ✅ 已保存: {output_path} ({file_size // 1024}KB)")
                            results.append({
                                "filename": filename,
                                "status": "success",
                                "path": output_path,
                            })
                            success = True
                            break
                        else:
                            # 下载失败或产物是缩略图/WebP：作废并重试
                            if downloaded:
                                print("  ⚠️  产物校验失败（缩略图或非 PNG），作废重试")
                                try:
                                    os.remove(output_path)
                                except OSError:
                                    pass
                            else:
                                print("  ⚠️  图片下载失败，将重试")
                            if await _quota_exhausted(page):
                                print("  ⛔ 检测到图片配额已用完，停止本轮")
                                results.append({"filename": filename, "status": "quota"})
                                quota_stop = True
                                break
                    else:
                        if await _quota_exhausted(page):
                            print("  ⛔ 检测到图片配额已用完，停止本轮")
                            results.append({"filename": filename, "status": "quota"})
                            quota_stop = True
                            break
                        debug_path = f"/tmp/chatgpt_debug_{filename}"
                        await page.screenshot(path=debug_path, full_page=False)
                        print(f"  ⚠️  未检测到图片，调试截图: {debug_path}")

                except Exception as e:
                    print(f"  ❌ 生成异常: {e}")
                    try:
                        await page.screenshot(
                            path=f"/tmp/chatgpt_error_{filename}", full_page=False
                        )
                    except Exception:
                        pass

            if not success and not quota_stop:
                print(f"  ❌ {filename} 多次重试仍失败")
                results.append({"filename": filename, "status": "failed"})

            if quota_stop:
                print("⛔ 配额耗尽，提前结束本轮（剩余图片由外层循环等待重置后续跑）")
                break

            if i < total - 1:
                print(f"  等待 {delay_between}s ...\n")
                await asyncio.sleep(delay_between)

        await _save_storage(context)

    succeeded = sum(1 for r in results if r["status"] == "success")
    print(f"\n=== 完成: {succeeded}/{total} 张图片生成成功 ===")
    for r in results:
        status = "✅" if r["status"] == "success" else "❌"
        print(f"  {status} {r['filename']}")
    return results


async def run_until_done(
    charts: list[dict],
    output_dir: str,
    delay_between: int = 15,
    quota_wait_minutes: int = 15,
    max_hours: float = 8,
):
    """主循环：反复扫描缺失图片并生成，直到全部完成或超时。

    - 每轮只跑缺失的文件（_is_valid_output 校验：真实 PNG 且 >100KB）
    - 撞到配额上限（status == "quota"）→ 等 quota_wait_minutes 后自动续跑
    - 其他失败 → 5 分钟后重试
    - 全程无人值守，不需要手动输入「继续」
    """
    os.makedirs(output_dir, exist_ok=True)
    deadline = time.time() + max_hours * 3600
    round_no = 0

    def _remaining():
        return [
            c for c in charts
            if not _is_valid_output(os.path.join(output_dir, c["filename"]))
        ]

    while time.time() < deadline:
        remaining = _remaining()
        if not remaining:
            print(f"\n🎉 全部 {len(charts)} 张图片已生成完毕")
            return True

        round_no += 1
        print(f"\n{'#'*56}")
        print(f"##### 第 {round_no} 轮：剩余 {len(remaining)}/{len(charts)} 张待生成")
        print(f"##### {[c['filename'] for c in remaining]}")
        print(f"{'#'*56}")

        results = await generate_images(
            remaining, output_dir,
            delay_between=delay_between, max_retries=1,
        )

        remaining = _remaining()
        if not remaining:
            print(f"\n🎉 全部 {len(charts)} 张图片已生成完毕")
            return True

        quota_hit = any(r.get("status") == "quota" for r in results)
        wait_min = quota_wait_minutes if quota_hit else 5
        print(
            f"\n⏳ 本轮结束仍有 {len(remaining)} 张未完成"
            f"（{'配额耗尽，等待重置' if quota_hit else '普通失败重试'}），"
            f"{wait_min} 分钟后自动开始第 {round_no + 1} 轮 ..."
        )
        await asyncio.sleep(wait_min * 60)

    print(f"\n❌ 超过 {max_hours} 小时仍未全部完成，退出")
    return False



def main():
    parser = argparse.ArgumentParser(
        description="Generate images via chatgpt.com (browser CDP automation)"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser("login", help="打开 CDP Chrome 登录 ChatGPT（仅需一次）")
    subparsers.add_parser("auth", help="检查 ChatGPT 登录态")

    gen_parser = subparsers.add_parser("generate", help="从配置文件批量生成图片")
    gen_parser.add_argument("--config", required=True, help="JSON config file")
    gen_parser.add_argument("--output-dir", required=True, help="Output directory")
    gen_parser.add_argument("--delay", type=int, default=15, help="图片间隔秒数")
    gen_parser.add_argument("--max-retries", type=int, default=2, help="每张图最大尝试次数")
    gen_parser.add_argument("--skip-existing", action="store_true",
                            help="跳过输出目录中已存在且校验合格（真实 PNG 且 >100KB）的文件")
    gen_parser.add_argument("--until-done", action="store_true",
                            help="主循环模式：反复生成缺失图片直到全部完成（撞配额自动等待重置），无需人工干预")
    gen_parser.add_argument("--quota-wait-minutes", type=int, default=15,
                            help="until-done 模式：撞配额后等待分钟数（默认 15）")
    gen_parser.add_argument("--max-hours", type=float, default=8,
                            help="until-done 模式：最长运行小时数（默认 8）")

    args = parser.parse_args()

    if args.command == "login":
        asyncio.run(interactive_login())
    elif args.command == "auth":
        ok = asyncio.run(test_auth())
        sys.exit(0 if ok else 1)
    elif args.command == "generate":
        with open(args.config, "r", encoding="utf-8") as f:
            charts = json.load(f)
        if isinstance(charts, dict):
            charts = charts.get("charts", [])
        if getattr(args, "until_done", False):
            ok = asyncio.run(
                run_until_done(
                    charts, args.output_dir,
                    delay_between=args.delay,
                    quota_wait_minutes=args.quota_wait_minutes,
                    max_hours=args.max_hours,
                )
            )
            sys.exit(0 if ok else 1)
        if getattr(args, "skip_existing", False):
            before = len(charts)
            charts = [
                c for c in charts
                if not _is_valid_output(os.path.join(args.output_dir, c["filename"]))
            ]
            print(f"⏭️  跳过已存在文件：{before - len(charts)} 张，待生成 {len(charts)} 张")
            if not charts:
                print("✅ 全部图片已存在，无需生成")
                sys.exit(0)
        asyncio.run(
            generate_images(
                charts, args.output_dir,
                delay_between=args.delay, max_retries=args.max_retries,
            )
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
