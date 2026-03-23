#!/bin/bash
# 使用 localhost.run SSH 隧道暴露本地服务到公网
# cloudflared 的 trycloudflare.com 临时域名会导致企微私聊消息推送失败
# localhost.run 可以正常工作

PORT="${WECOM_BOT_PORT:-5001}"

echo "Starting localhost.run tunnel for port $PORT ..."
echo "Tunnel URL will be printed below (*.lhr.life domain)"
echo "请将输出的 https://xxx.lhr.life 地址 + /wecom_bot 配置到企微后台回调URL"
echo ""

ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=60 -o ServerAliveCountMax=3 \
    -R 80:localhost:$PORT nokey@localhost.run 2>&1
