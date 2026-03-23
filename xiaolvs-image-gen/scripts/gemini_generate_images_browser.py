#!/usr/bin/env python3
"""
Generate images via Gemini web app using Playwright browser automation (DEFAULT).

This is the PRIMARY image generation method, using Chrome CDP mode to directly
control gemini.google.com. For each image, the script automatically:
1. Opens a Temporary chat (avoids polluting chat history)
2. Enables the "Create image" tool (Nano Banana Pro)
3. Switches to Pro model (critical for Chinese text quality)
4. Inputs the prompt and sends
5. Downloads via hover + download button (most reliable method)

The gemini-webapi library-based script (gemini_generate_images.py) is kept as
a fallback for when it is updated to support the latest Google API changes.

Authentication: Uses the shared Google login session from NotebookLM's
Playwright storage (~/.notebooklm/storage_state.json), or connects to
the user's real Chrome via CDP (preferred, uses existing login state).

Usage:
    # Check authentication
    python gemini_generate_images_browser.py auth

    # Generate images from config
    python gemini_generate_images_browser.py generate --config <JSON> --output-dir <DIR>

    # Generate single image from prompt file
    python gemini_generate_images_browser.py generate --prompt-file <TXT> --output-dir <DIR> --filename chart.png

Config JSON format (same as gemini_generate_images.py):
[
    {
        "prompt": "Generate an image: ...",
        "filename": "chart_1.png"
    },
    ...
]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

# Default NotebookLM storage path (shared Google login session)
DEFAULT_STORAGE_PATH = Path.home() / ".notebooklm" / "storage_state.json"
# Gemini storage for browser-specific session
GEMINI_STORAGE_DIR = Path.home() / ".gemini_browser"
GEMINI_STORAGE_PATH = GEMINI_STORAGE_DIR / "storage_state.json"

GEMINI_URL = "https://gemini.google.com/app"

# Timeout for waiting for image generation (seconds)
IMAGE_GENERATION_TIMEOUT = 300
# Interval for polling DOM changes (seconds)
POLL_INTERVAL = 3

# CDP debugging port for connecting to real Chrome
CDP_PORT = 9222

# ── Stealth JS ──────────────────────────────────────────────────────
# Comprehensive anti-detection script injected via addInitScript.
# Hides Playwright/automation fingerprints when using built-in Chromium.
STEALTH_JS = """
// 1. navigator.webdriver → false
Object.defineProperty(navigator, 'webdriver', { get: () => false });

// 2. Remove Playwright-specific properties
delete navigator.__proto__.webdriver;

// 3. Chrome runtime — pretend we have the chrome extension API
if (!window.chrome) { window.chrome = {}; }
if (!window.chrome.runtime) {
    window.chrome.runtime = {
        connect: function() {},
        sendMessage: function() {},
    };
}

// 4. Permissions — override query to report 'prompt' for notifications
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters);

// 5. Plugins — fake a realistic plugins array
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const plugins = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
              description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
              description: '' },
            { name: 'Native Client', filename: 'internal-nacl-plugin',
              description: '' },
        ];
        plugins.length = 3;
        return plugins;
    },
});

// 6. Languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en-US', 'en'],
});

// 7. WebGL vendor/renderer — generic strings
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.call(this, parameter);
};

// 8. Prevent headless detection via missing `window.outerWidth`
if (window.outerWidth === 0) { window.outerWidth = window.innerWidth; }
if (window.outerHeight === 0) { window.outerHeight = window.innerHeight; }

// 9. Connection type
if (navigator.connection) {
    Object.defineProperty(navigator.connection, 'rtt', { get: () => 50 });
}

// 10. Hardware concurrency
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

// 11. DeviceMemory
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
"""


def _detect_proxy() -> str | None:
    """Auto-detect proxy from environment or macOS system preferences."""
    proxy = (
        os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("HTTP_PROXY")
    )
    if proxy:
        return proxy

    # Try reading macOS system HTTP/HTTPS proxy
    import subprocess

    for cmd in ["getsecurewebproxy", "getwebproxy"]:
        try:
            result = subprocess.run(
                ["networksetup", f"-{cmd}", "Wi-Fi"],
                capture_output=True,
                text=True,
                timeout=5,
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
                return f"http://{server}:{port}"
        except Exception:
            pass
    return None


def _get_storage_state_path() -> str | None:
    """Find best available storage state for Google login.

    Priority:
    1. Gemini-specific storage (from previous browser session)
    2. NotebookLM shared storage
    """
    if GEMINI_STORAGE_PATH.exists():
        return str(GEMINI_STORAGE_PATH)
    if DEFAULT_STORAGE_PATH.exists():
        return str(DEFAULT_STORAGE_PATH)
    return None


def _find_chrome_executable() -> str | None:
    """Find the Chrome/Chromium executable on the current system."""
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
    else:  # Linux
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
        # Also try `which`
        found = shutil.which("google-chrome") or shutil.which("chromium")
        if found:
            candidates.insert(0, found)

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _get_chrome_user_data_dir() -> str:
    """Return the user's default Chrome profile directory.

    Using the real Chrome user-data-dir preserves complete login state
    (cookies, localStorage, sessionStorage, IndexedDB) so Google services
    like Gemini work without re-authentication.

    Falls back to a dedicated directory if the default can't be found.
    """
    if platform.system() == "Darwin":
        default_dir = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    elif platform.system() == "Windows":
        default_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    else:  # Linux
        default_dir = Path.home() / ".config" / "google-chrome"

    if default_dir.exists():
        return str(default_dir)

    # Fallback: dedicated directory
    data_dir = GEMINI_STORAGE_DIR / "chrome_user_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return str(data_dir)


_chrome_process: subprocess.Popen | None = None


async def _launch_real_chrome(port: int = CDP_PORT) -> bool:
    """Launch real Chrome with remote debugging enabled.

    macOS Chrome REQUIRES a non-default --user-data-dir for CDP to work.
    Using the default dir (~/Library/Application Support/Google/Chrome) will
    silently skip the CDP port. Therefore we always use a dedicated CDP
    profile directory (~/.gemini_browser/chrome_cdp_profile).

    On first run the user needs to log in to Google in this CDP Chrome.
    Subsequent runs reuse the same profile so login persists.

    If Chrome is already running with CDP on the target port, we skip
    launching and reuse the existing instance.
    """
    global _chrome_process
    import socket

    chrome_path = _find_chrome_executable()
    if not chrome_path:
        print("⚠️  未找到系统 Chrome 浏览器")
        return False

    # Check if port is already in use (Chrome might already be running with CDP)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(1)
        sock.connect(("127.0.0.1", port))
        sock.close()
        print(f"✅ Chrome 已在端口 {port} 运行，直接连接")
        return True
    except (ConnectionRefusedError, OSError):
        pass
    finally:
        sock.close()

    # Always use a dedicated CDP profile directory (NOT the default Chrome dir).
    # macOS Chrome refuses to open the CDP port when using the default
    # user-data-dir — it prints "DevTools remote debugging requires a
    # non-default data directory" and silently skips the port.
    cdp_profile_dir = str(GEMINI_STORAGE_DIR / "chrome_cdp_profile")
    os.makedirs(cdp_profile_dir, exist_ok=True)

    cmd = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={cdp_profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        "--disable-popup-blocking",
        "--disable-translate",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--window-size=1440,900",
    ]

    proxy = _detect_proxy()
    if proxy:
        cmd.append(f"--proxy-server={proxy}")
        print(f"🌐 使用代理: {proxy}")

    print(f"🚀 启动 Chrome CDP (profile: {cdp_profile_dir})")
    _chrome_process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Wait for CDP port to become available
    for i in range(30):
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

    # If we get here, port didn't open — read stderr for clues
    if _chrome_process and _chrome_process.poll() is not None:
        stderr_out = _chrome_process.stderr.read().decode(errors="replace") if _chrome_process.stderr else ""
        if stderr_out:
            print(f"❌ Chrome 启动失败:\n{stderr_out[:500]}")

    print("❌ Chrome CDP 启动超时（30s 内端口未就绪）")
    return False


def _is_chrome_running() -> bool:
    """Check if Chrome is currently running."""
    try:
        if platform.system() == "Darwin":
            result = subprocess.run(
                ["pgrep", "-f", "Google Chrome"],
                capture_output=True, timeout=5
            )
            return result.returncode == 0
        elif platform.system() == "Windows":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
                capture_output=True, text=True, timeout=5
            )
            return "chrome.exe" in result.stdout
        else:
            result = subprocess.run(
                ["pgrep", "-f", "chrome"],
                capture_output=True, timeout=5
            )
            return result.returncode == 0
    except Exception:
        return False


def _cleanup_chrome():
    """Terminate the Chrome process we started."""
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


async def _create_browser_context(playwright, headless: bool = True, use_real_chrome: bool = True):
    """Create a Playwright browser context.

    Two modes:
    1. CDP mode (use_real_chrome=True, default): Launches the user's real Chrome
       with --remote-debugging-port and connects via CDP. This is completely
       undetectable because it IS a real Chrome browser.
    2. Stealth mode (use_real_chrome=False): Uses Playwright's built-in Chromium
       with comprehensive anti-detection JS injection and launch args.
    """
    # ── Mode 1: Connect to real Chrome via CDP ──
    if use_real_chrome:
        launched = await _launch_real_chrome(CDP_PORT)
        if launched:
            try:
                browser = await playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{CDP_PORT}"
                )
                # Use existing context if available, otherwise create new
                if browser.contexts:
                    context = browser.contexts[0]
                    print(f"🔗 已连接到真实 Chrome（复用已有上下文）")
                else:
                    context = await browser.new_context(
                        viewport={"width": 1440, "height": 900},
                    )
                    print(f"🔗 已连接到真实 Chrome（新建上下文）")

                # Import cookies from storage_state if this is a fresh profile
                storage_state = _get_storage_state_path()
                if storage_state:
                    await _import_cookies_to_context(context, storage_state)

                return browser, context
            except Exception as e:
                print(f"⚠️  CDP 连接失败: {e}，回退到 stealth 模式")

    # ── Mode 2: Stealth Playwright Chromium ──
    print("🛡️  使用 Stealth 模式启动 Playwright Chromium")
    proxy = _detect_proxy()

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=AutomationControlled",
        "--disable-infobars",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1440,900",
    ]

    launch_kwargs = {
        "headless": headless,
        "args": launch_args,
    }
    if proxy:
        print(f"🌐 使用代理: {proxy}")
        launch_kwargs["proxy"] = {"server": proxy}

    browser = await playwright.chromium.launch(**launch_kwargs)

    storage_state = _get_storage_state_path()
    context_kwargs = {
        "viewport": {"width": 1440, "height": 900},
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "locale": "zh-CN",
    }
    if storage_state:
        context_kwargs["storage_state"] = storage_state
        print(f"🔑 使用登录态: {storage_state}")
    else:
        print("⚠️  未找到登录态，将尝试无登录访问")

    context = await browser.new_context(**context_kwargs)

    # Inject stealth JS before any page loads
    await context.add_init_script(STEALTH_JS)
    print("🛡️  已注入反检测脚本")

    return browser, context


async def _import_cookies_to_context(context, storage_state_path: str):
    """Import cookies from a Playwright storage_state.json into a CDP context."""
    try:
        with open(storage_state_path, "r") as f:
            state = json.load(f)
        cookies = state.get("cookies", [])
        if cookies:
            # Filter to Google-related cookies
            google_cookies = [
                c for c in cookies
                if any(domain in c.get("domain", "")
                       for domain in [".google.com", "gemini.google.com", ".googleapis.com"])
            ]
            if google_cookies:
                await context.add_cookies(google_cookies)
                print(f"🍪 已导入 {len(google_cookies)} 个 Google cookies")
    except Exception as e:
        print(f"⚠️  Cookie 导入失败: {e}")


async def _save_gemini_storage(context):
    """Save browser session for future reuse."""
    GEMINI_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(GEMINI_STORAGE_PATH))


async def _navigate_to_gemini(page, timeout: int = 60000):
    """Navigate to Gemini and wait for the chat interface to load."""
    print("📥 正在打开 Gemini...")
    try:
        await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=timeout)
    except Exception:
        pass

    # Handle "You've been signed out" dialog or other modals
    await _dismiss_dialogs(page)

    # Wait for the chat input to appear (Quill editor inside rich-textarea)
    input_selectors = [
        'rich-textarea div[contenteditable="true"]',
        'div.ql-editor[contenteditable="true"]',
        'div[role="textbox"]',
        'div[contenteditable="true"]',
    ]

    for selector in input_selectors:
        try:
            await page.wait_for_selector(selector, timeout=20000)
            # Extra wait for JS initialization
            await page.wait_for_timeout(3000)
            # Dismiss any dialogs that appeared after load
            await _dismiss_dialogs(page)
            print(f"✅ Gemini 聊天界面已加载")
            return True
        except Exception:
            continue

    # Last resort: check if we're on a login page
    current_url = page.url
    if "accounts.google.com" in current_url:
        print("❌ 被重定向到 Google 登录页面，认证已失效")
        return False

    print("⚠️  聊天界面加载超时，但页面已打开，继续尝试...")
    return True


async def _dismiss_dialogs(page):
    """Dismiss common Gemini dialogs (signed out, consent, etc.)."""
    dialog_handlers = [
        # "You've been signed out" → click "Sign in" or dismiss
        ('button:has-text("Sign in")', "Sign in dialog"),
        ('button:has-text("登录")', "登录对话框"),
        # Cookie consent
        ('button:has-text("Accept")', "Cookie consent"),
        ('button:has-text("接受")', "Cookie 同意"),
        # "Got it" / "I understand" buttons
        ('button:has-text("Got it")', "Got it dialog"),
        ('button:has-text("知道了")', "知道了对话框"),
        # Generic close/dismiss buttons on modals
        ('mat-dialog-container button[aria-label="Close"]', "Modal close"),
        ('mat-dialog-container button[aria-label="关闭"]', "Modal 关闭"),
        # Dismiss overlay by pressing Escape
    ]

    for selector, name in dialog_handlers:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                print(f"  🔄 已处理: {name}")
                await page.wait_for_timeout(1000)
        except Exception:
            continue

    # Try pressing Escape to dismiss any remaining overlays
    try:
        overlay = page.locator(".cdk-overlay-container, .cdk-overlay-backdrop")
        if await overlay.count() > 0:
            visible = await overlay.first.is_visible(timeout=500)
            if visible:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
    except Exception:
        pass


async def _check_login_status(page) -> bool:
    """Check if we're logged in to Gemini."""
    current_url = page.url
    if "accounts.google.com" in current_url:
        return False

    # Check for user avatar or account indicator
    logged_in = await page.evaluate("""() => {
        // Look for user avatar image (indicates logged in)
        const avatars = document.querySelectorAll(
            'img[class*="avatar"], img[class*="profile"], ' +
            'img[data-src*="googleusercontent"]'
        );
        // Look for account button
        const accountBtn = document.querySelector(
            'a[aria-label*="Google Account"], a[aria-label*="Google 帐号"], ' +
            'button[aria-label*="Google Account"], button[aria-label*="Google 帐号"]'
        );
        return avatars.length > 0 || !!accountBtn;
    }""")

    return logged_in


async def _start_new_chat(page):
    """Start a new chat conversation in Gemini."""
    # Try clicking "New chat" button if available
    new_chat_selectors = [
        'button[aria-label*="New chat"]',
        'button[aria-label*="新对话"]',
        'button[aria-label*="新聊天"]',
        'a[href*="/app"]',
    ]
    for selector in new_chat_selectors:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await page.wait_for_timeout(2000)
                print("  🆕 已开启新对话")
                return
        except Exception:
            continue

    # Fallback: navigate to fresh URL
    try:
        await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    await page.wait_for_timeout(5000)


async def _verify_temporary_chat(page) -> bool:
    """Verify the current page is in Temporary chat mode.

    Checks for the 'Temporary chat' indicator text on the page,
    which appears in the main content area when a temporary session is active.
    """
    try:
        result = await page.evaluate("""() => {
            const body = document.body.innerText || '';
            // Check for Temporary chat indicator text
            if (body.includes('Temporary chat') || body.includes('临时对话') || body.includes('临时聊天')) {
                return true;
            }
            // Check for the temporary chat description text
            if (body.includes("don't appear in Recent Chats") || body.includes('不会出现在最近的聊天')) {
                return true;
            }
            // Check URL for temporary indicator
            if (window.location.href.includes('temporary')) {
                return true;
            }
            return false;
        }""")
        return result
    except Exception:
        return False


async def _click_temporary_chat_button(page) -> bool:
    """Try to click the Temporary chat button using multiple strategies.

    Returns True if a button was clicked (caller should verify with _verify_temporary_chat).
    """
    # Strategy 1: Direct aria-label / mattooltip selectors
    temp_chat_selectors = [
        'button[aria-label="Temporary chat"]',
        'button[aria-label="临时对话"]',
        'button[aria-label="临时聊天"]',
        'button[data-test-id="temporary-chat"]',
        'button[mattooltip="Temporary chat"]',
        'button[mattooltip="临时对话"]',
        'button[mattooltip="临时聊天"]',
        'button[title="Temporary chat"]',
        'button[title="临时对话"]',
    ]
    for sel in temp_chat_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                print(f"  ✅ 已点击 Temporary chat 按钮 ({sel})")
                return True
        except Exception:
            continue

    # Strategy 2: Find the icon button adjacent to "New chat" in the sidebar
    # From the screenshot, the temporary chat button is a small square icon
    # next to "New chat" text in the left sidebar
    try:
        found = await page.evaluate("""() => {
            // Find "New chat" text element
            const allElements = document.querySelectorAll('*');
            for (const el of allElements) {
                const text = el.textContent?.trim();
                if (text === 'New chat' || text === '新建对话' || text === '新聊天') {
                    // Look for sibling or nearby button elements
                    const parent = el.closest('div, nav, aside, section');
                    if (!parent) continue;
                    const buttons = parent.querySelectorAll('button');
                    for (const btn of buttons) {
                        const btnText = btn.textContent?.trim() || '';
                        // The temporary chat button is typically a small icon button
                        // (not the "New chat" button itself)
                        if (btnText === text) continue;
                        const rect = btn.getBoundingClientRect();
                        // Small button (icon-only, typically < 60px wide)
                        if (rect.width > 0 && rect.width < 60 && rect.height > 0 && rect.height < 60) {
                            const label = btn.getAttribute('aria-label') || '';
                            const tooltip = btn.getAttribute('mattooltip') || '';
                            const title = btn.getAttribute('title') || '';
                            const allAttrs = (label + ' ' + tooltip + ' ' + title).toLowerCase();
                            // Accept if it mentions temporary/临时, or if it's an unlabeled icon button near New chat
                            if (allAttrs.includes('temporary') || allAttrs.includes('临时') ||
                                (!btnText && rect.width < 50)) {
                                btn.click();
                                return true;
                            }
                        }
                    }
                }
            }
            return false;
        }""")
        if found:
            print("  ✅ 已通过 JS 点击 New chat 旁的 Temporary chat 图标")
            return True
    except Exception:
        pass

    # Strategy 3: Brute-force search all buttons for temporary-related attributes
    try:
        all_buttons = page.locator('button')
        count = await all_buttons.count()
        for i in range(count):
            btn = all_buttons.nth(i)
            try:
                if not await btn.is_visible(timeout=500):
                    continue
                label = await btn.get_attribute("aria-label") or ""
                title = await btn.get_attribute("title") or ""
                mat_tooltip = await btn.get_attribute("mattooltip") or ""
                combined = f"{label} {title} {mat_tooltip}".lower()
                if "temporary" in combined or "临时" in combined:
                    await btn.click()
                    print(f"  ✅ 已点击 (暴力搜索): {label or title or mat_tooltip}")
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


async def _open_temporary_chat(page):
    """Open a Temporary chat session to avoid polluting chat history.

    Uses a multi-strategy approach with verification:
    1. Navigate to Gemini home
    2. Expand sidebar if collapsed
    3. Click Temporary chat button (multiple strategies)
    4. Verify we're actually in a temporary session
    5. Retry if verification fails

    The key improvement: we VERIFY the session is temporary after clicking,
    and retry with alternative strategies if it's not.
    """
    print("🔄 创建 Temporary chat...")

    max_attempts = 3
    for attempt in range(max_attempts):
        if attempt > 0:
            print(f"  🔄 第 {attempt + 1} 次尝试创建 Temporary chat...")

        # Step 1: Navigate to Gemini home (fresh start each attempt)
        try:
            await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)
        await _dismiss_dialogs(page)

        # Check if already in temporary mode (e.g. from previous session)
        if await _verify_temporary_chat(page):
            print("  ✅ 已在 Temporary chat 模式")
            return True

        # Step 2: Expand sidebar if collapsed (hamburger menu)
        hamburger_selectors = [
            'button[aria-label="Open menu"]',
            'button[aria-label="打开菜单"]',
            'button[aria-label="Main menu"]',
            'button[aria-label="主菜单"]',
            'button[aria-label="Open navigation"]',
        ]
        for sel in hamburger_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    print("  ✅ 已展开侧边栏")
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

        # Step 3: Click Temporary chat button
        clicked = await _click_temporary_chat_button(page)

        if clicked:
            await page.wait_for_timeout(3000)
            await _dismiss_dialogs(page)

            # Step 4: Verify we're in temporary mode
            if await _verify_temporary_chat(page):
                print("  ✅ 已确认进入 Temporary chat 模式")
                return True
            else:
                print("  ⚠️  点击了按钮但未确认进入 Temporary chat，重试...")
                # Take debug screenshot
                try:
                    await page.screenshot(
                        path=f"/tmp/gemini_temp_chat_attempt_{attempt}.png",
                        full_page=False,
                    )
                except Exception:
                    pass
        else:
            print(f"  ⚠️  未找到 Temporary chat 按钮 (尝试 {attempt + 1}/{max_attempts})")
            # Take debug screenshot to help diagnose
            try:
                await page.screenshot(
                    path=f"/tmp/gemini_temp_chat_attempt_{attempt}.png",
                    full_page=False,
                )
                print(f"  📸 调试截图: /tmp/gemini_temp_chat_attempt_{attempt}.png")
            except Exception:
                pass

    # All attempts failed — fall back to normal new chat
    print("  ⚠️  多次尝试后仍未能创建 Temporary chat，使用普通新对话")
    await _start_new_chat(page)
    await page.wait_for_timeout(3000)
    return False


async def _enable_create_image_tool(page):
    """Click Tools menu and select 'Create image' to enable Nano Banana Pro."""
    print("🎨 启用 Create image 工具...")

    # Click "Tools" button
    tools_selectors = [
        'button:has-text("Tools")',
        'button[aria-label="Tools"]',
        'button[aria-label="工具"]',
        'button:has-text("工具")',
    ]
    tools_clicked = False
    for sel in tools_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=3000):
                await btn.click()
                tools_clicked = True
                print("  ✅ 已点击 Tools 菜单")
                break
        except Exception:
            continue

    if not tools_clicked:
        print("  ⚠️  未找到 Tools 按钮")
        return False

    await page.wait_for_timeout(1500)

    # Click "Create image"
    create_image_selectors = [
        'text="Create image"',
        'text="创建图片"',
        '[role="menuitem"]:has-text("Create image")',
        '[role="menuitem"]:has-text("创建图片")',
        'button:has-text("Create image")',
        'div:has-text("Create image")',
    ]
    img_clicked = False
    for sel in create_image_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                await el.click()
                img_clicked = True
                print("  ✅ 已选择 Create image")
                break
        except Exception:
            continue

    if not img_clicked:
        print("  ⚠️  未找到 Create image 选项")
        await page.keyboard.press("Escape")
        return False

    await page.wait_for_timeout(1500)
    return True


async def _switch_to_pro_model(page):
    """Switch model selector from 'Fast' (default) to 'Pro'.

    Pro model (Gemini 3.1 Pro) produces much better Chinese text rendering
    and higher quality images compared to the Fast model.
    """
    print("🔧 切换到 Pro 模型...")

    # Click model selector button (shows "Fast" by default)
    model_selector_selectors = [
        'button:has-text("Fast")',
        'button:has-text("fast")',
        'button[aria-label*="model"]',
        'button[aria-label*="Model"]',
        'button[aria-label*="模型"]',
    ]
    selector_clicked = False
    for sel in model_selector_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=3000):
                await btn.click()
                selector_clicked = True
                print("  ✅ 已点击模型选择器")
                break
        except Exception:
            continue

    if not selector_clicked:
        # Fallback: search all buttons for model name text
        try:
            btns = page.locator('button')
            count = await btns.count()
            for i in range(count):
                btn = btns.nth(i)
                try:
                    text = (await btn.inner_text()).strip().lower()
                    if text in ["fast", "thinking", "pro"]:
                        await btn.click()
                        selector_clicked = True
                        print(f"  ✅ 已点击模型选择器 (text='{text}')")
                        break
                except Exception:
                    continue
        except Exception:
            pass

    if not selector_clicked:
        print("  ⚠️  未找到模型选择器")
        return False

    await page.wait_for_timeout(1500)

    # Click "Pro" in the dropdown
    pro_selectors = [
        'text="Pro"',
        '[role="menuitem"]:has-text("Pro")',
        '[role="option"]:has-text("Pro")',
        'button:has-text("Pro")',
        'div:has-text("Pro"):not(:has-text("Product"))',
        'li:has-text("Pro")',
    ]
    pro_clicked = False
    for sel in pro_selectors:
        try:
            els = page.locator(sel)
            count = await els.count()
            for i in range(count):
                el = els.nth(i)
                try:
                    if not await el.is_visible(timeout=1000):
                        continue
                    text = (await el.inner_text()).strip()
                    if text.startswith("Pro") and "Product" not in text and "profile" not in text.lower():
                        await el.click()
                        pro_clicked = True
                        print("  ✅ 已选择 Pro 模型")
                        break
                except Exception:
                    continue
            if pro_clicked:
                break
        except Exception:
            continue

    if not pro_clicked:
        try:
            pro_item = page.locator('text="Pro"').first
            if await pro_item.is_visible(timeout=2000):
                await pro_item.click()
                pro_clicked = True
                print("  ✅ 已选择 Pro 模型 (fallback)")
        except Exception:
            pass

    if not pro_clicked:
        print("  ⚠️  未找到 Pro 选项")
        await page.keyboard.press("Escape")
        return False

    await page.wait_for_timeout(1500)

    # Verify
    try:
        current_model = await page.evaluate("""() => {
            const btns = document.querySelectorAll('button');
            for (const btn of btns) {
                const text = btn.innerText.trim().toLowerCase();
                if (text === 'pro' || text === 'fast' || text === 'thinking') {
                    return btn.innerText.trim();
                }
            }
            return 'unknown';
        }""")
        print(f"  📋 当前模型: {current_model}")
    except Exception:
        pass

    return pro_clicked


async def _download_via_button(page, output_path: str) -> bool:
    """Download FULL SIZE image by clicking the image to open the preview overlay,
    then clicking the 'Download full size' button in the top-right corner.

    Flow: click image → full-screen preview opens → click download icon (top-right) → save file.
    This downloads the original high-resolution image (7-8MB) instead of the thumbnail (~200KB).
    """
    print("  📥 尝试通过全屏预览下载原图...")

    # Scroll to bottom to ensure generated image is in view
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(1500)

    # Find the generated image and scroll it into view
    target_index = await page.evaluate("""() => {
        const imgs = document.querySelectorAll('img');
        const skipPatterns = ['avatar', 'icon', 'logo', 'profile', 'favicon',
                              'emoji', 'accounts.google', 'lh3.googleusercontent.com/a/'];
        for (let i = imgs.length - 1; i >= 0; i--) {
            const img = imgs[i];
            const src = img.src || '';
            if (!src) continue;
            if (skipPatterns.some(p => src.includes(p))) continue;
            const w = img.naturalWidth || img.width || 0;
            const h = img.naturalHeight || img.height || 0;
            const rect = img.getBoundingClientRect();
            if ((w >= 150 && h >= 150) || (rect.width >= 150 && rect.height >= 150)) {
                img.scrollIntoView({ behavior: 'instant', block: 'center' });
                return i;
            }
        }
        return -1;
    }""")

    if target_index < 0:
        print("  ⚠️  未找到目标图片元素")
        return False

    target_img = page.locator('img').nth(target_index)
    print(f"  📍 找到图片元素 (index={target_index})")

    await page.wait_for_timeout(1000)
    await target_img.scroll_into_view_if_needed()
    await page.wait_for_timeout(500)

    # === PRIMARY METHOD: Click image to open full-screen preview, then download full size ===
    print("  🔄 尝试点击图片展开全屏预览...")
    await target_img.click()
    await page.wait_for_timeout(3000)

    # Selectors for the "Download full size" button in the preview overlay
    # The button is typically in the top-right corner of the overlay
    fullsize_download_selectors = [
        # aria-label based (most reliable)
        'button[aria-label="Download full size"]',
        'button[aria-label="下载完整尺寸"]',
        'button[aria-label="Download"]',
        'button[aria-label="下载"]',
        'button[aria-label*="ownload full"]',
        'button[aria-label*="ownload"]',
        'button[aria-label*="下载"]',
        # tooltip/title based
        'button[title="Download full size"]',
        'button[title*="ownload"]',
        'button[data-tooltip="Download full size"]',
        'button[data-tooltip*="ownload"]',
        # Icon button patterns in overlay/dialog
        'a[aria-label*="ownload"]',
        'a[aria-label*="下载"]',
        'a[download]',
    ]

    for sel in fullsize_download_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                async with page.expect_download(timeout=60000) as download_info:
                    await btn.click()
                    print(f"  ✅ 已点击全屏预览下载按钮 ({sel})")
                download = await download_info.value
                await download.save_as(output_path)
                file_size = Path(output_path).stat().st_size
                print(f"  ✅ 原图下载完成 ({file_size // 1024}KB)")
                # Close the preview overlay
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
                return True
        except Exception as e:
            print(f"  ⚠️  全屏预览 {sel} 尝试失败: {e}")
            continue

    # Try broader search: find any download-like icon button in the overlay
    try:
        # Look for download icon (typically an SVG with download arrow) in visible overlay
        downloaded_via_icon = await page.evaluate("""async () => {
            // Look for buttons with download icons in the overlay area
            const buttons = document.querySelectorAll('button, a');
            for (const btn of buttons) {
                const rect = btn.getBoundingClientRect();
                // The download button is in the top-right area of the viewport
                if (rect.top < 100 && rect.right > window.innerWidth - 200 && rect.width < 80) {
                    const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                    const title = (btn.getAttribute('title') || '').toLowerCase();
                    const tooltip = (btn.getAttribute('data-tooltip') || '').toLowerCase();
                    const text = (btn.textContent || '').toLowerCase().trim();
                    if (ariaLabel.includes('download') || ariaLabel.includes('下载') ||
                        title.includes('download') || title.includes('下载') ||
                        tooltip.includes('download') || tooltip.includes('下载') ||
                        text.includes('download') || text.includes('下载')) {
                        btn.click();
                        return true;
                    }
                }
            }
            return false;
        }""")

        if downloaded_via_icon:
            print("  ✅ 已通过 JS 点击预览区下载按钮")
            try:
                download = await page.wait_for_event("download", timeout=60000)
                await download.save_as(output_path)
                file_size = Path(output_path).stat().st_size
                print(f"  ✅ 原图下载完成 ({file_size // 1024}KB)")
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
                return True
            except Exception as e:
                print(f"  ⚠️  等待下载事件失败: {e}")
    except Exception as e:
        print(f"  ⚠️  JS 方式查找下载按钮失败: {e}")

    # Take a debug screenshot of the preview overlay to help diagnose
    try:
        debug_path = "/tmp/gemini_preview_overlay_debug.png"
        await page.screenshot(path=debug_path, full_page=False)
        print(f"  📸 预览界面调试截图: {debug_path}")
    except Exception:
        pass

    # Close preview overlay before trying fallback
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(1000)

    # === FALLBACK: Hover over image to reveal inline download button ===
    print("  🔄 回退到 hover 下载方式...")
    await target_img.scroll_into_view_if_needed()
    await page.wait_for_timeout(500)
    await target_img.hover()
    await page.wait_for_timeout(2000)

    hover_download_selectors = [
        'button[aria-label="Download"]',
        'button[aria-label="下载"]',
        'button[aria-label*="ownload"]',
        'button[aria-label*="下载"]',
        'a[download]',
        'a[aria-label*="ownload"]',
        'a[aria-label*="下载"]',
    ]

    for sel in hover_download_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                async with page.expect_download(timeout=30000) as download_info:
                    await btn.click()
                    print(f"  ✅ 已点击 hover 下载按钮 ({sel})")
                download = await download_info.value
                await download.save_as(output_path)
                print(f"  ⚠️  注意：hover 下载可能是缩略图")
                return True
        except Exception as e:
            continue

    return False


async def _type_prompt(page, prompt: str):
    """Type the prompt into the Gemini chat input.

    Primary method: execCommand('insertText') — fast and reliable, triggers
    the Quill editor's internal state update in most cases.

    Fallback: keyboard.type() with chunking for long prompts.

    Newlines are replaced with spaces to prevent Enter-triggered premature
    send in Gemini's chat input.
    """
    # Find the contenteditable input
    input_selectors = [
        'rich-textarea div[contenteditable="true"]',
        'div.ql-editor[contenteditable="true"]',
        'div[role="textbox"]',
        'div[contenteditable="true"]',
    ]

    input_el = None
    for selector in input_selectors:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=3000):
                input_el = el
                break
        except Exception:
            continue

    if not input_el:
        raise RuntimeError("无法找到 Gemini 输入框")

    # Click to focus
    await input_el.click()
    await page.wait_for_timeout(500)

    # Clear any existing content
    await page.keyboard.press("Meta+a")
    await page.keyboard.press("Backspace")
    await page.wait_for_timeout(300)

    # Sanitize prompt: replace newlines with spaces to prevent Enter-send
    safe_prompt = prompt.replace("\n", " ").replace("\r", " ")
    while "  " in safe_prompt:
        safe_prompt = safe_prompt.replace("  ", " ")
    safe_prompt = safe_prompt.strip()
    prompt_len = len(safe_prompt)

    # Primary: execCommand insertText (fast, no typing delay)
    await page.evaluate("""(text) => {
        const el = document.querySelector(
            'rich-textarea div[contenteditable="true"], ' +
            'div.ql-editor[contenteditable="true"], ' +
            'div[role="textbox"], ' +
            'div[contenteditable="true"]'
        );
        if (el) {
            el.focus();
            document.execCommand('insertText', false, text);
        }
    }""", safe_prompt)
    await page.wait_for_timeout(1000)

    # Verify text was entered
    content = await page.evaluate("""() => {
        const el = document.querySelector(
            'rich-textarea div[contenteditable="true"], ' +
            'div.ql-editor[contenteditable="true"], ' +
            'div[role="textbox"]'
        );
        return el ? el.innerText.trim().length : 0;
    }""")

    if content >= prompt_len * 0.5:
        print(f"  ✅ 文本已输入（{content} 字符）")
    else:
        # Fallback: keyboard.type() with chunking
        print(f"  ⚠️  execCommand 输入不完整（{content}/{prompt_len}），用 keyboard.type 重试...")
        await page.keyboard.press("Meta+a")
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(300)
        chunk_size = 200
        for start in range(0, prompt_len, chunk_size):
            chunk = safe_prompt[start:start + chunk_size]
            await page.keyboard.type(chunk, delay=1)
            await page.wait_for_timeout(100)
        await page.wait_for_timeout(500)
        content = await page.evaluate("""() => {
            const el = document.querySelector(
                'rich-textarea div[contenteditable="true"], ' +
                'div.ql-editor[contenteditable="true"], ' +
                'div[role="textbox"]'
            );
            return el ? el.innerText.trim().length : 0;
        }""")
        print(f"  ✅ 重新输入后 {content} 字符")


async def _submit_prompt(page):
    """Submit the prompt by clicking the send button or pressing Enter.

    Primary: click the send button (more reliable with execCommand input).
    Fallback: press Enter.
    """
    # Primary: try clicking the send button
    send_selectors = [
        'button[aria-label="Send message"]',
        'button[aria-label="发送消息"]',
        'button[aria-label*="Send"]',
        'button[aria-label*="发送"]',
        'button.send-button',
        'button[data-test-id="send-button"]',
    ]
    for selector in send_selectors:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                print("  📤 已点击发送按钮")
                await page.wait_for_timeout(2000)
                return True
        except Exception:
            continue

    # Fallback: press Enter
    await page.keyboard.press("Enter")
    print("  📤 已发送提示词（Enter fallback）")
    await page.wait_for_timeout(2000)

    # Verify the message was sent
    still_welcome = await page.evaluate("""() => {
        const body = document.body.innerText || '';
        return body.includes('Where should we start') ||
               body.includes('从哪里开始');
    }""")

    if not still_welcome:
        return True

    print("  ⚠️  发送按钮不可见，消息可能未发送")
    return False


async def _wait_for_image(page, timeout: int = IMAGE_GENERATION_TIMEOUT) -> list[dict]:
    """Wait for image(s) to appear in the Gemini response.

    Returns list of dicts with 'src' (URL or data URI) and 'alt' keys.
    """
    print(f"  ⏳ 等待图片生成（最长 {timeout}s）...")
    start = time.time()
    last_status = ""

    # First, wait briefly to confirm the message was actually sent
    # (the welcome text should disappear within a few seconds)
    await page.wait_for_timeout(3000)

    while time.time() - start < timeout:
        # Check for generated images in the response
        result = await page.evaluate("""() => {
            // Check if the model response area exists (message was sent)
            const hasResponse = !!document.querySelector(
                'model-response, .model-response-text, ' +
                'message-content, .response-container, ' +
                '[class*="response"], [class*="model-turn"], ' +
                '.conversation-container message-content'
            );

            // Broadly search for images that look like generated content
            const allImages = [];

            // Search entire page for substantial images
            const imgs = document.querySelectorAll('img');
            for (const img of imgs) {
                const src = img.src || '';
                if (!src) continue;

                // Skip known UI elements
                if (src.includes('avatar') || src.includes('icon') ||
                    src.includes('logo') || src.includes('profile') ||
                    src.includes('favicon') || src.includes('emoji') ||
                    src.includes('accounts.google') ||
                    src.includes('lh3.googleusercontent.com/a/')) continue;

                // Include images from known generation sources
                const isGenerated =
                    src.startsWith('blob:') ||
                    src.startsWith('data:image') ||
                    src.includes('lh3.googleusercontent.com') ||
                    src.includes('generated') ||
                    src.includes('gstatic');

                // Check size — generated images are substantial
                const w = img.naturalWidth || img.width || 0;
                const h = img.naturalHeight || img.height || 0;
                const rect = img.getBoundingClientRect();

                // Skip tiny images (icons, avatars)
                if (w > 0 && w < 80 && h > 0 && h < 80) continue;
                if (rect.width < 80 && rect.height < 80) continue;

                // Include if it's from a known generation source
                // or if it's a reasonably large image
                if (isGenerated || (w >= 200 && h >= 200) ||
                    (rect.width >= 200 && rect.height >= 200)) {
                    allImages.push({
                        src: src,
                        alt: img.alt || '',
                        width: w || Math.round(rect.width),
                        height: h || Math.round(rect.height),
                    });
                }
            }

            // Also check for canvas elements
            const canvases = document.querySelectorAll('canvas');
            for (const canvas of canvases) {
                if (canvas.width < 100 || canvas.height < 100) continue;
                try {
                    const dataUrl = canvas.toDataURL('image/png');
                    if (dataUrl && dataUrl.length > 5000) {
                        allImages.push({
                            src: dataUrl,
                            alt: 'canvas-rendered',
                            width: canvas.width,
                            height: canvas.height,
                        });
                    }
                } catch(e) {}
            }

            // Check if model is still generating
            // IMPORTANT: Avoid matching `bard-avatar ... thinking` (avatar animation
            // that is always visible). Use precise selectors instead of broad wildcards.
            let isThinking = false;

            // Method 1: Check for specific Gemini generation indicators
            // "Loading Nano Banana Pro..." text or similar tool loading messages
            const bodyText = document.body.innerText || '';
            if (bodyText.includes('Loading Nano Banana') ||
                bodyText.includes('Loading tool') ||
                bodyText.includes('Generating image')) {
                isThinking = true;
            }

            // Method 2: Check for the stop button (blue square) which appears during generation
            if (!isThinking) {
                const stopBtns = document.querySelectorAll('button[aria-label="Stop response"], button[aria-label="Stop"]');
                for (const btn of stopBtns) {
                    if (btn.offsetParent !== null) {
                        isThinking = true;
                        break;
                    }
                }
            }

            // Method 3: Check specific thinking/loading indicators (exclude avatar)
            if (!isThinking) {
                const thinkingSelectors = [
                    '.thinking-indicator', '.loading-indicator',
                    '.model-response-generating', '.response-streaming',
                    'mat-progress-bar[mode="indeterminate"]',
                    '.loading-content-spinner-container',
                ];
                for (const sel of thinkingSelectors) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) {
                        isThinking = true;
                        break;
                    }
                }
            }

            // Check for error messages
            const errorEl = document.querySelector(
                '.error-message, [class*="error-text"], ' +
                '.warning-message, [class*="blocked"]'
            );
            let errorText = '';
            if (errorEl && errorEl.offsetParent !== null) {
                errorText = errorEl.innerText.trim().substring(0, 200);
            }

            return {
                images: allImages,
                isThinking: isThinking,
                hasResponse: hasResponse,
                errorText: errorText,
            };
        }""")

        images = result.get("images", [])
        is_thinking = result.get("isThinking", False)
        has_response = result.get("hasResponse", False)
        error_text = result.get("errorText", "")

        # Status update
        elapsed = int(time.time() - start)
        status = f"[{elapsed}s]"
        if images:
            status += f" 发现 {len(images)} 张图片"
        elif is_thinking:
            status += " 模型生成中..."
        elif has_response:
            status += " 已有响应，等待图片..."
        elif error_text:
            status += f" 错误: {error_text[:80]}"
        else:
            status += " 等待响应..."

        if status != last_status:
            print(f"  {status}")
            last_status = status

        if images:
            return images

        if error_text and not is_thinking:
            print(f"  ❌ 模型返回错误: {error_text[:200]}")
            return []

        # Take periodic debug screenshots at key intervals
        if elapsed in (30, 90, 180):
            try:
                await page.screenshot(
                    path=f"/tmp/gemini_progress_{elapsed}s.png", full_page=False
                )
                print(f"  📸 进度截图: /tmp/gemini_progress_{elapsed}s.png")
            except Exception:
                pass

        # If model finished (was thinking but stopped), give extra time for images to load
        if has_response and not is_thinking and elapsed > 10:
            print("  ⏳ 模型已完成响应，额外等待 15s 让图片加载...")
            await page.wait_for_timeout(15000)
            # Re-check for images after waiting
            continue

        await page.wait_for_timeout(POLL_INTERVAL * 1000)

    print(f"  ⏰ 等待超时（{timeout}s），尝试提取当前页面图片...")
    return []


async def _download_image(page, img_info: dict, output_path: str) -> bool:
    """Download an image from URL or data URI and save to disk."""
    src = img_info.get("src", "")

    if not src:
        return False

    try:
        if src.startswith("data:image"):
            # Base64 data URI
            match = re.match(r"data:image/\w+;base64,(.*)", src)
            if match:
                img_data = base64.b64decode(match.group(1))
                Path(output_path).write_bytes(img_data)
                return True
        elif src.startswith("blob:"):
            # Blob URL — need to convert via canvas in browser
            img_data_b64 = await page.evaluate(
                """async (blobUrl) => {
                try {
                    const resp = await fetch(blobUrl);
                    const blob = await resp.blob();
                    return new Promise((resolve) => {
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result);
                        reader.readAsDataURL(blob);
                    });
                } catch(e) { return null; }
            }""",
                src,
            )
            if img_data_b64:
                match = re.match(r"data:image/\w+;base64,(.*)", img_data_b64)
                if match:
                    img_data = base64.b64decode(match.group(1))
                    Path(output_path).write_bytes(img_data)
                    return True
        else:
            # HTTP(S) URL — download via page context (inherits cookies)
            response = await page.request.get(src)
            if response.ok:
                content = await response.body()
                if len(content) > 500:
                    Path(output_path).write_bytes(content)
                    return True
    except Exception as e:
        print(f"  ⚠️  下载异常: {e}")

    return False


async def _try_download_via_menu(page, img_info: dict, output_path: str) -> bool:
    """Try to download image using Gemini's built-in download/export option.

    Gemini often provides a download button or context menu for generated images.
    """
    try:
        # Try to find and click on the image first
        img_src = img_info.get("src", "")
        if not img_src:
            return False

        # Look for download buttons near generated images
        downloaded = await page.evaluate(
            """async (imgSrc) => {
            // Find the image element
            const imgs = document.querySelectorAll('img');
            let targetImg = null;
            for (const img of imgs) {
                if (img.src === imgSrc) {
                    targetImg = img;
                    break;
                }
            }
            if (!targetImg) return null;

            // Look for nearby download/save buttons
            const parent = targetImg.closest(
                '.image-container, .generated-image-container, ' +
                '[class*="image-wrapper"], [class*="card"]'
            ) || targetImg.parentElement;

            if (!parent) return null;

            const downloadBtn = parent.querySelector(
                'button[aria-label*="download"], button[aria-label*="下载"], ' +
                'button[aria-label*="save"], button[aria-label*="保存"], ' +
                'a[download], a[href*="download"]'
            );

            if (downloadBtn) {
                // Get the download URL if it's a link
                const href = downloadBtn.getAttribute('href');
                if (href) return href;
            }

            // Try to extract image as data URL via canvas
            try {
                const canvas = document.createElement('canvas');
                canvas.width = targetImg.naturalWidth || targetImg.width;
                canvas.height = targetImg.naturalHeight || targetImg.height;
                const ctx = canvas.getContext('2d');
                ctx.drawImage(targetImg, 0, 0);
                return canvas.toDataURL('image/png');
            } catch(e) {
                // CORS may block this
                return null;
            }
        }""",
            img_src,
        )

        if downloaded and downloaded.startswith("data:image"):
            match = re.match(r"data:image/\w+;base64,(.*)", downloaded)
            if match:
                img_data = base64.b64decode(match.group(1))
                Path(output_path).write_bytes(img_data)
                return True
        elif downloaded and downloaded.startswith("http"):
            response = await page.request.get(downloaded)
            if response.ok:
                content = await response.body()
                Path(output_path).write_bytes(content)
                return True
    except Exception:
        pass
    return False


async def _screenshot_response_images(page, output_path: str) -> bool:
    """Fallback: screenshot the generated image area as a last resort."""
    # Try finding large images anywhere on page (not just in response containers)
    try:
        # Find large images that are likely generated content
        large_img = page.locator("img").filter(
            has=page.locator(":scope")
        )
        count = await large_img.count()
        for i in range(count - 1, -1, -1):
            img = large_img.nth(i)
            try:
                if not await img.is_visible(timeout=1000):
                    continue
                box = await img.bounding_box()
                if box and box["width"] >= 200 and box["height"] >= 200:
                    await img.screenshot(path=output_path)
                    print(f"  📸 使用截图方式保存图片")
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # Fallback: screenshot the last response container
    try:
        response_el = page.locator(
            "model-response, .model-response-text, "
            "message-content, .response-container, "
            "[class*='response'], [class*='model-turn']"
        ).last
        if await response_el.is_visible(timeout=3000):
            await response_el.screenshot(path=output_path)
            print(f"  📸 使用响应区域截图方式保存")
            return True
    except Exception:
        pass

    return False


async def test_auth(headless: bool = True, use_real_chrome: bool = True) -> bool:
    """Test if Google login session is valid for Gemini."""
    from playwright.async_api import async_playwright

    storage = _get_storage_state_path()
    if not storage and not use_real_chrome:
        print("❌ 未找到登录态文件")
        print(f"   预期路径: {DEFAULT_STORAGE_PATH}")
        print(f"   或: {GEMINI_STORAGE_PATH}")
        return False

    try:
        async with async_playwright() as p:
            browser, context = await _create_browser_context(
                p, headless=headless, use_real_chrome=use_real_chrome
            )
            page = await context.new_page()

            loaded = await _navigate_to_gemini(page)
            if not loaded:
                await browser.close()
                return False

            logged_in = await _check_login_status(page)
            if logged_in:
                print("✅ Gemini 认证有效")
                try:
                    await _save_gemini_storage(context)
                except Exception:
                    pass
            else:
                print("❌ 认证无效或已过期")

            # Take debug screenshot
            await page.screenshot(path="/tmp/gemini_auth_check.png")
            print(f"📸 调试截图: /tmp/gemini_auth_check.png")

            await browser.close()
            return logged_in
    finally:
        _cleanup_chrome()


async def interactive_login():
    """Open Chrome with our automation profile so the user can log in manually.

    This only needs to be done once. After logging in, the Chrome profile
    saves the complete session (cookies, localStorage, IndexedDB, etc.)
    which will be reused by all subsequent generate_images calls.
    """
    from playwright.async_api import async_playwright

    print("🔐 登录 Google 账号到 Gemini")
    print("=" * 50)
    print("将打开一个 Chrome 窗口，请在其中登录 Google 账号。")
    print("登录完成后，关闭该窗口或在终端按 Ctrl+C 退出。")
    print("=" * 50)

    # Use the dedicated CDP profile directory (same as _launch_real_chrome)
    cdp_profile_dir = str(GEMINI_STORAGE_DIR / "chrome_cdp_profile")
    os.makedirs(cdp_profile_dir, exist_ok=True)

    chrome_path = _find_chrome_executable()
    if not chrome_path:
        print("❌ 未找到 Chrome 浏览器")
        return False

    proxy = _detect_proxy()
    cmd = [
        chrome_path,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={cdp_profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1440,900",
        "https://gemini.google.com/app",
    ]
    if proxy:
        cmd.append(f"--proxy-server={proxy}")

    print(f"\n🚀 正在打开 Chrome...")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Wait for user to log in
    print("⏳ 等待登录完成（Chrome 关闭后自动退出）...\n")
    try:
        proc.wait()  # Blocks until Chrome is closed
    except KeyboardInterrupt:
        print("\n🛑 用户中断")
        proc.terminate()

    # Verify login worked
    print("\n🔍 验证登录状态...")
    try:
        async with async_playwright() as p:
            launched = await _launch_real_chrome(CDP_PORT)
            if not launched:
                print("❌ 无法启动 Chrome 验证")
                return False

            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()

            await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)
            await _dismiss_dialogs(page)

            logged_in = await _check_login_status(page)
            if logged_in:
                print("✅ 登录成功！后续可直接使用 generate 命令。")
            else:
                print("❌ 登录可能未完成，请重试 login 命令。")

            await page.screenshot(path="/tmp/gemini_login_check.png")
            print(f"📸 截图: /tmp/gemini_login_check.png")

            await browser.close()
            return logged_in
    finally:
        _cleanup_chrome()


async def generate_images(
    charts: list[dict],
    output_dir: str,
    delay_between: int = 15,
    max_retries: int = 2,
    headless: bool = True,
    use_real_chrome: bool = True,
):
    """Generate images from a list of chart definitions using browser automation.

    For each image, the flow is:
    1. Open Temporary chat (avoid polluting chat history)
    2. Enable Create image tool (Nano Banana Pro)
    3. Switch to Pro model (critical for Chinese text quality)
    4. Input prompt and send
    5. Wait for image generation
    6. Download via hover+download button (primary) or fallback methods

    Args:
        charts: List of dicts with 'prompt' and 'filename' keys.
        output_dir: Directory to save downloaded PNG files.
        delay_between: Seconds to wait between requests.
        max_retries: Max retry attempts per chart.
        headless: Run browser in headless mode (only applies to stealth mode).
        use_real_chrome: If True (default), connects to real Chrome via CDP
                         to avoid automation detection. Falls back to stealth
                         mode if Chrome is not available.
    """
    from playwright.async_api import async_playwright

    os.makedirs(output_dir, exist_ok=True)
    total = len(charts)

    try:
        async with async_playwright() as p:
            browser, context = await _create_browser_context(
                p, headless=headless, use_real_chrome=use_real_chrome
            )
            page = await context.new_page()

            # Inject stealth JS for CDP mode pages too (belt-and-suspenders)
            if use_real_chrome:
                try:
                    await page.add_init_script(STEALTH_JS)
                except Exception:
                    pass

            loaded = await _navigate_to_gemini(page)
            if not loaded:
                print("❌ 无法打开 Gemini，请检查网络和登录状态")
                await browser.close()
                return []

            logged_in = await _check_login_status(page)
            if not logged_in:
                print("⚠️  可能未登录，继续尝试...")

            print(f"\n=== 开始生成 {total} 张图片（浏览器 CDP 模式）===\n")
            results = []

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
                        wait = 20 * (2 ** (attempt - 1))
                        print(f"  第 {attempt+1} 次重试，等待 {wait}s ...")
                        await asyncio.sleep(wait)

                    try:
                        # Step 1: Open Temporary chat for each image
                        is_temporary = await _open_temporary_chat(page)
                        if not is_temporary:
                            print("  ⚠️  未能确认进入 Temporary chat，但继续生成...")

                        # Step 2: Enable Create image tool
                        await _enable_create_image_tool(page)

                        # Step 3: Switch to Pro model (critical for Chinese quality)
                        await _switch_to_pro_model(page)

                        # Step 4: Type and send prompt
                        await _type_prompt(page, prompt)
                        await page.wait_for_timeout(500)
                        sent = await _submit_prompt(page)

                        if not sent:
                            print("  ⚠️  消息可能未发送，跳到下一次重试")
                            continue

                        # Step 5: Wait for image generation
                        images = await _wait_for_image(page)

                        if images:
                            # Step 6: Download image
                            # Primary: hover + download button
                            downloaded = False
                            try:
                                downloaded = await _download_via_button(
                                    page, output_path
                                )
                            except Exception as e:
                                print(f"  ⚠️  下载按钮方式失败: {e}")

                            # Fallback 1: direct download (fetch/blob/base64)
                            if not downloaded:
                                img = images[0]
                                downloaded = await _download_image(
                                    page, img, output_path
                                )

                            # Fallback 2: download menu
                            if not downloaded:
                                downloaded = await _try_download_via_menu(
                                    page, images[0], output_path
                                )

                            # Fallback 3: screenshot
                            if not downloaded:
                                downloaded = await _screenshot_response_images(
                                    page, output_path
                                )

                            if downloaded:
                                file_size = Path(output_path).stat().st_size
                                print(
                                    f"  ✅ 已保存: {output_path} ({file_size // 1024}KB)"
                                )
                                results.append(
                                    {
                                        "filename": filename,
                                        "status": "success",
                                        "path": output_path,
                                    }
                                )
                                success = True
                                break
                            else:
                                print(f"  ⚠️  图片下载失败，将重试")
                        else:
                            debug_path = f"/tmp/gemini_debug_{filename}"
                            await page.screenshot(path=debug_path, full_page=False)
                            print(f"  ⚠️  未检测到图片，调试截图: {debug_path}")

                    except Exception as e:
                        print(f"  ❌ 生成异常: {e}")
                        try:
                            await page.screenshot(
                                path=f"/tmp/gemini_error_{filename}", full_page=False
                            )
                        except Exception:
                            pass

                if not success:
                    print(f"  ❌ {filename} 多次重试仍失败")
                    results.append({"filename": filename, "status": "failed"})

                # Wait between requests
                if i < total - 1:
                    print(f"  等待 {delay_between}s ...\n")
                    await asyncio.sleep(delay_between)

            # Save storage for future use
            try:
                await _save_gemini_storage(context)
            except Exception:
                pass
            await browser.close()

    finally:
        # Clean up Chrome process if we launched it
        _cleanup_chrome()

    # Summary
    succeeded = sum(1 for r in results if r["status"] == "success")
    print(f"\n=== 完成: {succeeded}/{total} 张图片生成成功 ===")
    for r in results:
        status = "✅" if r["status"] == "success" else "❌"
        print(f"  {status} {r['filename']}")

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Generate images via Gemini web app (browser automation, fallback mode)"
    )
    parser.add_argument(
        "--storage",
        type=Path,
        default=None,
        help="Path to storage_state.json (default: auto-detect)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show browser window (useful for debugging)",
    )
    parser.add_argument(
        "--stealth",
        action="store_true",
        help="Use Playwright stealth mode instead of real Chrome CDP mode",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # auth - check authentication
    subparsers.add_parser(
        "auth",
        help="Check Gemini authentication (uses NotebookLM storage)",
    )

    # login - interactive login to Google in automation Chrome profile
    subparsers.add_parser(
        "login",
        help="打开 Chrome 让用户登录 Google 账号（仅需执行一次）",
    )

    # generate - generate images
    gen_parser = subparsers.add_parser(
        "generate", help="Generate images from config or prompt file"
    )
    gen_parser.add_argument("--config", help="JSON config file with chart definitions")
    gen_parser.add_argument("--prompt-file", help="Single prompt text file")
    gen_parser.add_argument("--output-dir", required=True, help="Output directory")
    gen_parser.add_argument(
        "--filename",
        default="output.png",
        help="Output filename (for --prompt-file mode)",
    )
    gen_parser.add_argument(
        "--delay", type=int, default=15, help="Delay between requests in seconds"
    )
    gen_parser.add_argument(
        "--max-retries", type=int, default=2, help="Max retries per chart"
    )

    args = parser.parse_args()
    headless = not args.no_headless
    use_real_chrome = not args.stealth

    # Override storage path if specified
    if args.storage:
        global DEFAULT_STORAGE_PATH
        DEFAULT_STORAGE_PATH = args.storage

    if args.command == "auth":
        asyncio.run(test_auth(headless=headless, use_real_chrome=use_real_chrome))

    elif args.command == "login":
        asyncio.run(interactive_login())

    elif args.command == "generate":
        if not use_real_chrome:
            storage = _get_storage_state_path()
            if not storage:
                print("❌ 未找到登录态")
                print(f"   预期路径: {DEFAULT_STORAGE_PATH}")
                print("   请确保已通过 NotebookLM 登录过 Google 账号")
                sys.exit(1)

        if args.config:
            with open(args.config, "r", encoding="utf-8") as f:
                charts = json.load(f)
        elif args.prompt_file:
            with open(args.prompt_file, "r", encoding="utf-8") as f:
                prompt = f.read().strip()
            charts = [{"prompt": prompt, "filename": args.filename}]
        else:
            print("❌ 请指定 --config 或 --prompt-file")
            sys.exit(1)

        asyncio.run(
            generate_images(
                charts,
                args.output_dir,
                delay_between=args.delay,
                max_retries=args.max_retries,
                headless=headless,
                use_real_chrome=use_real_chrome,
            )
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
