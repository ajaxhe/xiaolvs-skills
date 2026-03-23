#!/usr/bin/env python3
"""
Publish image-card posts (贴图) to WeChat Official Account (微信公众号)
via browser-use Agent + Playwright.

This script uses browser-use (AI-driven browser automation) for complex UI
interactions (filling title/description, handling popups, collection selection),
while keeping Playwright for simple operations (login QR scan, image upload
via file input).

Requires:
  - browser-use >= 0.12.0
  - GEMINI_API_KEY or GOOGLE_API_KEY env var (for Google Gemini LLM)

Usage:
    # First-time login (scan QR code)
    python wechat_mp_publish.py login

    # Check login status
    python wechat_mp_publish.py check

    # Inspect the DOM of the 贴图 editor page (for development)
    python wechat_mp_publish.py inspect

    # Publish from a JSON config file
    python wechat_mp_publish.py publish --config <path_to_config.json>

Config JSON format:
{
    "title": "贴图标题",
    "images": ["path/to/img1.png", "path/to/img2.png"],
    "description": "可选描述",
    "collection": "可选合集名称（如 AI前沿）"
}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Storage state file for WeChat MP login session
WX_STORAGE_DIR = Path.home() / ".wechat_mp"
WX_STORAGE_PATH = WX_STORAGE_DIR / "storage_state.json"

# WeChat MP URLs
WX_MP_URL = "https://mp.weixin.qq.com"
WX_MP_HOME_URL = "https://mp.weixin.qq.com/cgi-bin/home"

# Common browser context options
BROWSER_OPTS = dict(
    viewport={"width": 1440, "height": 900},
    user_agent=(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
)


async def _goto_home_and_extract_token(page) -> str | None:
    """Navigate to WeChat MP home page and extract the token from URL.

    The WeChat MP backend requires a `token` query parameter for all internal
    pages.  After loading storage_state, visiting the root URL redirects to
    the home page whose URL contains `token=<value>`.

    Returns the token string, or None if session is invalid.
    """
    await page.goto(WX_MP_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    current_url = page.url
    if "login" in current_url.lower() or current_url.rstrip("/") == WX_MP_URL:
        return None

    parsed = urlparse(current_url)
    token_list = parse_qs(parsed.query).get("token", [])
    if token_list:
        return token_list[0]

    # Fallback: try to extract from page JS context
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
    """Build the 贴图 editor URL with token.

    Key params from the official '贴图' entry:
      - t=media/appmsg_edit_v2  (v2 editor)
      - isNew=1                 (new post)
      - type=77                 (贴图 content type)
      - createType=8            (贴图 creation type — critical)
      - lang=zh_CN
    """
    ts = int(time.time() * 1000)
    return (
        "https://mp.weixin.qq.com/cgi-bin/appmsg"
        f"?t=media/appmsg_edit_v2&action=edit&isNew=1&type=77"
        f"&createType=8&token={token}&lang=zh_CN&timestamp={ts}"
    )


async def login(headless: bool = False):
    """Login to WeChat MP via QR code scan.

    Opens the mp.weixin.qq.com login page and waits for user to scan QR code.
    Saves the authenticated session to storage_state.json for future use.
    """
    from playwright.async_api import async_playwright

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

        # Wait for successful login — detect by URL navigating to home/dashboard
        try:
            await page.wait_for_url(
                "**/cgi-bin/home**",
                timeout=120_000,  # 2 minutes to scan QR code
            )
        except Exception:
            # Fallback: wait for any navigation away from login
            try:
                await page.wait_for_selector(
                    ".weui-desktop-account__nickname, .main_bd, #app",
                    timeout=120_000,
                )
            except Exception:
                print("⚠️  登录超时，请在 2 分钟内完成扫码")
                await browser.close()
                return False

        # Small delay to ensure cookies are set
        await page.wait_for_timeout(3000)

        # Save storage state
        await context.storage_state(path=str(WX_STORAGE_PATH))
        print(f"\n✅ 登录成功！Session 已保存到 {WX_STORAGE_PATH}")
        await browser.close()
        return True


async def check_login() -> bool:
    """Check if the saved session is still valid."""
    if not WX_STORAGE_PATH.exists():
        print("❌ 未找到登录 session，请先运行: python wechat_mp_publish.py login")
        return False

    from playwright.async_api import async_playwright

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

        # If still on login page, session is expired
        if "login" in url.lower() or url.rstrip("/") == WX_MP_URL:
            print("❌ Session 已过期，请重新登录: python wechat_mp_publish.py login")
            return False

        print("✅ Session 有效")
        return True


async def inspect_page():
    """Open the 贴图 editor page and inspect DOM structure.

    This is a development helper to understand the page layout and find
    the correct selectors for automation.
    """
    if not WX_STORAGE_PATH.exists():
        print("❌ 未找到登录 session，请先运行: python wechat_mp_publish.py login")
        return

    from playwright.async_api import async_playwright

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
            print("❌ Session 已过期或无法提取 token，请重新登录: python wechat_mp_publish.py login")
            await browser.close()
            return

        current_url = page.url
        print(f"首页URL: {current_url}")
        print(f"提取到 token: {token}")

        # Take screenshot of home page
        await page.screenshot(path="/tmp/wx_mp_home.png", full_page=True)
        print("📸 首页截图已保存到 /tmp/wx_mp_home.png")

        # Navigate to the 贴图 editor with token
        edit_url = _build_tietie_edit_url(token)
        print(f"\n📝 正在访问贴图编辑页面...")
        print(f"URL: {edit_url}")
        await page.goto(edit_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        current_url = page.url
        print(f"贴图编辑页URL: {current_url}")

        # Take screenshot
        await page.screenshot(path="/tmp/wx_mp_tietie_edit.png", full_page=True)
        print("📸 贴图编辑页截图已保存到 /tmp/wx_mp_tietie_edit.png")

        # Inspect key elements
        print("\n--- DOM 检查 ---")

        # Check all input elements
        inputs = await page.locator("input").all()
        print(f"\n[input 元素] 共 {len(inputs)} 个:")
        for i, inp in enumerate(inputs):
            try:
                inp_type = await inp.get_attribute("type") or ""
                inp_class = await inp.get_attribute("class") or ""
                inp_placeholder = await inp.get_attribute("placeholder") or ""
                inp_name = await inp.get_attribute("name") or ""
                inp_id = await inp.get_attribute("id") or ""
                visible = await inp.is_visible()
                print(
                    f"  input[{i}]: type={inp_type}, id={inp_id}, name={inp_name}, "
                    f"class={inp_class[:60]}, placeholder={inp_placeholder}, visible={visible}"
                )
            except Exception as e:
                print(f"  input[{i}]: error - {e}")

        # Check all textarea elements
        textareas = await page.locator("textarea").all()
        print(f"\n[textarea 元素] 共 {len(textareas)} 个:")
        for i, ta in enumerate(textareas):
            try:
                ta_class = await ta.get_attribute("class") or ""
                ta_placeholder = await ta.get_attribute("placeholder") or ""
                ta_name = await ta.get_attribute("name") or ""
                ta_id = await ta.get_attribute("id") or ""
                visible = await ta.is_visible()
                print(
                    f"  textarea[{i}]: id={ta_id}, name={ta_name}, "
                    f"class={ta_class[:60]}, placeholder={ta_placeholder}, visible={visible}"
                )
            except Exception as e:
                print(f"  textarea[{i}]: error - {e}")

        # Check contenteditable elements
        editables = await page.locator("[contenteditable]").all()
        print(f"\n[contenteditable 元素] 共 {len(editables)} 个:")
        for i, el in enumerate(editables):
            try:
                el_tag = await el.evaluate("el => el.tagName")
                el_class = await el.get_attribute("class") or ""
                el_ce = await el.get_attribute("contenteditable") or ""
                parent_class = await el.evaluate("el => el.parentElement?.className || ''")
                visible = await el.is_visible()
                print(
                    f"  contenteditable[{i}]: tag={el_tag}, class={el_class[:60]}, "
                    f"contenteditable={el_ce}, parent_class={parent_class[:60]}, visible={visible}"
                )
            except Exception as e:
                print(f"  contenteditable[{i}]: error - {e}")

        # Check file input (for image upload)
        file_inputs = await page.locator('input[type="file"]').all()
        print(f"\n[file input 元素] 共 {len(file_inputs)} 个:")
        for i, fi in enumerate(file_inputs):
            try:
                accept = await fi.get_attribute("accept") or ""
                multiple = await fi.get_attribute("multiple")
                fi_id = await fi.get_attribute("id") or ""
                fi_name = await fi.get_attribute("name") or ""
                print(
                    f"  file[{i}]: id={fi_id}, name={fi_name}, accept={accept}, multiple={multiple}"
                )
            except Exception as e:
                print(f"  file[{i}]: error - {e}")

        # Check buttons
        buttons = await page.locator("button, a.btn, .btn, [role='button']").all()
        print(f"\n[按钮元素] 共 {len(buttons)} 个:")
        for i, btn in enumerate(buttons):
            try:
                btn_text = (await btn.inner_text()).strip()[:40]
                btn_class = await btn.get_attribute("class") or ""
                btn_id = await btn.get_attribute("id") or ""
                visible = await btn.is_visible()
                if btn_text or visible:
                    print(
                        f"  button[{i}]: text={btn_text}, id={btn_id}, "
                        f"class={btn_class[:60]}, visible={visible}"
                    )
            except Exception:
                pass

        # Check iframes (WeChat MP might use iframes for the editor)
        iframes = await page.locator("iframe").all()
        print(f"\n[iframe 元素] 共 {len(iframes)} 个:")
        for i, iframe in enumerate(iframes):
            try:
                src = await iframe.get_attribute("src") or ""
                iframe_id = await iframe.get_attribute("id") or ""
                iframe_class = await iframe.get_attribute("class") or ""
                visible = await iframe.is_visible()
                print(
                    f"  iframe[{i}]: id={iframe_id}, class={iframe_class[:40]}, "
                    f"src={src[:80]}, visible={visible}"
                )
            except Exception as e:
                print(f"  iframe[{i}]: error - {e}")

        # Look for specific 贴图-related elements
        print("\n--- 贴图相关元素 ---")

        # Check for image upload area
        upload_areas = await page.locator(
            '[class*="upload"], [class*="drop"], [class*="image-picker"], '
            '[class*="add-pic"], [class*="pic_upload"]'
        ).all()
        print(f"上传区域: {len(upload_areas)} 个")
        for i, area in enumerate(upload_areas):
            try:
                area_class = await area.get_attribute("class") or ""
                area_text = (await area.inner_text()).strip()[:40]
                visible = await area.is_visible()
                if visible:
                    print(f"  upload[{i}]: class={area_class[:60]}, text={area_text}")
            except Exception:
                pass

        print("\n⏸️  浏览器保持打开，可以手动检查页面。按 Enter 关闭...")
        await asyncio.get_event_loop().run_in_executor(None, input)

        await context.storage_state(path=str(WX_STORAGE_PATH))
        await browser.close()


def _wx_char_count(text: str) -> int:
    """Calculate title length using WeChat's rule: CJK/fullwidth=1, ASCII=0.5 (ceil)."""
    import math
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


def _build_navigate_task() -> str:
    """Build task prompt for navigating to the 贴图 editor."""
    return (
        "请按以下步骤进入微信公众号后台的贴图编辑页面：\n"
        "a. 先导航到微信公众号后台首页：https://mp.weixin.qq.com/\n"
        "b. 等待页面完全加载（大约 5 秒），确认已经登录成功（能看到后台管理界面）。\n"
        "c. 在首页找到并点击左侧菜单中的「内容与互动」>「图文/视频」或直接找到「贴图」入口。\n"
        "   如果没有直接的贴图入口，找到页面上的「新的创作」或「+」按钮，点击后在弹出菜单中选择「贴图」。\n"
        "d. 等待贴图编辑页面加载完成（大约 5 秒）。\n"
        "e. 确认已进入贴图编辑器（页面上应有图片上传区域、标题输入框等）。\n\n"
        "重要提示：\n"
        "- 这是微信公众号的贴图（图片合集）编辑器，不是普通文章编辑器。\n"
        "- 请使用中文操作界面。"
    )


def _build_upload_one_image_task(image_path: str, index: int, total: int) -> str:
    """Build task prompt for uploading a single image.

    Each image upload is a separate Agent call to ensure reliability.
    """
    label = 'infographic' if 'infographic' in image_path else f'chart'
    return (
        f"当前在微信公众号后台的贴图编辑页面。请上传第 {index}/{total} 张图片（{label}）。\n\n"
        f"你只需要执行以下 2 步，不要做任何多余操作：\n\n"
        f"第一步：使用 upload_file 操作上传这个文件（只执行一次 upload_file，绝对不要执行第二次）：\n"
        f"   {image_path}\n\n"
        f"第二步：使用 wait 操作等待 10 秒。\n\n"
        f"等待结束后，直接调用 done 报告完成即可。\n\n"
        f"【严格禁止】\n"
        f"- 禁止执行两次或更多次 upload_file\n"
        f"- 禁止填写标题、描述或其他任何内容\n"
        f"- 禁止点击任何按钮（上传按钮除外）"
    )


def _build_fill_and_save_task(
    title: str,
    description: str,
    tags: list[str],
    collection: str,
    auto_publish: bool,
    num_images: int,
) -> str:
    """Build task prompt for filling title, description, collection, and saving."""
    task_parts = []

    task_parts.append(
        f"当前在微信公众号后台的贴图编辑页面，{num_images} 张图片已上传完毕。\n"
        "请按顺序完成以下操作："
    )

    # Step 1: Fill title (preserve emoji)
    step = 1
    wx_len = _wx_char_count(title)
    if wx_len > 20:
        display_title = _wx_truncate(title, 20)
    else:
        display_title = title
    task_parts.append(
        f'\n{step}. 填写标题（注意保留标题中的emoji符号）：找到标题输入框（textarea 或 placeholder 包含"标题"的输入框），'
        f'清空已有内容，然后输入以下标题（包括开头的emoji，不要省略任何字符）：\n'
        f'   「{display_title}」'
    )

    # Step 2: Fill description — exact formatting, with tags appended as last line
    step += 1
    full_description = description or ""
    if tags:
        tags_line = " ".join(f"#{t}" for t in tags)
        if full_description:
            full_description = full_description + "\n\n" + tags_line
        else:
            full_description = tags_line

    if full_description:
        desc_lines = full_description.split("\n")
        lines_instruction = []
        for i, line in enumerate(desc_lines):
            if line.strip() == "":
                lines_instruction.append(f"   第{i+1}行: （空行）→ 直接按 Enter 键")
            else:
                lines_instruction.append(f"   第{i+1}行: {line}")
        lines_text = "\n".join(lines_instruction)

        task_parts.append(
            f'\n{step}. 填写描述/正文（非常重要，必须逐行输入，严格保持格式）：\n'
            f'   找到描述/正文编辑区域（通常是 ProseMirror 富文本编辑器，'
            f'class 包含 "ProseMirror" 的 contenteditable div），点击进入编辑状态。\n\n'
            f'   【关键】不要使用 input_text 一次性输入全部内容！必须逐行输入：\n'
            f'   共 {len(desc_lines)} 行，逐行输入方式如下：\n'
            f'{lines_text}\n\n'
            f'   操作方法：\n'
            f'   a. 对于有文字的行：使用 input_text 输入该行文字，然后用 key_press 按 Enter 键换行。\n'
            f'   b. 对于空行：直接用 key_press 按 Enter 键（产生一个空行）。\n'
            f'   c. 最后一行输入完毕后不需要再按 Enter。\n'
            f'   d. 最后一行是标签行（以 # 开头），标签行的输入方式：输入 #，紧接着输入标签文字，然后输入一个空格，再输入下一个 #标签，以此类推。\n'
            f'   e. 输入完成后检查，确认所有 {len(desc_lines)} 行都已输入且格式正确。'
        )
    else:
        task_parts.append(f'\n{step}. 跳过描述（不需要填写）。')

    # Step 3: Skip 原创声明/创作来源
    step += 1
    task_parts.append(
        f'\n{step}. 跳过「原创」/「创作来源」设置：不要点击任何与「原创」、「创作来源」、'
        f'「声明原创」相关的按钮或开关。保持默认状态即可。'
    )

    # Step 4: Collection
    step += 1
    if collection:
        task_parts.append(
            f'\n{step}. 选择合集（重要，必须完成）：\n'
            f'   a. 在页面右侧的设置面板中，找到「合集」区域。\n'
            f'   b. 点击「合集」区域中的「未添加」或下拉选择按钮，打开合集选择弹窗。\n'
            f'   c. 在弹出的对话框中，找到「{collection}」并点击选中（确保被勾选/高亮）。\n'
            f'   d. 点击弹窗中的「确定」或「确认」按钮完成选择。\n'
            f'   e. 确认合集区域已显示「{collection}」。如果没有，请重试。'
        )
    else:
        task_parts.append(f'\n{step}. 跳过合集选择（不需要）。')

    # Step 5: Save as draft
    step += 1
    if auto_publish:
        task_parts.append(
            f'\n{step}. 保存为草稿：找到并点击页面上的「保存为草稿」按钮。'
            f'如果出现确认对话框，点击「确定」或「我知道了」。'
            f'等待页面响应。注意：只保存为草稿，不要点击「发表」或「群发」。'
        )
    else:
        task_parts.append(
            f'\n{step}. 检查所有内容是否正确填写。'
            f'任务完成，不要点击任何保存或发布按钮（用户会手动确认）。'
        )

    task_parts.append(
        '\n重要提示：'
        '\n- 每一步操作后等待页面响应再进行下一步。'
        '\n- 如果某个元素暂时不可见，先尝试上下滚动页面查找。'
        '\n- 操作过程中如遇到弹窗或对话框，先处理弹窗再继续。'
        '\n- 请使用中文操作界面。'
        '\n- 这是微信公众号的贴图（图片合集）编辑器，不是普通文章编辑器。'
        '\n- 绝对不要点击或修改任何「原创」「创作来源」相关的设置。'
    )

    return "\n".join(task_parts)


async def publish_post(
    title: str,
    image_paths: list[str],
    description: str = "",
    tags: list[str] | None = None,
    collection: str = "",
    auto_publish: bool = False,
    headless: bool = False,
):
    """Publish an image-card post (贴图) to WeChat MP.

    Uses browser-use Agent for all browser interactions: navigation,
    image upload (one by one), form filling (title, description, tags,
    collection), and saving to draft.

    Args:
        title: Post title.
        image_paths: List of absolute paths to image files.
        description: Optional description text (preserving exact line breaks).
        tags: Optional list of tag strings (without #).
        collection: Optional collection name (e.g. "AI前沿").
        auto_publish: If True, save to drafts directly without pausing.
        headless: Run browser in headless mode.
    """
    if not WX_STORAGE_PATH.exists():
        print("❌ 未找到登录 session，请先运行: python wechat_mp_publish.py login")
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

    # Load API key from env
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("❌ 未设置 GEMINI_API_KEY 或 GOOGLE_API_KEY 环境变量")
        print("   请设置: export GEMINI_API_KEY='your-api-key'")
        return False

    # --- Use browser-use Agent for everything ---
    from browser_use import Agent
    from browser_use.browser.session import BrowserSession
    from browser_use.browser.profile import BrowserProfile
    from browser_use.llm.google.chat import ChatGoogle

    llm = ChatGoogle(model="gemini-2.5-pro", api_key=api_key)

    browser_profile = BrowserProfile(
        headless=headless,
        viewport={"width": 1440, "height": 900},
        enable_default_extensions=False,
        user_data_dir=None,
    )
    browser_session = BrowserSession(
        browser_profile=browser_profile,
        storage_state=str(WX_STORAGE_PATH),
        keep_alive=True,
    )

    try:
        print("\n🤖 启动 AI Agent 执行页面操作...")

        # === Phase 1: Navigate to 贴图 editor ===
        print("\n📌 Phase 1/3: 导航到贴图编辑器...")
        nav_agent = Agent(
            task=_build_navigate_task(),
            llm=llm,
            browser=browser_session,
            use_vision=True,
            max_actions_per_step=5,
            max_failures=10,
        )
        await nav_agent.run()
        print("✅ 导航完成")

        # === Phase 2: Upload images one by one (each as a separate Agent call) ===
        print(f"\n📌 Phase 2/3: 逐张上传图片（共 {len(resolved_paths)} 张）...")
        for i, img_path in enumerate(resolved_paths):
            img_label = 'infographic' if 'infographic' in img_path else f'chart'
            print(f"\n   📤 上传第 {i+1}/{len(resolved_paths)} 张（{img_label}）: {Path(img_path).name}")

            upload_agent = Agent(
                task=_build_upload_one_image_task(img_path, i + 1, len(resolved_paths)),
                llm=llm,
                browser=browser_session,
                use_vision=True,
                max_actions_per_step=2,
                max_failures=3,
                available_file_paths=[img_path],
            )
            await upload_agent.run()
            print(f"   ✅ 第 {i+1} 张上传完成")

            # Rate-limiting wait between uploads
            if i < len(resolved_paths) - 1:
                wait_secs = 8
                print(f"   ⏳ 等待 {wait_secs} 秒（限频保护）...")
                await asyncio.sleep(wait_secs)

        print(f"\n✅ 全部 {len(resolved_paths)} 张图片上传完成")

        # === Phase 3: Fill title, description, collection, save ===
        print("\n📌 Phase 3/3: 填写标题/描述/合集/保存...")
        fill_task = _build_fill_and_save_task(
            title=title,
            description=description,
            tags=tags or [],
            collection=collection,
            auto_publish=auto_publish,
            num_images=len(resolved_paths),
        )
        fill_agent = Agent(
            task=fill_task,
            llm=llm,
            browser=browser_session,
            use_vision=True,
            max_actions_per_step=5,
            max_failures=10,
        )
        await fill_agent.run()
        print("✅ 填写和保存完成")

        # --- Preview and confirm ---
        print("\n" + "=" * 60)
        print("📋 发布预览")
        print(f"   标题: {title}")
        print(f"   描述: {description[:80]}..." if len(description) > 80 else f"   描述: {description or '无'}")
        print(f"   图片: {len(resolved_paths)} 张")
        print(f"   标签: {', '.join(tags) if tags else '无'}")
        if collection:
            print(f"   合集: {collection}")
        print("=" * 60)

        if not auto_publish:
            print("\n" + "=" * 60)
            print("⏸️  内容已填写完毕，浏览器保持打开。")
            print("   请在浏览器中检查并调整。")
            print("")
            print("   👉 输入 Enter 保存为草稿，输入 'p' 直接发表，输入 'q' 取消。")
            print("=" * 60)
            sys.stdout.flush()

            import threading
            confirmed = threading.Event()
            user_choice = {"value": "draft"}
            signal_file = Path("/tmp/wx_mp_close_browser")
            signal_file.unlink(missing_ok=True)

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
                if signal_file.exists():
                    signal_file.unlink(missing_ok=True)
                    confirmed.set()
                    break
                await asyncio.sleep(1)
                waited += 1

            if waited >= max_wait and not confirmed.is_set():
                print(f"\n   ⏰ 等待超时 ({max_wait}s)，自动关闭浏览器")
                return False

            choice = user_choice["value"]

            if choice in ("q", "quit", "cancel", "n", "no"):
                print("❌ 已取消发布")
                try:
                    await browser_session.export_storage_state(str(WX_STORAGE_PATH))
                except Exception:
                    pass
                return False

            if choice in ("p", "publish"):
                print("📤 正在发表...")
                publish_agent = Agent(
                    task='点击页面上的「发表」按钮。如果弹出确认对话框，点击「确定」。',
                    llm=llm,
                    browser=browser_session,
                    use_vision=True,
                    max_actions_per_step=2,
                    max_failures=3,
                )
                await publish_agent.run()
                await asyncio.sleep(3)
                print("✅ 发表完成！")
            else:
                print("📤 正在保存为草稿...")
                draft_agent = Agent(
                    task='点击页面上的「保存为草稿」按钮。如果出现确认对话框，点击「确定」或「我知道了」。',
                    llm=llm,
                    browser=browser_session,
                    use_vision=True,
                    max_actions_per_step=2,
                    max_failures=3,
                )
                await draft_agent.run()
                await asyncio.sleep(3)
                print("✅ 已保存为草稿！")

        # --- Wait for user confirmation before closing browser ---
        print("\n" + "=" * 60)
        print("⏸️  浏览器保持打开，请在浏览器中确认内容无误。")
        print("   👉 确认完毕后，在终端输入 OK（或任意内容）后按 Enter 关闭浏览器。")
        print("=" * 60)
        sys.stdout.flush()

        import threading as _threading
        close_confirmed = _threading.Event()

        def _wait_close_input():
            try:
                input("   等待确认 > ")
                close_confirmed.set()
            except (EOFError, KeyboardInterrupt):
                close_confirmed.set()

        close_thread = _threading.Thread(target=_wait_close_input, daemon=True)
        close_thread.start()

        max_close_wait = 1800  # 30 minutes
        close_waited = 0
        while close_waited < max_close_wait:
            if close_confirmed.is_set():
                break
            await asyncio.sleep(1)
            close_waited += 1

        if close_waited >= max_close_wait and not close_confirmed.is_set():
            print(f"\n   ⏰ 等待超时 ({max_close_wait}s)，自动关闭浏览器")

        # Save storage state and kill browser
        print("🔒 正在关闭浏览器...")
        try:
            await browser_session.export_storage_state(str(WX_STORAGE_PATH))
        except Exception:
            pass
        try:
            await browser_session.kill()
        except Exception:
            pass
        return True

    except Exception as e:
        print(f"❌ 发布失败: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        # Ensure browser process is killed
        try:
            await browser_session.kill()
        except Exception:
            pass


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

    if not title:
        print("❌ 配置文件中缺少 title 字段")
        return False
    if not images:
        print("❌ 配置文件中缺少 images 字段")
        return False

    # Resolve relative image paths relative to config file directory
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
        description="微信公众号贴图发布工具 (browser-use Agent + Playwright)"
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # Login command
    subparsers.add_parser("login", help="扫码登录微信公众号")

    # Check login command
    subparsers.add_parser("check", help="检查登录状态")

    # Inspect command
    subparsers.add_parser("inspect", help="检查贴图编辑页面 DOM 结构")

    # Publish command
    pub_parser = subparsers.add_parser("publish", help="发布贴图")
    pub_parser.add_argument("--config", help="JSON 配置文件路径")
    pub_parser.add_argument("--copywriting", help="copywriting.txt 文件路径（自动提取标题和描述）")
    pub_parser.add_argument("--title", help="贴图标题")
    pub_parser.add_argument("--images", nargs="+", help="图片路径列表")
    pub_parser.add_argument("--description", default="", help="描述文字")
    pub_parser.add_argument("--tags", nargs="*", help="标签列表（不带 # 号）")
    pub_parser.add_argument("--collection", default="", help="合集名称（如 AI前沿）")
    pub_parser.add_argument(
        "--auto",
        action="store_true",
        help="跳过预览确认，直接保存到草稿箱",
    )
    pub_parser.add_argument(
        "--headless",
        action="store_true",
        help="无头模式运行",
    )

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
            asyncio.run(
                publish_from_config(
                    args.config,
                    auto_publish=args.auto,
                    headless=args.headless,
                )
            )
        elif args.copywriting and args.images:
            # Read title, description, and tags from copywriting.txt
            # Format:
            #   Line 0: title (may have leading emoji)
            #   Lines 1..N-1: description body (preserve exact line breaks)
            #   Last non-empty line starting with #: tags line
            import re
            cw_path = Path(args.copywriting).resolve()
            if not cw_path.exists():
                print(f"❌ copywriting 文件不存在: {cw_path}")
                sys.exit(1)
            cw_text = cw_path.read_text(encoding="utf-8").strip()
            cw_lines = cw_text.split("\n")
            raw_title = cw_lines[0].strip()
            # Keep the full title including any emoji
            cw_title = raw_title

            # Find last line that starts with # (tags line)
            cw_tags = []
            content_end = len(cw_lines)
            for i in range(len(cw_lines) - 1, 0, -1):
                line = cw_lines[i].strip()
                if line.startswith("#"):
                    cw_tags = [t.strip().lstrip("#") for t in line.split("#") if t.strip()]
                    content_end = i
                    break

            # Description = everything between title and tags line
            # Preserve exact line breaks (including empty lines)
            cw_desc = "\n".join(cw_lines[1:content_end]).strip()

            asyncio.run(
                publish_post(
                    title=cw_title,
                    image_paths=args.images,
                    description=cw_desc,
                    tags=cw_tags,
                    collection=args.collection,
                    auto_publish=args.auto,
                    headless=args.headless,
                )
            )
        elif args.title and args.images:
            asyncio.run(
                publish_post(
                    title=args.title,
                    image_paths=args.images,
                    description=args.description,
                    tags=args.tags or [],
                    collection=args.collection,
                    auto_publish=args.auto,
                    headless=args.headless,
                )
            )
        else:
            print("❌ 请提供 --config 配置文件路径，或同时提供 --title 和 --images")
            pub_parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
