# 手绘信息图风格指南

本文件定义了小绿书手绘信息图的视觉风格规范。目前支持两种命名风格，可通过 `gemini_charts_config.json` 中的 `style` 字段切换。

## 风格列表

| 风格名称 | 英文 ID | 简述 |
|----------|---------|------|
| 暖纸手账 | `warm-sketch` | 浅奶油白背景 + 棕色手绘线条，温馨手账风 |
| 黑金粉笔 | `blackboard-gold` | 深黑背景 + 金色/铜色线条，高端黑板风 |

> **切换方式**：在 `gemini_charts_config.json` 每张图的对象中添加 `"style": "warm-sketch"` 或 `"style": "blackboard-gold"` 字段。不指定时默认使用 `warm-sketch`。

---

## 风格 A：暖纸手账（warm-sketch）— 默认风格

### STYLE_PREFIX_WARM_SKETCH

以下文本必须作为每张信息图 `instructions` 的开头，确保风格统一：

```
Visual Style (MANDATORY, highest priority):
1. Background: very light creamy white / pale beige (#F5F0E8 ~ #FAF6EE), bright and airy like slightly yellowed vintage paper. NEVER use dark, muddy, or heavy kraft-paper brown.
2. Lines & illustrations: medium brown (#8B7355) hand-drawn pen-sketch style, loose and casual, not too heavy.
3. Text: titles in dark brown bold; body in dark brown regular weight. All text MUST be standard Simplified Chinese (标准简体中文), clear and legible, no garbled characters.
4. Palette: brown family (light / medium / dark brown) with sparse warm accents (soft red, gold, orange).
5. FORBIDDEN: absolutely no blue, green, purple, or any cool tones. No glow effects, gradients, drop shadows, techno/futuristic elements, or modern-UI styling. No dark backgrounds.
6. Overall feel: a bright, warm hand-journal page — light paper with brown-ink hand-drawn doodles and text. Relaxed, warm, bright.
7. Visual richness (critical): the page must contain diverse hand-drawn visual elements to avoid monotony:
   - At least 4-5 different container shapes (cloud bubbles, scroll banners, rounded-rect cards, dashed boxes, wavy borders, flag/ribbon shapes, etc.).
   - Abundant hand-drawn icons and visual metaphors (scales, bridges, brains, lightbulbs, pyramids, globes, speech bubbles, megaphones, magnifying glasses, coins, rockets, gears, books, keys, arrows, stars, flags, etc.) — pair one icon with every key data point or concept.
   - Multiple data-viz forms (donut charts, bar charts, progress bars, pyramids, funnels) — not just tables or plain text.
   - Connect blocks with dashed lines, arrows, curved connectors for visual flow.
   - Vary block size, shape, and orientation — avoid monotonous top-to-bottom linear layout.
   - Overall layout should feel like a carefully arranged hand-journal page full of playful doodles.
```

### 色彩规范（暖纸手账）

| 元素 | 色号 | 说明 |
|------|------|------|
| 背景色 | #F5F0E8 ~ #FAF6EE | 浅奶油白/浅米色，明亮通透 |
| 主线条 | #8B7355 | 中等棕色，手绘钢笔素描 |
| 标题文字 | 深棕色 | 粗体字（标准简体中文） |
| 正文文字 | 深棕色 | 清晰可读 |
| 点缀色 | 柔和红/金/橙 | 少量使用 |
| 禁用色 | 蓝/绿/紫等冷色调 | 绝对禁止 |

---

## 风格 B：黑金粉笔（blackboard-gold）

### STYLE_PREFIX_BLACKBOARD_GOLD

```
Visual Style (MANDATORY, highest priority):
1. Background: deep matte black (#0A0A0A ~ #141414), solid and uniform like a premium blackboard or matte-black poster board. No texture noise, no gradients, just clean pure dark.
2. Lines & illustrations: hand-drawn chalk-sketch style using gold / copper / brass tones (#C9A84C ~ #D4AF37 for main gold, #B87333 for copper accents). Lines feel loose and chalky — like drawing with a metallic chalk or gold marker on a blackboard. Line weight varies naturally, thicker for containers and thinner for details.
3. Text & Typography: ALL text must use a natural handwritten / hand-lettered font style — as if written with a gold chalk marker by hand. Titles in bright gold (#D4AF37) large handwritten strokes with slight unevenness (not perfectly aligned, slightly wobbly baselines); body text in off-white (#E8E0D0) in a lighter handwritten style. Label tags use white (#FFFFFF) handwritten bold inside rounded-rect containers with thin gold borders. FORBIDDEN: no printed/typed fonts, no sans-serif, no serif, no monospace — everything must look hand-written on the blackboard. All text MUST be standard Simplified Chinese (标准简体中文), clear and legible despite the handwritten style.
4. Palette: gold family as primary (pale gold / rich gold / copper / brass), off-white for body text and secondary labels, occasional warm accents (amber #FFBF00, soft orange #CC7722). Very sparing use only.
5. FORBIDDEN: absolutely no blue, green, purple, or neon colors. No 3D renders, photo-realism, glossy reflections, or lens flares. No colored fills inside containers — keep interiors transparent (showing the dark background through). No busy gradients.
6. Overall feel: a premium, high-end blackboard diagram — like a strategic whiteboard in an executive boardroom, but on black with gold chalk. Clean, authoritative, elegant. Think NVIDIA keynote visual style or luxury brand tech illustration.
7. Visual richness (critical): the page must look like a carefully crafted blackboard infographic:
   - Use at least 3-4 different container shapes (rounded rectangles with thin gold borders, bracket/brace groupings, layered horizontal bars/slabs, tag/pill shapes for labels).
   - Hand-drawn gold-line icons for each concept (atoms, robots, cars, brains, factories, lightning bolts, server racks, chips, molecular structures, etc.) — sketchy but recognizable, like quick whiteboard drawings.
   - Data visualization: layered cake/stack diagrams, bracket trees, grid/matrix layouts, connection lines with arrowheads — all in gold outlines on black.
   - Connect blocks with thin gold lines, arrows, and curly braces to show hierarchy and relationships.
   - Layout can use a split-panel approach: main diagram on left, detail breakout on right (or top-bottom), creating visual depth and information hierarchy.
   - Overall layout should feel like a premium conference keynote diagram — structured, clear, impactful.
```

### 色彩规范（黑金粉笔）

| 元素 | 色号 | 说明 |
|------|------|------|
| 背景色 | #0A0A0A ~ #141414 | 纯净深黑，无纹理 |
| 主线条/图标 | #D4AF37 / #C9A84C | 金色/黄铜色手绘线条 |
| 铜色点缀 | #B87333 | 铜色辅助线条和装饰 |
| 标题文字 | #D4AF37 | 金色手写体大字 |
| 正文/标签文字 | #E8E0D0 / #FFFFFF | 米白色手写体正文，纯白标签 |
| 点缀色 | 琥珀 #FFBF00 / 暖橙 #CC7722 | 极少量使用 |
| 禁用色 | 蓝/绿/紫/霓虹色 | 绝对禁止 |

---

## 视觉元素库

编写每张图的 instructions 时，根据所选风格从以下元素中选择搭配：

### 信息容器形状

| 元素 | warm-sketch | blackboard-gold |
|------|:-----------:|:---------------:|
| 云朵形气泡框 | ✅ | ❌ |
| 卷轴/横幅标题框 | ✅ | ❌ |
| 圆角矩形卡片 | ✅ | ✅ |
| 虚线框 | ✅ | ✅（金色虚线） |
| 波浪边框 | ✅ | ❌ |
| 旗帜形状 | ✅ | ❌ |
| 六边形框 | ✅ | ✅ |
| 椭圆框 | ✅ | ❌ |
| 水平层叠条/板块 | ❌ | ✅ |
| 大括号分组 | ❌ | ✅ |
| 标签/药丸形状 | ❌ | ✅ |
| 薄金线边框卡片 | ❌ | ✅ |

### 手绘图标
- 概念类：天平、桥梁、大脑、灯泡、金字塔、地球、齿轮
- 动作类：放大镜、搜索框、钥匙、锁、火箭
- 交流类：对话气泡、喇叭、信封、电话
- 数据类：金币、购物车、钻石、钱包
- 标记类：箭头、星星、旗帜、书签、勾号
- 科技类（blackboard-gold 专用）：芯片、服务器机架、原子结构、机器人、闪电、工厂、分子结构、卫星

### 数据可视化

| 类型 | warm-sketch | blackboard-gold |
|------|:-----------:|:---------------:|
| 手绘环形图/饼图 | ✅ | ✅ |
| 手绘柱状图 | ✅ | ✅ |
| 手绘金字塔分层图 | ✅ | ✅ |
| 手绘漏斗图 | ✅ | ✅ |
| 手绘天平图 | ✅ | ❌ |
| 手绘桥梁图 | ✅ | ❌ |
| 蜿蜒路径/时间线 | ✅ | ✅ |
| 进度条 | ✅ | ✅ |
| 层叠蛋糕/堆叠图 | ❌ | ✅ |
| 大括号树状图 | ❌ | ✅ |
| 网格/矩阵布局 | ❌ | ✅ |
| 左右分栏对比 | ❌ | ✅ |

### 连接元素
- 弯曲箭头
- 虚线连接
- 弯曲连接线
- 手绘指示箭头
- 大括号连接（blackboard-gold 专用）
- 金色细线箭头（blackboard-gold 专用）

---

## Instructions 编写规范

每张图的 `instructions` = **所选风格的 STYLE_PREFIX** + 专属内容。

- warm-sketch → 使用 `STYLE_PREFIX_WARM_SKETCH`
- blackboard-gold → 使用 `STYLE_PREFIX_BLACKBOARD_GOLD`

专属内容应包含：

1. **聚焦声明**：本图关注原文哪部分内容
2. **中文标题**：简短有力，带冒号分隔副标题（如「AI用途的价值鸿沟：为什么大多数用户不愿付费？」）
3. **内容布局和视觉元素要求**（逐条列出）：
   - 标题区：什么形状的标题框 + 配什么图标
   - 核心数据区：用什么图表形式展示
   - 视觉隐喻区：用什么隐喻图表达概念
   - 连接方式：各区块如何串联

### 编写要点
- 越详细越好，模糊描述会导致生成结果不可控
- 为每个数据点或概念指定具体的图标
- 明确图表类型（金字塔/柱状图/漏斗/环形图等）
- 指定区块之间的连接方式
- instructions 中的引号使用中文全角引号「」，避免与 Python 字符串分隔符冲突

### 风格选择建议
- **warm-sketch（暖纸手账）**：适合知识分享、方法论总结、产品分析等「亲切感」强的内容
- **blackboard-gold（黑金粉笔）**：适合技术架构、战略分析、行业趋势等「权威感」强的内容
