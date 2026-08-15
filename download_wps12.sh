#!/bin/bash
# ======================================================================
# 下载 WPS Office 12.1.2.28080 for Linux deb（官网签名 URL）
#
# 官网 linux.wps.cn 的下载按钮走 downLoad(url) 前端函数，真实直链需带签名：
#   t = 当前 Unix 秒
#   k = MD5(secrityKey + uri + t)     # uri = url 的 pathname 部分
#   url = base + "?t=" + t + "&k=" + k
# 签名参数（secrityKey、BASE、URI）来自 linux.wps.cn 页面 HTML 源码。
#
# 注意：官网升级 WPS 12 版本后，需同步更新下方 BASE/URI/KEY（从
#       https://linux.wps.cn/ 页面源码提取新版直链与 key）。
#
# 用法: download_wps12.sh [输出路径]   # 默认 /tmp/wps12.deb
# ======================================================================
set -euo pipefail

OUT="${1:-/tmp/wps12.deb}"

BASE="https://wps-linux-personal.wpscdn.cn/wps/download/ep/Linux2023/28080/wps-office_12.1.2.28080.AK.preread.sw.Personal_765474_amd64.deb"
URI="/wps/download/ep/Linux2023/28080/wps-office_12.1.2.28080.AK.preread.sw.Personal_765474_amd64.deb"
KEY="7f8faaaa468174dc1c9cd62e5f218a5b"

T=$(date +%s)
K=$(printf '%s%s%s' "$KEY" "$URI" "$T" | md5sum | awk '{print $1}')
URL="${BASE}?t=${T}&k=${K}"

echo "[download_wps12] 下载 WPS 12 deb -> $OUT"
curl -fL --retry 5 --retry-delay 3 -C - -o "$OUT" "$URL"
echo "[download_wps12] 完成: $(stat -c %s "$OUT") bytes"
