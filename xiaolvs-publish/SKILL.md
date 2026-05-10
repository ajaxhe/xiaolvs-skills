---
name: xiaolvs-publish
description: 小绿书发布到小红书和微信公众号。当用户需要将已生成的信息图和文案发布到小红书或微信公众号时，使用此 skill。关键词触发：发布小红书、发布公众号、微信公众号发布、公众号贴图、小红书发布、创建草稿、上传图片。
---

# 小绿书发布

## 概述

将已生成的信息图 + 文案自动发布到**小红书**和**微信公众号**平台，默认创建草稿供用户检查后手动发布。

### CDP 模式（推荐，必须使用）

**必须使用 `--cdp` 模式**，通过 Chrome DevTools Protocol 连接用户真实 Chrome 浏览器，绕过平台的自动化检测。**不走 CDP 的 Playwright 模式极易被平台检测为自动化操作导致封号。**

**CDP Chrome 启动方式**（使用独立 profile 目录）：
```bash
# ⚠️ macOS Chrome 不支持在默认 user-data-dir 上开 CDP
# 必须指定独立 --user-data-dir
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.config/google-chrome-cdp" &
```

> **⚠️ 关键限制**：macOS Chrome 在默认 data directory 上启动 CDP 会报错 `DevTools remote debugging requires a non-default data directory`。必须使用独立 profile 目录 `~/.config/google-chrome-cdp`。

### CDP 登录态管理

1. **首次使用**：启动 CDP Chrome 后需要手动登录小红书和微信公众号（扫码）
2. **登录态持久化**：登录后 session 保存在 `~/.config/google-chrome-cdp/` 中，后续无需重新登录
3. **发布前必须检查登录态**：执行发布脚本前，先用 Playwright 连接 CDP 打开目标平台页面，检查是否跳转到 login 页。如果未登录，**等待用户扫码登录完成后**再执行发布脚本
4. **检查登录态的代码模式**：

```python
# 在执行发布前先确认登录态
async with async_playwright() as p:
    browser = await p.chromium.connect_over_cdp('http://localhost:9222')
    context = browser.contexts[0]
    page = await context.new_page()
    await page.goto('https://creator.xiaohongshu.com/publish/publish')
    # 等待最多120秒直到不再是登录页面
    for i in range(60):
        await asyncio.sleep(2)
        if 'login' not in page.url.lower() and 'passport' not in page.url.lower():
            print('登录确认成功')
            break
```

5. **脚本检测到未登录时不应直接退出**：应打开登录页面并等待用户扫码，而非打印错误直接退出。如果当前脚本实现是直接退出的，需要在执行脚本前手动完成登录检查

### 前置条件

本 skill 假设以下文件已存在于项目子目录：
- `infographic.png` + `chart_1~N.png` — 信息图图片
- `copywriting.txt` — 公众号文案（标题 + 正文 + 标签）

如果尚未生成，请先使用 `xiaolvs-image-gen` skill。

## 平台 1：小红书发布

### 1.1 首次登录

```bash
python scripts/xhs_publish.py login
```

在弹出的浏览器中扫码登录小红书创作者平台，session 自动保存到 `~/.xiaohongshu/storage_state.json`。

### 1.2 检查登录状态

```bash
python scripts/xhs_publish.py check
```

### 1.3 发布

**⚠️ 必须使用 `--copywriting` 模式（推荐），禁止手动创建 JSON 配置文件。**

`copywriting.txt` 中的正文常包含中文引号 `""` 等特殊字符，手动构建 JSON 会导致引号转义失败（`json.decoder.JSONDecodeError`）。`--copywriting` 模式直接从纯文本文件读取，自动提取标题/正文/标签，彻底规避此问题。

```bash
# 推荐：--copywriting 模式（直接读取 copywriting.txt，自动提取标题/正文/标签，保留所有 emoji）
python scripts/xhs_publish_playwright.py publish \
    --copywriting <项目子目录>/copywriting.txt \
    --images <项目子目录>/infographic.png <项目子目录>/chart_1.png <项目子目录>/chart_2.png <项目子目录>/chart_3.png <项目子目录>/chart_4.png \
    --collection "AI产品经理" \
    --auto
```

> **⚠️ 外部链接自动过滤**：脚本内置 `_strip_urls()` 函数，自动从正文中移除所有 `http://` 和 `https://` 链接及纯链接行。

> **copywriting.txt 解析规则**：
> - **标题**：第 1 行（保留 emoji，不超过 20 字）
> - **正文**：第 2 行起到标签行之前
> - **标签**：最后一行（`#标签1 #标签2` 格式，自动去掉 `#` 号）

<details>
<summary>备选：--config 模式（不推荐，仅在无 copywriting.txt 时使用）</summary>

```bash
python scripts/xhs_publish_playwright.py publish --config <项目子目录>/xhs_publish_config.json --auto
```

⚠️ 使用 `--config` 模式时，正文中的中文引号 `""`、`''` 等特殊字符必须正确转义，否则会导致 JSON 解析失败。**强烈建议使用 `--copywriting` 模式替代。**
</details>

> **⚠️ 发布前必须等待用户确认。** 无论是否使用 `--auto`，脚本在填写完所有字段后都会在**浏览器顶部注入一个红色确认提示条**（包含「取消」和「确认发布 ✓」按钮），等待用户在浏览器中检查内容无误后点击「确认发布」，才会执行发布按钮。最长等待 30 分钟。
> **⚠️ 标题和正文必须保留 emoji。** 从 `copywriting.txt` 提取标题和正文时，不要去除 emoji 符号（如 ⚡📌📊💡1️⃣2️⃣3️⃣ 等），它们是内容格式的一部分。
> **⚠️ 外部链接自动过滤**：脚本内置 `_strip_urls()` 函数，自动从正文中移除所有 `http://` 和 `https://` 链接。

> **发布流程**：打开创作者平台 → JS click 切换图文 tab → 上传图片 → 填标题 → 填正文（保留 emoji，自动过滤外链）→ 添加话题 → 勾选原创声明 → 选择合集 → 勾选定时发布 → **浏览器顶部显示确认提示条，等待用户点击「确认发布」后执行发布** → 发布成功后自动截图并关闭浏览器。

---

## 平台 2：微信公众号发布

### 2.1 首次登录

```bash
python scripts/wechat_mp_publish.py login
```

在弹出的浏览器中扫码登录微信公众号后台，session 保存到 `~/.wechat_mp/storage_state.json`。

### 2.2 检查登录状态

```bash
python scripts/wechat_mp_publish.py check
```

### 2.3 准备发布配置

支持两种模式：

**方式 A：`--copywriting` 模式（推荐）**，直接从 `copywriting.txt` 自动提取标题和完整描述（保留所有 emoji）：

```bash
python scripts/wechat_mp_publish_playwright.py publish \
    --copywriting <项目子目录>/copywriting.txt \
    --images <项目子目录>/infographic.png <项目子目录>/chart_1.png <项目子目录>/chart_2.png <项目子目录>/chart_3.png <项目子目录>/chart_4.png \
    --collection AI前沿 \
    --auto
```

**方式 B：`--config` 模式**，使用 `wechat_publish_config.json` 配置（**必须设置 `copywriting_file` 以确保描述内容完整**）：

```bash
python scripts/wechat_mp_publish_playwright.py publish \
    --config <项目子目录>/wechat_publish_config.json \
    --auto
```

config 文件示例：
```json
{
    "title": "⚡ AI已来，只是分布不均",
    "images": ["infographic.png", "chart_1.png", "chart_2.png", "chart_3.png", "chart_4.png"],
    "tags": ["AI趋势", "科技前沿"],
    "collection": "AI前沿",
    "copywriting_file": "copywriting.txt"
}
```

> **⚠️ 使用 `--config` 模式时，必须设置 `copywriting_file` 字段**，指向 `copywriting.txt` 文件。脚本会从中读取完整的正文内容作为描述，确保不会遗漏内容。如果同时存在 `description` 字段和 `copywriting_file`，以 `copywriting_file` 为准。
> **⚠️ 默认一律使用 `--auto` 创建草稿。** 除非用户明确说「发布到线上」，否则不要直接发表。
> **⚠️ 必须使用 `--collection` 参数或 config 中的 `collection` 字段指定合集。**
> **⚠️ 标题中的 emoji 必须保留**，不要在配置或代码中去除标题开头的 emoji 符号。

> **图片顺序**：`infographic.png` 必须排第一位（总览图在最前），后接 `chart_1~N`。
> **标题截断**：微信公众号贴图标题限 20 字，超出自动截断。

### 2.4 发布流程（三阶段分步执行）

脚本采用**三阶段分步执行架构**，确保图片上传的可靠性：

**Phase 1 — 导航**：导航到微信公众号后台贴图编辑器（提取 token 构建编辑 URL）。

**Phase 2 — 逐张图片上传**：
- 每张图片由 Playwright 逐张上传，程序化控制顺序。
- **必须使用 `file_inputs[1]`**（`[0]` 是菜单 input 无效）。每次上传前**必须重新查询 DOM** 获取最新 file input。
- 每张上传完成后固定等待确保处理完毕。
- 图片计数通过 **mmbiz 缩略图元素** 的 `getBoundingClientRect()` 尺寸过滤（20-200px），**不使用页面 N/M 文本匹配**（会误匹配表单字数计数器 `0/20`）。

**Phase 3 — 填写与保存**：
- **标题**：通过 `div.ProseMirror[data-placeholder="请在这里输入标题"]` 精确定位（contenteditable div，非 textarea），使用 `keyboard.type()` 输入后按 Escape 移出焦点。
- **描述**：遍历所有可见 ProseMirror contenteditable div，**排除**含"标题"的 data-placeholder，定位到描述区域。使用合成 `ClipboardEvent('paste')` + `text/plain` 输入内容，空行用 `\u200B` 零宽空格占位。
- **合集**：通过 `#js_article_tags_area` 定位。
- **保存草稿**：保存后最多等待 30 秒验证成功（检测 toast 提示、URL 跳转、确认对话框）。

### 2.5 保存后确认与浏览器关闭

- `--auto` 模式下草稿保存成功后**自动等待 5 秒**确保保存请求完成，**不依赖 stdin input()**（避免非交互终端 EOFError 导致浏览器提前关闭）。
- 非 auto 模式下浏览器保持打开，终端提示用户确认后关闭。
- `finally` 块确保异常时浏览器进程会被杀掉。

---

## 脚本文件

| 文件 | 用途 |
|------|------|
| `scripts/xhs_publish_playwright.py` | 小红书图文发布脚本（纯 Playwright，默认等待用户确认后发布） |
| `scripts/wechat_mp_publish_playwright.py` | 微信公众号贴图发布脚本（纯 Playwright，逐张上传图片、填写内容、保存草稿） |
| `scripts/xhs_publish.py` | 小红书发布脚本（browser-use 版，备选） |
| `scripts/wechat_mp_publish.py` | 微信公众号发布脚本（browser-use 版，备选） |

## 常见问题

| 问题 | 原因 | 修复方法 |
|------|------|----------|
| 小红书 JSON 配置文件引号转义失败（反复出现） | `copywriting.txt` 正文含中文引号 `""`、`''` 等，AI 构建 `xhs_publish_config.json` 时中文引号破坏 JSON 格式 | **已修复**：禁止创建 JSON 配置文件，改用 `--copywriting` 模式直接读取纯文本。SKILL.md 已将 `--copywriting` 设为唯一推荐方式 |
| 小红书合集未设置 | `_select_collection` 选择器未匹配实际页面 | **已修复**：多策略查找（JS遍历DOM + Playwright locator），支持"选择合集"/"加入合集"等多种入口文案，自动截图辅助调试 |
| 小红书原创声明未勾选 | `_toggle_original` 选择器未匹配实际页面开关 | **已修复**：多策略查找开关元素（JS遍历 + role=switch + 父级容器），自动处理确认弹窗，截图辅助调试 |
| 小红书定时发布未开启 | `_set_schedule` 选择器未匹配 | **已修复**：多策略查找定时发布开关，支持 `schedule_time: "default"` 仅勾选使用系统默认时间 |
| 小红书标签插入到正文中间而非末尾 | `_add_tags` 重新点击编辑器后光标不在末尾，Enter 在中间位置插入了标签 | **已修复**：先用 JS `Selection API` 将光标移到编辑器内容最末尾（`range.collapse(false)`），再按 Enter 添加标签 |
| 微信公众号图片计数返回错误值 20 | 全页面 `N/M` 正则匹配到标题字数计数器 `<em class="frm_counter">0/20</em>` | **已修复**：改为计数 mmbiz 缩略图元素，通过 `getBoundingClientRect()` 尺寸过滤（20-200px），排除 `frm_counter` 类元素 |
| 微信公众号标题/描述填入错位 | 标题是 ProseMirror `contenteditable <div>`（非 textarea），`fill()` 无效；描述随后误贴到标题区 | **已修复**：标题用 `div.ProseMirror[data-placeholder="请在这里输入标题"]` 精确定位，`keyboard.type()` 输入后按 Escape 移出焦点；描述遍历所有 ProseMirror 排除含"标题"的 placeholder |
| 微信公众号 auto 模式草稿保存被中断 | `_save_draft` 后 `input()` 等待用户按回车，非交互终端抛 `EOFError` → catch 后立即 `browser.close()`，保存请求未完成 | **已修复**：auto 模式移除 `input()` 调用，改为固定等待 5 秒确保保存完成；`_save_draft` 内增加最多 30 秒保存成功验证 |
| 微信公众号图片上传检测误判导致重复上传 | 图片预览元素选择器未匹配实际DOM，验证逻辑误判为上传失败触发重试 | **已修复**：移除复杂验证+重试逻辑，改为每张图片固定等待30秒的简单策略 |
| 微信公众号图片漏传/只上传1张 | 上传间隔太短，微信后台限频 | **已修复**：每张图片上传后固定等待 30 秒 + 图片间额外等待 5 秒 |
| 微信公众号图片上传被限频 | 微信后台有上传频率限制，连续上传会失败 | 每张图片上传后程序层面强制等待 15 秒 + 图片间 8 秒间隔 |
| 微信公众号标题 emoji 丢失 | 代码中正则去除了标题开头的 emoji | **已修复**：不再使用正则清理 emoji，保留完整标题 |
| 微信公众号浏览器残留进程 | `browser_session.close()` 不杀进程 | **已修复**：使用 `browser_session.kill()` 关闭，`finally` 块也确保进程退出 |
| 微信公众号描述内容不完整 | 使用 `--config` 模式手动填写 description 遗漏 | **已修复**：`--config` 模式支持 `copywriting_file` 字段，自动从 copywriting.txt 读取完整内容 |
| 微信公众号草稿保存「假成功」 | 保存判断逻辑不够严格 | 四级判断（success_text/navigated/url_changed/ambiguous） |
| 微信公众号标签不生效 | 一次性输入不触发标签识别 | 标签作为描述最后一行附加，格式 `#标签1 #标签2` |
| 微信公众号合集未设置 | 未传 `--collection` 参数 | **必须显式传入 `--collection` 参数或在 config 中设置** |
| 微信公众号图片顺序错误 | 上传顺序不可控 | 程序化逐张上传，`infographic.png` 排第一 |
| 小红书标题被截断/少字 | `len()` 计算含 emoji 时字符数超过 20 导致截断 | **已修复**：标题长度验证准确计算 emoji 字符位，并打印实际字数供检查 |
| 小红书正文/标题 emoji 丢失 | copywriting 模式中正则去除了 emoji | **已修复**：copywriting 模式不再去除标题和正文中的 emoji |
| 小红书发布后外链被封禁 | 小红书封禁正文中的外部链接 | 脚本内置 `_strip_urls()` 自动过滤 |
| 小红书 --auto 模式直接发布无确认 | 原 --auto 跳过了用户确认步骤 | **已修复**：--auto 模式也会等待用户确认后才执行发布 |
| 小红书非交互终端下确认自动取消 | `input()` 在 stdin 不可用时抛出 EOFError，被 catch 后默认设为取消 | **已修复**：改用浏览器内注入 HTML 顶部提示条（confirm banner），通过 `window.__xhs_publish_decision` 变量轮询用户操作，不依赖 stdin |
| 小红书 window.confirm() 被自动 dismiss | Playwright 默认自动 dismiss `confirm()` 对话框返回 false | **已修复**：不使用 `window.confirm()`，改用自定义 HTML overlay + JS 事件监听 |
| 小红书标题超过 20 字 | config 中 title 过长 | 标题必须精简到 20 字以内（emoji 也算 1 个字） |
| 小红书正文段落间距丢失 | URL 行移除后连续换行被过度清理 | 脚本仅合并 3+ 连续换行为 `\n\n` |
| 小红书合集选择失败 | 自定义 Vue 组件 | 脚本通过 `div.collection-plugin-button` 定位 |
| 微信公众号合集选择失败 | popover 遮挡 | 脚本自动关闭 popover，通过 `#js_article_tags_area` 定位 |
| 小红书非交互环境下自动发布 | fallback 到发布按钮 | 非交互环境禁止 fallback，仅保存 session |
| ProseMirror 编辑器输入不生效 | `keyboard.type()` 不触发编辑器事务 | 使用 `execCommand('insertText')` 替代 |

## 关键发布规则

1. **小红书必须使用 `--copywriting` 模式，禁止创建 JSON 配置文件**：`copywriting.txt` 正文中常含中文引号 `""`、`''` 等特殊字符，手动构建 `xhs_publish_config.json` 反复导致 `json.decoder.JSONDecodeError`。`--copywriting` 模式直接读取纯文本，自动提取标题/正文/标签，彻底规避此问题
2. **小红书发布前必须等待用户确认**：无论是否使用 `--auto`，脚本填写完所有字段后会在浏览器顶部注入红色确认提示条（不遮挡页面内容），用户在浏览器中点击「确认发布 ✓」后才会执行发布。使用浏览器内 UI 确认而非 stdin input()，确保在非交互终端环境中也能正常工作
3. **标题和正文保留所有 emoji**：从 `copywriting.txt` 提取时不要去除任何 emoji（包括 ⚡📌📊💡1️⃣2️⃣3️⃣🔄 等）
4. **微信公众号 config 必须设置 `copywriting_file`**：确保描述内容完整，不要手动摘要
5. **微信公众号图片始终使用 `file_inputs[1]`**：`[0]` 是菜单 input（无效），`[1]` 是编辑器 input（有效），每次上传前必须重新查询 DOM
6. **微信公众号标题通过 ProseMirror 定位**：标题是 `div.ProseMirror[data-placeholder="请在这里输入标题"]`（contenteditable div，非 textarea），使用 `keyboard.type()` 输入，**不能用 `fill()`**。输入后按 Escape 移出焦点，防止描述误填到标题区
7. **微信公众号描述使用合成 paste 事件**：ProseMirror 拦截键盘事件，唯一可靠方案是 `ClipboardEvent('paste')` + `text/plain`；空行用 `\u200B` 零宽空格占位。描述区域通过遍历所有 ProseMirror 并排除含"标题"的 placeholder 定位
8. **微信公众号图片计数禁用 N/M 文本匹配**：页面上的 `0/20`、`0/8`、`0/120` 是表单字数计数器（`<em class="frm_counter">`），不是图片计数。必须通过 mmbiz 缩略图元素 + `getBoundingClientRect()` 尺寸过滤来计数
9. **微信公众号 auto 模式不依赖 stdin**：`--auto` 模式保存草稿后自动等待 5 秒确认保存完成，不调用 `input()`，避免非交互终端 EOFError 导致浏览器提前关闭
10. **content/description 必须与 copywriting.txt 一致**：不要自行编辑、删减或重写 copywriting 中的内容
