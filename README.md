# xiaolvs-skills

小绿书 AI Skills — 一键将文章转化为手绘风格信息图并发布到小红书/微信公众号的自动化工作流。

## Skills 列表

| Skill | 说明 |
|-------|------|
| **xiaolvs-pipeline** | 一键流水线：抓取 → 转存知识库 → 图文生成 → 发布，全流程自动化（含两个人工确认点） |
| **xiaolvs-image-gen** | 将文章/观点生成手绘风格竖版信息图 + 配套公众号文案。**首选 ChatGPT 网页 CDP 通道**（chatgpt.com/images，走订阅配额、无 API 消耗），备选 Gemini 网页 CDP / NotebookLM / OpenAI Images API |
| **xiaolvs-publish** | 将信息图和文案发布到小红书和微信公众号草稿（Playwright CDP 自动化） |

## 安装

### 方式一：git clone + 软链（推荐，便于 `git pull` 持续更新）

```bash
git clone git@github.com:ajaxhe/xiaolvs-skills.git ~/Projects/xiaolvs-skills

# WorkBuddy（用户级，全项目可用）
ln -s ~/Projects/xiaolvs-skills/xiaolvs-image-gen ~/.workbuddy/skills/xiaolvs-image-gen
ln -s ~/Projects/xiaolvs-skills/xiaolvs-pipeline ~/.workbuddy/skills/xiaolvs-pipeline
ln -s ~/Projects/xiaolvs-skills/xiaolvs-publish ~/.workbuddy/skills/xiaolvs-publish
```

### 方式二：直接拷贝

```bash
# WorkBuddy
cp -R xiaolvs-image-gen xiaolvs-pipeline xiaolvs-publish ~/.workbuddy/skills/

# OpenClaw
cp -R xiaolvs-image-gen xiaolvs-pipeline xiaolvs-publish ~/.openclaw/skills/

# CodeBuddy（项目级）
cp -R xiaolvs-image-gen xiaolvs-pipeline xiaolvs-publish <project>/.codebuddy/skills/
```

## 前置依赖

| 依赖 | 用途 |
|------|------|
| Chrome 浏览器 | 所有 CDP 通道的载体（脚本使用独立 profile + 独立调试端口，不影响日常使用） |
| ChatGPT 账号 | image-gen 首选通道（chatgpt.com/images；免费版约 3 张大图/天，撞配额脚本自动等待续跑） |
| Google 账号（Gemini Pro 订阅） | image-gen 备选通道（中文渲染必须 Pro 模型） |
| Python 3.9+ 且安装 playwright | 全部脚本依赖：`pip install playwright`。注意用**安装了 playwright 的那个 Python**执行脚本（如 venv 路径），不要假设系统 python3 有 |
| 网络代理 | chatgpt.com / gemini.google.com 需要可访问的代理；脚本会自动探测系统代理（含死端口存活检测） |

> **macOS 沙盒环境注意**：在 WorkBuddy 等沙盒中启动 Chrome 会被拦截，脚本已内置 `--no-sandbox --disable-gpu` 兼容参数；如仍失败，请在沙盒外运行。

## 快速使用

### 一键流水线（推荐入口）

直接对 Agent 说：

```
https://<文章链接> 基于这篇文章执行 xiaolvs-pipeline，创建小绿书图文素材并发布
```

流水线会依次执行：抓取归档 → ⏸️ 确认点 1（大纲+风格）→ 图文生成 → ⏸️ 确认点 2（图文审阅）→ 发布小红书 + 公众号草稿。

### 单独生成信息图（xiaolvs-image-gen）

```bash
PY=<安装了playwright的python路径>

# 首次：打开 CDP Chrome 登录 ChatGPT（登录好后人工确认，脚本不轮询）
$PY xiaolvs-image-gen/scripts/chatgpt_generate_images_browser.py login

# 检查登录态
$PY xiaolvs-image-gen/scripts/chatgpt_generate_images_browser.py auth

# 批量生成（推荐 --until-done：撞配额自动等待续跑，直到全部完成，无需人工干预）
$PY -u xiaolvs-image-gen/scripts/chatgpt_generate_images_browser.py generate --until-done \
    --config <项目目录>/chatgpt_charts_config.json \
    --output-dir <项目目录> \
    --quota-wait-minutes 15 --max-hours 30
```

配置文件格式（prompt + filename 数组）：

```json
[
  {"prompt": "Generate a TALL PORTRAIT infographic image, 3:4 aspect ratio...", "filename": "infographic.png"},
  {"prompt": "...", "filename": "chart_1.png"}
]
```

### 单独发布（xiaolvs-publish）

将已生成的 `infographic.png` / `chart_N.png` + `copywriting.txt` 发布为小红书笔记和公众号草稿，详见 `xiaolvs-publish/SKILL.md`。

## 目录结构

```
xiaolvs-skills/
├── xiaolvs-image-gen/       # 图文生成（事实筛查 + 信息图 + 文案）
│   ├── SKILL.md
│   ├── scripts/
│   │   ├── chatgpt_generate_images_browser.py   # 首选：ChatGPT 网页 CDP（端口 9223）
│   │   ├── gemini_generate_images_browser.py    # 备选：Gemini 网页 CDP（端口 9222）
│   │   ├── gemini_generate_images.py            # 备选：gemini-webapi 库
│   │   ├── generate_images.py                   # 备选：NotebookLM API
│   │   └── openai_generate_images.py            # 备选：OpenAI Images API
│   └── references/          # 风格规范 / 术语库 / 文案指南
├── xiaolvs-pipeline/        # 一键流水线编排
└── xiaolvs-publish/         # 小红书 + 公众号发布
```

## License

MIT
