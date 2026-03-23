#!/usr/bin/env python3
"""
Generate hand-drawn style infographic images via NotebookLM.

Usage:
    python generate_images.py --notebook-id <ID> --output-dir <DIR> --config <JSON>

The --config argument accepts a JSON file path containing a list of chart definitions:
[
    {
        "instructions": "...",
        "filename": "infographic.png"
    },
    ...
]

Each chart's instructions should already include the STYLE_PREFIX.
"""
import argparse
import asyncio
import json
import sys

from notebooklm import NotebookLMClient
from notebooklm._artifacts import (
    InfographicOrientation,
    InfographicDetail,
    StudioContentType,
    ArtifactStatus,
)


async def check_auth() -> bool:
    """Verify NotebookLM authentication is valid."""
    try:
        async with await NotebookLMClient.from_storage() as client:
            notebooks = await client.notebooks.list()
            print(f"✅ 认证有效，共 {len(notebooks)} 个 notebook")
            return True
    except Exception as e:
        print(f"❌ 认证失败: {e}")
        return False


async def create_notebook(title: str, url: str) -> dict:
    """Create a new notebook and add a URL source.

    Returns dict with notebook_id and source_id.
    """
    async with await NotebookLMClient.from_storage() as client:
        notebook = await client.notebooks.create(title)
        print(f"✅ Notebook 已创建: {notebook.id}")

        source = await client.sources.add_url(notebook.id, url, wait=True)
        print(f"✅ 来源已添加: {source.id}")

        return {"notebook_id": notebook.id, "source_id": source.id}


async def generate_and_download(
    notebook_id: str,
    charts: list[dict],
    output_dir: str,
    max_retries: int = 5,
    max_wait: int = 300,
    poll_interval: int = 10,
):
    """Generate infographics serially and download after completion.

    Args:
        notebook_id: The NotebookLM notebook ID.
        charts: List of dicts with 'instructions' and 'filename' keys.
        output_dir: Directory to save downloaded PNG files.
        max_retries: Max retry attempts per chart submission.
        max_wait: Max seconds to wait for all tasks to complete.
        poll_interval: Seconds between status polls.
    """
    import os

    os.makedirs(output_dir, exist_ok=True)
    total = len(charts)

    async with await NotebookLMClient.from_storage() as client:
        # --- Serial submission with exponential backoff ---
        print(f"=== 逐个提交 {total} 张竖版信息图生成任务 ===")
        task_ids = [None] * total

        for i, chart in enumerate(charts):
            for attempt in range(max_retries):
                if attempt > 0:
                    wait = 30 * (2 ** (attempt - 1))
                    print(f"  [{i+1}] 第 {attempt} 次重试，等待 {wait}s ...")
                    await asyncio.sleep(wait)
                try:
                    # Support per-chart orientation override
                    orientation_str = chart.get("orientation", "PORTRAIT").upper()
                    orientation_map = {
                        "LANDSCAPE": InfographicOrientation.LANDSCAPE,
                        "PORTRAIT": InfographicOrientation.PORTRAIT,
                        "SQUARE": InfographicOrientation.SQUARE,
                    }
                    orientation = orientation_map.get(orientation_str, InfographicOrientation.PORTRAIT)
                    # Support per-chart detail_level override
                    detail_level_str = chart.get("detail_level", "DETAILED").upper()
                    detail_level_map = {
                        "CONCISE": InfographicDetail.CONCISE,
                        "STANDARD": InfographicDetail.STANDARD,
                        "DETAILED": InfographicDetail.DETAILED,
                    }
                    detail_level = detail_level_map.get(detail_level_str, InfographicDetail.DETAILED)
                    result = await client.artifacts.generate_infographic(
                        notebook_id,
                        language="zh_Hans",
                        instructions=chart["instructions"],
                        orientation=orientation,
                        detail_level=detail_level,
                    )
                    if result.task_id:
                        print(f"  [{i+1}] ✅ 已提交，task_id: {result.task_id}")
                        task_ids[i] = result.task_id
                        break
                    else:
                        print(f"  [{i+1}] ⚠️  task_id 为空，将重试")
                except Exception as e:
                    print(f"  [{i+1}] ❌ 提交异常: {e}")

            if not task_ids[i]:
                print(f"  [{i+1}] ❌ 多次重试仍失败")

            if i < total - 1:
                await asyncio.sleep(5)

        failed = [i for i, tid in enumerate(task_ids) if not tid]
        if failed:
            print(f"\n❌ 以下图表提交失败: {[i+1 for i in failed]}")
            return False

        # --- Poll until all complete ---
        print("\n=== 等待所有任务完成 ===")
        elapsed = 0

        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            artifacts_data = await client.artifacts._list_raw(notebook_id)
            infographics = [
                a for a in artifacts_data
                if isinstance(a, list) and len(a) > 4
                and a[2] == StudioContentType.INFOGRAPHIC
            ]
            completed_ids = {
                a[0] for a in infographics if a[4] == ArtifactStatus.COMPLETED
            }
            done = sum(1 for tid in task_ids if tid in completed_ids)
            print(f"  [{elapsed}s] 已完成 {done}/{total}")

            if done == total:
                break
        else:
            print("⚠️  超时，部分任务可能未完成")

        # --- Download ---
        print("\n=== 下载信息图 ===")
        for i, (tid, chart) in enumerate(zip(task_ids, charts)):
            output_path = os.path.join(output_dir, chart["filename"])
            try:
                path = await client.artifacts.download_infographic(
                    notebook_id, output_path, artifact_id=tid
                )
                print(f"  [{i+1}] ✅ 已下载: {path}")
            except Exception as e:
                print(f"  [{i+1}] ❌ 下载失败 ({tid}): {e}")

        print("\n=== 全部完成 ===")
        return True


def main():
    parser = argparse.ArgumentParser(description="Generate NotebookLM infographics")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # auth check
    subparsers.add_parser("auth", help="Check NotebookLM authentication")

    # create notebook
    create_parser = subparsers.add_parser("create", help="Create notebook and add URL source")
    create_parser.add_argument("--title", required=True, help="Notebook title")
    create_parser.add_argument("--url", required=True, help="Source URL to add")

    # generate images
    gen_parser = subparsers.add_parser("generate", help="Generate and download infographics")
    gen_parser.add_argument("--notebook-id", required=True, help="Notebook ID")
    gen_parser.add_argument("--output-dir", required=True, help="Output directory")
    gen_parser.add_argument("--config", required=True, help="JSON config file with chart definitions")
    gen_parser.add_argument("--max-retries", type=int, default=5, help="Max retries per chart")
    gen_parser.add_argument("--max-wait", type=int, default=300, help="Max wait seconds")

    args = parser.parse_args()

    if args.command == "auth":
        asyncio.run(check_auth())
    elif args.command == "create":
        result = asyncio.run(create_notebook(args.title, args.url))
        print(json.dumps(result, indent=2))
    elif args.command == "generate":
        with open(args.config, "r", encoding="utf-8") as f:
            charts = json.load(f)
        asyncio.run(generate_and_download(
            args.notebook_id, charts, args.output_dir,
            max_retries=args.max_retries, max_wait=args.max_wait,
        ))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
