#!/usr/bin/env python3
from __future__ import annotations
"""
Publish image-text notes to Xiaohongshu (小红书) via pure Playwright.

No browser-use / LLM dependency. Uses Playwright for ALL interactions:
login, image upload, form filling, tag entry, original declaration, etc.

Requires:
  - playwright (pip install playwright && playwright install chromium)

Usage:
    # First-time login (scan QR code)
    python xhs_publish_playwright.py login

    # Check login status
    python xhs_publish_playwright.py check

    # Publish from a JSON config file
    python xhs_publish_playwright.py publish --config <path_to_config.json>

    # Publish with explicit args
    python xhs_publish_playwright.py publish \
        --title "笔记标题" \
        --content "笔记正文" \
        --images img1.png img2.png \
        --tags "标签1" "标签2" \
        --original

Config JSON format:
{
    "title": "笔记标题（不超过20字）",
    "content": "笔记正文内容",
    "images": ["path/to/img1.png", "path/to/img2.png"],
    "tags": ["标签1", "标签2"],
    "collection": "合集名称",
    "original": true
}
"""
import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright, Page

# Storage state file for Xiaohongshu login session
XHS_STORAGE_DIR = Path.home() / ".xiaohongshu"
XHS_STORAGE_PATH = XHS_STORAGE_DIR / "storage_state.json"

# Xiaohongshu creator platform URLs
XHS_CREATOR_URL = "https://creator.xiaohongshu.com"
XHS_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish"

BROWSER_OPTS = dict(
    viewport={"width": 1280, "height": 900},
    user_agent=(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
)


async def login(headless: bool = False):
    """Login to Xiaohongshu via QR code scan."""
    XHS_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(**BROWSER_OPTS)
        page = await context.new_page()
        await page.goto(XHS_CREATOR_URL)

        print("=" * 60)
        print("请在浏览器中扫码登录小红书创作者平台")
        print("登录成功后会自动保存 session，后续无需重新登录")
        print("=" * 60)

        try:
            await page.wait_for_url(
                "**/creator.xiaohongshu.com/new/**",
                timeout=120_000,
            )
        except Exception:
            try:
                await page.wait_for_selector(
                    ".user-avatar, .creator-header, .user-info",
                    timeout=120_000,
                )
            except Exception:
                print("⚠️  登录超时，请在 2 分钟内完成扫码")
                await browser.close()
                return False

        await page.wait_for_timeout(2000)
        await context.storage_state(path=str(XHS_STORAGE_PATH))
        print(f"\n✅ 登录成功！Session 已保存到 {XHS_STORAGE_PATH}")
        await browser.close()
        return True


async def check_login() -> bool:
    """Check if the saved session is still valid."""
    if not XHS_STORAGE_PATH.exists():
        print("❌ 未找到登录 session，请先运行: python xhs_publish_playwright.py login")
        return False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=str(XHS_STORAGE_PATH),
            **BROWSER_OPTS,
        )
        page = await context.new_page()
        await page.goto(XHS_CREATOR_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        url = page.url
        await browser.close()

        if "login" in url.lower() or "passport" in url.lower():
            print("❌ Session 已过期，请重新登录: python xhs_publish_playwright.py login")
            return False

        print("✅ Session 有效")
        return True


def _strip_urls(text: str) -> str:
    """Remove URLs from text (小红书会封禁外部链接)."""
    lines = text.split("\n")
    filtered = []
    for line in lines:
        stripped = line.strip()
        if re.match(r'^[\U0001f000-\U0001ffff\s]*https?://\S+\s*$', stripped):
            continue
        cleaned = re.sub(r'https?://\S+', '', line).rstrip()
        if cleaned.strip() in ('🔗', '📎', '🔗 ', '📎 ', ''):
            if re.search(r'https?://\S+', line):
                continue
        filtered.append(cleaned)
    return "\n".join(filtered)


async def _type_text_human(page: Page, selector: str, text: str, delay: int = 30):
    """Type text into element with human-like delay."""
    await page.click(selector)
    await page.wait_for_timeout(200)
    # Clear existing content
    await page.evaluate(f"""() => {{
        const el = document.querySelector('{selector}');
        if (el) {{
            if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {{
                el.value = '';
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            }} else {{
                el.innerHTML = '';
            }}
        }}
    }}""")
    await page.wait_for_timeout(200)
    await page.type(selector, text, delay=delay)


async def _fill_content_prosemirror(page: Page, content: str):
    """Fill content into ProseMirror editor preserving line breaks."""
    editor_selector = '.ql-editor, [contenteditable="true"].tiptap, .ProseMirror, [contenteditable="true"]'

    await page.click(editor_selector)
    await page.wait_for_timeout(300)

    # Clear existing content
    await page.evaluate("""() => {
        const selectors = ['.ql-editor', '.tiptap.ProseMirror', '.ProseMirror', '[contenteditable="true"]'];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el && el.isContentEditable) {
                el.focus();
                el.innerHTML = '<p><br></p>';
                return true;
            }
        }
        return false;
    }""")
    await page.wait_for_timeout(300)

    # Type content line by line
    lines = content.split('\n')
    for i, line in enumerate(lines):
        if line.strip():
            await page.keyboard.type(line, delay=15)
        if i < len(lines) - 1:
            await page.keyboard.press('Enter')
            await page.wait_for_timeout(50)


async def _move_cursor_to_end(page: Page):
    """Move cursor to the absolute end of the contenteditable editor using JS Selection API."""
    await page.evaluate("""() => {
        const selectors = ['.ql-editor', '.tiptap.ProseMirror', '.ProseMirror', '[contenteditable="true"]'];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el && el.isContentEditable) {
                el.focus();
                const range = document.createRange();
                const selection = window.getSelection();
                // Walk to the deepest last child node for precise cursor placement
                let lastNode = el;
                while (lastNode.lastChild) {
                    lastNode = lastNode.lastChild;
                }
                if (lastNode.nodeType === Node.TEXT_NODE) {
                    range.setStart(lastNode, lastNode.textContent.length);
                    range.setEnd(lastNode, lastNode.textContent.length);
                } else {
                    range.selectNodeContents(lastNode);
                    range.collapse(false);
                }
                selection.removeAllRanges();
                selection.addRange(range);
                return true;
            }
        }
        return false;
    }""")
    await page.wait_for_timeout(200)


async def _add_tags(page: Page, tags: list[str]):
    """Add tags by typing # in the editor. Tags are always appended at the END of content."""
    if not tags:
        return

    # CRITICAL: Move cursor to the absolute END of the editor content via JS
    await _move_cursor_to_end(page)

    # Press Enter to start a new line at the end
    await page.keyboard.press('Enter')
    await page.wait_for_timeout(200)

    for i, tag in enumerate(tags):
        await page.keyboard.type(f'#{tag}', delay=30)
        await page.wait_for_timeout(500)

        # Try to click the first suggestion if dropdown appears
        try:
            suggestion = page.locator('.publish-hash-tag .tag-item, .hash-tag-list .tag-item, [class*="tag-suggestion"] .item').first
            if await suggestion.is_visible(timeout=1500):
                await suggestion.click()
                await page.wait_for_timeout(300)
                # After clicking suggestion, cursor may have moved — restore to end
                await _move_cursor_to_end(page)
            else:
                # Press space to confirm tag
                await page.keyboard.press('Space')
                await page.wait_for_timeout(300)
        except Exception:
            await page.keyboard.press('Space')
            await page.wait_for_timeout(300)


async def _scroll_to_bottom(page: Page):
    """Scroll the publish page to bottom to reveal all settings."""
    await page.evaluate("""() => {
        // Try scrolling various container elements
        const containers = [
            document.querySelector('.content-container'),
            document.querySelector('.publish-container'),
            document.querySelector('[class*="publish"]'),
            document.querySelector('.main'),
            document.documentElement,
        ];
        for (const el of containers) {
            if (el) {
                el.scrollTop = el.scrollHeight;
            }
        }
        window.scrollTo(0, document.body.scrollHeight);
    }""")
    await page.wait_for_timeout(500)


async def _expand_content_settings(page: Page):
    """Expand the '内容设置' section if it is collapsed. Returns True if expanded/already open."""
    # First scroll to bottom to reveal the section
    await _scroll_to_bottom(page)
    await page.wait_for_timeout(500)

    # Check if "内容设置" section exists and expand it
    expanded = await page.evaluate("""() => {
        // Look for "内容设置" text
        const allElements = document.querySelectorAll('*');
        for (const el of allElements) {
            const text = el.textContent.trim();
            // Match elements whose own text (not deep children) contains "内容设置"
            if (el.childNodes.length <= 3) {
                const ownText = Array.from(el.childNodes)
                    .filter(n => n.nodeType === Node.TEXT_NODE)
                    .map(n => n.textContent.trim())
                    .join('');
                if (ownText === '内容设置') {
                    // Found the section header; look for "展开" button nearby
                    const parent = el.parentElement;
                    if (parent) {
                        const expandBtn = parent.querySelector('[class*="expand"], [class*="toggle"], [class*="arrow"]');
                        if (expandBtn) {
                            expandBtn.click();
                            return 'clicked_expand';
                        }
                        // Look for sibling text "展开" or "收起"
                        const siblings = parent.querySelectorAll('*');
                        for (const sib of siblings) {
                            const sibText = sib.textContent.trim();
                            if (sibText === '展开') {
                                sib.click();
                                return 'clicked_展开';
                            }
                            if (sibText === '收起') {
                                return 'already_expanded';
                            }
                        }
                    }
                    return 'found_no_toggle';
                }
            }
        }
        return null;
    }""")

    if not expanded:
        # Try Playwright locator approach
        try:
            expand_btn = page.locator('text=展开').first
            if await expand_btn.is_visible(timeout=2000):
                await expand_btn.click()
                await page.wait_for_timeout(800)
                return True
        except Exception:
            pass

        # Try clicking "收起" area - section might already be expanded
        try:
            collapse_btn = page.locator('text=收起').first
            if await collapse_btn.is_visible(timeout=1000):
                return True  # already expanded
        except Exception:
            pass
    elif expanded in ('clicked_expand', 'clicked_展开'):
        await page.wait_for_timeout(800)
        return True
    elif expanded == 'already_expanded':
        return True

    # Scroll again after expanding
    await _scroll_to_bottom(page)
    await page.wait_for_timeout(500)
    return True


async def _toggle_original(page: Page):
    """Toggle original declaration on.

    Flow (based on actual XHS UI — 3 steps):
    1. In "内容设置" section, find "原创声明" row and click its toggle switch
    2. A dialog pops up with title "笔记完成原创声明后，将获得以下权益"
       - At the bottom: a checkbox "我已阅读并同意《原创声明须知》..." — MUST check it
       - An orange button "声明原创" (disabled until checkbox is checked)
    3. Check the checkbox, then click "声明原创" button
    """
    try:
        # Step 1: Expand "内容设置" section
        await _expand_content_settings(page)
        await page.screenshot(path="/tmp/xhs_before_original.png")

        # Step 2: Click the toggle switch next to "原创声明"
        # The toggle is the round switch element in the same row as "原创声明" text
        toggle_clicked = False

        # Strategy A: Find the exact "原创声明" text node, then look for a nearby
        # clickable toggle in the same row container
        toggle_clicked = await page.evaluate("""() => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            while (walker.nextNode()) {
                const node = walker.currentNode;
                if (node.textContent.trim() === '原创声明') {
                    let container = node.parentElement;
                    for (let i = 0; i < 6 && container; i++) {
                        // Look for any switch-like element in the container
                        const toggle = container.querySelector(
                            '[class*="switch"], [class*="toggle"], [role="switch"], ' +
                            'button[class*="switch"], span[class*="switch"], ' +
                            '[class*="Switch"], [class*="Toggle"]'
                        );
                        if (toggle) {
                            toggle.click();
                            return 'found_toggle';
                        }
                        container = container.parentElement;
                    }
                    // Didn't find a toggle; click the parent row itself
                    node.parentElement.click();
                    return 'clicked_text_parent';
                }
            }
            return null;
        }""")

        if not toggle_clicked:
            # Fallback: Click to the right of "原创声明" text (where the toggle visually is)
            try:
                original_text = page.locator('text=原创声明').first
                if await original_text.is_visible(timeout=3000):
                    bbox = await original_text.bounding_box()
                    if bbox:
                        await page.mouse.click(bbox['x'] + bbox['width'] + 50, bbox['y'] + bbox['height'] / 2)
                        toggle_clicked = 'clicked_right_of_text'
            except Exception:
                pass

        if not toggle_clicked:
            await page.screenshot(path="/tmp/xhs_original_not_found.png")
            print("   ⚠️ 未找到原创声明开关（截图: /tmp/xhs_original_not_found.png）")
            return

        # Step 3: Wait for the confirmation dialog to appear
        await page.wait_for_timeout(2000)
        await page.screenshot(path="/tmp/xhs_original_dialog.png")

        # Step 4: Check the checkbox "我已阅读并同意《原创声明须知》"
        # The checkbox is at the bottom of the dialog. It's a small round icon/circle
        # to the LEFT of the text "我已阅读并同意..."
        checkbox_checked = False

        # Strategy A: Find the checkbox icon/element to the LEFT of "我已阅读" text
        # In XHS UI, the checkbox is a custom component (not a real <input type="checkbox">)
        # It's usually a small circle/icon that becomes filled/checked when clicked
        checkbox_checked = await page.evaluate("""() => {
            // Find the row containing "我已阅读" text
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            while (walker.nextNode()) {
                const node = walker.currentNode;
                if (node.textContent.includes('我已阅读') && node.textContent.includes('原创声明')) {
                    // Found the text. The checkbox is to the left of this text.
                    // Walk up to find the clickable row container
                    let container = node.parentElement;
                    for (let i = 0; i < 5 && container; i++) {
                        // Look for checkbox-like elements: icons, SVGs, small circles
                        const checkbox = container.querySelector(
                            'input[type="checkbox"], [class*="checkbox"], [role="checkbox"], ' +
                            '[class*="check-box"], [class*="Checkbox"], svg, [class*="icon"]'
                        );
                        if (checkbox && checkbox !== container) {
                            checkbox.click();
                            return 'clicked_checkbox_element';
                        }
                        // If the container itself is small enough to be the checkbox row, click it
                        if (container.offsetHeight && container.offsetHeight < 80) {
                            container.click();
                            return 'clicked_checkbox_row';
                        }
                        container = container.parentElement;
                    }
                    // Last resort: click the text node's parent directly
                    node.parentElement.click();
                    return 'clicked_text_parent';
                }
            }
            return null;
        }""")

        if not checkbox_checked:
            # Strategy B: Use Playwright to find and click the checkbox area
            # The checkbox text is "我已阅读并同意《原创声明须知》..."
            try:
                cb_text = page.locator('text=我已阅读').first
                if await cb_text.is_visible(timeout=3000):
                    # Click to the LEFT of the text (where the checkbox circle is)
                    bbox = await cb_text.bounding_box()
                    if bbox:
                        await page.mouse.click(bbox['x'] - 15, bbox['y'] + bbox['height'] / 2)
                        checkbox_checked = 'clicked_left_of_text'
                    else:
                        await cb_text.click()
                        checkbox_checked = 'clicked_text'
            except Exception:
                pass

        await page.wait_for_timeout(1000)
        await page.screenshot(path="/tmp/xhs_original_checkbox.png")

        # Step 5: Click "声明原创" button (the orange/pink button)
        btn_clicked = False

        # Strategy A: Playwright locator for button text
        for btn_text in ['声明原创']:
            try:
                btn = page.locator(f'button:has-text("{btn_text}")').first
                if await btn.is_visible(timeout=3000):
                    # Make sure button is enabled (checkbox must be checked)
                    is_disabled = await btn.is_disabled()
                    if is_disabled:
                        print("   ⚠️ 声明原创按钮仍为禁用状态，尝试重新点击复选框...")
                        # Re-try clicking checkbox by clicking left of text
                        try:
                            cb_text = page.locator('text=我已阅读').first
                            bbox = await cb_text.bounding_box()
                            if bbox:
                                await page.mouse.click(bbox['x'] - 15, bbox['y'] + bbox['height'] / 2)
                                await page.wait_for_timeout(800)
                        except Exception:
                            pass
                    await btn.click()
                    btn_clicked = True
                    break
            except Exception:
                pass

        if not btn_clicked:
            # Strategy B: JS fallback — find any element with "声明原创" text and click
            btn_clicked = await page.evaluate("""() => {
                const all = document.querySelectorAll('button, [class*="btn"], [class*="Button"], [role="button"]');
                for (const el of all) {
                    if (el.textContent.trim().includes('声明原创')) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }""")

        await page.wait_for_timeout(1500)
        await page.screenshot(path="/tmp/xhs_after_original.png")

        if btn_clicked:
            print("   ✅ 原创声明已开启")
        elif checkbox_checked:
            print("   ⚠️ 复选框已勾选但「声明原创」按钮未点击成功（截图: /tmp/xhs_after_original.png）")
        else:
            print("   ⚠️ 原创声明可能未完全设置（截图: /tmp/xhs_after_original.png）")

    except Exception as e:
        await page.screenshot(path="/tmp/xhs_original_error.png")
        print(f"   ⚠️ 原创声明设置失败: {e}（截图: /tmp/xhs_original_error.png）")


async def _select_collection(page: Page, collection: str):
    """Select a collection for the note.

    Flow (based on actual XHS UI):
    1. Expand "内容设置" section
    2. Find "加入合集" row and click "选择合集" dropdown button (right side)
    3. A dropdown appears with collection list items
    4. Click the target collection (e.g. "AI产品经理")
    """
    if not collection:
        return
    try:
        # Step 1: Expand "内容设置" section
        await _expand_content_settings(page)
        await page.screenshot(path="/tmp/xhs_before_collection.png")

        # Step 2: Click "选择合集" to open the dropdown
        dropdown_opened = False

        # Strategy A: Find "选择合集" text and click it
        dropdown_opened = await page.evaluate("""() => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            while (walker.nextNode()) {
                const node = walker.currentNode;
                if (node.textContent.trim() === '选择合集') {
                    // Click the parent element (the clickable dropdown trigger)
                    const clickTarget = node.parentElement.closest(
                        '[class*="select"], [class*="dropdown"], [class*="trigger"], [class*="click"]'
                    ) || node.parentElement;
                    clickTarget.click();
                    return true;
                }
            }
            return false;
        }""")

        if not dropdown_opened:
            # Fallback: Playwright locator
            try:
                select_btn = page.locator('text=选择合集').first
                if await select_btn.is_visible(timeout=3000):
                    await select_btn.click()
                    dropdown_opened = True
            except Exception:
                pass

        if not dropdown_opened:
            await page.screenshot(path="/tmp/xhs_collection_not_found.png")
            print(f"   ⚠️ 未找到合集选择入口（截图: /tmp/xhs_collection_not_found.png）")
            return

        # Wait for dropdown to appear
        await page.wait_for_timeout(1500)
        await page.screenshot(path="/tmp/xhs_collection_dropdown.png")

        # Step 3: Find and click the target collection in the dropdown list
        item_clicked = False

        # Strategy A: Use text walker for exact match
        item_clicked = await page.evaluate("""(targetName) => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            while (walker.nextNode()) {
                const node = walker.currentNode;
                if (node.textContent.trim() === targetName) {
                    // Click the parent (list item)
                    const clickTarget = node.parentElement.closest(
                        '[class*="item"], [class*="option"], [class*="collection"], li, [role="option"]'
                    ) || node.parentElement;
                    clickTarget.click();
                    return true;
                }
            }
            return false;
        }""", collection)

        if not item_clicked:
            # Strategy B: Playwright text locator
            try:
                coll_item = page.locator(f'text="{collection}"').first
                if await coll_item.is_visible(timeout=2000):
                    await coll_item.click()
                    item_clicked = True
            except Exception:
                pass

        if not item_clicked:
            # Strategy C: Try partial text match with Playwright
            try:
                coll_item = page.locator(f'text={collection}').first
                if await coll_item.is_visible(timeout=2000):
                    await coll_item.click()
                    item_clicked = True
            except Exception:
                pass

        if item_clicked:
            await page.wait_for_timeout(1000)
            await page.screenshot(path="/tmp/xhs_after_collection.png")
            print(f"   ✅ 已选择合集: {collection}")
        else:
            await page.screenshot(path="/tmp/xhs_collection_item_not_found.png")
            print(f"   ⚠️ 未找到合集: {collection}（截图: /tmp/xhs_collection_item_not_found.png）")

    except Exception as e:
        await page.screenshot(path="/tmp/xhs_collection_error.png")
        print(f"   ⚠️ 合集选择失败: {e}（截图: /tmp/xhs_collection_error.png）")


async def _set_schedule(page: Page, schedule_time: str):
    """Set scheduled publish time.

    The "定时发布" toggle is in the "更多设置" section (NOT "内容设置").
    It sits below "允许合拍" and "公开可见" options.
    If schedule_time is 'default', just toggle on and use system default.
    """
    if not schedule_time:
        return

    try:
        # Scroll to bottom to reveal all settings sections
        await _scroll_to_bottom(page)
        await page.wait_for_timeout(500)

        # First, try to expand "更多设置" section if it exists and is collapsed
        await page.evaluate("""() => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            while (walker.nextNode()) {
                const node = walker.currentNode;
                const text = node.textContent.trim();
                if (text === '更多设置') {
                    // Look for an expand/collapse toggle in the parent
                    const parent = node.parentElement;
                    if (parent) {
                        const expandBtn = parent.querySelector('[class*="expand"], [class*="arrow"]');
                        if (expandBtn) {
                            expandBtn.click();
                            return 'expanded';
                        }
                        // Check if there's a sibling "展开" text
                        const siblings = parent.parentElement ? parent.parentElement.querySelectorAll('*') : [];
                        for (const sib of siblings) {
                            if (sib.textContent.trim() === '展开') {
                                sib.click();
                                return 'expanded_via_text';
                            }
                        }
                    }
                    return 'found_no_toggle';
                }
            }
            return null;
        }""")
        await page.wait_for_timeout(800)

        # Scroll again after potential expansion
        await _scroll_to_bottom(page)
        await page.wait_for_timeout(500)
        await page.screenshot(path="/tmp/xhs_before_schedule.png")

        # Find and toggle the "定时发布" switch
        toggled = False

        # Strategy A: JS — find "定时发布" text node and locate the toggle in same row
        toggled = await page.evaluate("""() => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            while (walker.nextNode()) {
                const node = walker.currentNode;
                if (node.textContent.trim() === '定时发布') {
                    let container = node.parentElement;
                    for (let i = 0; i < 6 && container; i++) {
                        const toggle = container.querySelector(
                            '[class*="switch"], [class*="toggle"], [role="switch"], ' +
                            '[class*="Switch"], [class*="Toggle"]'
                        );
                        if (toggle) {
                            toggle.click();
                            return 'found_toggle';
                        }
                        container = container.parentElement;
                    }
                    // Fallback: click the text's parent
                    node.parentElement.click();
                    return 'clicked_text_parent';
                }
            }
            return null;
        }""")

        if not toggled:
            # Strategy B: Playwright — find "定时发布" text and click the toggle to its right
            try:
                schedule_text = page.locator('text=定时发布').first
                if await schedule_text.is_visible(timeout=3000):
                    bbox = await schedule_text.bounding_box()
                    if bbox:
                        # Toggle is to the RIGHT of the text
                        await page.mouse.click(bbox['x'] + bbox['width'] + 50, bbox['y'] + bbox['height'] / 2)
                        toggled = 'clicked_right_of_text'
            except Exception:
                pass

        if toggled:
            await page.wait_for_timeout(1000)
            await page.screenshot(path="/tmp/xhs_after_schedule_toggle.png")

            if schedule_time == 'default':
                print("   ✅ 定时发布已开启（使用系统默认时间）")
            else:
                from datetime import datetime, timedelta
                hour, minute = schedule_time.split(":")
                now = datetime.now()
                target = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                target_str = target.strftime("%Y-%m-%d %H:%M")

                dt_input = page.locator('input[type="text"][placeholder*="选择"], input.el-input__inner, input[class*="date"], input[class*="time"]').last
                try:
                    if await dt_input.is_visible(timeout=3000):
                        await dt_input.click()
                        await page.wait_for_timeout(500)
                        await dt_input.fill(target_str)
                        await page.keyboard.press('Enter')
                        await page.wait_for_timeout(500)
                        print(f"   ✅ 定时发布: {target_str}")
                    else:
                        print("   ✅ 定时发布已开启（使用系统默认时间，未找到时间输入框）")
                except Exception:
                    print("   ✅ 定时发布已开启（使用系统默认时间）")
        else:
            await page.screenshot(path="/tmp/xhs_schedule_not_found.png")
            print("   ⚠️ 未找到定时发布开关（截图: /tmp/xhs_schedule_not_found.png）")
    except Exception as e:
        await page.screenshot(path="/tmp/xhs_schedule_error.png")
        print(f"   ⚠️ 定时发布设置失败: {e}（截图: /tmp/xhs_schedule_error.png）")


async def publish_note(
    title: str,
    content: str,
    image_paths: list[str],
    tags: list[str] | None = None,
    original: bool = True,
    collection: str | None = None,
    schedule_time: str | None = "default",
    auto_publish: bool = False,
    headless: bool = False,
):
    """Publish an image-text note to Xiaohongshu using pure Playwright."""
    if not XHS_STORAGE_PATH.exists():
        print("❌ 未找到登录 session，请先运行: python xhs_publish_playwright.py login")
        return False

    # Validate image paths
    resolved_paths = []
    for img in image_paths:
        p = Path(img).resolve()
        if not p.exists():
            print(f"❌ 图片不存在: {p}")
            return False
        resolved_paths.append(str(p))

    if not resolved_paths:
        print("❌ 至少需要 1 张图片")
        return False

    # 小红书标题限制 20 字（emoji 也算 1 个字符）
    char_count = len(title)
    if char_count > 20:
        print(f"⚠️  标题超过 20 字（当前 {char_count} 字），自动截断为 20 字")
        title = title[:20]
    print(f"📏 标题长度: {char_count} 字 → {'OK' if char_count <= 20 else '已截断'}")

    # Strip URLs from content
    clean_content = _strip_urls(content)
    clean_content = re.sub(r'\n{3,}', '\n\n', clean_content).strip()

    async with async_playwright() as p:
        print("🚀 正在启动浏览器...")
        sys.stdout.flush()
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            storage_state=str(XHS_STORAGE_PATH),
            **BROWSER_OPTS,
        )
        page = await context.new_page()
        print("   ✅ 浏览器已启动")
        sys.stdout.flush()

        try:
            # Step 1: Navigate to publish page
            print("📝 正在打开小红书创作者平台...")
            sys.stdout.flush()
            await page.goto(XHS_PUBLISH_URL, wait_until="domcontentloaded", timeout=90000)
            print("   ✅ 页面已加载，等待渲染...")
            sys.stdout.flush()
            await page.wait_for_timeout(5000)

            if "login" in page.url.lower() or "passport" in page.url.lower():
                print("❌ Session 已过期，请重新登录: python xhs_publish_playwright.py login")
                return False

            # Debug: dump page URL and main content area
            print(f"   📍 当前 URL: {page.url}")
            # Dump key text on the page to understand structure
            page_text = await page.evaluate("""() => {
                // Get text from main content area
                const main = document.querySelector('.main-content, .publish-container, [class*="publish"], [class*="creator"], #app') || document.body;
                // Get all visible text nodes
                const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
                const texts = [];
                while (walker.nextNode()) {
                    const t = walker.currentNode.textContent.trim();
                    if (t && t.length > 1) texts.push(t);
                }
                return texts.slice(0, 50).join(' | ');
            }""")
            print(f"   📄 页面文本: {page_text[:500]}")
            
            # Step 2: Click "发布笔记" to open publish UI, then switch to "上传图文" tab
            print("📤 进入发布编辑器...")
            # Click the publish button area to trigger Vue component rendering
            await page.evaluate("""() => {
                const btn = document.querySelector('.btn-inner, .btn-wrapper, .publish-video');
                if (btn) btn.click();
            }""")
            await page.wait_for_timeout(5000)

            # Now wait for the "上传图文" tab to appear and click it
            print("📤 切换到图文发布页面...")
            sys.stdout.flush()
            for attempt in range(10):
                switched = await page.evaluate("""() => {
                    const allEls = document.querySelectorAll('span, div, a');
                    for (const el of allEls) {
                        if (el.textContent.trim() === '上传图文' && el.children.length === 0) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }""")
                if switched:
                    print(f"   ✅ 成功切换到图文 tab (尝试 {attempt+1})")
                    break
                print(f"   ⏳ 等待图文 tab 出现... (尝试 {attempt+1}/10)")
                await page.wait_for_timeout(3000)

            if not switched:
                await page.screenshot(path="/tmp/xhs_no_tab.png")
                print("   ❌ 未找到「上传图文」tab")
                return False

            await page.wait_for_timeout(3000)
            await page.screenshot(path="/tmp/xhs_after_tab_switch.png")

            # Step 3: Upload images
            print(f"🖼️  上传 {len(resolved_paths)} 张图片...")

            # Wait for image file input to appear
            img_file_input = None
            for attempt in range(10):
                all_inputs_info = await page.evaluate("""() => {
                    const inputs = document.querySelectorAll('input[type="file"]');
                    return Array.from(inputs).map((el, i) => ({
                        index: i,
                        accept: el.getAttribute('accept') || '',
                        multiple: el.multiple,
                        className: el.className
                    }));
                }""")
                for info in all_inputs_info:
                    accept = info.get('accept', '')
                    if 'image' in accept or '.jpg' in accept or '.png' in accept or '.jpeg' in accept or '.webp' in accept:
                        img_file_input = page.locator('input[type="file"]').nth(info['index'])
                        print(f"   ✅ 找到图片 file input #{info['index']} (accept={accept})")
                        break
                    if info.get('multiple') and '.mp4' not in accept:
                        img_file_input = page.locator('input[type="file"]').nth(info['index'])
                        print(f"   ✅ 找到 multiple file input #{info['index']} (accept={accept})")
                        break
                if img_file_input:
                    break
                print(f"   ⏳ 等待图片 file input 出现... (尝试 {attempt+1}/10)")
                await page.wait_for_timeout(3000)

            if img_file_input is None:
                print(f"   🔍 当前 file inputs: {all_inputs_info}")
                await page.screenshot(path="/tmp/xhs_no_img_input.png")
                print("   ❌ 未找到图片上传 input")
                return False

            await img_file_input.set_input_files(resolved_paths)

            print("⏳ 等待图片上传完成...")
            upload_done = False
            for wait_round in range(24):  # 24 rounds × 5s = 120s total
                try:
                    await page.wait_for_selector(
                        'input[placeholder*="填写标题"]',
                        state="visible",
                        timeout=5_000,
                    )
                    upload_done = True
                    break
                except Exception:
                    elapsed = (wait_round + 1) * 5
                    print(f"   ⏳ 上传中... ({elapsed}s)")
                    sys.stdout.flush()
            if not upload_done:
                print("⚠️  等待标题输入框超时，继续尝试...")
                await page.wait_for_timeout(10000)

            await page.wait_for_timeout(2000)
            print("✅ 图片上传完成")

            # Step 4: Fill title
            print("📝 填写标题...")
            title_input = page.locator('input[placeholder*="填写标题"]').first
            await title_input.click()
            await title_input.fill(title)
            await page.wait_for_timeout(500)
            print(f"   ✅ 标题: {title}")

            # Step 5: Fill content
            print("📝 填写正文...")
            await _fill_content_prosemirror(page, clean_content)
            await page.wait_for_timeout(500)
            print("   ✅ 正文已填写")

            # Step 6: Add tags
            if tags:
                print(f"🏷️  添加标签: {', '.join(tags)}...")
                await _add_tags(page, tags)
                print("   ✅ 标签已添加")

            # Step 7: Select collection (in "内容设置" section, top item)
            if collection:
                print(f"📂 选择合集: {collection}...")
                await _select_collection(page, collection)

            # Step 8: Original declaration (in "内容设置" section, below collection)
            if original:
                print("📜 设置原创声明...")
                await _toggle_original(page)

            # Step 9: Set schedule
            if schedule_time:
                print(f"⏰ 设置定时发布: {schedule_time}...")
                await _set_schedule(page, schedule_time)

            # Preview
            print("\n" + "=" * 60)
            print("📋 发布预览")
            print(f"   标题: {title}")
            print(f"   正文: {content[:50]}..." if len(content) > 50 else f"   正文: {content}")
            print(f"   图片: {len(resolved_paths)} 张")
            print(f"   标签: {', '.join(tags) if tags else '无'}")
            print(f"   原创: {'是' if original else '否'}")
            print(f"   合集: {collection if collection else '无'}")
            print(f"   定时: {schedule_time if schedule_time else '立即发布'}")
            print("=" * 60)

            # --- Confirmation: use file-based signaling ---
            # Write a signal file; inject a top-bar in browser; poll the file for user decision.
            import os
            signal_file = "/tmp/xhs_publish_signal"
            # Clean up any stale signal
            if os.path.exists(signal_file):
                os.remove(signal_file)

            print("\n⏸️  内容已填写完毕。")
            print("   浏览器中已显示确认提示条，请检查内容后点击按钮。")
            print("   ⏳ 等待浏览器确认（最长 30 分钟）...")
            sys.stdout.flush()

            await page.screenshot(path="/tmp/xhs_before_confirm.png")

            # Inject a non-blocking top banner with confirm/cancel buttons
            await page.evaluate("""(signalPath) => {
                // Remove any existing banner
                const existing = document.getElementById('xhs-confirm-banner');
                if (existing) existing.remove();

                const banner = document.createElement('div');
                banner.id = 'xhs-confirm-banner';
                banner.style.cssText = `
                    position: fixed; top: 0; left: 0; right: 0; z-index: 999999;
                    background: linear-gradient(135deg, #ff4757, #ff6b81);
                    color: white; padding: 14px 24px;
                    display: flex; align-items: center; justify-content: center; gap: 20px;
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                    font-size: 15px; box-shadow: 0 4px 20px rgba(255,71,87,0.4);
                `;
                banner.innerHTML = `
                    <span style="font-weight: 500;">📋 请检查内容，确认无误后点击发布</span>
                    <button id="xhs-confirm-cancel" style="
                        padding: 8px 24px; border-radius: 6px; border: 2px solid rgba(255,255,255,0.8);
                        background: transparent; color: white; font-size: 14px; cursor: pointer;
                        font-weight: 500;
                    ">取消</button>
                    <button id="xhs-confirm-publish" style="
                        padding: 8px 24px; border-radius: 6px; border: none;
                        background: white; color: #ff4757; font-size: 14px; cursor: pointer;
                        font-weight: 700;
                    ">✓ 确认发布</button>
                `;
                document.body.appendChild(banner);
                // Shift page content down so banner doesn't cover it
                document.body.style.marginTop = '52px';

                // Store decision in window for Playwright polling
                window.__xhs_publish_decision = 'pending';
                document.getElementById('xhs-confirm-publish').addEventListener('click', () => {
                    window.__xhs_publish_decision = 'confirmed';
                    banner.style.background = '#2ed573';
                    banner.innerHTML = '<span style="font-weight:600;">✅ 已确认，正在发布...</span>';
                });
                document.getElementById('xhs-confirm-cancel').addEventListener('click', () => {
                    window.__xhs_publish_decision = 'cancelled';
                    banner.style.background = '#666';
                    banner.innerHTML = '<span style="font-weight:600;">❌ 已取消发布</span>';
                    setTimeout(() => banner.remove(), 2000);
                });
            }""", signal_file)

            # Poll for user's decision via window variable (every 2s, up to 30 min)
            max_wait = 1800
            waited = 0
            user_confirmed = None
            while waited < max_wait:
                try:
                    decision = await page.evaluate("() => window.__xhs_publish_decision || 'pending'")
                except Exception:
                    decision = 'pending'
                if decision == 'confirmed':
                    user_confirmed = True
                    break
                elif decision == 'cancelled':
                    user_confirmed = False
                    break
                await asyncio.sleep(2)
                waited += 2
                # Print progress every 30 seconds to avoid idle timeout
                if waited % 30 == 0:
                    print(f"   ⏳ 等待浏览器确认... ({waited}s)")
                    sys.stdout.flush()

            if waited >= max_wait:
                print("\n   ⏰ 确认超时（30 分钟），已取消发布")
                return False

            if not user_confirmed:
                print("❌ 用户取消发布")
                return False

            print("✅ 用户已确认，正在发布...")

            # Click publish button — try multiple selectors for the new XHS UI
            print("📤 正在点击发布按钮...")
            publish_clicked = False

            # Strategy 1: Find the primary action button (usually bottom-right, red/orange)
            # In new XHS UI the publish button text may be "发布" or "发布笔记"
            for btn_text in ['发布笔记', '发布']:
                try:
                    # Use strict text matching to avoid matching other elements containing "发布"
                    btns = page.locator(f'button:has-text("{btn_text}")')
                    count = await btns.count()
                    for i in range(count):
                        btn = btns.nth(i)
                        if await btn.is_visible(timeout=2000):
                            # Skip if it's the top-level navigation "发布笔记" button
                            btn_classes = await btn.get_attribute('class') or ''
                            btn_parent_text = await btn.evaluate('el => el.parentElement?.className || ""')
                            # The real publish button is usually at the bottom of the form
                            bbox = await btn.bounding_box()
                            if bbox and bbox['y'] > 400:  # Bottom half of page
                                await btn.click()
                                publish_clicked = True
                                print(f"   ✅ 点击了「{btn_text}」按钮 (位置 y={bbox['y']:.0f})")
                                break
                    if publish_clicked:
                        break
                except Exception:
                    continue

            # Strategy 2: JS fallback — find the submit/publish button
            if not publish_clicked:
                publish_clicked = await page.evaluate("""() => {
                    // Look for buttons with publish-related text at the bottom of the form
                    const buttons = document.querySelectorAll('button, [role="button"]');
                    const candidates = [];
                    for (const btn of buttons) {
                        const text = btn.textContent.trim();
                        if ((text === '发布' || text === '发布笔记') && btn.offsetParent !== null) {
                            const rect = btn.getBoundingClientRect();
                            candidates.push({ btn, y: rect.y, text });
                        }
                    }
                    // Click the one that's furthest down (most likely the form submit button)
                    candidates.sort((a, b) => b.y - a.y);
                    if (candidates.length > 0) {
                        candidates[0].btn.click();
                        return true;
                    }
                    return false;
                }""")

            if publish_clicked:
                await page.wait_for_timeout(5000)
                await page.screenshot(path="/tmp/xhs_after_publish.png")
                print("✅ 已执行发布！（截图: /tmp/xhs_after_publish.png）")
            else:
                await page.screenshot(path="/tmp/xhs_publish_btn_not_found.png")
                print("⚠️ 未找到发布按钮，请在浏览器中手动点击发布（截图: /tmp/xhs_publish_btn_not_found.png）")

            # Save storage state
            await context.storage_state(path=str(XHS_STORAGE_PATH))
            return True

        except Exception as e:
            print(f"❌ 发布失败: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            # Wait a bit before closing to let the publish request complete
            await asyncio.sleep(3)
            await browser.close()


async def publish_from_config(
    config_path: str,
    auto_publish: bool = False,
    headless: bool = False,
    schedule_time: str | None = "default",
):
    """Publish a note from a JSON config file."""
    config_file = Path(config_path).resolve()
    if not config_file.exists():
        print(f"❌ 配置文件不存在: {config_file}")
        return False

    with open(config_file, "r", encoding="utf-8") as f:
        config = json.load(f)

    title = config.get("title", "")
    content = config.get("content", "")
    images = config.get("images", [])
    tags = config.get("tags", [])
    original = config.get("original", True)
    collection = config.get("collection", None)
    cfg_schedule = config.get("schedule_time", schedule_time)

    if not title:
        print("❌ 配置文件中缺少 title 字段")
        return False
    if not images:
        print("❌ 配置文件中缺少 images 字段")
        return False

    config_dir = config_file.parent
    resolved_images = []
    for img in images:
        ip = Path(img)
        if not ip.is_absolute():
            ip = config_dir / ip
        resolved_images.append(str(ip.resolve()))

    return await publish_note(
        title=title,
        content=content,
        image_paths=resolved_images,
        tags=tags,
        original=original,
        collection=collection,
        schedule_time=cfg_schedule,
        auto_publish=auto_publish,
        headless=headless,
    )


def main():
    parser = argparse.ArgumentParser(
        description="小红书图文发布工具 (纯 Playwright 版)"
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    subparsers.add_parser("login", help="扫码登录小红书")
    subparsers.add_parser("check", help="检查登录状态")

    pub_parser = subparsers.add_parser("publish", help="发布图文笔记")
    pub_parser.add_argument("--config", help="JSON 配置文件路径")
    pub_parser.add_argument("--copywriting", help="copywriting.txt 文件路径")
    pub_parser.add_argument("--title", help="笔记标题")
    pub_parser.add_argument("--content", help="笔记正文")
    pub_parser.add_argument("--images", nargs="+", help="图片路径列表")
    pub_parser.add_argument("--tags", nargs="*", help="标签列表")
    pub_parser.add_argument("--original", action="store_true", default=True, help="原创声明（默认开启）")
    pub_parser.add_argument("--no-original", action="store_true", help="不勾选原创声明")
    pub_parser.add_argument("--collection", help="合集名称")
    pub_parser.add_argument("--schedule", default="default", help="定时发布 HH:MM（默认 'default' 使用系统默认时间）。设为 'now'/'none' 则立即发布")
    pub_parser.add_argument("--auto", action="store_true", help="跳过确认直接发布")
    pub_parser.add_argument("--headless", action="store_true", help="无头模式")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "login":
        asyncio.run(login())
    elif args.command == "check":
        asyncio.run(check_login())
    elif args.command == "publish":
        schedule = args.schedule if args.schedule not in ('now', 'none') else None
        if args.config:
            asyncio.run(publish_from_config(args.config, auto_publish=args.auto, headless=args.headless, schedule_time=schedule))
        elif args.copywriting and args.images:
            cw_path = Path(args.copywriting).resolve()
            if not cw_path.exists():
                print(f"❌ copywriting 文件不存在: {cw_path}")
                sys.exit(1)
            cw_text = cw_path.read_text(encoding="utf-8").strip()
            cw_lines = cw_text.split("\n")
            raw_title = cw_lines[0].strip()
            # 保留标题中的 emoji，不做任何清理
            cw_title = raw_title
            cw_tags = []
            content_end = len(cw_lines)
            for i in range(len(cw_lines) - 1, 0, -1):
                line = cw_lines[i].strip()
                if line.startswith("#"):
                    cw_tags = [t.strip().lstrip("#") for t in line.split("#") if t.strip()]
                    content_end = i
                    break
            cw_content = "\n".join(cw_lines[1:content_end]).strip()
            original = not args.no_original
            asyncio.run(publish_note(
                title=cw_title, content=cw_content, image_paths=args.images,
                tags=cw_tags or args.tags or [], original=original,
                collection=args.collection, schedule_time=schedule,
                auto_publish=args.auto, headless=args.headless,
            ))
        elif args.title and args.images:
            original = not args.no_original
            asyncio.run(publish_note(
                title=args.title, content=args.content or "", image_paths=args.images,
                tags=args.tags, original=original, collection=args.collection,
                schedule_time=schedule, auto_publish=args.auto, headless=args.headless,
            ))
        else:
            print("❌ 请提供 --config/--copywriting 配置文件路径，或同时提供 --title 和 --images")
            pub_parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
