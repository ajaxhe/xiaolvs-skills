#!/usr/bin/env python3
from __future__ import annotations
"""
Publish image-card posts (贴图) to WeChat Official Account (微信公众号)
via pure Playwright.

No browser-use / LLM dependency. Uses Playwright for ALL interactions:
login, navigation, image upload, form filling, collection selection, saving.

Requires:
  - playwright (pip install playwright && playwright install chromium)

Usage:
    # First-time login (scan QR code)
    python wechat_mp_publish_playwright.py login

    # Check login status
    python wechat_mp_publish_playwright.py check

    # Inspect DOM structure (dev helper)
    python wechat_mp_publish_playwright.py inspect

    # Publish from a JSON config file
    python wechat_mp_publish_playwright.py publish --config <path_to_config.json>

Config JSON format:
{
    "title": "贴图标题",
    "images": ["path/to/img1.png", "path/to/img2.png"],
    "description": "可选描述",
    "tags": ["标签1", "标签2"],
    "collection": "可选合集名称（如 AI前沿）"
}
"""
import argparse
import asyncio
import json
import math
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright, Page

# Storage state file for WeChat MP login session
WX_STORAGE_DIR = Path.home() / ".wechat_mp"
WX_STORAGE_PATH = WX_STORAGE_DIR / "storage_state.json"

# WeChat MP URLs
WX_MP_URL = "https://mp.weixin.qq.com"
WX_MP_HOME_URL = "https://mp.weixin.qq.com/cgi-bin/home"

BROWSER_OPTS = dict(
    viewport={"width": 1440, "height": 900},
    user_agent=(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
)


def _wx_char_count(text: str) -> int:
    """Calculate title length using WeChat's rule: CJK/fullwidth=1, ASCII=0.5 (ceil)."""
    count = 0.0
    for ch in text:
        if ord(ch) <= 127:
            count += 0.5
        else:
            count += 1.0
    return math.ceil(count)


def _wx_truncate(text: str, max_chars: int = 20) -> str:
    """Truncate text to fit WeChat's max_chars limit."""
    result = []
    count = 0.0
    for ch in text:
        inc = 0.5 if ord(ch) <= 127 else 1.0
        if count + inc > max_chars:
            break
        result.append(ch)
        count += inc
    return "".join(result)


async def _goto_home_and_extract_token(page: Page) -> str | None:
    """Navigate to WeChat MP home page and extract the token from URL."""
    await page.goto(WX_MP_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    current_url = page.url
    if "login" in current_url.lower() or current_url.rstrip("/") == WX_MP_URL:
        return None

    parsed = urlparse(current_url)
    token_list = parse_qs(parsed.query).get("token", [])
    if token_list:
        return token_list[0]

    try:
        token = await page.evaluate(
            "() => { const m = location.search.match(/token=(\\d+)/); return m ? m[1] : null; }"
        )
        if token:
            return str(token)
    except Exception:
        pass

    return None


def _build_tietie_edit_url(token: str) -> str:
    """Build the 贴图 editor URL with token."""
    ts = int(time.time() * 1000)
    return (
        "https://mp.weixin.qq.com/cgi-bin/appmsg"
        f"?t=media/appmsg_edit_v2&action=edit&isNew=1&type=77"
        f"&createType=8&token={token}&lang=zh_CN&timestamp={ts}"
    )


async def login(headless: bool = False):
    """Login to WeChat MP via QR code scan."""
    WX_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(**BROWSER_OPTS)
        page = await context.new_page()
        await page.goto(WX_MP_URL)

        print("=" * 60)
        print("请在浏览器中扫码登录微信公众号后台")
        print("登录成功后会自动保存 session，后续无需重新登录")
        print("=" * 60)

        try:
            await page.wait_for_url("**/cgi-bin/home**", timeout=120_000)
        except Exception:
            try:
                await page.wait_for_selector(
                    ".weui-desktop-account__nickname, .main_bd, #app",
                    timeout=120_000,
                )
            except Exception:
                print("⚠️  登录超时，请在 2 分钟内完成扫码")
                await browser.close()
                return False

        await page.wait_for_timeout(3000)
        await context.storage_state(path=str(WX_STORAGE_PATH))
        print(f"\n✅ 登录成功！Session 已保存到 {WX_STORAGE_PATH}")
        await browser.close()
        return True


async def check_login() -> bool:
    """Check if the saved session is still valid."""
    if not WX_STORAGE_PATH.exists():
        print("❌ 未找到登录 session，请先运行: python wechat_mp_publish_playwright.py login")
        return False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=str(WX_STORAGE_PATH),
            **BROWSER_OPTS,
        )
        page = await context.new_page()
        await page.goto(WX_MP_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        url = page.url
        await browser.close()

        if "login" in url.lower() or url.rstrip("/") == WX_MP_URL:
            print("❌ Session 已过期，请重新登录: python wechat_mp_publish_playwright.py login")
            return False

        print("✅ Session 有效")
        return True


async def inspect_page():
    """Open the 贴图 editor page and inspect DOM structure."""
    if not WX_STORAGE_PATH.exists():
        print("❌ 未找到登录 session，请先运行: python wechat_mp_publish_playwright.py login")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            storage_state=str(WX_STORAGE_PATH),
            **BROWSER_OPTS,
        )
        page = await context.new_page()

        print("📝 正在打开微信公众号后台...")
        token = await _goto_home_and_extract_token(page)

        if not token:
            print("❌ Session 已过期或无法提取 token")
            await browser.close()
            return

        print(f"Token: {token}")

        edit_url = _build_tietie_edit_url(token)
        print(f"📝 正在访问贴图编辑页面...")
        await page.goto(edit_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        await page.screenshot(path="/tmp/wx_mp_tietie_edit.png", full_page=True)
        print("📸 截图已保存到 /tmp/wx_mp_tietie_edit.png")

        # Inspect key elements
        print("\n--- DOM 检查 ---")

        inputs = await page.locator("input").all()
        print(f"\n[input] 共 {len(inputs)} 个:")
        for i, inp in enumerate(inputs):
            try:
                inp_type = await inp.get_attribute("type") or ""
                inp_placeholder = await inp.get_attribute("placeholder") or ""
                visible = await inp.is_visible()
                if visible or inp_type == "file":
                    print(f"  [{i}] type={inp_type}, placeholder={inp_placeholder}, visible={visible}")
            except Exception:
                pass

        textareas = await page.locator("textarea").all()
        print(f"\n[textarea] 共 {len(textareas)} 个:")
        for i, ta in enumerate(textareas):
            try:
                ta_placeholder = await ta.get_attribute("placeholder") or ""
                visible = await ta.is_visible()
                print(f"  [{i}] placeholder={ta_placeholder}, visible={visible}")
            except Exception:
                pass

        editables = await page.locator("[contenteditable]").all()
        print(f"\n[contenteditable] 共 {len(editables)} 个:")
        for i, el in enumerate(editables):
            try:
                el_tag = await el.evaluate("el => el.tagName")
                el_class = await el.get_attribute("class") or ""
                visible = await el.is_visible()
                if visible:
                    print(f"  [{i}] tag={el_tag}, class={el_class[:60]}")
            except Exception:
                pass

        file_inputs = await page.locator('input[type="file"]').all()
        print(f"\n[file input] 共 {len(file_inputs)} 个:")
        for i, fi in enumerate(file_inputs):
            try:
                accept = await fi.get_attribute("accept") or ""
                multiple = await fi.get_attribute("multiple")
                print(f"  [{i}] accept={accept}, multiple={multiple}")
            except Exception:
                pass

        print("\n⏸️  浏览器保持打开。按 Enter 关闭...")
        await asyncio.get_event_loop().run_in_executor(None, input)
        await context.storage_state(path=str(WX_STORAGE_PATH))
        await browser.close()


async def _navigate_to_tietie(page: Page) -> str | None:
    """Navigate to the 贴图 editor and return the token."""
    print("📝 正在打开微信公众号后台...")
    token = await _goto_home_and_extract_token(page)
    if not token:
        return None

    edit_url = _build_tietie_edit_url(token)
    print(f"📝 正在访问贴图编辑页面...")
    import sys; sys.stdout.flush()
    await page.goto(edit_url, wait_until="domcontentloaded")
    print(f"   ✅ 页面已加载，等待渲染...")
    sys.stdout.flush()
    await page.wait_for_timeout(5000)

    # Verify we're on the editor page
    current_url = page.url
    if "appmsg_edit" not in current_url:
        print(f"⚠️ 页面未正确加载: {current_url}")
        # Try clicking through menu
        try:
            # Try the direct 贴图 entry via menu
            new_btn = page.locator('a:has-text("新的创作"), button:has-text("新的创作")').first
            if await new_btn.is_visible(timeout=3000):
                await new_btn.click()
                await page.wait_for_timeout(1000)
                tietie_btn = page.locator('text=贴图').first
                if await tietie_btn.is_visible(timeout=3000):
                    await tietie_btn.click()
                    await page.wait_for_timeout(5000)
        except Exception:
            pass

    return token


async def _count_uploaded_images(page: Page) -> int:
    """Count uploaded images in the 贴图 editor.

    Based on actual DOM inspection (with uploaded images):
    - Bottom of editor shows thumbnail images as small clickable previews
    - Main image area shows "N/M" indicator (e.g. "5/5") as an overlay
    - Form field counters like "0/20" are in <em class="frm_counter"> — ignore these!
    """
    return await page.evaluate("""() => {
        // Method 1: Count thumbnail img elements at the bottom of the editor
        // These are mmbiz images that serve as clickable thumbnails
        const allImgs = document.querySelectorAll('img[src*="mmbiz"]');
        let thumbCount = 0;
        for (const img of allImgs) {
            const rect = img.getBoundingClientRect();
            // Thumbnails are small (width ~50-80px), visible, and in the lower area
            if (rect.width > 20 && rect.width < 200 && rect.height > 20 && rect.height < 200 && rect.top > 0) {
                thumbCount++;
            }
        }
        if (thumbCount > 0) return thumbCount;

        // Method 2: Count larger content images (main preview + thumbnails)
        let largeCount = 0;
        for (const img of allImgs) {
            const rect = img.getBoundingClientRect();
            if (rect.width > 100 && rect.height > 100 && rect.top > 0) {
                largeCount++;
            }
        }
        // Subtract 1 for the main preview image (it duplicates one of the thumbnails)
        if (largeCount > 1) return largeCount - 1;
        if (largeCount === 1) return 1;

        return 0;
    }""")


async def _wait_for_upload_complete(page: Page, expected_count: int, timeout_ms: int = 60000) -> bool:
    """Poll until the uploaded image count reaches expected_count or timeout."""
    import sys
    interval_ms = 2000
    elapsed = 0
    while elapsed < timeout_ms:
        current = await _count_uploaded_images(page)
        if current >= expected_count:
            return True
        await page.wait_for_timeout(interval_ms)
        elapsed += interval_ms
        # Print progress every 10 seconds to avoid idle timeout
        if elapsed % 10000 == 0:
            print(f"      ⏳ 上传中... ({elapsed // 1000}s, 当前 {current} 张)")
            sys.stdout.flush()
    return False


async def _upload_images_one_by_one(page: Page, image_paths: list[str]):
    """Upload images one by one to the 贴图 editor.

    Strategy (based on diagnosis):
    - file_inputs[0] is the menu '本地上传' input → does NOT upload to editor.
    - file_inputs[1] is the editor's own upload input → WORKS every time.
    - After each upload, the DOM inserts a new file input; we must re-query
      and always use index [1] again (it becomes the fresh editor input).
    - Verification uses thumbnail/indicator counting, NOT mmbiz img counting.
    """

    for i, img_path in enumerate(image_paths):
        img_name = Path(img_path).name
        label = 'infographic' if 'infographic' in img_path else 'chart'
        print(f"   📤 上传第 {i+1}/{len(image_paths)} 张（{label}）: {img_name}")

        before_count = await _count_uploaded_images(page)
        expected_count = before_count + 1

        # Always re-query file inputs before each upload
        file_inputs = await page.locator('input[type="file"]').all()
        if len(file_inputs) < 2:
            print(f"   ❌ file inputs 不足（当前 {len(file_inputs)} 个），无法上传")
            break

        print(f"   🔍 file inputs: {len(file_inputs)} 个, 当前图片数: {before_count}")

        # Use file_inputs[1] (editor input) — this works for every upload
        await file_inputs[1].set_input_files(img_path)
        print(f"   ⏳ 等待第 {i+1} 张图片上传完成...")

        if await _wait_for_upload_complete(page, expected_count, timeout_ms=60000):
            current = await _count_uploaded_images(page)
            print(f"   ✅ 第 {i+1} 张上传成功（图片数: {before_count} → {current}）")
        else:
            current = await _count_uploaded_images(page)
            print(f"   ⚠️ 第 {i+1} 张上传超时（图片数: {before_count} → {current}），继续下一张")

        # Brief pause before next upload to let DOM stabilize
        if i < len(image_paths) - 1:
            await page.wait_for_timeout(3000)

    final_count = await _count_uploaded_images(page)
    print(f"   📊 上传流程完成，编辑器中共 {final_count} 张图片")


async def _fill_title(page: Page, title: str):
    """Fill the 贴图 title field.

    Based on actual DOM inspection, the 贴图 title is a ProseMirror
    contenteditable <div> with data-placeholder="请在这里输入标题".
    There's also a <textarea> for the article title (64 char limit),
    but the 贴图 title is the ProseMirror one (20 char limit).
    """
    # Method 1: Use the ProseMirror contenteditable with data-placeholder
    try:
        title_el = page.locator('div.ProseMirror[data-placeholder="请在这里输入标题"]').first
        if await title_el.is_visible(timeout=3000):
            await title_el.click()
            await page.wait_for_timeout(300)
            # Use keyboard.type instead of fill() for contenteditable
            await page.keyboard.type(title)
            await page.wait_for_timeout(300)
            print(f"   ✅ 标题（ProseMirror）: {title}")
            return True
    except Exception as e:
        print(f"   ⚠️ ProseMirror 标题填写失败: {e}")

    # Method 2: Also fill the textarea title as fallback
    try:
        textarea_title = page.locator('textarea.js_title').first
        if await textarea_title.is_visible(timeout=2000):
            await textarea_title.click()
            await textarea_title.fill(title)
            print(f"   ✅ 标题（textarea）: {title}")
            return True
    except Exception as e:
        print(f"   ⚠️ textarea 标题填写失败: {e}")

    # Method 3: Click the placeholder label
    try:
        label = page.locator('label:has-text("请在这里输入标题")').first
        if await label.is_visible(timeout=2000):
            await label.click()
            await page.wait_for_timeout(300)
            await page.keyboard.type(title)
            print(f"   ✅ 标题（label click）: {title}")
            return True
    except Exception:
        pass

    print("   ⚠️ 未找到标题输入框")
    return False


async def _paste_into_contenteditable(page: Page, full_text: str):
    """Paste multi-line text into a focused ProseMirror contenteditable element.

    The WeChat 贴图 description editor (ProseMirror) intercepts Enter and
    Shift+Enter keystrokes, so keyboard-based line breaks do not work.

    The only reliable method (verified by diagnosis) is to dispatch a synthetic
    ClipboardEvent('paste') with text/plain.  ProseMirror converts \\n → <br>.

    However, ProseMirror collapses consecutive \\n\\n (blank lines) into a
    single <br>.  To preserve blank lines, we replace each empty line with a
    zero-width space (\\u200B) so ProseMirror sees content on that line and
    keeps the <br> before and after it, producing a visual empty line.
    """
    # Clear existing content
    await page.keyboard.press('Meta+a')
    await page.wait_for_timeout(100)
    await page.keyboard.press('Backspace')
    await page.wait_for_timeout(300)

    # Replace blank lines (\n\n) with \n + ZWSP + \n to preserve spacing
    patched = full_text.replace('\n\n', '\n\u200B\n')

    # Dispatch synthetic paste event with text/plain
    await page.evaluate("""(text) => {
        const el = document.activeElement;
        const dt = new DataTransfer();
        dt.setData('text/plain', text);
        const event = new ClipboardEvent('paste', {
            clipboardData: dt,
            bubbles: true,
            cancelable: true,
        });
        el.dispatchEvent(event);
    }""", patched)
    await page.wait_for_timeout(500)


async def _fill_description(page: Page, description: str, tags: list[str]):
    """Fill the description/body text, appending tags.

    Based on actual DOM inspection, the description area is the second visible
    ProseMirror contenteditable <div>. The placeholder text is
    "填写描述信息，让大家了解更多内容" shown as a <span> overlay.
    """
    if not description and not tags:
        return

    full_text = description or ""
    if tags:
        tags_line = " ".join(f"#{t}" for t in tags)
        if full_text:
            full_text = full_text + "\n\n" + tags_line
        else:
            full_text = tags_line

    # Method 1: Find all visible ProseMirror contenteditable divs
    # Title is the first one (data-placeholder="请在这里输入标题")
    # Description is the second one (larger, no data-placeholder for title)
    try:
        prosemirrors = await page.locator('div.ProseMirror[contenteditable="true"]').all()
        visible_pms = []
        for pm in prosemirrors:
            try:
                if await pm.is_visible():
                    dph = await pm.get_attribute('data-placeholder') or ''
                    visible_pms.append((pm, dph))
            except Exception:
                continue

        # Find the one that is NOT the title
        desc_pm = None
        for pm, dph in visible_pms:
            if '标题' not in dph:
                desc_pm = pm
                break

        if desc_pm:
            await desc_pm.click()
            await page.wait_for_timeout(500)
            await _paste_into_contenteditable(page, full_text)
            await page.wait_for_timeout(300)
            print("   ✅ 描述已填写（ProseMirror）")
            return True
    except Exception as e:
        print(f"   ⚠️ ProseMirror 描述填写失败: {e}")

    # Method 2: Click the placeholder span text
    try:
        placeholder = page.locator('span:has-text("填写描述信息")').first
        if await placeholder.is_visible(timeout=2000):
            await placeholder.click()
            await page.wait_for_timeout(500)
            await _paste_into_contenteditable(page, full_text)
            await page.wait_for_timeout(300)
            print("   ✅ 描述已填写（通过占位符）")
            return True
    except Exception as e:
        print(f"   ⚠️ 占位符描述填写失败: {e}")

    # Method 3: Get all visible contenteditable, use the second one
    try:
        editables = await page.locator('[contenteditable="true"]').all()
        visible_eds = []
        for ed in editables:
            try:
                if await ed.is_visible():
                    visible_eds.append(ed)
            except Exception:
                continue

        if len(visible_eds) >= 2:
            await visible_eds[1].click()
            await page.wait_for_timeout(500)
            await _paste_into_contenteditable(page, full_text)
            await page.wait_for_timeout(300)
            print("   ✅ 描述已填写（第2个contenteditable）")
            return True
    except Exception:
        pass

    print("   ⚠️ 未找到描述输入框")
    return False


async def _select_collection(page: Page, collection: str):
    """Select a collection for the post."""
    if not collection:
        return

    try:
        coll_area = page.locator('text=合集, text=选择合集, [class*="collection"]').first
        if await coll_area.is_visible(timeout=3000):
            # Find the clickable element near "合集"
            parent = coll_area.locator('..')
            clickable = parent.locator('text=未添加, text=选择, [class*="select"], [class*="add"]').first
            try:
                if await clickable.is_visible(timeout=2000):
                    await clickable.click()
                else:
                    await coll_area.click()
            except Exception:
                await coll_area.click()

            await page.wait_for_timeout(1000)

            # Find and click the target collection in the dialog
            coll_item = page.locator(f'text="{collection}"').first
            if await coll_item.is_visible(timeout=3000):
                await coll_item.click()
                await page.wait_for_timeout(500)

                # Click confirm
                confirm_btn = page.locator('button:has-text("确定"), button:has-text("确认")').first
                if await confirm_btn.is_visible(timeout=2000):
                    await confirm_btn.click()
                    await page.wait_for_timeout(500)

                print(f"   ✅ 已选择合集: {collection}")
            else:
                print(f"   ⚠️ 未找到合集: {collection}")
        else:
            print("   ⚠️ 未找到合集选择入口")
    except Exception as e:
        print(f"   ⚠️ 合集选择失败: {e}")


async def _save_draft(page: Page):
    """Click save as draft button and verify save was successful."""
    # Take screenshot before save attempt for debugging
    try:
        await page.screenshot(path="/tmp/wx_mp_before_save.png", full_page=False)
        print("   📸 保存前截图: /tmp/wx_mp_before_save.png")
    except Exception:
        pass

    selectors = [
        'button:has-text("保存为草稿")',
        'a:has-text("保存为草稿")',
        '[class*="draft"] button',
        'button:has-text("保存")',
    ]

    clicked = False
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                btn_text = await btn.inner_text()
                print(f"   🔘 找到保存按钮: \"{btn_text.strip()}\" (selector: {sel})")
                sys.stdout.flush()
                await btn.click()
                clicked = True
                print(f"   ✅ 已点击保存按钮")
                sys.stdout.flush()
                break
        except Exception as e:
            print(f"   ⚠️ 尝试 selector {sel} 失败: {e}")
            continue

    if not clicked:
        print("   ❌ 未找到任何保存按钮")
        # Take screenshot for debugging
        try:
            await page.screenshot(path="/tmp/wx_mp_no_save_btn.png", full_page=True)
            print("   📸 截图: /tmp/wx_mp_no_save_btn.png")
        except Exception:
            pass
        return False

    # Wait for save to complete — look for success indicators
    print("   ⏳ 等待保存完成...")
    sys.stdout.flush()
    save_confirmed = False
    for wait_round in range(15):  # 15 rounds × 2s = 30s max
        await page.wait_for_timeout(2000)

        # Check for confirmation dialog (e.g. "确定", "我知道了")
        try:
            confirm = page.locator('button:has-text("确定"), button:has-text("我知道了")').first
            if await confirm.is_visible(timeout=500):
                await confirm.click()
                print("   ✅ 已点击确认对话框")
                sys.stdout.flush()
                await page.wait_for_timeout(2000)
                save_confirmed = True
                break
        except Exception:
            pass

        # Check for success toast/message
        try:
            success_indicators = [
                'text="保存成功"',
                'text="已保存"',
                '.weui-desktop-toast:has-text("成功")',
                '[class*="success"]',
                '[class*="toast"]:has-text("成功")',
            ]
            for indicator in success_indicators:
                try:
                    el = page.locator(indicator).first
                    if await el.is_visible(timeout=300):
                        print(f"   ✅ 检测到保存成功提示")
                        save_confirmed = True
                        break
                except Exception:
                    continue
            if save_confirmed:
                break
        except Exception:
            pass

        # Check if URL changed (redirected away from editor after save)
        current_url = page.url
        if "appmsg_edit" not in current_url and "appmsg" in current_url:
            print(f"   ✅ 页面已跳转（保存成功）: {current_url[:80]}")
            save_confirmed = True
            break

        elapsed = (wait_round + 1) * 2
        if elapsed % 6 == 0:
            print(f"      ⏳ 等待保存确认... ({elapsed}s)")
            sys.stdout.flush()

    if save_confirmed:
        print("   ✅ 草稿保存成功")
    else:
        print("   ⚠️ 未检测到明确的保存成功信号（按钮已点击，可能已保存）")

    # Take screenshot after save
    try:
        await page.screenshot(path="/tmp/wx_mp_after_save.png", full_page=False)
        print("   📸 保存后截图: /tmp/wx_mp_after_save.png")
    except Exception:
        pass

    return save_confirmed or clicked  # At least the button was clicked


async def publish_post(
    title: str,
    image_paths: list[str],
    description: str = "",
    tags: list[str] | None = None,
    collection: str = "",
    auto_publish: bool = False,
    headless: bool = False,
):
    """Publish an image-card post (贴图) to WeChat MP using pure Playwright."""
    if not WX_STORAGE_PATH.exists():
        print("❌ 未找到登录 session，请先运行: python wechat_mp_publish_playwright.py login")
        return False

    # Validate image paths
    resolved_paths = []
    for img in image_paths:
        img_path = Path(img).resolve()
        if not img_path.exists():
            print(f"❌ 图片不存在: {img_path}")
            return False
        resolved_paths.append(str(img_path))

    if not resolved_paths:
        print("❌ 至少需要 1 张图片")
        return False

    # Truncate title if needed
    wx_len = _wx_char_count(title)
    if wx_len > 20:
        title = _wx_truncate(title, 20)
        print(f"⚠️ 标题已截断为: {title}")

    async with async_playwright() as p:
        print("🚀 正在启动浏览器...")
        sys.stdout.flush()
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            storage_state=str(WX_STORAGE_PATH),
            **BROWSER_OPTS,
        )
        page = await context.new_page()
        print("   ✅ 浏览器已启动")
        sys.stdout.flush()

        try:
            # Phase 1: Navigate to 贴图 editor
            print("\n📌 Phase 1/3: 导航到贴图编辑器...")
            sys.stdout.flush()
            token = await _navigate_to_tietie(page)
            if not token:
                print("❌ Session 已过期或无法提取 token，请重新登录")
                return False
            print("✅ 导航完成")
            sys.stdout.flush()

            # Take screenshot after navigation
            try:
                await page.screenshot(path="/tmp/wx_mp_after_navigate.png", full_page=False)
                print("   📸 导航后截图: /tmp/wx_mp_after_navigate.png")
            except Exception:
                pass

            # Check if editor already has images (stale draft detection)
            initial_count = await _count_uploaded_images(page)
            if initial_count > 0:
                print(f"   ⚠️ 检测到编辑器已有 {initial_count} 张图片（可能是旧草稿）")
                print(f"   🔄 尝试打开全新编辑器...")
                sys.stdout.flush()
                # Force a new editor by navigating again with fresh timestamp
                new_url = _build_tietie_edit_url(token)
                await page.goto(new_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(5000)
                initial_count = await _count_uploaded_images(page)
                if initial_count > 0:
                    print(f"   ⚠️ 仍有 {initial_count} 张图片，将在此基础上操作")
                else:
                    print(f"   ✅ 全新编辑器已打开")
                sys.stdout.flush()

            # Phase 2: Upload images
            print(f"\n📌 Phase 2/3: 上传图片（共 {len(resolved_paths)} 张）...")
            sys.stdout.flush()
            await _upload_images_one_by_one(page, resolved_paths)
            final_img_count = await _count_uploaded_images(page)
            print(f"✅ 上传流程完成（编辑器显示 {final_img_count} 张图片）")
            sys.stdout.flush()

            # Take screenshot after upload
            try:
                await page.screenshot(path="/tmp/wx_mp_after_upload.png", full_page=False)
                print("   📸 上传后截图: /tmp/wx_mp_after_upload.png")
            except Exception:
                pass

            # Phase 3: Fill form
            print("\n📌 Phase 3/3: 填写标题/描述/合集...")
            sys.stdout.flush()

            print("📝 填写标题...")
            await _fill_title(page, title)
            await page.wait_for_timeout(500)

            # Take screenshot after title to verify
            try:
                await page.screenshot(path="/tmp/wx_mp_after_title.png", full_page=False)
                print("   📸 标题填写后截图: /tmp/wx_mp_after_title.png")
            except Exception:
                pass

            # Move focus away from title before filling description
            # Press Escape or click elsewhere to deselect title
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
            except Exception:
                pass

            if description or tags:
                print("📝 填写描述...")
                await _fill_description(page, description, tags or [])
                await page.wait_for_timeout(500)

            if collection:
                print(f"📂 选择合集: {collection}...")
                await _select_collection(page, collection)

            # Preview
            print("\n" + "=" * 60)
            print("📋 发布预览")
            print(f"   标题: {title}")
            print(f"   描述: {description[:80]}..." if len(description) > 80 else f"   描述: {description or '无'}")
            print(f"   图片: {len(resolved_paths)} 张")
            print(f"   标签: {', '.join(tags) if tags else '无'}")
            if collection:
                print(f"   合集: {collection}")
            print("=" * 60)

            # Take screenshot before save for debugging
            try:
                await page.screenshot(path="/tmp/wx_mp_before_save_form.png", full_page=False)
                print("   📸 表单填写后截图: /tmp/wx_mp_before_save_form.png")
            except Exception:
                pass

            if auto_publish:
                print("📤 正在保存为草稿...")
                sys.stdout.flush()
                save_result = await _save_draft(page)
                if not save_result:
                    print("❌ 草稿保存失败")
                    return False

                # In auto mode: wait a bit more for save to fully complete, then close
                print("   ⏳ 等待保存操作完成...")
                sys.stdout.flush()
                await page.wait_for_timeout(5000)

                # Save storage state
                await context.storage_state(path=str(WX_STORAGE_PATH))
                print("✅ 公众号贴图草稿保存完成")
                return True
            else:
                print("\n⏸️  内容已填写完毕，浏览器保持打开。")
                print("   请在浏览器中检查并调整。")
                print("   👉 输入 Enter 保存为草稿，输入 'p' 直接发表，输入 'q' 取消。")
                sys.stdout.flush()

                import threading
                confirmed = threading.Event()
                user_choice = {"value": "draft"}

                def wait_stdin():
                    try:
                        line = input("   > ")
                        user_choice["value"] = line.strip().lower()
                        confirmed.set()
                    except (EOFError, KeyboardInterrupt):
                        user_choice["value"] = "q"
                        confirmed.set()

                stdin_thread = threading.Thread(target=wait_stdin, daemon=True)
                stdin_thread.start()

                max_wait = 600
                waited = 0
                while waited < max_wait:
                    if confirmed.is_set():
                        break
                    await asyncio.sleep(1)
                    waited += 1

                if waited >= max_wait and not confirmed.is_set():
                    print(f"\n   ⏰ 等待超时 ({max_wait}s)")
                    return False

                choice = user_choice["value"]
                if choice in ("q", "quit", "cancel", "n", "no"):
                    print("❌ 已取消")
                    return False

                if choice in ("p", "publish"):
                    print("📤 正在发表...")
                    pub_btn = page.locator('button:has-text("发表"), button:has-text("群发")').first
                    if await pub_btn.is_visible(timeout=3000):
                        await pub_btn.click()
                        await page.wait_for_timeout(2000)
                        confirm = page.locator('button:has-text("确定")').first
                        try:
                            if await confirm.is_visible(timeout=3000):
                                await confirm.click()
                        except Exception:
                            pass
                        print("✅ 发表完成！")
                    else:
                        print("⚠️ 未找到发表按钮")
                else:
                    print("📤 正在保存为草稿...")
                    await _save_draft(page)

                # Wait for user to confirm before closing (interactive mode only)
                print("\n⏸️  浏览器保持打开，请确认内容无误。")
                print("   👉 按 Enter 关闭浏览器。")
                sys.stdout.flush()
                try:
                    await asyncio.get_event_loop().run_in_executor(None, input)
                except (EOFError, KeyboardInterrupt):
                    pass

                # Save storage state
                await context.storage_state(path=str(WX_STORAGE_PATH))
                return True

        except Exception as e:
            print(f"❌ 发布失败: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            await browser.close()


async def publish_from_config(
    config_path: str,
    auto_publish: bool = False,
    headless: bool = False,
):
    """Publish a post from a JSON config file."""
    config_file = Path(config_path).resolve()
    if not config_file.exists():
        print(f"❌ 配置文件不存在: {config_file}")
        return False

    with open(config_file, "r", encoding="utf-8") as f:
        config = json.load(f)

    title = config.get("title", "")
    images = config.get("images", [])
    description = config.get("description", "")
    tags = config.get("tags", [])
    collection = config.get("collection", "")

    # If copywriting_file is specified, read full content from it
    copywriting_file = config.get("copywriting_file", "")
    if copywriting_file:
        cw_path = Path(copywriting_file)
        if not cw_path.is_absolute():
            cw_path = config_file.parent / cw_path
        cw_path = cw_path.resolve()
        if cw_path.exists():
            cw_text = cw_path.read_text(encoding="utf-8").strip()
            cw_lines = cw_text.split("\n")
            # Extract title from first line (preserve emoji)
            if not title:
                title = cw_lines[0].strip()
            # Extract tags from last line
            cw_tags = []
            content_end = len(cw_lines)
            for i in range(len(cw_lines) - 1, 0, -1):
                line = cw_lines[i].strip()
                if line.startswith("#"):
                    cw_tags = [t.strip().lstrip("#") for t in line.split("#") if t.strip()]
                    content_end = i
                    break
            if not tags and cw_tags:
                tags = cw_tags
            # Use full copywriting content as description (override config description)
            description = "\n".join(cw_lines[1:content_end]).strip()
            print(f"📄 从 copywriting 文件读取完整内容: {cw_path.name}")
        else:
            print(f"⚠️ copywriting_file 不存在: {cw_path}")

    if not title:
        print("❌ 配置文件中缺少 title 字段")
        return False
    if not images:
        print("❌ 配置文件中缺少 images 字段")
        return False

    config_dir = config_file.parent
    resolved_images = []
    for img in images:
        img_p = Path(img)
        if not img_p.is_absolute():
            img_p = config_dir / img_p
        resolved_images.append(str(img_p.resolve()))

    return await publish_post(
        title=title,
        image_paths=resolved_images,
        description=description,
        tags=tags,
        collection=collection,
        auto_publish=auto_publish,
        headless=headless,
    )


def main():
    parser = argparse.ArgumentParser(
        description="微信公众号贴图发布工具 (纯 Playwright 版)"
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    subparsers.add_parser("login", help="扫码登录微信公众号")
    subparsers.add_parser("check", help="检查登录状态")
    subparsers.add_parser("inspect", help="检查贴图编辑页面 DOM 结构")

    pub_parser = subparsers.add_parser("publish", help="发布贴图")
    pub_parser.add_argument("--config", help="JSON 配置文件路径")
    pub_parser.add_argument("--copywriting", help="copywriting.txt 文件路径")
    pub_parser.add_argument("--title", help="贴图标题")
    pub_parser.add_argument("--images", nargs="+", help="图片路径列表")
    pub_parser.add_argument("--description", default="", help="描述文字")
    pub_parser.add_argument("--tags", nargs="*", help="标签列表")
    pub_parser.add_argument("--collection", default="", help="合集名称")
    pub_parser.add_argument("--auto", action="store_true", help="跳过确认直接保存草稿")
    pub_parser.add_argument("--headless", action="store_true", help="无头模式")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "login":
        asyncio.run(login())
    elif args.command == "check":
        asyncio.run(check_login())
    elif args.command == "inspect":
        asyncio.run(inspect_page())
    elif args.command == "publish":
        if args.config:
            asyncio.run(publish_from_config(args.config, auto_publish=args.auto, headless=args.headless))
        elif args.copywriting and args.images:
            cw_path = Path(args.copywriting).resolve()
            if not cw_path.exists():
                print(f"❌ copywriting 文件不存在: {cw_path}")
                sys.exit(1)
            cw_text = cw_path.read_text(encoding="utf-8").strip()
            cw_lines = cw_text.split("\n")
            raw_title = cw_lines[0].strip()
            cw_title = raw_title
            cw_tags = []
            content_end = len(cw_lines)
            for i in range(len(cw_lines) - 1, 0, -1):
                line = cw_lines[i].strip()
                if line.startswith("#"):
                    cw_tags = [t.strip().lstrip("#") for t in line.split("#") if t.strip()]
                    content_end = i
                    break
            cw_desc = "\n".join(cw_lines[1:content_end]).strip()
            asyncio.run(publish_post(
                title=cw_title, image_paths=args.images, description=cw_desc,
                tags=cw_tags, collection=args.collection,
                auto_publish=args.auto, headless=args.headless,
            ))
        elif args.title and args.images:
            asyncio.run(publish_post(
                title=args.title, image_paths=args.images, description=args.description,
                tags=args.tags or [], collection=args.collection,
                auto_publish=args.auto, headless=args.headless,
            ))
        else:
            print("❌ 请提供 --config 配置文件路径，或同时提供 --title 和 --images")
            pub_parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
