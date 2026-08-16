#!/bin/bash
# ======================================================================
# pywpsrpc RPC 文档→PDF 转换入口
# 两种模式：
#   1) CLI 模式（默认）：docker run --rm -v $PWD:/data wps-docx2pdf [input] [output.pdf]
#      默认：/data/input.docx → /data/output.pdf
#      按扩展名自动路由到对应组件（wps/wpp/et），支持 Word/PPT/Excel/WPS/ODF/txt/csv/html 等
#   2) API 模式：MODE=api docker run -p 8080:8080 wps-docx2pdf
#      启动常驻 HTTP 服务（POST /convert 上传文档 → PDF，GET /health）
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
# 只 preload libmqsim2（seccomp 拦 mq_open 的环境修复）
# 注：libexitfix（1.1.0 的启动器退出码补丁）在 pywpsrpc 2.4.0 下已不需要（实测 9662/12 均无需），
#     镜像内仍保留该文件便于回退验证，但不再 preload。
export LD_PRELOAD="/opt/wpsrpc-fix/libmqsim2.so"

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
# entrypoint 层兜底超时（默认 300s）：python 内部另有 180s 主超时（子进程强杀 + 友好报错，
# 见 convert_docx2pdf.py CONVERT_TIMEOUT），这里只在内部机制失效时兜底，避免 CLI 无限等待。
# 可用 CONVERT_TIMEOUT 环境变量覆盖；超时后转明确失败码
CONVERT_TIMEOUT="${CONVERT_TIMEOUT:-300}"
if timeout --signal=TERM --kill-after=10 "$CONVERT_TIMEOUT" python3 /opt/wpsrpc-rpc/convert_docx2pdf.py "$SRC" "$OUT"; then
    RC=0
else
    RC=$?
    if [ "$RC" -eq 124 ]; then
        echo "== 转换超时（${CONVERT_TIMEOUT}s）已被终止 ==" >&2
        RC=1
    fi
fi

kill "$XPID" 2>/dev/null || true
echo "== 转换结束 (exit=$RC) =="
exit $RC
