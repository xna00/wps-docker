#!/bin/bash
# ======================================================================
# 字体统一安装脚本：镜像里所有字体相关操作都在这一个文件里
#   1) apt 安装开源字体包（思源黑体 CJK / 文泉驿 / Carlito·Caladea）
#   2) 下载并解压常用中文字体包 fonts.tar.xz（宋体/黑体/楷体/仿宋/方正/Times）
#   3) 用 fonttools 从 NotoSansCJK.ttc 抽 SC 字面 → 独立 NotoSansSC（思源黑体 SC）
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

echo "== [3/5] 生成 Noto Sans SC 独立字面（fonttools 抽 SC 面 + 改名）=="
# 文档（尤其 Google Docs 导出/网页模板）常指定 "Noto Sans SC"，而镜像只有
# "Noto Sans CJK SC"（.ttc 集合）。直接给 ttc 追加别名时 WPS 会退回 ttc 第 0 面(JP)；
# 独立单字面文件则无此问题，且家族名精确为 "Noto Sans SC"。
pip3 install --no-cache-dir --break-system-packages -i https://mirrors.aliyun.com/pypi/simple/ fonttools
python3 - <<'PY'
import os
from fontTools.ttLib import TTFont
from fontTools.ttLib.ttCollection import TTCollection

FONT_DIR = "/usr/share/fonts/opentype/noto"
FAMILY = "Noto Sans SC"

def extract_face(ttc_path, out_path, sub):
    coll = TTCollection(ttc_path)
    idx = None
    for i, f in enumerate(coll.fonts):
        if f["name"].getDebugName(1) == "Noto Sans CJK SC":
            idx = i
            break
    if idx is None:
        raise SystemExit(f"[noto-sc] ERROR: SC face not found in {ttc_path}")
    f = TTFont(ttc_path, fontNumber=idx)
    name = f["name"]
    for nid in (1, 4, 6, 16, 17):
        name.removeNames(nameID=nid)
    ps = f"NotoSansSC-{sub}"
    name.setName(FAMILY, 1, 3, 1, 0x409)
    name.setName(FAMILY, 1, 1, 0, 0)
    name.setName(f"{FAMILY} {sub}", 4, 3, 1, 0x409)
    name.setName(ps, 6, 3, 1, 0x409)
    name.setName(ps, 6, 1, 0, 0)
    name.setName(FAMILY, 16, 3, 1, 0x409)
    name.setName(sub, 17, 3, 1, 0x409)
    f.save(out_path)
    print(f"[noto-sc] {out_path} ({os.path.getsize(out_path)} bytes)")

for sub in ("Regular", "Bold"):
    ttc = os.path.join(FONT_DIR, f"NotoSansCJK-{sub}.ttc")
    out = os.path.join(FONT_DIR, f"NotoSansSC-{sub}.otf")
    if not os.path.exists(ttc):
        print(f"[noto-sc] skip: {ttc} not found")
        continue
    extract_face(ttc, out, sub)
PY
pip3 uninstall -y -q --break-system-packages fonttools >/dev/null 2>&1 || true

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
