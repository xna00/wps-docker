#!/bin/bash
# ======================================================================
# 字体统一安装脚本：镜像里所有字体相关操作都在这一个文件里
#   1) apt 安装开源字体包（思源黑体 CJK / 文泉驿 / Carlito·Caladea）
#   2) 下载并解压常用中文字体包 fonts.tar.xz（宋体/黑体/楷体/仿宋/方正/Times）
#   3) 下载 Google Fonts 官方 Noto Sans SC 静态 TTF（TrueType，家族名即 "Noto Sans SC"）
#   4) 写入缺失字体名别名 conf（微软雅黑/等线/Noto Sans/Calibri...→ 已有字体）
#   5) fc-cache 刷新并打印注册结果
#
# 用法：bash fonts_setup.sh [FONTS_URL]
#   FONTS_URL: fonts.tar.xz 下载地址（默认 GitHub Release 附件，可用加速镜像覆盖）
# 依赖：curl / python3 / pip3 / fontconfig（运行时基础镜像已安装）
# ======================================================================
set -euo pipefail

FONTS_URL="${1:-https://github.com/xna00/wps-docker/releases/download/wps-11.1.0.9662/fonts.tar.xz}"

echo "== [1/5] apt 安装开源字体包 =="
apt-get update -qq
apt-get install -y --no-install-recommends \
    fonts-wqy-zenhei fonts-wqy-microhei fonts-noto-cjk \
    fonts-crosextra-carlito fonts-crosextra-caladea

echo "== [2/5] 下载并解压常用中文字体包 fonts.tar.xz =="
# 来源: https://github.com/DoveOutland/Common-Chinese-office-fonts-font-library-
# 打包为 fonts.tar.xz 随仓库 Release 分发（~57MB，解压后 ~136MB）
# 字体仅供非商业用途，商用需自行授权（详见该仓库 README）
curl -fL --retry 5 --retry-delay 5 -C - -o /tmp/fonts.tar.xz "${FONTS_URL}"
mkdir -p /usr/share/fonts/wps-office
python3 -c "import tarfile; tarfile.open('/tmp/fonts.tar.xz').extractall('/usr/share/fonts/wps-office/')"
rm -f /tmp/fonts.tar.xz

echo "== [3/5] 下载 Google Fonts 官方 Noto Sans SC TTF =="
# 文档（尤其 Google Docs 导出/网页模板）常指定 "Noto Sans SC"，而镜像里只有
# "Noto Sans CJK SC"（CFF .ttc，WPS 对 CFF 整字嵌入 → PDF 膨胀到 ~28MB）。
# 官方 Google Fonts 静态 TTF 是 TrueType(glyf)：家族名天生就是 "Noto Sans SC"，
# 无需转换/改名，且 WPS 对 TrueType 正常子集化（实测 13 页 PPT 输出仅 1.4MB）。
# 注：fonts.gstatic.com 国内可直接访问；CSS 解析用 fonts.googleapis.com，失败回退 loli 镜像。
FONT_DIR=/usr/share/fonts/opentype/noto
mkdir -p "$FONT_DIR"
for WEIGHT_SPEC in "" ":wght@700"; do
  if [ "$WEIGHT_SPEC" = ":wght@700" ]; then SUB=Bold; else SUB=Regular; fi
  # 1) 解析 CSS 拿 ttf 直链（默认 curl UA 返回 ttf 而非 woff2）
  CSS=""
  for API in "https://fonts.googleapis.com/css2?family=Noto+Sans+SC${WEIGHT_SPEC}" \
             "https://fonts.loli.net/css2?family=Noto+Sans+SC${WEIGHT_SPEC}"; do
    CSS=$(curl -fsSL --max-time 30 "$API" 2>/dev/null || true)
    [ -n "$CSS" ] && break
  done
  URL=$(echo "$CSS" | grep -oE "https://[^)]*\.ttf" | head -1)
  # 2) 域名统一换回 fonts.gstatic.com（国内直连可用，CI 同样可达）
  URL=$(echo "$URL" | sed 's|^https://gstatic\.loli\.net|https://fonts.gstatic.com|')
  if [ -z "$URL" ]; then
    echo "[noto-sc] ERROR: 无法解析 Noto Sans SC TTF 直链（CSS API 不可达）"
    exit 1
  fi
  # 3) 下载（weight 400 → Regular，700 → Bold）
  curl -fL --retry 5 --retry-delay 3 -C - -o "$FONT_DIR/NotoSansSC-$SUB.ttf" "$URL"
  echo "[noto-sc] $FONT_DIR/NotoSansSC-$SUB.ttf ($(stat -c%s "$FONT_DIR/NotoSansSC-$SUB.ttf") bytes)"
done

echo "== [4/5] 写入缺失字体名别名 conf =="
# 为什么用 scan 而不是 pattern 别名：WPS 不认 pattern 别名（查询时重定向），
# 只认字体枚举（fc-list）里真实出现的家族名；scan 阶段 append 让目标字体多一个
# 家族名，WPS 枚举时即可见。目标均为独立单字面字体，无 ttc 第 0 面问题。
# 名单（8 个）：
#   中文高频：微软雅黑 / Microsoft YaHei / Microsoft YaHei UI / 等线 / DengXian
#              → 思源黑体 SC（微软雅黑为微软专有，版权原因不随镜像分发，用同设计映射）
#   Noto 系： Noto Sans（纯西文名）→ 思源黑体 SC
#   Office 西文默认：Calibri → Carlito、Cambria → Caladea（度量兼容 OFL 替代，apt 安装）
cat > /etc/fonts/conf.d/99-font-aliases.conf <<'XML'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <!-- 常见缺失字体名 → 思源黑体 SC -->
  <match target="scan">
    <test name="family"><string>Noto Sans SC</string></test>
    <edit name="family" mode="append" binding="strong"><string>微软雅黑</string></edit>
    <edit name="family" mode="append" binding="strong"><string>Microsoft YaHei</string></edit>
    <edit name="family" mode="append" binding="strong"><string>Microsoft YaHei UI</string></edit>
    <edit name="family" mode="append" binding="strong"><string>Noto Sans</string></edit>
    <edit name="family" mode="append" binding="strong"><string>等线</string></edit>
    <edit name="family" mode="append" binding="strong"><string>DengXian</string></edit>
  </match>
  <!-- Calibri / Cambria → 度量兼容的 OFL 替代字体（LibreOffice 同款方案） -->
  <match target="scan">
    <test name="family"><string>Carlito</string></test>
    <edit name="family" mode="append" binding="strong"><string>Calibri</string></edit>
  </match>
  <match target="scan">
    <test name="family"><string>Caladea</string></test>
    <edit name="family" mode="append" binding="strong"><string>Cambria</string></edit>
  </match>
</fontconfig>
XML

echo "== [5/5] fc-cache 刷新 + 结果统计 =="
fc-cache -f >/dev/null 2>&1
F1=$(fc-list | grep -cE "宋体|黑体|楷体|仿宋|Times" || true)
F2=$(fc-list | grep -cE "NotoSansSC-|Carlito-|Caladea-" || true)
YAHEI=$(fc-match "微软雅黑" | cut -d: -f1 || true)
CALIBRI=$(fc-match "Calibri" | cut -d: -f1 || true)
echo "  fonts.tar.xz 字体族数: $F1"
echo "  Noto Sans SC/Carlito/Caladea 注册数: $F2"
echo "  别名验证: 微软雅黑->$YAHEI, Calibri->$CALIBRI"
echo "PASS: 字体安装完成"
apt-get clean && rm -rf /var/lib/apt/lists/*
