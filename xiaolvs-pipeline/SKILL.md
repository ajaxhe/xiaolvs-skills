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
3. **转存到乐享知识库**（⚠️ 不可跳过）：
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

# Phase 3: 小红书草稿（填写完成后等待用户确认再发布）
python scripts/xhs_publish_playwright.py publish --config <项目子目录>/xhs_publish_config.json --auto

# Phase 3: 微信公众号草稿
python scripts/wechat_mp_publish_playwright.py publish \
    --copywriting <项目子目录>/copywriting.txt \
    --images <项目子目录>/infographic.png <项目子目录>/chart_1.png ... \
    --collection AI前沿 \
    --auto
```

## 流水线配置

### 项目子目录命名

每个文章项目使用独立子目录，命名格式：`<来源>-<主题关键词>`

示例：
- `simon-code-is-cheap`
- `lenny-ai-analysis`
- `a16z-vertical-saas`

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

## 注意事项

- **两个关键确认点不可跳过**：确认点 1（素材大纲）和确认点 2（图文审阅）必须等待用户明确回复后才能继续
- **乐享知识库转存不可跳过**：Phase 1 中抓取原文后，**必须**将原文转存到乐享知识库。这是素材归档和可追溯的基础，不能因为急于进入图文生成而跳过。目标知识库和配置由 fetch-archive skill 管理
- **原文全文保存**：Phase 1 保存的必须是原文全文，不是总结/摘要/改写
- **图片不可丢失**：`web_fetch` **无法提取和下载页面中的图片**，任何包含图片/截图/图表的文章必须用 `fetch_article.py`。当不确定文章是否含图时，默认用 `fetch_article.py`
- **用原文标题命名**：本地文件用 `<原文标题>.md` 命名（不用 `article.md`），乐享知识库转存也使用原文标题
- 流水线不跳过任何用户确认点，确保人工把关内容质量
- 每个 Phase 的详细说明请参考对应子 skill 的 SKILL.md
- 乐享知识库上传前自动按文件名+类型去重
- 企微机器人触发场景使用 `scripts/wecom_bot_service.py`，可自动执行完整流水线
