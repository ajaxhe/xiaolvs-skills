# NotebookLM Python API 参考

本文件记录了 `notebooklm` 包的关键 API，供生成脚本使用。

## 安装

```bash
pip install notebooklm
```

要求 Python 3.10+。

## 认证

认证数据存储在 `~/.notebooklm/storage_state.json`，通过 Playwright 浏览器登录获取。

```python
from notebooklm import NotebookLMClient

# 从存储文件创建客户端（推荐）
async with await NotebookLMClient.from_storage() as client:
    ...
```

## 核心 API

### 创建 Notebook

```python
notebook = await client.notebooks.create("标题")
# notebook.id -> str (notebook ID)
```

### 添加 URL 来源

```python
source = await client.sources.add_url(
    notebook_id,
    "https://example.com",
    wait=True,        # 等待处理完成
    wait_timeout=120.0 # 超时秒数
)
# source.id -> str (source ID)
```

### 生成信息图

```python
from notebooklm._artifacts import InfographicOrientation, InfographicDetail

result = await client.artifacts.generate_infographic(
    notebook_id,
    source_ids=None,           # None 使用全部 sources
    language="zh_Hans",        # 中文简体
    instructions="...",        # 生成指令
    orientation=InfographicOrientation.PORTRAIT,  # 3:4 竖版
    detail_level=InfographicDetail.DETAILED,      # 详细级别
)
# result.task_id -> str (用于轮询和下载)
```

### 枚举值

**InfographicOrientation:**
| 成员 | 值 | 说明 |
|------|-----|------|
| LANDSCAPE | 1 | 9:16 竖屏/横版模式（实际生成为9:16竖屏长图） |
| PORTRAIT | 2 | 竖版 3:4 |
| SQUARE | 3 | 正方形 |

> **注意**：LANDSCAPE 虽然字面意思是「横版」，但在 NotebookLM 信息图生成中实际对应 **9:16 竖屏长图**。如需生成 9:16 比例的图片，应使用 `LANDSCAPE`。

**InfographicDetail:**
| 成员 | 值 | 说明 |
|------|-----|------|
| CONCISE | 1 | 简洁 |
| STANDARD | 2 | 标准 |
| DETAILED | 3 | 详细 |

**ArtifactStatus:**
| 成员 | 值 | 说明 |
|------|-----|------|
| PROCESSING | 1 | 生成中 |
| PENDING | 2 | 排队中 |
| COMPLETED | 3 | 已完成 |

**StudioContentType:**
| 成员 | 值 | 说明 |
|------|-----|------|
| INFOGRAPHIC | 7 | 信息图 |

### 轮询任务状态

```python
from notebooklm._artifacts import StudioContentType, ArtifactStatus

artifacts_data = await client.artifacts._list_raw(notebook_id)
infographics = [
    a for a in artifacts_data
    if isinstance(a, list) and len(a) > 4
    and a[2] == StudioContentType.INFOGRAPHIC
]
completed_ids = {a[0] for a in infographics if a[4] == ArtifactStatus.COMPLETED}
```

### 下载信息图

```python
path = await client.artifacts.download_infographic(
    notebook_id,
    "output.png",           # 输出路径
    artifact_id="some-id"   # 指定 artifact ID
)
```

## 配额限制

- NotebookLM 有**账号级别**的信息图生成配额（非 notebook 级别）
- 累计生成约 10-15 张后可能触发速率限制
- API 返回 `UserDisplayableError`（"Request rejected by API - may indicate rate limiting or quota exceeded"）
- 需等待数小时配额重置后重试

## 生成策略

- **串行提交**：逐个提交，每次间隔 5 秒（避免并发 RPC 失败）
- **指数退避重试**：失败后等待 30s → 60s → 120s → 240s，最多 5 次
- **task_id 校验**：提交后检查 task_id 是否为空，为空则自动重试
- **按 artifact_id 下载**：避免下载到错误的 artifact
