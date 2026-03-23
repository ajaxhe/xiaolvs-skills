#!/usr/bin/env python3
"""
Publish image-text notes to Xiaohongshu (小红书) via browser-use Agent.

This script uses browser-use (AI-driven browser automation) for complex UI
interactions (filling forms, handling popups, scheduling), while keeping
Playwright for simple operations (login QR scan, image upload via file input).

Requires:
  - browser-use >= 0.12.0
  - GEMINI_API_KEY or GOOGLE_API_KEY env var (for Google Gemini LLM)

Usage:
    # First-time login (scan QR code)
    python xhs_publish.py login

    # Publish from a JSON config file
    python xhs_publish.py publish --config <path_to_config.json>

    # Publish with explicit args
    python xhs_publish.py publish \
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
    "original": true
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

# Storage state file for Xiaohongshu login session
XHS_STORAGE_DIR = Path.home() / ".xiaohongshu"
XHS_STORAGE_PATH = XHS_STORAGE_DIR / "storage_state.json"

# Xiaohongshu creator platform URLs
XHS_CREATOR_URL = "https://creator.xiaohongshu.com"
XHS_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish"


async def login(headless: bool = False):
    """Login to Xiaohongshu via QR code scan.

    Opens the creator platform login page and waits for user to scan QR code.
    Saves the authenticated session to storage_state.json for future use.
    """
    from playwright.async_api import async_playwright

    XHS_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        await page.goto(XHS_CREATOR_URL)

        print("=" * 60)
        print("请在浏览器中扫码登录小红书创作者平台")
        print("登录成功后会自动保存 session，后续无需重新登录")
        print("=" * 60)

        # Wait for successful login - detect by URL change or user avatar
        try:
            await page.wait_for_url(
                "**/creator.xiaohongshu.com/new/**",
                timeout=120_000,  # 2 minutes to scan QR code
            )
        except Exception:
            # Fallback: wait for any navigation away from login
            try:
                await page.wait_for_selector(
                    ".user-avatar, .creator-header, .user-info",
                    timeout=120_000,
                )
            except Exception:
                print("⚠️  登录超时，请在 2 分钟内完成扫码")
                await browser.close()
                return False

        # Small delay to ensure cookies are set
        await page.wait_for_timeout(2000)

        # Save storage state
        await context.storage_state(path=str(XHS_STORAGE_PATH))
        print(f"\n✅ 登录成功！Session 已保存到 {XHS_STORAGE_PATH}")
        await browser.close()
        return True


async def check_login() -> bool:
    """Check if the saved session is still valid."""
    if not XHS_STORAGE_PATH.exists():
        print("❌ 未找到登录 session，请先运行: python xhs_publish.py login")
        return False

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=str(XHS_STORAGE_PATH),
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        await page.goto(XHS_CREATOR_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        url = page.url
        await browser.close()

        if "login" in url.lower() or "passport" in url.lower():
            print("❌ Session 已过期，请重新登录: python xhs_publish.py login")
            return False

        print("✅ Session 有效")
        return True


def _strip_urls(text: str) -> str:
    """Remove URLs from text (小红书会封禁外部链接)."""
    import re
    # Remove lines that are purely a URL (with optional leading emoji/label)
    lines = text.split("\n")
    filtered = []
    for line in lines:
        stripped = line.strip()
        # Skip lines that are just a URL or "🔗 https://..." style
        if re.match(r'^[\U0001f000-\U0001ffff\s]*https?://\S+\s*$', stripped):
            continue
        # Remove inline URLs
        cleaned = re.sub(r'https?://\S+', '', line).rstrip()
        # Skip lines that become empty after URL removal (e.g. "🔗 ")
        if cleaned.strip() in ('🔗', '📎', '🔗 ', '📎 ', ''):
            # Only skip if the original line had a URL
            if re.search(r'https?://\S+', line):
                continue
        filtered.append(cleaned)
    return "\n".join(filtered)


def _build_agent_task(
    title: str,
    content: str,
    tags: list[str] | None,
    original: bool,
    collection: str | None,
    schedule_time: str | None,
    target_datetime: str | None,
    auto_publish: bool,
) -> str:
    """Build a natural-language task prompt for browser-use Agent.

    This describes everything the Agent needs to do on the already-loaded
    XHS publish page (images are already uploaded via Playwright beforehand).
    """
    task_parts = []

    task_parts.append(
        "你现在在小红书创作者平台的发布页面 (creator.xiaohongshu.com/publish/publish)。"
        "图片已经上传完成，标题和正文输入框应该已经可见。"
        "请按顺序完成以下操作："
    )

    # Step 1: Fill title
    task_parts.append(
        f'\n1. 填写标题：点击标题输入框（placeholder 包含"填写标题"），'
        f'清空已有内容，然后输入：「{title}」'
    )

    # Step 2: Fill content
    # Escape content for embedding in prompt (truncate if very long to save tokens)
    display_content = content if len(content) <= 2000 else content[:2000] + "...(内容已截断)"
    task_parts.append(
        f'\n2. 填写正文：点击正文编辑区域（class 包含 "tiptap ProseMirror" 的 contenteditable div），'
        f'然后逐字输入以下内容（保持换行格式）：\n'
        f'---\n{display_content}\n---'
    )

    # Step 3: Add tags
    if tags:
        tags_str = "、".join(tags)
        task_parts.append(
            f'\n3. 添加标签：在正文最后按回车换行，然后依次添加以下标签：{tags_str}。'
            f'每个标签的添加方式：点击编辑区下方的「话题」按钮，输入标签文字，'
            f'等待建议列表出现后点击第一个匹配项（或按回车确认）。'
        )
    else:
        task_parts.append('\n3. 跳过标签（不需要添加）。')

    # Step 4: Original declaration
    if original:
        task_parts.append(
            '\n4. 原创声明：向下滚动页面，找到「原创声明」的开关/toggle，点击开启。'
            '开启后会弹出一个确认弹窗，弹窗底部有一个复选框「我已阅读并同意《原创声明须知》」'
            '和一个「声明原创」按钮。'
            '先点击勾选该复选框（复选框在文字左侧），然后点击「声明原创」按钮确认。'
        )
    else:
        task_parts.append('\n4. 跳过原创声明（不需要勾选）。')

    # Step 5: Collection
    if collection:
        task_parts.append(
            f'\n5. 选择合集：在页面中找到「选择合集」按钮并点击，'
            f'在弹出的列表中选择「{collection}」。'
        )
    else:
        task_parts.append('\n5. 跳过合集选择（不需要）。')

    # Step 6: Schedule
    if schedule_time and target_datetime:
        task_parts.append(
            f'\n6. 定时发布：向下滚动找到「定时发布」的开关/toggle，点击开启。'
            f'开启后会出现一个日期时间文本和日历图标，点击它打开日期时间选择器。'
            f'在选择器底部有一个输入框，显示类似 "YYYY-MM-DD HH:MM" 的格式，'
            f'清空该输入框，输入：「{target_datetime}」，然后按回车确认。'
            f'如果找不到底部输入框，则在右侧的时间滚动列中找到并点击'
            f'「{schedule_time.split(":")[0]}时」和「{schedule_time.split(":")[1]}分」。'
        )
    else:
        task_parts.append('\n6. 跳过定时发布（将立即发布）。')

    # Step 7: Final action
    if auto_publish:
        task_parts.append(
            '\n7. 滚动到页面顶部，找到并点击「发布」按钮完成发布。'
        )
    else:
        task_parts.append(
            '\n7. 滚动到页面顶部，检查所有内容是否正确填写。'
            '任务完成，不要点击发布按钮（用户会手动确认后发布）。'
        )

    task_parts.append(
        '\n重要提示：'
        '\n- 每一步操作后等待页面响应再进行下一步。'
        '\n- 如果某个元素暂时不可见，先尝试滚动页面。'
        '\n- 操作过程中如遇到弹窗或对话框，先处理弹窗再继续。'
        '\n- 请使用中文操作界面。'
    )

    return "\n".join(task_parts)


async def publish_note(
    title: str,
    content: str,
    image_paths: list[str],
    tags: list[str] | None = None,
    original: bool = True,
    collection: str | None = None,
    schedule_time: str | None = "07:17",
    auto_publish: bool = False,
    headless: bool = False,
):
    """Publish an image-text note to Xiaohongshu.

    Uses browser-use Agent for intelligent UI interaction, combined with
    Playwright for reliable file upload operations.

    Args:
        title: Note title (max 20 chars).
        content: Note body text.
        image_paths: List of absolute paths to image files.
        tags: Optional list of tag strings (without #).
        original: Whether to check "原创声明" checkbox.
        collection: Optional collection name to add the note to (e.g. "AI产品经理").
        schedule_time: Scheduled publish time in HH:MM format (default "07:17").
                       Set to None to publish immediately.
        auto_publish: If True, publish directly without pausing for confirmation.
        headless: Run browser in headless mode (not recommended for first use).
    """
    if not XHS_STORAGE_PATH.exists():
        print("❌ 未找到登录 session，请先运行: python xhs_publish.py login")
        return False

    # Validate image paths
    resolved_paths = []
    for img in image_paths:
        p = Path(img).resolve()
        if not p.exists():
            print(f"❌ 图片不存在: {p}")
            return False
        resolved_paths.append(str(p))

    if len(resolved_paths) == 0:
        print("❌ 至少需要 1 张图片")
        return False

    if len(title) > 20:
        print(f"⚠️  标题超过 20 字（当前 {len(title)} 字），自动截断为 20 字")
        title = title[:20]

    # Strip URLs from content
    import re as _re
    clean_content = _strip_urls(content)
    clean_content = _re.sub(r'\n{3,}', '\n\n', clean_content).strip()

    # Compute target datetime for scheduling
    target_datetime = None
    if schedule_time:
        from datetime import datetime, timedelta
        hour, minute = schedule_time.split(":")
        now = datetime.now()
        target = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        target_datetime = target.strftime("%Y-%m-%d %H:%M")

    # --- Phase 1: Use Playwright directly for page load + image upload ---
    # (browser-use Agent cannot handle file input directly; Playwright is better here)
    from browser_use import Agent
    from browser_use.browser.session import BrowserSession
    from browser_use.llm.google.chat import ChatGoogle

    # Load API key from env
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("❌ 未设置 GEMINI_API_KEY 或 GOOGLE_API_KEY 环境变量")
        print("   请设置: export GEMINI_API_KEY='your-api-key'")
        return False

    llm = ChatGoogle(model="gemini-2.5-flash", api_key=api_key)

    browser_session = BrowserSession(
        headless=headless,
        storage_state=str(XHS_STORAGE_PATH),
        viewport={"width": 1280, "height": 900},
        keep_alive=True,
    )

    try:
        # Start browser session and get playwright page for direct operations
        await browser_session.start()
        page = await browser_session.get_current_page()

        print("📝 正在打开小红书创作者平台...")
        await page.goto(XHS_PUBLISH_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Check if redirected to login
        if "login" in page.url.lower() or "passport" in page.url.lower():
            print("❌ Session 已过期，请重新登录: python xhs_publish.py login")
            return False

        # Switch to image-text tab
        print("📤 切换到图文发布页面...")
        await page.evaluate("""() => {
            const spans = document.querySelectorAll('span');
            for (const s of spans) {
                if (s.textContent.trim() === '上传图文') {
                    const tab = s.closest('.creator-tab, [class*="tab"]') || s.parentElement;
                    if (tab) tab.click();
                    return true;
                }
            }
            return false;
        }""")
        await page.wait_for_timeout(2000)

        # Upload images via Playwright file input (Agent can't do this)
        print(f"🖼️  上传 {len(resolved_paths)} 张图片...")
        file_input = page.locator('input[type="file"]').first
        await file_input.set_input_files(resolved_paths)

        # Wait for upload to complete
        print("⏳ 等待图片上传完成...")
        try:
            await page.wait_for_selector(
                'input[placeholder*="填写标题"]',
                state="visible",
                timeout=60_000,
            )
        except Exception:
            print("⚠️  等待标题输入框超时，继续尝试...")
            await page.wait_for_timeout(8000)

        await page.wait_for_timeout(1000)
        print("✅ 图片上传完成")

        # --- Phase 2: Use browser-use Agent for all form interactions ---
        print("🤖 启动 AI Agent 执行页面操作...")

        task = _build_agent_task(
            title=title,
            content=clean_content,
            tags=tags,
            original=original,
            collection=collection,
            schedule_time=schedule_time,
            target_datetime=target_datetime,
            auto_publish=auto_publish,
        )

        agent = Agent(
            task=task,
            llm=llm,
            browser=browser_session,
            use_vision=True,
            max_actions_per_step=3,
            max_failures=3,
        )

        print("🚀 Agent 开始执行任务...")
        result = await agent.run()
        print(f"✅ Agent 执行完成")

        # --- Phase 3: Preview and confirm ---
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

        if not auto_publish:
            # Wait for user confirmation before publishing
            print("\n" + "=" * 60)
            print("⏸️  内容已填写完毕，浏览器保持打开。")
            print("   请在浏览器中检查并调整：")
            print("   - 内容、图片顺序、标签、原创声明、合集")
            print("")
            print("   👉 确认无误后，在此终端按「回车」键，脚本将自动点击发布。")
            print("=" * 60)
            sys.stdout.flush()

            import threading
            confirmed = threading.Event()
            signal_file = Path("/tmp/xhs_close_browser")
            signal_file.unlink(missing_ok=True)

            def wait_stdin():
                try:
                    for line in sys.stdin:
                        confirmed.set()
                        return
                except Exception:
                    pass

            stdin_thread = threading.Thread(target=wait_stdin, daemon=True)
            stdin_thread.start()

            max_wait = 600
            waited = 0
            while waited < max_wait:
                if confirmed.is_set():
                    print("\n   ✅ 收到回车确认，正在执行发布...")
                    break
                if signal_file.exists():
                    signal_file.unlink(missing_ok=True)
                    print("\n   ✅ 收到信号文件确认，正在执行发布...")
                    confirmed.set()
                    break
                await asyncio.sleep(1)
                waited += 1

            if waited >= max_wait and not confirmed.is_set():
                print(f"\n   ⏰ 等待超时 ({max_wait}s)，自动关闭浏览器")
                return False

            # Use Agent to click publish button
            publish_agent = Agent(
                task='点击页面上的「发布」按钮（通常在页面顶部或底部，是一个红色/主色调的按钮，文字为"发布"或"发布笔记"）。',
                llm=llm,
                browser=browser_session,
                use_vision=True,
                max_actions_per_step=2,
                max_failures=3,
            )
            await publish_agent.run()
            print("   ✅ 已执行发布")

            # Wait for publish to complete
            await asyncio.sleep(3)

        print("🔒 正在关闭浏览器...")
        # Save storage state before closing
        try:
            context = await browser_session.get_browser_context()
            await context.storage_state(path=str(XHS_STORAGE_PATH))
        except Exception:
            pass
        return True

    except Exception as e:
        print(f"❌ 发布失败: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        try:
            await browser_session.close()
        except Exception:
            pass


async def publish_from_config(
    config_path: str,
    auto_publish: bool = False,
    headless: bool = False,
    schedule_time: str | None = "07:17",
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

    # Resolve relative image paths relative to config file directory
    config_dir = config_file.parent
    resolved_images = []
    for img in images:
        p = Path(img)
        if not p.is_absolute():
            p = config_dir / p
        resolved_images.append(str(p.resolve()))

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
        description="小红书图文发布工具 (browser-use Agent + Playwright)"
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # Login command
    login_parser = subparsers.add_parser("login", help="扫码登录小红书")

    # Check login command
    check_parser = subparsers.add_parser("check", help="检查登录状态")

    # Publish command
    pub_parser = subparsers.add_parser("publish", help="发布图文笔记")
    pub_parser.add_argument("--config", help="JSON 配置文件路径")
    pub_parser.add_argument("--copywriting", help="copywriting.txt 文件路径（自动提取标题、正文和标签）")
    pub_parser.add_argument("--title", help="笔记标题")
    pub_parser.add_argument("--content", help="笔记正文")
    pub_parser.add_argument("--images", nargs="+", help="图片路径列表")
    pub_parser.add_argument("--tags", nargs="*", help="标签列表")
    pub_parser.add_argument(
        "--original",
        action="store_true",
        default=True,
        help="勾选原创声明（默认开启）",
    )
    pub_parser.add_argument(
        "--no-original",
        action="store_true",
        help="不勾选原创声明",
    )
    pub_parser.add_argument(
        "--collection",
        help="合集名称（如 'AI产品经理'），发布时自动选择对应合集",
    )
    pub_parser.add_argument(
        "--schedule",
        default="07:17",
        help="定时发布时间，格式 HH:MM（默认 07:17）。设为 'now' 则立即发布",
    )
    pub_parser.add_argument(
        "--auto",
        action="store_true",
        help="跳过预览确认，直接保存草稿",
    )
    pub_parser.add_argument(
        "--headless",
        action="store_true",
        help="无头模式运行（不推荐首次使用）",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "login":
        asyncio.run(login())

    elif args.command == "check":
        asyncio.run(check_login())

    elif args.command == "publish":
        schedule = args.schedule if args.schedule != 'now' else None
        if args.config:
            asyncio.run(
                publish_from_config(
                    args.config,
                    auto_publish=args.auto,
                    headless=args.headless,
                    schedule_time=schedule,
                )
            )
        elif args.copywriting and args.images:
            # Read title, content and tags from copywriting.txt
            import re
            cw_path = Path(args.copywriting).resolve()
            if not cw_path.exists():
                print(f"❌ copywriting 文件不存在: {cw_path}")
                sys.exit(1)
            cw_text = cw_path.read_text(encoding="utf-8").strip()
            cw_lines = cw_text.split("\n")
            # Line 0 = title (strip leading emoji)
            raw_title = cw_lines[0].strip()
            cw_title = re.sub(r'^[\U0001f000-\U0001ffff\s]+', '', raw_title).strip() or raw_title
            # Last non-empty line starting with # = tags line
            cw_tags = []
            content_end = len(cw_lines)
            for i in range(len(cw_lines) - 1, 0, -1):
                line = cw_lines[i].strip()
                if line.startswith("#"):
                    cw_tags = [t.strip().lstrip("#") for t in line.split("#") if t.strip()]
                    content_end = i
                    break
            # Content = everything between title and tags line (skip empty lines around edges)
            cw_content = "\n".join(cw_lines[1:content_end]).strip()
            original = not args.no_original
            asyncio.run(
                publish_note(
                    title=cw_title,
                    content=cw_content,
                    image_paths=args.images,
                    tags=cw_tags or args.tags or [],
                    original=original,
                    collection=args.collection,
                    schedule_time=schedule,
                    auto_publish=args.auto,
                    headless=args.headless,
                )
            )
        elif args.title and args.images:
            original = not args.no_original
            asyncio.run(
                publish_note(
                    title=args.title,
                    content=args.content or "",
                    image_paths=args.images,
                    tags=args.tags,
                    original=original,
                    collection=args.collection,
                    schedule_time=schedule,
                    auto_publish=args.auto,
                    headless=args.headless,
                )
            )
        else:
            print("❌ 请提供 --config/--copywriting 配置文件路径，或同时提供 --title 和 --images")
            pub_parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
