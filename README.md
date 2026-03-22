# xiaolvs-skills

小绿书 AI Skills — 一键将文章转化为手绘风格信息图并发布到小红书/微信公众号的自动化工作流。

## Skills 列表

| Skill | 说明 |
|-------|------|
| **xiaolvs-image-gen** | 将文章/观点生成手绘风格信息图 + 配套公众号文案，基于 Gemini Pro 图片生成 |
| **xiaolvs-pipeline** | 一键流水线：抓取 → 转存知识库 → 图文生成 → 发布，全流程自动化 |
| **xiaolvs-publish** | 将信息图和文案自动发布到小红书和微信公众号（Playwright 自动化） |

## 安装

将对应 skill 目录拷贝到你的 skills 目录下即可使用：

```bash
# OpenClaw
cp -R xiaolvs-image-gen ~/.openclaw/skills/
cp -R xiaolvs-pipeline ~/.openclaw/skills/
cp -R xiaolvs-publish ~/.openclaw/skills/

# CodeBuddy
cp -R xiaolvs-image-gen <project>/.codebuddy/skills/
cp -R xiaolvs-pipeline <project>/.codebuddy/skills/
cp -R xiaolvs-publish <project>/.codebuddy/skills/
```

## 前置依赖

- **xiaolvs-image-gen**: Chrome 浏览器 + Google 账号（Gemini Pro 订阅）
- **xiaolvs-pipeline**: 依赖 `fetch-archive` skill 进行文章抓取
- **xiaolvs-publish**: Python 3 + Playwright（`pip install playwright && playwright install chromium`）

## License

MIT
