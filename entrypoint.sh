#!/bin/bash
# ======================================================================
# pywpsrpc RPC docx→pdf 转换入口
# 用法：docker run --rm -v $PWD:/data wps-docx2pdf [input.docx] [output.pdf]
#   默认：/data/input.docx → /data/output.pdf
# ======================================================================
set -e

SRC="${1:-/data/input.docx}"
OUT="${2:-/data/output.pdf}"

# --- 虚拟显示 + 会话环境 ---
export DISPLAY=:99
export XDG_RUNTIME_DIR=/tmp/runtime-root
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"

# dbus session bus
eval "$(dbus-launch --sh-syntax)"

# Xvfb 虚拟显示
Xvfb :99 -screen 0 1280x800x24 -ac +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
XPID=$!
sleep 2

# 窗口管理器（WPS 稳定运行需要）
fluxbox >/tmp/fluxbox.log 2>&1 &
sleep 1

# --- 环境修复（沙箱 seccomp / 无桌面环境的针对性修复）---
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LD_PRELOAD="/opt/wpsrpc-fix/libexitfix.so:/opt/wpsrpc-fix/libmqsim2.so"

echo "== pywpsrpc RPC 转换开始 =="
echo "   输入: $SRC"
echo "   输出: $OUT"

# 用 if 包裹：python3 失败时不被 set -e 中断，保证走到清理/退出码打印
if python3 /opt/wpsrpc-rpc/convert_docx2pdf.py "$SRC" "$OUT"; then
    RC=0
else
    RC=$?
fi

kill "$XPID" 2>/dev/null || true
echo "== 转换结束 (exit=$RC) =="
exit $RC
