---
name: xiaolvs-pipeline
description: 小绿书一键流水线：自动按顺序执行抓取→转存→图文生成→发布的完整工作流。当用户提供文章链接并要求一键生成小绿书图文并发布时，使用此 skill。关键词触发：小绿书、一键生成、自动发布、完整流程、pipeline、流水线。
---

# 小绿书一键流水线

## 概述

一键编排「抓取 → ⏸️确认 → 图文生成 → ⏸️确认 → 发布」完整工作流，依次调用以下 3 个子 skill，在关键节点暂停等待人工确认：

1. **fetch-archive** — 抓取文章内容 + 转存「贾维斯」知识库
2. **⏸️ 确认点 1** — 输出素材大纲和图表规划，等待用户确认
3. **xiaolvs-image-gen** — 事实筛查 + 信息图生成 + 文案撰写
4. **⏸️ 确认点 2** — 展示生成的图片和文案，等待用户确认
5. **xiaolvs-publish** — 发布到小红书 + 微信公众号

### 最终产出物
1. `<原文标题>.md` + `<原文标题>_meta.json` — 原文存档（使用原文标题命名，不用 article.md）
2. 「贾维斯」乐享知识库文档副本
3. `infographic.png` + `chart_1~N.png` — 手绘信息图
4. `copywriting.txt` — 公众号文案
5. 小红书草稿 + 微信公众号草稿

## 完整工作流

### Phase 1：抓取与归档（fetch-archive）

**输入**：用户提供的文章 URL 或文字素材
**输出**：`<项目子目录>/<原文标题>.md`、`<原文标题>_meta.json`、乐享知识库文档

1. 根据 URL 类型选择抓取方式（详见 fetch-archive 决策树）。**绝大多数网页文章都包含图片，默认用 `fetch_article.py`**；仅确认为纯文字文章时才可用 `web_fetch`
2. 保存**原文全文**为 `<原文标题>.md` + `<原文标题>_meta.json`（使用原文标题命名，不用 article.md；保存原文全文，不做总结/摘要/改写）
3. **非中文文章翻译**（⚠️ 不可跳过）：检测原文语言，如果非中文则翻译为中英对照格式（一段原文 + 一段中文翻译，不加分隔线和国旗emoji，保留图片引用），生成 `<原文标题>_translated.md`。详见 fetch-archive SKILL.md 的「步骤 3.5：非中文文章翻译」
4. **转存到乐享知识库**（⚠️ 不可跳过）：
   - 将抓取的原文完整转存到乐享知识库，确保素材归档和可追溯
   - **目标知识库、目录结构、上传方式等配置均在 fetch-archive skill 的 `config.json` 中定义**，初始化时由 fetch-archive 的对话式配置流程自动完成
   - 具体操作步骤（获取根节点 → 检查/创建日期目录 → 去重 → 上传）详见 fetch-archive SKILL.md 的「Step 2：原文保存到乐享知识库」章节
   - 图文文章转 PDF 上传，纯文本文章以在线文档（page）格式创建
   - 上传完成后向用户展示知识库文档链接

### ⏸️ 确认点 1：素材大纲确认（Phase 1 → Phase 2 之间）

原文下载到本地后，**必须暂停等待用户确认**，再进入图文生成阶段。

**输出内容**：
```
📋 素材大纲
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【文章主题】一句话概括
【核心论点】3-5 个关键观点（编号列出）
【关键数据】文章中的重要数据/图表/案例
【利益相关】作者/机构的立场与潜在倾向（如有）
【原文出处】完整标题 + URL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📊 图表规划
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
第 1 张（总览图）：<主题描述>
第 2 张（论点1）：<主题描述>
第 3 张（论点2）：<主题描述>
...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

👉 请确认以上大纲和图表规划是否 OK，或提出修改意见。
```

**等待用户回复**后才进入 Phase 2。用户可以：
- 确认 → 继续
- 调整图表数量/主题划分
- 补充/删除核心论点
- 修改聚焦方向

### ⏸️ 确认点 1.5：选择配图风格

素材大纲确认后，**必须让用户选择图片风格**，再进入图文生成阶段。

使用 `ask_followup_question` 向用户展示两种风格选项，**默认选择黑金粉笔（`blackboard-gold`）**：

| 风格名称 | 英文 ID | 视觉特征 | 适用场景 |
|----------|---------|----------|----------|
| **暖纸手账** | `warm-sketch` | 浅奶油白纸张 + 棕色手绘线条 + 暖色点缀 | 知识分享、方法论、产品分析（亲切温馨） |
| **黑金粉笔** | `blackboard-gold` | 深黑底 + 金色/铜色粉笔线条 + 米白手写文字 | 技术架构、战略分析、行业趋势（权威高端） |

用户选择后，将所选风格传递给 Phase 2 的图文生成步骤。

### Phase 2：图文生成（xiaolvs-image-gen）

**输入**：用户确认后的素材大纲 + 图表规划
**输出**：`infographic.png`、`chart_1~N.png`、`copywriting.txt`、`gemini_charts_config.json`

1. 检查 Python 环境与 Chrome CDP 认证（脚本自动启动独立 CDP Chrome，无需手动关闭/重启用户 Chrome）
2. 事实信息筛查（Fact-Check），向用户报告筛查结果
3. 编写 Gemini 提示词，生成配置文件
4. 运行生成脚本批量生成信息图
5. 撰写公众号文案，保存为 `copywriting.txt`

### ⏸️ 确认点 2：图文审阅（Phase 2 → Phase 3 之间）

图文全部生成后，**必须暂停等待用户确认**，再进入发布阶段。

> **⚠️ 严禁通过 `read_file` 展示图片文件。** 多张 PNG 图片内容过大（每张 ~10000 行 base64），同时 read_file 多张图片会导致上下文溢出、生成卡住。应列出文件清单（名称+大小），提示用户在 IDE 或 Finder 中预览。

**输出内容**：
```
🎨 图文生成完成，请审阅：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📷 生成的图片（请在 IDE 或 Finder 中预览）：
  1. infographic.png (xxxKB) — 总览信息图
  2. chart_1.png (xxxKB) — <主题>
  3. chart_2.png (xxxKB) — <主题>
  ...

📝 文案预览（copywriting.txt）：
  <显示 copywriting.txt 完整内容>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👉 请确认图片质量和文案内容是否符合预期：
  - 图片：风格、配色、文字是否清晰、内容是否准确
  - 文案：标题、正文、标签是否合适
  - 如需修改某张图，请指出具体问题
```

**等待用户回复**后才进入 Phase 3。用户可以：
- 确认全部 OK → 继续发布
- 要求重新生成某张图片
- 修改文案内容
- 调整标签

### Phase 3：发布（xiaolvs-publish）

**输入**：用户确认后的图片文件 + `copywriting.txt`
**输出**：小红书草稿 + 微信公众号草稿

1. **小红书**：创建 `xhs_publish_config.json` → 使用 `--auto`（填写完成后仍等待用户确认再发布）
2. **微信公众号**：使用 `--copywriting` 模式（或 `--config` + `copywriting_file`）+ `--collection` 参数 + `--auto` → 逐张图片顺序上传 → 填写保存 → 保存草稿

**用户确认点**：小红书脚本填写完成后等待回车确认；微信公众号在后台检查草稿后手动发布

---

## 使用方式

用户只需提供文章 URL，例如：

> "帮我把这篇文章做成小绿书：https://example.com/article"

流水线会自动依次执行 3 个 Phase，在每个关键节点暂停等待用户确认。

### 快速参考命令

```bash
# Phase 1: 抓取（付费文章）
python scripts/fetch_article.py <URL> --output-dir <项目子目录>

# Phase 1: 转存「贾维斯」知识库（使用原文标题）
source ~/.codebuddy/skills/lexiang/scripts/init.sh
bash scripts/save_to_lexiang.sh "<项目子目录>/<原文标题>.md" "<乐享知识库URL，从 fetch-archive 的 config.json 中读取>" "<原文标题>"

# ⏸️ 确认点 1：输出素材大纲 + 图表规划，等待用户确认

# Phase 2: 生成信息图
python scripts/gemini_generate_images_browser.py generate \
    --config <项目子目录>/gemini_charts_config.json \
    --output-dir <项目子目录>

# ⏸️ 确认点 2：展示图片和文案，等待用户确认

# Phase 3: 小红书草稿（CDP模式，填写完成后等待用户确认再发布）
python scripts/xhs_publish_playwright.py publish --copywriting <项目子目录>/copywriting.txt --images <项目子目录>/infographic.png <项目子目录>/chart_1.png ... --collection "AI产品经理" --auto --cdp

# Phase 3: 微信公众号草稿（CDP模式）
python scripts/wechat_mp_publish_playwright.py publish \
    --copywriting <项目子目录>/copywriting.txt \
    --images <项目子目录>/infographic.png <项目子目录>/chart_1.png ... \
    --collection AI前沿 \
    --auto --cdp
```

## 流水线配置

### 项目子目录命名

每个文章项目使用独立子目录，命名格式：**文章英文标题，单词用 `-` 分隔，全小写**。中文标题需先翻译成英文再转换。

**不要使用日期作为父目录**（如 `articles/2026-04-01/`），直接在 `articles/` 下创建以标题命名的子目录。

示例：
- `articles/ai-powered-customer-discovery/`（英文标题直接转换）
- `articles/the-five-moats-in-ai-era/`（中文标题「当AI可以做一切，剩下的护城河只有这5种」翻译后转换）
- `articles/the-fluency-test/`

### 默认参数

| 参数 | 小红书 | 微信公众号 |
|------|--------|-----------|
| 合集 | AI产品经理 | AI前沿 |
| 草稿模式 | `--auto`（填写后等待确认） | `--auto`（逐张图片上传，保存草稿） |
| 图片顺序 | infographic 排第一 | infographic 排第一 |

### 中断恢复

流水线在任何 Phase 中断后，可从上次完成的 Phase 继续：
- 如果 `<原文标题>.md` 已存在 → 跳过 Phase 1，从 Phase 2 开始
- 如果 `copywriting.txt` 和图片已存在 → 跳过 Phase 1-2，从 Phase 3 开始
- 单个 Phase 失败不影响已完成的 Phase 产出物

## 子 Skill 关系

```
xiaolvs-pipeline（编排层）
  ├── fetch-archive（抓取+归档）
  │     ├── scripts/fetch_article.py
  │     └── scripts/save_to_lexiang.sh
  ├── xiaolvs-image-gen（图文生成）
  │     ├── scripts/gemini_generate_images_browser.py
  │     ├── scripts/gemini_generate_images.py
  │     ├── scripts/generate_images.py
  │     └── references/*
  └── xiaolvs-publish（发布）
        ├── scripts/xhs_publish.py
        └── scripts/wechat_mp_publish.py
```

## ⚠️ 数据纪律（最高优先级）

**信息图中的所有数据必须严格来源于原文，绝不混入外部数据源。**

1. **事实筛查（Fact-Check）的作用是验证，不是替换**：通过 web_search 验证原文数据是否合理，但最终写入图片 prompt 的必须是原文原话/原文数据，不是外网搜到的数据
2. **禁止将外部验证数据写入图片**：例如原文写「ARR 突破 $1000 亿」，即使外网数据显示不同，图片中仍使用原文的 $1000 亿
3. **允许标注推导公式/假设**：如果用户提供了推导公式（如 `Anthropic ARR × 65% + OpenAI ARR × 50%`），可以在图片中展示公式作为数据来源说明
4. **没有出现在原文中的具体数字一律不用**：如外网搜到的「Cursor $20亿 ARR」「GitHub Copilot 37%份额」等，如果原文没提到就不能出现在图片中
5. **发现原文数据与外网矛盾时**：向用户报告差异，由用户决定是否修正，不要自行替换

## CDP Chrome 管理（Phase 2 图片生成 + Phase 3 发布共用）

### 两个 CDP Chrome 实例的区别

| 用途 | Profile 目录 | 登录态 |
|------|-------------|--------|
| **Gemini 图片生成** | `~/.gemini_browser/chrome_cdp_profile` | Google 账号（Gemini Pro） |
| **平台发布（小红书+微信公众号）** | `~/.config/google-chrome-cdp` | 小红书 + 微信公众号 |

### 发布 CDP Chrome 启动方式

```bash
# 首次启动（需要手动登录小红书和微信公众号）
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.config/google-chrome-cdp" &
```

> **⚠️ macOS Chrome 限制**：不能在默认 user-data-dir 上开启 CDP（报错 `DevTools remote debugging requires a non-default data directory`），必须指定独立 `--user-data-dir`。

### 登录态管理

1. **首次使用**：启动 CDP Chrome 后，手动打开小红书创作者平台和微信公众号后台扫码登录
2. **登录态持久化**：登录态保存在 `~/.config/google-chrome-cdp/` 目录中，只要不删除此目录，后续复用无需重新登录
3. **登录态过期处理**：如果脚本检测到页面跳转到 login 页面，应先在 CDP Chrome 中打开对应平台等待用户重新登录，确认登录成功后再执行发布脚本
4. **同时保存 storage_state**：登录成功后用 Playwright 导出 storage_state 作为备份：
   - 小红书：`~/.xiaohongshu/storage_state.json`
   - 微信公众号：`~/.wechat_mp/storage_state.json`

### 发布前检查登录态流程

```python
# 在执行发布脚本前，先检查 CDP Chrome 是否可连接，平台是否已登录
# 如果未登录，打开登录页面并等待用户扫码，确认成功后再执行发布
```

## 注意事项

- **两个关键确认点不可跳过**：确认点 1（素材大纲）和确认点 2（图文审阅）必须等待用户明确回复后才能继续
- **乐享知识库转存不可跳过**：Phase 1 中抓取原文后，**必须**将原文转存到乐享知识库。这是素材归档和可追溯的基础，不能因为急于进入图文生成而跳过。目标知识库和配置由 fetch-archive skill 管理
- **原文全文保存**：Phase 1 保存的必须是原文全文，不是总结/摘要/改写
- **图片不可丢失**：`web_fetch` **无法提取和下载页面中的图片**，任何包含图片/截图/图表的文章必须用 `fetch_article.py`。当不确定文章是否含图时，默认用 `fetch_article.py`
- **用原文标题命名**：本地文件用 `<原文标题>.md` 命名（不用 `article.md`），乐享知识库转存也使用原文标题
- **数据来源一致性**：所有图片中的数据只能来自原文，不能混入 web_search 的外部数据（详见上方「数据纪律」）
- **风格一致性**：所有图片必须使用同一份 STYLE_PREFIX，第一张总览图的 prompt 应与后续图保持相同的字体/手写体强调程度
- 流水线不跳过任何用户确认点，确保人工把关内容质量
- 每个 Phase 的详细说明请参考对应子 skill 的 SKILL.md
- 乐享知识库上传前自动按文件名+类型去重
- 企微机器人触发场景使用 `scripts/wecom_bot_service.py`，可自动执行完整流水线
