#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "========================================================"
echo " QQ 大脑 + NapCat [Linux / WSL] 一体化整合启动"
echo "========================================================"
echo " [1/3] 正在启动 QQ 核心大脑业务服务..."
echo " [2/3] 正在启动 Web 调控后台: http://127.0.0.1:18888"
echo " [3/3] 正在唤起 Linux NapCat (xvfb-run 无头扫码客户端)..."
echo ""
echo " 提示:"
echo " - 终端若显示二维码，请使用手机 QQ 扫码；"
echo " - 扫码登录成功后，大脑会自动直接对接，无需再手动打开第二个窗口！"
echo " - 按 Ctrl+C 可同时停止 QQ 大脑与 NapCat 协议端。"
echo "========================================================"
echo ""

python3 run_qq.py --with-napcat "$@"
