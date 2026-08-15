#!/bin/bash
# ======================================================================
# pywpsrpc RPC docx→pdf 转换入口
# 两种模式：
#   1) CLI 模式（默认）：docker run --rm -v $PWD:/data wps-docx2pdf [input.docx] [output.pdf]
#      默认：/data/input.docx → /data/output.pdf
#   2) API 模式：MODE=api docker run -p 8080:8080 wps-docx2pdf
#      启动常驻 HTTP 服务（POST /convert 上传 docx → PDF，GET /health）
#      可用环境变量：HOST / PORT / MAX_FILE_MB（详见 http_server.py）
# ======================================================================
set -e

MODE="${MODE:-cli}"
SRC="${1:-/data/input.docx}"
OUT="${2:-/data/output.pdf}"

# --- 虚拟显示 + 会话环境 ---
export DISPLAY=:99
export XDG_RUNTIME_DIR=/tmp/runtime-root
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"

# --- 清理上次运行的残留（容器 stop/start 场景，SIGKILL 不会自动清理）---
# 1) Xvfb 锁：残留会导致 Xvfb :99 启动失败 → WPS 无显示 → getApplication E_FAIL
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
# 2) RPC socket 残留：影响新 RPC 握手
rm -rf /root/.local/share/Kingsoft/daemon && mkdir -p /root/.local/share/Kingsoft/daemon
# 3) WPS 崩溃重启记录（RestartAppInfo）：残留会导致"幽灵 WPS 实例"用历史参数重启
sed -i '/RestartAppInfo=/d' /root/.config/Kingsoft/Office.conf 2>/dev/null || true

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

if [ "$MODE" = "api" ]; then
    echo "== pywpsrpc HTTP 服务启动 =="
    echo "   监听: ${HOST:-0.0.0.0}:${PORT:-8080}"
    echo "   接口: POST /convert  GET /health"
    # 常驻服务：异常退出时清理 Xvfb
    trap 'kill "$XPID" 2>/dev/null || true' EXIT
    exec python3 /opt/wpsrpc-rpc/http_server.py
fi

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
