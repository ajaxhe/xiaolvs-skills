#!/bin/bash
# 小绿书助手企微机器人启动脚本

# 从项目根目录的 .env 文件加载环境变量（如果存在）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$SKILL_DIR/.env"

if [ -f "$ENV_FILE" ]; then
  echo "📄 从 $ENV_FILE 加载环境变量..."
  set -a
  source "$ENV_FILE"
  set +a
else
  echo "⚠️  未找到 $ENV_FILE，请确保以下环境变量已设置："
  echo "   WECOM_BOT_TOKEN, WECOM_BOT_AES_KEY, WECOM_BOT_PORT"
  echo ""
  echo "   创建方法: cp $SKILL_DIR/.env.example $ENV_FILE && 编辑填入实际凭证"
fi

# 设置默认值（仅当环境变量未设置时）
export WECOM_BOT_NAME="${WECOM_BOT_NAME:-小绿书助手}"
export WECOM_BOT_PORT="${WECOM_BOT_PORT:-5001}"
export WECOM_BOT_KEY="${WECOM_BOT_KEY:-}"
export CODEBUDDY_PROJECT="${CODEBUDDY_PROJECT:-/Users/ajaxhe/CodeBuddy/NotebookLM-skill}"

# 检查必要凭证
if [ -z "$WECOM_BOT_TOKEN" ] || [ -z "$WECOM_BOT_AES_KEY" ]; then
  echo "❌ 错误：WECOM_BOT_TOKEN 和 WECOM_BOT_AES_KEY 必须设置"
  exit 1
fi

cd "$CODEBUDDY_PROJECT"
PYTHONUNBUFFERED=1 python3 -u .codebuddy/skills/xiaolvs-image-writer/scripts/wecom_bot_service.py
