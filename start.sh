#!/bin/bash
# Render 启动脚本：先启动 bgutil PO Token 服务器（若可用），再启动主应用
cd "$(dirname "$0")"

# 若部署时下载了静态 Node 并构建了 bgutil 服务器，则启动它（默认端口 4416，仅监听本机）
if [ -x "$PWD/node-runtime/bin/node" ] && [ -f "$PWD/bgutil/server/build/main.js" ]; then
  "$PWD/node-runtime/bin/node" "$PWD/bgutil/server/build/main.js" --port 4416 &
  echo "[start.sh] bgutil POT server started on :4416"
fi

exec python douyin_nowm_web.py
