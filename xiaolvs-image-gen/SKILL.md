---
name: xiaolvs-image-gen
description: 小绿书图文生成。当用户需要将文章/观点生成手绘风格信息图和配套公众号文案时，使用此 skill。关键词触发：生成信息图、手绘图、图文生成、小绿书、NotebookLM 图片、Gemini 生成图片、生成图表。
---

# 小绿书图文生成

## 概述

将文章分析结果转化为**多张手绘风格竖版信息图 + 配套公众号文案 + 话题标签**。

**默认使用 Gemini Nano Banana Pro（3:4 竖版）生成图片**，通过 Gemini 3 Pro 模型 + 图片生成工具激活，利用 Gemini Pro 订阅配额，无额外 API token 消耗。认证通过 Chrome CDP 模式直接复用用户浏览器的 Google 登录态，无需手动配置。

**脚本自动完成以下设置**（对应网页端手动操作）：
1. 选择「Pro」模型（等价于网页端切换到 Pro 模式）
2. 启用「生成图片 🍌」工具（等价于网页端点击 Nano Banana 图片生成工具）
3. 设置 Pro 质量模式（高质量图片输出）
4. 启用「临时对话」模式（等价于网页端点击「临时对话」，生成的会话不会保存到 Gemini 聊天历史）

仅当用户**明确要求使用 NotebookLM 生成信息图**时，才走 NotebookLM 信息图 API 通道。

### 前置条件

本 skill 假设已完成素材收集（`article.md` + `article_meta.json` 已存在于项目子目录）。如果尚未完成，请先使用 `fetch-archive` skill 抓取和归档文章。

### 最终产出物
1. 1 张**总览信息图**（涵盖全部核心要点）
2. 2-7 张**数据/观点拆解图**（每张聚焦单个核心论点或数据，总张数含总览图**不超过 8 张**）
3. 1 篇 **300 字以内的公众号文案**（`copywriting.txt`）
4. 5-8 个**话题标签**

## 工作流程

### Step 1：检查环境与认证

#### 1.1 Python 环境

脚本需要 Python 3.9+（使用 `from __future__ import annotations` 兼容）。macOS 系统自带的 `/usr/bin/python3` 通常是 3.9.6，可直接使用。如果系统有 homebrew 安装的更高版本（如 `/opt/homebrew/bin/python3.12`），也可使用。

**确认 Python 和 playwright 可用**：

```bash
# 检查 Python 版本（3.9+ 即可）
python3 --version

# 检查 playwright 是否已安装
python3 -c "from playwright.sync_api import sync_playwright; print('playwright OK')"

# 如未安装 playwright：
pip3 install playwright  # 或 pip3 install --break-system-packages playwright
```

> **⚠️ 避免重复安装**：如果 `python3 -c "import playwright"` 已成功，不要再次运行 pip install。当 `python3` 是系统 Python 3.9 但 playwright 装在 homebrew Python 3.12 下时，用 `/opt/homebrew/bin/python3.12` 执行脚本即可。

#### 1.2 Chrome CDP 认证

脚本会自动启动一个**独立的 CDP Chrome 实例**（使用专用 profile 目录 `~/.gemini_browser/chrome_cdp_profile`），**无需关闭用户正在使用的 Chrome**，两者可同时运行互不冲突。

> **⚠️ 关键机制**：macOS Chrome **禁止**在默认 user-data-dir 上开启 CDP 远程调试端口（会报错 `DevTools remote debugging requires a non-default data directory`）。脚本已改为使用独立 profile 目录，彻底避免此问题。

**首次使用**需要在 CDP Chrome 中登录 Google 账号：

```bash
# 方式1：运行 login 命令（自动打开 CDP Chrome，手动登录 Google）
python3 scripts/gemini_generate_images_browser.py login

# 方式2：直接运行 generate，脚本检测到未登录会提示
```

首次登录后，Google 登录态保存在 `~/.gemini_browser/chrome_cdp_profile/` 中，后续运行 generate 命令自动复用，无需再次登录。

**验证 CDP 可连接**（可选，脚本会自动处理）：

```bash
curl -s http://localhost:9222/json/version
```

> **认证原理**: 脚本自动启动一个独立的 Chrome 实例（专用 profile + CDP 端口 9222），通过 Playwright `connect_over_cdp` 连接并操控 gemini.google.com。独立 profile 意味着不影响用户日常使用的 Chrome，也不需要手动关闭/重启 Chrome。

### Step 2：事实信息筛查（Fact-Check）

在编写提示词之前，**必须**对素材中的所有事实性内容进行准确性验证，防止将错误信息写入信息图。

#### 2.1 提取并分类事实性声明

从结构化分析中，逐条提取所有事实性声明，并按以下类别分类：

| 类别 | 示例 | 验证要求 |
|------|------|----------|
| 产品发布时间 | "2024.06 Sonnet 3.5 发布" | **必须验证** — `web_search` 查证确切日期 |
| 财务/营收数据 | "年营收 $150 亿" | **必须验证** — 确认数据来源和时效性 |
| 用户量/使用数据 | "GitHub 20% 公共提交来自 X" | **必须验证** — 确认数据出处和统计口径 |
| 第三方引用/名言 | "某人说了 XXX" | **必须验证** — 确认引用来源真实存在 |
| 技术概念/产品名称 | "Claude Code 支持 XXX" | 常识性核实即可 |
| 嘉宾个人观点/经历 | "我每天提交 10-30 个 PR" | 无需外部验证 — 标注为嘉宾原话即可 |

#### 2.2 交叉验证事实

对每条**必须验证**的声明，执行以下步骤：

1. **搜索验证**：使用 `web_search` 搜索关键词（产品名 + 发布日期 / 公司名 + 营收数据 / 引用原文）
2. **来源比对**：将搜索结果与原文声明比对，确认：
   - 日期是否准确（精确到月份）
   - 数据量级是否正确（注意 ARR vs 年营收 vs 估值的区别）
   - 引用是否存在可追溯的原始出处
3. **标记结果**：

```
【事实筛查结果】
✅ [已验证] Sonnet 3.5 发布时间 → 2024.06.21（来源：Anthropic 官方博客）
⚠️ [已修正] "年营收 $150 亿" → 实际 2025 年底 ARR ~$9B（来源：Sacra 估算）
❌ [不可验证] "Spotify 最好的开发者从 12 月起没写过代码" → 搜索未找到原始出处，删除或替换
ℹ️ [嘉宾原话] "我每天提交 10-30 个 PR" → 保留，标注来源
```

#### 2.3 数据纪律（⚠️ 最高优先级）

> **核心原则：事实筛查是「验证」而非「替换」。图片 prompt 中只允许使用原文出现的数据，绝不混入外部数据源。**

对于验证结果的处理：
- **✅ 已验证（原文数据与外网一致）**：直接使用原文数据
- **⚠️ 原文数据与外网有出入**：**仍然使用原文数据**，但向用户报告差异，由用户决定是否修正。不要自行用外网数据替换
- **❌ 原文数据明显错误（如日期/人名写错）**：向用户报告，由用户决定。如果是笔误（如「2024」写成「2025」）可建议修正
- **引用无出处 / 不可验证**：保留原文表述，标注「原文声明」即可

**绝对禁止的行为：**
- ❌ 将 web_search 搜到的具体数据（如「$440亿」「$20亿 ARR」）直接写入图片 prompt
- ❌ 为图片补充原文没有的数据点（如原文没提 Cursor，就不能在图中加 Cursor 数据）
- ❌ 用外网「最新」数据替换原文中的预测/估算值

**允许的做法：**
- ✅ 使用原文中的原话、原始数据、原始表述
- ✅ 展示用户补充的推导公式作为数据说明
- ✅ 在 footer/来源标注中写「来源：原文/作者名」

#### 2.4 向用户报告筛查结果

将事实筛查结果呈现给用户，特别是原文与外网有差异的部分。明确告知：「以下数据与外网公开信息有出入，但图片中将按原文使用，你确认吗？」

等用户确认后再进入提示词编写阶段。

---

### Step 2.5：选择图片风格（⏸️ 用户确认）

事实筛查完成后、编写提示词之前，**必须让用户选择图片风格**。

当前支持两种命名风格（详见 `references/style_guide.md`）：

| 风格名称 | 英文 ID | 视觉特征 | 适用场景 |
|----------|---------|----------|----------|
| **暖纸手账** | `warm-sketch` | 浅奶油白纸张 + 棕色手绘线条 + 暖色点缀 | 知识分享、方法论、产品分析（亲切温馨） |
| **黑金粉笔** | `blackboard-gold` | 深黑背景 + 金色/铜色粉笔线条 + 米白手写文字 | 技术架构、战略分析、行业趋势（权威高端） |

**交互方式**：使用 `ask_followup_question` 向用户展示两种风格选项，**默认选择黑金粉笔（`blackboard-gold`）**：

```
🎨 请选择配图风格：
  A. 暖纸手账（warm-sketch）— 浅色暖纸 + 棕色手绘，亲切温馨
  B. 黑金粉笔（blackboard-gold）— 深黑底 + 金色粉笔，权威优雅 [默认]
```

用户选择后，记录所选风格 ID，后续 Step 3 编写提示词时使用对应的 STYLE_PREFIX。

---

### Step 3：编写提示词并生成信息图

#### 3.1 应用所选风格

根据 Step 2.5 用户选择的风格（默认 `blackboard-gold`），在 `gemini_charts_config.json` 中通过 `"style"` 字段记录，并在每张图的提示词开头使用对应的 STYLE_PREFIX。

> **⚠️ 风格一致性（关键）**：所有图片（包括第一张总览图 infographic.png）必须使用**完全相同的 STYLE_PREFIX 文本**。不能因为总览图内容简单就省略手写体强调。常见错误是第一张图的字体描述不够强烈，导致 Gemini 用印刷体渲染，与后续图片风格割裂。每张图的 Typography 部分都必须包含以下强调：
> - `ALL text MUST look hand-written / hand-lettered`
> - `ABSOLUTELY FORBIDDEN: no clean printed fonts, no sans-serif, no serif, no monospace`
> - `wobbly, uneven baselines with natural stroke variation`

#### 3.2 构建 Gemini 提示词（使用 Step 2.5 选定的风格）

Gemini 提示词格式与 NotebookLM instructions 不同。每张图的提示词结构：

```
Generate a TALL PORTRAIT infographic image. The image MUST be in 3:4 aspect ratio (width:height = 3:4, i.e. taller than wide, like a phone screen in portrait mode). Do NOT generate a square or landscape image.

All text must use standard Simplified Chinese characters (标准简体中文字符), ensuring every character is clear, legible, and free of garbled text.

<所选风格的 STYLE_PREFIX（参考 references/style_guide.md 中对应风格的英文版描述）>

<具体内容布局和视觉元素描述（英文为主，中文内容用中文）>
```

**关键规则**：
- Gemini 提示词以强调宽高比的指令开头，必须反复强调 `3:4 portrait, taller than wide`
- 风格描述用英文（Gemini 对英文提示词理解更好）
- 具体中文标题和文本内容保留中文
- **必须使用 Pro 模型**：默认的 Fast 模型中文渲染质量极差（严重乱码），必须切换到 Pro（Gemini 3.1 Pro）模型才能正确渲染中文

#### 3.3 为每张图编写提示词

每张图的提示词 = **所选风格的 STYLE_PREFIX** + 专属内容描述。

专属内容仍需包含：
1. **中文标题** — 用「」引用
2. **各 Section 的布局描述** — 用英文描述结构，中文内容保留中文
3. **视觉元素要求** — 容器形状、图标、连接方式

从 `references/style_guide.md` 的「视觉元素库」中选择搭配。

> **内容风格要求**：信息图中的文字内容（标题、要点、金句等）必须贴合作者「实战派技术布道者」的风格（参考用户级 skill `wechat-article-writer`）：
> - 标题用「不是X，而是Y」「X意味着Y」等句式增强洞察感
> - 要点表述务实接地气，避免空泛的技术堆砌
> - 数据引用标注来源，体现专业性
> - 保留英文术语原词（参考 `references/terminology.json`）

> **术语翻译规则**：编写提示词和文案时，**必须参考 `references/terminology.json` 英文术语保留词库**：
> - `keep_english` 列表中的词（如 Agent、token、API、GPU、LLM、prompt 等）**保持英文原词不翻译**
> - `translate_as` 映射中的词翻译为**指定的中文**（如 workflow → 工作流、scaling law → 规模定律）

#### 3.3 生成配置文件

将所有图表定义写入 JSON 配置文件（存放在项目子目录下）：

```json
[
    {
        "prompt": "<Gemini 提示词全文>",
        "filename": "infographic.png",
        "model": "pro"
    },
    {
        "prompt": "<Gemini 提示词全文>",
        "filename": "chart_1.png",
        "model": "pro"
    }
]
```

> **model 字段**: 默认为 `"pro"`（Gemini 3.1 Pro），**必须使用 Pro 模型以确保中文渲染正确**。可选 `"flash"`（快速模式）但中文质量无保障。

#### 3.4 运行生成脚本

**方式1：浏览器 CDP 模式（默认，中文渲染质量最佳）**

```bash
# 前置条件：确保 Chrome 已开启 CDP 端口（见 Step 1）
# 从配置文件批量生成
python scripts/gemini_generate_images_browser.py generate \
    --config <项目子目录>/gemini_charts_config.json \
    --output-dir <项目子目录>

# 显示浏览器窗口（调试用）
python scripts/gemini_generate_images_browser.py --no-headless generate \
    --config <项目子目录>/gemini_charts_config.json \
    --output-dir <项目子目录>
```

**浏览器 CDP 模式每张图的完整流程**：
1. 连接 Chrome CDP 端口 9222 → 打开 gemini.google.com
2. 展开侧边栏 → 点击 **Temporary chat**（临时对话，不污染聊天历史）→ **验证页面显示 "Temporary chat" 文本后才继续**（最多重试 3 次，若验证失败回退到普通新对话）
3. 点击 **Tools** 菜单 → 启用 **Create image**（激活 Nano Banana Pro 图片生成能力）
4. 点击模型选择器 → 切换到 **Pro** 模型（Gemini 3.1 Pro，中文渲染正确）
5. 通过 `execCommand('insertText')` 输入提示词（换行符替换为空格防止 Enter 触发发送）
6. 点击发送按钮提交
7. 等待图片生成完成
8. 滚动到图片位置 → hover 图片 → **点击下载按钮保存原图**

> **⚠️ 严禁截图保存信息图，必须通过下载按钮获取原图。** 截图会导致分辨率降低、画质损失、且可能包含浏览器 UI 元素。如果找不到下载入口（hover 后未出现下载按钮），则**重新生成该图片**，不要退而求其次使用截图。

**方式2：gemini-webapi 库模式（备选，等库更新后酌情启用）**

```bash
# 从配置文件批量生成
python scripts/gemini_generate_images.py generate \
    --config <项目子目录>/gemini_charts_config.json \
    --output-dir <项目子目录>
```

> **默认策略**: 优先使用方式1（浏览器 CDP 模式），中文渲染质量最佳。
> **脚本路径**: `scripts/gemini_generate_images_browser.py`（默认）和 `scripts/gemini_generate_images.py`（备选）位于本 skill 目录下。

### Step 4：撰写公众号文案并保存为 copywriting.txt

参照 `references/copywriting_guide.md` 撰写 300 字以内的公众号文案。

> **⚠️ 风格要求**：文案必须贴合作者的「实战派技术布道者」人设风格，具体参考用户级 skill `wechat-article-writer` 的风格定义。核心风格要点：
> - **场景驱动**：从真实痛点/场景出发，而非技术本身
> - **三层语言系统**：专业层（行业术语建立权威）+ 解释层（类比降低门槛）+ 口语层（自嘲/网络用语拉近距离）
> - **务实乐观**：对新技术保持热情但不盲目吹捧，会指出局限性
> - **典型句式**：「不是X，而是Y」（纠正认知）、「X意味着Y」（揭示本质）、「X才是Y」（强调重点）
> - **数据说话**：关键论点用数据支撑，引用权威来源
> - **利益披露**：涉及投资关系/商业利益时必须标注

文案结构：
1. 吸引力标题（**不超过 20 个字，含标点和 emoji**）
2. 核心观点段（场景切入，从读者痛点或好奇心出发）
3. 要点列表（3-5 个，▸ 标记）
4. 关键数据/金句
5. 立场声明（如适用，⚠️ 标注利益相关）
6. 原文出处
7. 话题标签（5-8 个）

**必须将文案保存为 `<项目子目录>/copywriting.txt`**。此文件是后续发布步骤的**文案数据源**，发布脚本会从中自动提取标题和正文内容。

文件格式：
```
<emoji> <标题>
<空行>
<正文内容（核心观点段 + 要点列表 + 金句 + 声明 + 出处）>
<空行>
#标签1 #标签2 #标签3 ...
```

### Step 5：展示结果与迭代

> **⚠️ 严禁通过 `read_file` 展示图片文件。** 多张 PNG 图片内容过大（每张 ~10000 行 base64），同时 read_file 5 张图片会导致上下文溢出、生成卡住。

1. **列出生成的文件清单**（文件名 + 文件大小），不读取图片内容：
   ```
   📷 生成的图片：
     1. infographic.png (1709KB) — 总览信息图
     2. chart_1.png (1800KB) — <主题>
     3. chart_2.png (1679KB) — <主题>
     ...
   图片已保存在 <项目子目录>/ 下，请在 IDE 或 Finder 中预览。
   ```
2. **展示 `copywriting.txt` 文案全文**（纯文本，可安全 read_file）
3. 收集用户反馈，可能的迭代方向：
   - 风格调整 → 修改提示词风格描述（参考 `references/style_guide.md`）
   - 视觉丰富度不足 → 在提示词中增加具体视觉元素描述
   - 内容聚焦调整 → 修改单张图的提示词
   - 文案优化 → 直接修改文案
4. 修改配置文件后重新运行生成脚本

## 关键经验

### 风格一致性控制
- 提示词中的风格描述是控制风格的**唯一且核心**手段，必须详细到色号级别
- 仅写「手绘风格」远远不够，必须精确指定背景色号、线条色号、禁止颜色
- 视觉丰富度需要在提示词中列出**具体的视觉元素**，否则生成结果偏单调

## 常见问题

| 问题 | 原因 | 修复方法 |
|------|------|----------|
| 图表冷色调（蓝/绿/紫） | 提示词未明确禁止 | 在风格描述中列出所有禁止的冷色调 |
| 背景色太深 | 背景描述不够精确 | 加色号参考（#F5F0E8）+ 明确禁止深色 |
| 视觉元素单一 | 提示词过于简略 | 详细描述每个区块的图标、图表类型、容器形状 |
| Gemini 返回文本而非图片 | 提示词未触发图片生成 | 确保以 `Generate an infographic image` 开头；浏览器 CDP 模式下脚本会自动启用 Create image 工具 |
| Gemini 认证失败（CDP 模式） | Chrome 未登录 Google 或 CDP profile 未初始化 | 运行 `python3 scripts/gemini_generate_images_browser.py login` 在 CDP Chrome 中登录 Google |
| CDP 端口不可用（macOS） | 使用了默认 user-data-dir 启动 Chrome，macOS Chrome 拒绝在默认目录上开 CDP | 脚本已修复：自动使用独立 profile 目录 `~/.gemini_browser/chrome_cdp_profile`。如仍失败，删除该目录重试 |
| Python `str \| None` 语法报错 | Python 版本低于 3.10 | 脚本已改用 `from __future__ import annotations` 兼容 3.9+。如仍报错，用 `/opt/homebrew/bin/python3.12` |
| playwright 模块未找到 | 当前 Python 环境未安装 playwright | `pip3 install playwright`（或 `pip3 install --break-system-packages playwright`） |
| 图片内容重复 | 提示词聚焦范围重叠 | 为每张图指定明确的聚焦主题 |
| 中文乱码/字符缺失 | 使用了 Fast 模型（默认） | **必须切换到 Pro 模型**（Gemini 3.1 Pro） |
| 图片下载失败 | 截图方式不可靠 | **严禁截图保存**。必须通过 hover → 下载按钮方式保存原图 |
| 多张图之间内容重复 | 提示词聚焦范围重叠 | 为每张图指定明确、互不重叠的聚焦主题 |
| 标题超过 20 字 | copywriting.txt 标题过长 | 在生成阶段就确保标题不超过 20 字 |
| 信息图中事实信息错误 | 素材提取阶段未验证事实性声明 | 执行 Step 2 事实信息筛查 |
| "token" 被翻译为"代币" | 未参考术语保留词库 | **必须参考 `references/terminology.json`** |
| 单张图重新生成时全量重跑 | 修改单张图的 prompt 后错误地用完整 config 重新生成 | 为单张图创建临时配置文件，生成完成后删除 |
| Gemini 临时对话打开失败/超时 | 侧边栏展开或 Temporary chat 按钮定位不稳定 | **已优化**：脚本现采用多策略定位（aria-label / mattooltip / JS DOM 遍历 / 暴力搜索）+ 创建后验证（检查页面是否显示 "Temporary chat" 文本）+ 最多 3 次重试。每次失败会保存调试截图到 `/tmp/gemini_temp_chat_attempt_N.png`。如仍失败，回退到普通新对话并在日志中警告 |
| Gemini Pro 模型切换失败 | 模型选择器 DOM 结构变化，或 Pro 按钮未找到 | 确认 Gemini 订阅有效（Pro 模型需付费订阅），检查脚本中模型选择器是否匹配最新页面结构 |
| Gemini 图片生成超时/长时间无响应 | Pro 模型排队繁忙（尤其高峰时段），或网络代理不稳定 | **增加等待超时**（默认 180s → 建议 300s）；确保代理稳定；避开高峰时段（北京时间凌晨质量最佳）；如超时可尝试重新提交同一 prompt |
| Gemini 返回 "I can't generate that image" | 提示词触发了 Gemini 安全过滤，或 Pro 模型繁忙降级 | 检查提示词是否含敏感内容；去除可能触发过滤的用词；等待几分钟后重试 |
| 图片生成过程中浏览器被冻结/崩溃 | 同时打开多个 Gemini 会话，或 Chrome 内存不足 | 关闭其他不必要的 Chrome Tab；确保 CDP 连接只打开一个 gemini.google.com 页面 |
| 下载按钮 hover 后不出现 | Gemini 页面 DOM 更新，hover 区域变化 | 脚本会尝试多种 hover + 点击策略，如全部失败则重新生成该图片（不用截图替代） |
| 批量生成中途某张失败 | 网络波动或 Gemini 临时限流 | 脚本支持 `--config` 传入部分图表重新生成；为失败的单张图创建临时配置，不需要全量重跑 |
| 整体生成耗时过长（>30分钟） | Pro 模型每张图生成 + 下载需 3-8 分钟 | 属正常范围。4-5 张图预计 15-30 分钟。**不要中途手动中断**，让脚本完成自动重试 |

### Gemini 图片生成性能优化建议

1. **时间预期**：Pro 模型每张图生成耗时 3-8 分钟（含等待排队），4-5 张图总计 15-30 分钟属正常范围
2. **最佳时段**：北京时间凌晨-上午（美西夜间）生成速度最快，下午高峰时段可能排队更久
3. **网络要求**：确保代理连接稳定，Gemini 图片生成过程中网络中断会导致需要重新生成
4. **不要并发**：一次只运行一个生成任务，不要同时启动多个 `gemini_generate_images_browser.py` 实例
5. **中断恢复**：如果批量生成中途中断，查看输出目录已生成的图片，只重新生成缺失的图片（创建临时配置文件）

## 备选方案：NotebookLM 信息图 API

当用户**明确要求**使用 NotebookLM 生成信息图时，使用 `scripts/generate_images.py` 脚本：

```bash
python scripts/generate_images.py generate \
    --notebook-id <NOTEBOOK_ID> \
    --output-dir <项目子目录路径> \
    --config <项目子目录>/charts_config.json
```

> **NotebookLM 配额警告**: 有账号级别配额（约 10-15 张/天），触发后需等待数小时恢复。

## 资源文件

| 文件 | 用途 |
|------|------|
| `scripts/gemini_generate_images_browser.py` | **默认**图片生成脚本（浏览器 CDP 模式） |
| `scripts/gemini_generate_images.py` | **备选**图片生成脚本（gemini-webapi 库模式） |
| `scripts/generate_images.py` | **备选**信息图生成脚本（NotebookLM API） |
| `references/style_guide.md` | 手绘风格规范、STYLE_PREFIX 完整文本、视觉元素库 |
| `references/notebooklm_api.md` | NotebookLM Python API 参考、配额限制说明 |
| `references/terminology.json` | 英文术语保留词库 |
| `references/copywriting_guide.md` | 公众号文案结构、语言风格、话题标签选取规范 |
