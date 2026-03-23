#!/usr/bin/env python3
"""企业微信机器人 → CodeBuddy Code CLI 桥接服务。

通过企微群机器人接收消息，调用 codebuddy -p 执行小绿书生成/发布流程，
并将结果回传到企微群。

架构：
  企微群消息 → wecom-bot-svr 回调 → 解析意图 → codebuddy -p 执行 → 回传结果

依赖：
  pip3 install wecom-bot-svr requests

环境变量（或 .env 文件）：
  WECOM_BOT_NAME     - 企微机器人名称（必须与企微后台配置一致）
  WECOM_BOT_TOKEN    - 回调配置中的 Token
  WECOM_BOT_AES_KEY  - 回调配置中的 EncodingAESKey
  WECOM_BOT_CORP_ID  - 企业 ID
  WECOM_BOT_KEY      - 机器人 Webhook URL 中的 key（用于主动发消息/文件）
  WECOM_BOT_PORT     - 服务端口（默认 5001）
  CODEBUDDY_PROJECT  - 小绿书项目根目录（默认 ~/CodeBuddy/NotebookLM-skill）
"""

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

BOT_NAME = os.getenv("WECOM_BOT_NAME", "小绿书助手")
BOT_TOKEN = os.getenv("WECOM_BOT_TOKEN", "xxx")
BOT_AES_KEY = os.getenv("WECOM_BOT_AES_KEY", "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
BOT_CORP_ID = os.getenv("WECOM_BOT_CORP_ID", "")
BOT_KEY = os.getenv("WECOM_BOT_KEY", "")  # webhook key，用于主动发消息
BOT_PORT = int(os.getenv("WECOM_BOT_PORT", "5001"))

PROJECT_DIR = os.getenv(
    "CODEBUDDY_PROJECT",
    str(Path.home() / "CodeBuddy" / "NotebookLM-skill"),
)

# Webhook URL（用于主动发送消息，当被动回复不够用时）
WEBHOOK_URL = (
    f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={BOT_KEY}"
    if BOT_KEY
    else ""
)

# 任务会话存储（简单内存版，重启丢失）
# chat_id → { "stage": str, "task_id": str, "url": str, "project_dir": str, ... }
_sessions: dict[str, dict] = {}

# 任务历史记录（用于 dashboard 展示）
_task_history: list[dict] = []
# 最近日志（环形缓冲区）
_recent_logs: list[dict] = []
_MAX_LOGS = 200


def _log_event(chat_id: str, event: str, detail: str = ""):
    """记录一条事件到内存日志，供 dashboard 展示。"""
    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": time.time(),
        "chat_id": chat_id,
        "event": event,
        "detail": detail[:500],
    }
    _recent_logs.append(entry)
    if len(_recent_logs) > _MAX_LOGS:
        _recent_logs[:] = _recent_logs[-_MAX_LOGS:]


_server_start_time = time.time()

# ---------------------------------------------------------------------------
# CodeBuddy CLI 调用
# ---------------------------------------------------------------------------


def run_codebuddy(prompt: str, timeout: int = 600) -> str:
    """调用 codebuddy -p 非交互模式执行任务。

    Args:
        prompt: 要发送给 CodeBuddy 的自然语言指令。
        timeout: 超时秒数（默认 10 分钟，图片生成较慢）。

    Returns:
        CodeBuddy 的输出文本。
    """
    cmd = [
        "codebuddy",
        "-p",
        "--dangerously-skip-permissions",
        prompt,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=PROJECT_DIR,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            output += f"\n\n⚠️ stderr: {result.stderr.strip()}"
        return output or "(无输出)"
    except subprocess.TimeoutExpired:
        return "⏰ 任务超时（超过 10 分钟），请检查后重试"
    except Exception as e:
        return f"❌ 调用 CodeBuddy 失败: {e}"


# ---------------------------------------------------------------------------
# Webhook 主动发消息（支持文本、markdown、图片、文件）
# ---------------------------------------------------------------------------


def send_text(text: str):
    """通过 Webhook 主动发送文本消息。"""
    if not WEBHOOK_URL:
        print(f"[WARN] WECOM_BOT_KEY 未配置，无法主动发消息: {text[:100]}")
        return
    # 企微 Webhook 文本消息限 2048 字
    if len(text) > 2000:
        text = text[:1997] + "..."
    requests.post(
        WEBHOOK_URL,
        json={"msgtype": "text", "text": {"content": text}},
        timeout=10,
    )


def send_markdown(content: str):
    """通过 Webhook 主动发送 Markdown 消息。"""
    if not WEBHOOK_URL:
        return
    if len(content) > 4000:
        content = content[:3997] + "..."
    requests.post(
        WEBHOOK_URL,
        json={"msgtype": "markdown", "markdown": {"content": content}},
        timeout=10,
    )


def send_image(image_path: str):
    """通过 Webhook 发送图片（需 base64 + md5）。"""
    if not WEBHOOK_URL:
        return
    import base64
    import hashlib

    with open(image_path, "rb") as f:
        data = f.read()
    b64 = base64.b64encode(data).decode()
    md5 = hashlib.md5(data).hexdigest()
    requests.post(
        WEBHOOK_URL,
        json={"msgtype": "image", "image": {"base64": b64, "md5": md5}},
        timeout=30,
    )


# ---------------------------------------------------------------------------
# 意图解析
# ---------------------------------------------------------------------------

# URL 模式
URL_PATTERN = re.compile(r"https?://\S+")
YOUTUBE_PATTERN = re.compile(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+")


def parse_intent(text: str) -> dict:
    """从用户消息中解析意图。

    Returns:
        dict with keys: action, url, extra
        action: "generate" | "confirm_publish" | "modify" | "status" | "help" | "unknown"
    """
    text = text.strip()

    # 帮助
    if text in ("help", "帮助", "?", "？"):
        return {"action": "help"}

    # 确认发布
    if text in ("确认", "确认发布", "发布", "ok", "OK", "好", "发"):
        return {"action": "confirm_publish"}

    # 取消
    if text in ("取消", "算了", "不要了"):
        return {"action": "cancel"}

    # 查看状态
    if text in ("状态", "status"):
        return {"action": "status"}

    # 修改指令
    modify_prefixes = ("修改", "改", "重新生成", "标题改", "换个")
    for prefix in modify_prefixes:
        if text.startswith(prefix):
            return {"action": "modify", "extra": text}

    # 包含 URL → 生成任务
    urls = URL_PATTERN.findall(text)
    if urls:
        return {"action": "generate", "url": urls[0], "extra": text}

    # 纯文字观点 → 也可以生成
    if len(text) > 10:
        return {"action": "generate", "url": "", "extra": text}

    return {"action": "unknown"}


# ---------------------------------------------------------------------------
# 任务执行（异步，在后台线程运行）
# ---------------------------------------------------------------------------


def reply_via_response_url(response_url: str, content: str):
    """通过 response_url 主动回复 Markdown 消息（智能机器人专用，每个 URL 只能用一次）。"""
    import sys
    if not response_url:
        print(f"[WARN] 无 response_url，无法回复: {content[:100]}")
        sys.stdout.flush()
        return
    if len(content) > 20000:
        content = content[:19997] + "..."
    try:
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
        print(f"[REPLY] sending to {response_url[:80]} payload_len={len(json.dumps(payload))}")
        sys.stdout.flush()
        resp = requests.post(
            response_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        print(f"[REPLY] status={resp.status_code} body={resp.text[:200]}")
        sys.stdout.flush()
    except Exception as e:
        print(f"[REPLY] ERROR: {e}")
        import traceback; traceback.print_exc()
        sys.stdout.flush()


def _notify(chat_id: str, content: str):
    """向用户发送通知，优先用 response_url，回退到 Webhook。"""
    session = _sessions.get(chat_id, {})
    response_url = session.pop("response_url", "")
    if response_url:
        reply_via_response_url(response_url, content)
    elif WEBHOOK_URL:
        send_text(content)
    else:
        print(f"[NOTIFY] 无可用通道: {content[:200]}")


def _generate_task(chat_id: str, url: str, extra: str):
    """后台执行生成任务。"""
    session = _sessions.get(chat_id, {})
    session["stage"] = "generating"
    session["started_at"] = time.time()
    session["started_str"] = time.strftime("%Y-%m-%d %H:%M:%S")
    session["source_url"] = url
    session["source_extra"] = extra[:200]
    session["task_type"] = "generate"
    _sessions[chat_id] = session

    _log_event(chat_id, "task_start", f"生成任务启动 url={url[:100]}")

    # 构造 prompt
    is_youtube = bool(YOUTUBE_PATTERN.match(url)) if url else False

    if url and is_youtube:
        source_desc = f"YouTube 视频链接: {url}"
        fetch_method = "先通过 NotebookLM skill 获取视频转录内容"
    elif url:
        source_desc = f"文章链接: {url}"
        fetch_method = "使用 web_fetch 获取文章内容"
    else:
        source_desc = f"用户观点: {extra}"
        fetch_method = "直接使用用户提供的文字素材"

    prompt = f"""按照 xiaolvs-image-writer skill 的完整工作流程，为以下素材生成小绿书图文：

{source_desc}

要求：
1. {fetch_method}
2. 执行 Step 1（素材收集）→ Step 3（事实筛查）→ Step 4（生成图片）→ Step 5（生成文案 copywriting.txt）
3. 生成完成后，输出以下信息：
   - 项目子目录路径
   - 生成的图片文件列表
   - copywriting.txt 的完整内容
   - 是否可以进入发布流程

不要执行发布步骤（Step 7/8），等待用户确认后再发布。"""

    _log_event(chat_id, "codebuddy_start", "调用 codebuddy CLI 开始执行")
    output = run_codebuddy(prompt)
    _log_event(chat_id, "codebuddy_done", f"codebuddy 返回 {len(output)} 字符")

    # 解析输出，提取项目目录
    session["stage"] = "preview"
    session["finished_at"] = time.time()
    session["finished_str"] = time.strftime("%Y-%m-%d %H:%M:%S")
    session["duration"] = round(session["finished_at"] - session["started_at"], 1)
    session["codebuddy_output"] = output
    session["pending_result"] = (
        f"**小绿书图文生成完成**\n\n{output[:3500]}\n\n---\n"
        f"回复「确认」→ 自动发布\n"
        f"回复「修改 xxx」→ 修改后重新生成\n"
        f"回复「取消」→ 取消本次任务"
    )
    _sessions[chat_id] = session

    # 尝试提取项目目录
    _try_extract_project_dir(session)

    _log_event(chat_id, "task_done", f"生成完成 耗时={session['duration']}s")
    _task_history.append({
        "chat_id": chat_id, "type": "generate", "url": url[:100],
        "started": session.get("started_str"), "finished": session.get("finished_str"),
        "duration": session.get("duration"), "status": "done",
    })

    print(f"[TASK] generate done for chat={chat_id}, stage=preview")


def _try_extract_project_dir(session: dict):
    """从 CodeBuddy 输出中提取项目子目录。"""
    output = session.get("codebuddy_output", "")
    dir_match = re.search(r"(?:项目子目录|目录|路径)[：:]\s*(\S+)", output)
    if not dir_match:
        return
    project_subdir = dir_match.group(1)
    if not os.path.isabs(project_subdir):
        project_subdir = os.path.join(PROJECT_DIR, project_subdir)
    session["project_dir"] = project_subdir


def _publish_task(chat_id: str):
    """后台执行发布任务。"""
    session = _sessions.get(chat_id, {})
    session["stage"] = "publishing"
    session["task_type"] = "publish"
    session["started_at"] = time.time()
    session["started_str"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _sessions[chat_id] = session
    _log_event(chat_id, "task_start", "发布任务启动")

    project_dir = session.get("project_dir", "")

    prompt = f"""继续上一步的小绿书图文生成结果，现在执行发布：

1. 按照 xiaolvs-image-writer skill 的 Step 7 发布到小红书（使用 --auto 参数跳过确认）
2. 按照 Step 8 发布到微信公众号（使用 --auto 参数，合集选 AI前沿）

项目子目录: {project_dir}

使用 --auto 参数自动完成，不要暂停等待确认。"""

    output = run_codebuddy(prompt)

    session["stage"] = "done"
    session["finished_at"] = time.time()
    session["finished_str"] = time.strftime("%Y-%m-%d %H:%M:%S")
    session["duration"] = round(session["finished_at"] - session["started_at"], 1)
    session["pending_result"] = f"**发布完成**\n\n{output[:3500]}"
    _sessions[chat_id] = session

    _log_event(chat_id, "task_done", f"发布完成 耗时={session['duration']}s")
    _task_history.append({
        "chat_id": chat_id, "type": "publish",
        "started": session.get("started_str"), "finished": session.get("finished_str"),
        "duration": session.get("duration"), "status": "done",
    })
    print(f"[TASK] publish done for chat={chat_id}")


def _modify_task(chat_id: str, instruction: str):
    """后台执行修改任务。"""
    session = _sessions.get(chat_id, {})
    session["stage"] = "generating"
    session["task_type"] = "modify"
    session["started_at"] = time.time()
    session["started_str"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _sessions[chat_id] = session
    _log_event(chat_id, "task_start", f"修改任务启动: {instruction[:100]}")

    project_dir = session.get("project_dir", "")

    prompt = f"""继续上一步的小绿书图文，用户要求修改：

{instruction}

项目子目录: {project_dir}

修改后重新输出：项目子目录路径、图片文件列表、copywriting.txt 完整内容。"""

    output = run_codebuddy(prompt)

    session["stage"] = "preview"
    session["finished_at"] = time.time()
    session["finished_str"] = time.strftime("%Y-%m-%d %H:%M:%S")
    session["duration"] = round(session["finished_at"] - session["started_at"], 1)
    session["codebuddy_output"] = output
    session["pending_result"] = (
        f"**修改完成**\n\n{output[:3500]}\n\n---\n"
        f"回复「确认」发布，或继续「修改 xxx」"
    )
    _sessions[chat_id] = session
    _try_extract_project_dir(session)

    _log_event(chat_id, "task_done", f"修改完成 耗时={session['duration']}s")
    _task_history.append({
        "chat_id": chat_id, "type": "modify",
        "started": session.get("started_str"), "finished": session.get("finished_str"),
        "duration": session.get("duration"), "status": "done",
    })
    print(f"[TASK] modify done for chat={chat_id}")


# ---------------------------------------------------------------------------
# 消息处理
# ---------------------------------------------------------------------------

HELP_TEXT = """🌿 **小绿书助手使用指南**

**生成图文**：
• 发送文章链接 → 自动生成小绿书图文
• 发送 YouTube 链接 → 自动提取内容并生成
• 发送一段文字观点 → 基于观点生成

**交互确认**：
• 「确认」→ 发布到小红书 + 微信公众号
• 「修改 xxx」→ 修改后重新生成
• 「取消」→ 取消当前任务

**其他**：
• 「状态」→ 查看当前任务状态
• 「帮助」→ 显示本指南"""


def handle_message(text: str, chat_id: str = "default") -> str:
    """处理收到的消息，返回即时回复文本。

    后台任务完成后结果存在 pending_result 中，
    用户下次发消息时优先返回积攒的结果。
    """
    session = _sessions.get(chat_id, {})

    # 如果有待返回的结果（后台任务完成），优先返回
    pending = session.pop("pending_result", "")
    if pending:
        _sessions[chat_id] = session
        return pending

    intent = parse_intent(text)
    action = intent["action"]

    if action == "help":
        return HELP_TEXT

    if action == "status":
        session = _sessions.get(chat_id, {})
        stage = session.get("stage", "idle")
        stage_map = {
            "idle": "空闲，发送链接或文字开始",
            "generating": "正在生成图文，请稍候... 完成后 @我 任意消息即可获取结果",
            "preview": "等待确认/修改",
            "publishing": "正在发布...",
            "done": "上一个任务已完成，发送新链接开始下一个",
        }
        return stage_map.get(stage, f"状态: {stage}")

    if action == "cancel":
        _sessions.pop(chat_id, None)
        return "✅ 已取消当前任务"

    if action == "confirm_publish":
        session = _sessions.get(chat_id, {})
        if session.get("stage") != "preview":
            return "⚠️ 当前没有待发布的任务。先发送链接或文字生成图文吧"
        threading.Thread(
            target=_publish_task, args=(chat_id,), daemon=True
        ).start()
        return "收到！正在发布到小红书和微信公众号，请稍候...\n\n完成后 @我 发送任意消息即可获取结果。"

    if action == "modify":
        session = _sessions.get(chat_id, {})
        if session.get("stage") not in ("preview", "done"):
            return "⚠️ 当前没有可修改的任务"
        threading.Thread(
            target=_modify_task,
            args=(chat_id, intent.get("extra", text)),
            daemon=True,
        ).start()
        return "收到修改指令，正在处理...\n\n完成后 @我 发送任意消息即可获取结果。"

    if action == "generate":
        # 检查是否有正在进行的任务
        session = _sessions.get(chat_id, {})
        if session.get("stage") in ("generating", "publishing"):
            return "⚠️ 当前有任务正在执行，请等待完成后再发送新任务"

        url = intent.get("url", "")
        extra = intent.get("extra", text)
        _sessions[chat_id] = {"stage": "generating", "url": url}

        threading.Thread(
            target=_generate_task, args=(chat_id, url, extra), daemon=True
        ).start()
        return "收到！正在后台生成小绿书图文，预计需要 3-5 分钟。\n\n完成后 @我 发送任意消息即可获取结果。"

    return "🤔 没理解你的意思。发送「帮助」查看使用指南"


# ---------------------------------------------------------------------------
# 企微机器人回调服务
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dashboard HTML（嵌入式，无需额外文件）
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🌿 小绿书助手 - 任务监控</title>
<style>
  :root {
    --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a;
    --text: #e1e4eb; --dim: #8b8fa3; --green: #34d399;
    --yellow: #fbbf24; --blue: #60a5fa; --red: #f87171;
    --purple: #a78bfa;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg); color: var(--text); padding: 20px;
    min-height: 100vh;
  }
  .header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border);
  }
  .header h1 { font-size: 22px; font-weight: 600; }
  .header h1 span { color: var(--green); }
  .meta { color: var(--dim); font-size: 13px; }
  .meta .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: var(--green); margin-right: 6px; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }

  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }

  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 18px; transition: border-color 0.2s;
  }
  .card:hover { border-color: #3a3d4a; }
  .card h2 { font-size: 14px; color: var(--dim); text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 12px; }

  .stat-row { display: flex; gap: 20px; margin-bottom: 20px; }
  .stat {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 16px 20px; flex: 1; text-align: center;
  }
  .stat .value { font-size: 28px; font-weight: 700; }
  .stat .label { font-size: 12px; color: var(--dim); margin-top: 4px; }

  .badge {
    display: inline-block; padding: 3px 10px; border-radius: 20px;
    font-size: 12px; font-weight: 600;
  }
  .badge-idle { background: #1e293b; color: var(--dim); }
  .badge-generating { background: #422006; color: var(--yellow); animation: pulse 1.5s infinite; }
  .badge-publishing { background: #172554; color: var(--blue); animation: pulse 1.5s infinite; }
  .badge-preview { background: #14532d; color: var(--green); }
  .badge-done { background: #1e1b4b; color: var(--purple); }

  .session-card {
    background: #12141c; border: 1px solid var(--border); border-radius: 8px;
    padding: 14px; margin-bottom: 10px;
  }
  .session-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .session-id { font-weight: 600; font-size: 14px; }
  .session-detail { font-size: 13px; color: var(--dim); line-height: 1.6; }
  .session-detail a { color: var(--blue); text-decoration: none; }
  .elapsed { color: var(--yellow); font-weight: 600; font-size: 13px; }

  .log-container {
    background: #0a0c12; border: 1px solid var(--border); border-radius: 8px;
    padding: 12px; max-height: 350px; overflow-y: auto; font-family: 'SF Mono', Monaco, monospace;
    font-size: 12px; line-height: 1.7;
  }
  .log-entry { padding: 2px 0; border-bottom: 1px solid #151820; }
  .log-time { color: #4a5568; margin-right: 8px; }
  .log-event { font-weight: 600; margin-right: 6px; }
  .log-event-msg_received { color: var(--blue); }
  .log-event-task_start { color: var(--yellow); }
  .log-event-task_done { color: var(--green); }
  .log-event-codebuddy_start { color: var(--purple); }
  .log-event-codebuddy_done { color: var(--purple); }

  .history-table { width: 100%; font-size: 13px; border-collapse: collapse; }
  .history-table th { color: var(--dim); text-align: left; padding: 8px 6px;
    border-bottom: 1px solid var(--border); font-weight: 500; }
  .history-table td { padding: 8px 6px; border-bottom: 1px solid #151820; }
  .history-table tr:hover td { background: #151820; }

  .file-log { max-height: 300px; overflow-y: auto; }
  .file-log-line { font-size: 11px; color: var(--dim); line-height: 1.5;
    padding: 1px 0; white-space: pre-wrap; word-break: break-all; }
  .file-log-line:hover { color: var(--text); }

  .output-preview {
    max-height: 200px; overflow-y: auto; font-size: 12px; color: var(--dim);
    background: #0a0c12; padding: 10px; border-radius: 6px; margin-top: 8px;
    white-space: pre-wrap; word-break: break-all; line-height: 1.5;
  }

  .refresh-info { color: var(--dim); font-size: 12px; }
  #countdown { color: var(--green); font-weight: 600; }
</style>
</head>
<body>

<div class="header">
  <h1>🌿 <span>小绿书助手</span> 任务监控</h1>
  <div class="meta">
    <span class="dot"></span>
    <span id="serverTime">-</span>
    &nbsp;|&nbsp; 运行 <span id="uptime">-</span>
    &nbsp;|&nbsp; <span class="refresh-info">自动刷新 <span id="countdown">3</span>s</span>
  </div>
</div>

<div class="stat-row">
  <div class="stat">
    <div class="value" id="activeTasks" style="color:var(--yellow)">0</div>
    <div class="label">进行中任务</div>
  </div>
  <div class="stat">
    <div class="value" id="completedTasks" style="color:var(--green)">0</div>
    <div class="label">已完成任务</div>
  </div>
  <div class="stat">
    <div class="value" id="totalSessions" style="color:var(--blue)">0</div>
    <div class="label">活跃会话</div>
  </div>
  <div class="stat">
    <div class="value" id="codebuddyProcs" style="color:var(--purple)">0</div>
    <div class="label">CodeBuddy 进程</div>
  </div>
</div>

<div class="grid">
  <div class="card">
    <h2>📋 活跃会话</h2>
    <div id="sessionsContainer"><div style="color:var(--dim)">暂无会话</div></div>
  </div>
  <div class="card">
    <h2>📡 实时事件</h2>
    <div class="log-container" id="logsContainer"></div>
  </div>
</div>

<div class="grid">
  <div class="card">
    <h2>📜 任务历史</h2>
    <div id="historyContainer" style="max-height:300px;overflow-y:auto;"></div>
  </div>
  <div class="card">
    <h2>🔧 服务日志（最近 50 行）</h2>
    <div class="log-container file-log" id="fileLogContainer"></div>
  </div>
</div>

<script>
const REFRESH_INTERVAL = 3000;
let countdown = 3;

function formatDuration(sec) {
  if (!sec && sec !== 0) return '-';
  sec = parseFloat(sec);
  if (sec < 60) return sec.toFixed(1) + 's';
  const m = Math.floor(sec / 60), s = (sec % 60).toFixed(0);
  return m + 'm ' + s + 's';
}

function renderSessions(sessions) {
  const c = document.getElementById('sessionsContainer');
  const keys = Object.keys(sessions);
  if (!keys.length) { c.innerHTML = '<div style="color:var(--dim);padding:10px">暂无活跃会话</div>'; return; }
  c.innerHTML = keys.map(id => {
    const s = sessions[id];
    const badgeClass = 'badge-' + (s.stage || 'idle');
    let info = '';
    if (s.source_url) info += '<div>🔗 <a href="' + s.source_url + '" target="_blank">' + s.source_url.substring(0, 60) + '...</a></div>';
    if (s.source_extra && !s.source_url) info += '<div>💬 ' + s.source_extra.substring(0, 80) + '</div>';
    if (s.started) info += '<div>⏱ 开始: ' + s.started + '</div>';
    if (s.elapsed) info += '<div class="elapsed">⏳ 已运行: ' + s.elapsed + '</div>';
    if (s.finished) info += '<div>✅ 完成: ' + s.finished + ' (耗时 ' + formatDuration(s.duration) + ')</div>';
    if (s.project_dir) info += '<div>📁 ' + s.project_dir + '</div>';
    if (s.has_pending) info += '<div style="color:var(--green)">📬 有待推送结果</div>';
    let output = '';
    if (s.output_preview) output = '<div class="output-preview">' + escapeHtml(s.output_preview) + '</div>';
    return '<div class="session-card"><div class="session-header"><span class="session-id">' + id + '</span><span class="badge ' + badgeClass + '">' + (s.stage || 'idle') + '</span></div><div class="session-detail">' + info + '</div>' + output + '</div>';
  }).join('');
}

function renderLogs(logs) {
  const c = document.getElementById('logsContainer');
  if (!logs.length) { c.innerHTML = '<div style="color:var(--dim)">暂无事件</div>'; return; }
  c.innerHTML = logs.slice().reverse().map(l =>
    '<div class="log-entry"><span class="log-time">' + l.time.split(' ')[1] + '</span>' +
    '<span class="log-event log-event-' + l.event + '">[' + l.event + ']</span>' +
    '<span>' + escapeHtml(l.detail) + '</span></div>'
  ).join('');
}

function renderHistory(history) {
  const c = document.getElementById('historyContainer');
  if (!history.length) { c.innerHTML = '<div style="color:var(--dim);padding:10px">暂无历史记录</div>'; return; }
  c.innerHTML = '<table class="history-table"><thead><tr><th>会话</th><th>类型</th><th>开始</th><th>耗时</th><th>状态</th></tr></thead><tbody>' +
    history.slice().reverse().map(h =>
      '<tr><td>' + h.chat_id + '</td><td>' + h.type + '</td><td>' + (h.started || '-') + '</td><td>' + formatDuration(h.duration) + '</td><td><span class="badge badge-done">' + h.status + '</span></td></tr>'
    ).join('') + '</tbody></table>';
}

function renderFileLog(lines) {
  const c = document.getElementById('fileLogContainer');
  c.innerHTML = lines.slice(-50).map(l =>
    '<div class="file-log-line">' + escapeHtml(l) + '</div>'
  ).join('');
  c.scrollTop = c.scrollHeight;
}

function escapeHtml(t) {
  if (!t) return '';
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function refresh() {
  try {
    const [statusRes, logsRes] = await Promise.all([
      fetch('/api/status'), fetch('/api/logs')
    ]);
    const status = await statusRes.json();
    const logs = await logsRes.json();

    document.getElementById('serverTime').textContent = status.server_time;
    document.getElementById('uptime').textContent = formatDuration(status.uptime);

    const sessions = status.sessions || {};
    const active = Object.values(sessions).filter(s => ['generating','publishing'].includes(s.stage)).length;
    document.getElementById('activeTasks').textContent = active;
    document.getElementById('completedTasks').textContent = (status.task_history || []).length;
    document.getElementById('totalSessions').textContent = Object.keys(sessions).length;
    document.getElementById('codebuddyProcs').textContent = (status.codebuddy_processes || []).length;

    renderSessions(sessions);
    renderLogs(status.recent_logs || []);
    renderHistory(status.task_history || []);
    renderFileLog(logs.lines || []);
  } catch (e) {
    console.error('refresh error:', e);
  }
}

setInterval(() => {
  countdown--;
  if (countdown <= 0) { countdown = 3; refresh(); }
  document.getElementById('countdown').textContent = countdown;
}, 1000);

refresh();
</script>
</body>
</html>"""


def start_wecom_server():
    """启动企微机器人回调服务（使用 Flask + WXBizMsgCrypt 直接处理）。"""
    from flask import Flask, request as flask_request, make_response
    from wx_crypt.WXBizMsgCrypt3 import WXBizMsgCrypt
    import xml.etree.ElementTree as ET

    app = Flask(BOT_NAME)

    @app.before_request
    def log_all_requests():
        """记录所有到达 Flask 的请求。"""
        print(f"[REQUEST] {flask_request.method} {flask_request.url} "
              f"from={flask_request.remote_addr} "
              f"headers={dict(flask_request.headers)}")
        import sys; sys.stdout.flush()

    def _get_crypto():
        return WXBizMsgCrypt(BOT_TOKEN, BOT_AES_KEY, BOT_CORP_ID or "")

    @app.route("/wecom_bot", methods=["GET"])
    def verify():
        """处理企微回调 URL 验证（GET 请求）。"""
        msg_signature = flask_request.args.get("msg_signature", "")
        timestamp = flask_request.args.get("timestamp", "")
        nonce = flask_request.args.get("nonce", "")
        echostr = flask_request.args.get("echostr", "")

        print(f"[VERIFY] msg_signature={msg_signature} timestamp={timestamp} nonce={nonce}")

        try:
            wx_cpt = _get_crypto()
            ret, reply_echostr = wx_cpt.VerifyURL(msg_signature, timestamp, nonce, echostr)
            if ret == 0:
                print(f"[VERIFY] SUCCESS, echostr={reply_echostr}")
                # 返回解密后的 echostr 明文
                response = make_response(reply_echostr)
                response.content_type = "text/plain"
                return response
            else:
                print(f"[VERIFY] FAILED, ret={ret}")
                return make_response(f"verify failed: {ret}", 403)
        except Exception as e:
            print(f"[VERIFY] ERROR: {e}")
            return make_response(f"error: {e}", 500)

    @app.route("/wecom_bot", methods=["POST"])
    def receive_message():
        """处理企微智能机器人消息回调（POST 请求，JSON 格式）。"""
        import urllib.parse

        msg_signature = flask_request.args.get("msg_signature", "")
        timestamp = flask_request.args.get("timestamp", "")
        nonce = flask_request.args.get("nonce", "")
        raw_data = flask_request.get_data(as_text=True)

        # 智能机器人回调需要先 URL Decode
        decoded_data = urllib.parse.unquote(raw_data)

        print(f"[POST] msg_signature={msg_signature} data_len={len(raw_data)}")
        print(f"[POST] decoded_data={decoded_data[:500]}")

        try:
            wx_cpt = _get_crypto()

            # 尝试 JSON 格式解密（智能机器人）
            try:
                json_body = json.loads(decoded_data)
                encrypt_str = json_body.get("encrypt", "")
            except (json.JSONDecodeError, TypeError):
                encrypt_str = ""

            if encrypt_str:
                # 智能机器人 JSON 格式：手动验签 + 解密
                from wx_crypt.WXBizMsgCrypt3 import SHA1
                sha1 = SHA1()
                ret, signature = sha1.getSHA1(BOT_TOKEN, timestamp, nonce, encrypt_str)
                if ret != 0 or signature != msg_signature:
                    print(f"[POST] signature mismatch: computed={signature} expected={msg_signature}")
                    return make_response("signature mismatch", 403)

                # AES 解密
                from wx_crypt.WXBizMsgCrypt3 import Prpcrypt
                pc = Prpcrypt(wx_cpt.key)
                ret, decrypted = pc.decrypt(encrypt_str, "")
                if ret != 0:
                    print(f"[POST] decrypt failed, ret={ret}")
                    return make_response("decrypt failed", 403)

                print(f"[POST] decrypted={decrypted[:500]}")

                # 解密后是 JSON
                msg_data = json.loads(decrypted)
            else:
                # 回退：尝试 XML 格式（兼容旧协议）
                ret, xml_content = wx_cpt.DecryptMsg(
                    decoded_data.encode("utf-8"), msg_signature, timestamp, nonce
                )
                if ret != 0:
                    print(f"[POST] XML decrypt failed, ret={ret}")
                    return make_response("decrypt failed", 403)
                import xml.etree.ElementTree as ET
                root = ET.fromstring(xml_content)
                msg_data = {
                    "msgtype": root.findtext("MsgType", ""),
                    "text": {"content": root.findtext("Content", "")},
                    "chatid": root.findtext("ChatId", "default"),
                }

            # 提取消息字段
            msg_type = msg_data.get("msgtype", "")
            chat_type = msg_data.get("chattype", "single")  # single 或 group
            from_user = msg_data.get("from", {}).get("userid", "unknown")
            msg_id = msg_data.get("msgid", "")
            response_url = msg_data.get("response_url", "")

            # session key：群聊用 chatid，私聊用 userid
            if chat_type == "group":
                chat_id = msg_data.get("chatid", "") or f"user_{from_user}"
            else:
                # 私聊没有 chatid，用 userid 作为 session key
                chat_id = f"user_{from_user}"

            # 消息排重：同一 msgid 不重复处理
            if msg_id and hasattr(receive_message, '_seen_msgs'):
                if msg_id in receive_message._seen_msgs:
                    print(f"[POST] SKIP duplicate msgid={msg_id}")
                    return make_response("", 200)
                receive_message._seen_msgs.add(msg_id)
                # 只保留最近 500 条
                if len(receive_message._seen_msgs) > 500:
                    receive_message._seen_msgs = set(list(receive_message._seen_msgs)[-200:])
            elif msg_id:
                receive_message._seen_msgs = {msg_id}

            # 提取文本内容
            content = ""
            if msg_type == "text":
                content = msg_data.get("text", {}).get("content", "")
            elif msg_type == "mixed":
                # 图文混排：提取所有文本部分
                items = msg_data.get("mixed", {}).get("msg_item", [])
                content = " ".join(
                    item.get("text", {}).get("content", "")
                    for item in items if item.get("msgtype") == "text"
                )

            # 去掉 @机器人名字 的前缀（只去 @xxx 部分，保留后面的内容）
            content = re.sub(r"@\S+\s*", "", content).strip()

            print(f"[POST] type={msg_type} chattype={chat_type} chat={chat_id} user={from_user} content={content[:100]}")
            print(f"[POST] msgid={msg_id} response_url={response_url[:100] if response_url else 'NONE'}")
            _log_event(chat_id, "msg_received", f"[{chat_type}] {from_user}: {content[:100]}")
            import sys; sys.stdout.flush()

            # 保存 response_url 到 session 供后台任务使用
            session = _sessions.get(chat_id, {})
            if response_url:
                session["response_url"] = response_url
                _sessions[chat_id] = session

            if msg_type == "event":
                event_type = msg_data.get("event", {}).get("eventtype", "")
                if event_type == "enter_chat" and response_url:
                    reply_text = "🌿 小绿书助手已上线！发送文章链接或「帮助」开始"
                elif event_type == "enter_chat":
                    # enter_chat 没有 response_url 时无法回复，静默处理
                    print(f"[POST] enter_chat event without response_url, skip reply")
                    return make_response("", 200)
                else:
                    reply_text = "🌿 小绿书助手已上线！发送文章链接或「帮助」开始"
            elif content:
                reply_text = handle_message(content, chat_id)
            else:
                reply_text = "🤔 暂不支持此消息类型，请发送文字或链接"

            # 通过 response_url 回复（智能机器人推荐方式）
            if response_url:
                reply_via_response_url(response_url, reply_text)
                return make_response("", 200)

            # 兼容：被动回复 XML 格式
            reply_xml = f"""<xml>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[{reply_text}]]></Content>
</xml>"""
            ret, encrypted_reply = wx_cpt.EncryptMsg(reply_xml, nonce, timestamp)
            if ret == 0:
                response = make_response(encrypted_reply)
                response.content_type = "application/xml"
                return response
            return make_response("", 200)

        except Exception as e:
            print(f"[POST] ERROR: {e}")
            import traceback
            traceback.print_exc()
            return make_response("", 200)

    # ----- Dashboard API -----

    @app.route("/api/status", methods=["GET"])
    def api_status():
        """返回所有 session 状态和日志（JSON）。"""
        from flask import jsonify
        sessions_out = {}
        for cid, s in _sessions.items():
            elapsed = ""
            if s.get("stage") in ("generating", "publishing") and s.get("started_at"):
                elapsed = f"{round(time.time() - s['started_at'], 1)}s"
            sessions_out[cid] = {
                "stage": s.get("stage", "idle"),
                "task_type": s.get("task_type", ""),
                "source_url": s.get("source_url", ""),
                "source_extra": s.get("source_extra", ""),
                "started": s.get("started_str", ""),
                "finished": s.get("finished_str", ""),
                "duration": s.get("duration", ""),
                "elapsed": elapsed,
                "project_dir": s.get("project_dir", ""),
                "has_pending": bool(s.get("pending_result")),
                "output_preview": (s.get("codebuddy_output") or "")[:800],
            }
        # 检查 codebuddy 子进程是否在运行
        try:
            import subprocess as _sp
            result = _sp.run(
                ["pgrep", "-f", "codebuddy.*-p.*--dangerously"],
                capture_output=True, text=True, timeout=3,
            )
            codebuddy_pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        except Exception:
            codebuddy_pids = []

        return jsonify({
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "uptime": round(time.time() - _server_start_time, 1),
            "sessions": sessions_out,
            "task_history": _task_history[-20:],
            "recent_logs": _recent_logs[-50:],
            "codebuddy_processes": codebuddy_pids,
        })

    @app.route("/api/logs", methods=["GET"])
    def api_logs():
        """返回服务日志文件最新内容。"""
        from flask import jsonify
        try:
            with open("/tmp/wecom_bot.log", "r") as f:
                lines = f.readlines()
                return jsonify({"lines": [l.rstrip() for l in lines[-100:]]})
        except Exception as e:
            return jsonify({"lines": [f"Error reading log: {e}"]})

    @app.route("/dashboard")
    def dashboard():
        """任务实时监控面板。"""
        return DASHBOARD_HTML

    # catch-all 路由放在所有具体路由之后，捕获未匹配的请求
    @app.route("/", defaults={"catch_all": ""}, methods=["GET", "POST"])
    @app.route("/<path:catch_all>", methods=["GET", "POST"])
    def root_catch(catch_all):
        """捕获所有非 /wecom_bot 路径的请求（调试用）。"""
        import sys
        print(f"[CATCH-ALL] path=/{catch_all} method={flask_request.method} args={dict(flask_request.args)}")
        if flask_request.method == "POST":
            body = flask_request.get_data(as_text=True)[:500]
            print(f"[CATCH-ALL] body={body}")
            # 如果包含 encrypt 字段，尝试当作智能机器人消息处理
            if "encrypt" in body.lower():
                print(f"[CATCH-ALL] detected encrypt field, forwarding to receive_message handler")
                sys.stdout.flush()
                return receive_message()
        sys.stdout.flush()
        return "OK", 200

    print(f"\n{'='*60}")
    print(f"🌿 小绿书助手企微机器人服务已启动")
    print(f"   监听端口: {BOT_PORT}")
    print(f"   回调地址: http://0.0.0.0:{BOT_PORT}/wecom_bot")
    print(f"   项目目录: {PROJECT_DIR}")
    print(f"{'='*60}\n")

    app.run(host="0.0.0.0", port=BOT_PORT)


# ---------------------------------------------------------------------------
# 独立测试模式（不依赖企微，本地命令行测试）
# ---------------------------------------------------------------------------


def start_local_test():
    """本地命令行测试模式，模拟消息收发。"""
    print(f"\n{'='*60}")
    print("🌿 小绿书助手 - 本地测试模式")
    print(f"   项目目录: {PROJECT_DIR}")
    print("   输入消息模拟企微交互，输入 quit 退出")
    print(f"{'='*60}\n")

    while True:
        try:
            text = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if text.lower() in ("quit", "exit", "q"):
            break
        if not text:
            continue

        reply = handle_message(text, "local_test")
        print(f"🤖> {reply}\n")

        # 等待后台任务的消息（如果有 Webhook 就通过 Webhook 发，没有就轮询打印）
        if not WEBHOOK_URL:
            time.sleep(1)
            session = _sessions.get("local_test", {})
            if session.get("stage") in ("generating", "publishing"):
                print("   (后台任务执行中，结果将在完成后输出...)\n")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        start_local_test()
    else:
        start_wecom_server()
