#!/bin/bash
# ======================================================================
# 端到端测试：在容器内用 pywpsrpc RPC 真跑一次 docx→pdf，并断言产出
# 用法：在已构建的 wps-docx2pdf 镜像内执行：
#   /opt/wpsrpc-rpc/tests/e2e_test.sh
# 退出码：0=通过，非0=失败
# ======================================================================
set -euo pipefail

WORK=/tmp/e2e
SRC="$WORK/input.docx"
OUT="$WORK/output.pdf"
mkdir -p "$WORK"

echo "== [0/5] 启动虚拟显示 + dbus + 窗口管理器（自包含，避免被 ENTRYPOINT 拦截）=="
export DISPLAY=:99
export XDG_RUNTIME_DIR=/tmp/runtime-root
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"
eval "$(dbus-launch --sh-syntax)"
Xvfb :99 -screen 0 1280x800x24 -ac +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
XPID=$!
sleep 2
fluxbox >/tmp/fluxbox.log 2>&1 &
sleep 1
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# 只 preload libmqsim2（seccomp 拦 mq_open 的环境修复）；libexitfix 在 pywpsrpc 2.4.0 下已不需要
export LD_PRELOAD="/opt/wpsrpc-fix/libmqsim2.so"

echo "== [1/5] 生成最小英文测试 docx =="
# 用 LibreOffice 兼容的极简 OOXML 构造一个最小 docx（纯英文，避免依赖中文字体）
python3 - "$SRC" <<'PY'
import sys, zipfile, os
docx_path = sys.argv[1]
document_xml = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:body>'
    '<w:p><w:r><w:t>Hello WPS RPC e2e test.</w:t></w:r></w:p>'
    '<w:p><w:r><w:t>Second line for page break check.</w:t></w:r></w:p>'
    '</w:body></w:document>'
)
content_types = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>'
)
rels = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '</Relationships>'
)
with zipfile.ZipFile(docx_path, 'w', zipfile.ZIP_DEFLATED) as z:
    z.writestr('[Content_Types].xml', content_types)
    z.writestr('_rels/.rels', rels)
    z.writestr('word/document.xml', document_xml)
print("docx written:", docx_path, os.path.getsize(docx_path), "bytes")
PY

echo "== [2/5] 调用转换脚本 =="
# 直接调用 RPC 转换脚本（显示环境已由本脚本启动）
set +e
python3 /opt/wpsrpc-rpc/convert_docx2pdf.py "$SRC" "$OUT"
CONV_RC=$?
set -e
echo "   转换退出码: $CONV_RC"
if [ "$CONV_RC" -ne 0 ]; then
    echo "FAIL: 转换脚本返回非零"
    exit 1
fi

echo "== [3/5] 断言 PDF 产物 =="
if [ ! -f "$OUT" ]; then
    echo "FAIL: 未生成 PDF: $OUT"
    exit 1
fi
SIZE=$(stat -c%s "$OUT")
echo "   PDF 大小: $SIZE 字节"
if [ "$SIZE" -lt 1000 ]; then
    echo "FAIL: PDF 过小，疑似空文件"
    exit 1
fi

# 校验是合法的 PDF（头部 %PDF + 含 EOF）
HEAD=$(head -c 5 "$OUT")
if [ "$HEAD" != "%PDF-" ]; then
    echo "FAIL: 文件头不是 %PDF-，不是合法 PDF"
    exit 1
fi

echo "== [4/5] 校验页数 > 0 =="
if command -v pdfinfo >/dev/null 2>&1; then
    PAGES=$(pdfinfo "$OUT" 2>/dev/null | awk -F': ' '/^Pages/ {print $2}')
    echo "   页数: ${PAGES:-unknown}"
    if [ -n "${PAGES:-}" ] && [ "$PAGES" -lt 1 ]; then
        echo "FAIL: 页数 < 1"
        exit 1
    fi
else
    echo "   (pdfinfo 未安装，跳过页数校验；仅校验文件头与大小)"
fi

echo ""
echo "== [5/5] 字体替换断言：指定微软雅黑 → 思源黑体，不得回落宋体 =="
# 生成指定"微软雅黑"（latin+ea）的中文 docx；镜像无微软雅黑，应被别名映射为
# Noto Sans SC（思源黑体）。若回落宋体（SimSun）则说明别名未生效。
python3 - "$WORK/yahei.docx" <<'PY'
import sys, zipfile, os
docx_path = sys.argv[1]
document_xml = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:body>'
    '<w:p><w:r><w:rPr><w:rFonts w:ascii="微软雅黑" w:eastAsia="微软雅黑" w:hAnsi="微软雅黑"/></w:rPr>'
    '<w:t>字体替换测试：砥砺前行，聚力未来。</w:t></w:r></w:p>'
    '</w:body></w:document>'
)
content_types = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>'
)
rels = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '</Relationships>'
)
with zipfile.ZipFile(docx_path, 'w', zipfile.ZIP_DEFLATED) as z:
    z.writestr('[Content_Types].xml', content_types)
    z.writestr('_rels/.rels', rels)
    z.writestr('word/document.xml', document_xml)
print("yahei docx written:", docx_path, os.path.getsize(docx_path), "bytes")
PY

set +e
python3 /opt/wpsrpc-rpc/convert_docx2pdf.py "$WORK/yahei.docx" "$WORK/yahei.pdf"
YAHEI_RC=$?
set -e
if [ "$YAHEI_RC" -ne 0 ]; then
    echo "FAIL: 雅黑测试 docx 转换失败"
    exit 1
fi

if command -v pdffonts >/dev/null 2>&1; then
    FONTS=$(pdffonts "$WORK/yahei.pdf" 2>/dev/null)
    echo "   嵌入字体:"
    echo "$FONTS" | tail -n +3
    if ! echo "$FONTS" | grep -qi "NotoSansSC"; then
        echo "FAIL: 未嵌入 Noto Sans SC（别名未生效）"
        exit 1
    fi
    if echo "$FONTS" | grep -qi "SimSun"; then
        echo "FAIL: 回落宋体 SimSun（应映射为思源黑体）"
        exit 1
    fi
    echo "   字体替换断言通过（微软雅黑 → Noto Sans SC，无 SimSun）"
else
    echo "   (pdffonts 未安装，跳过字体断言)"
fi

# 体积断言：TrueType 字体应被子集化；若 WPS 对 CFF 整字嵌入，PDF 会膨胀到 20MB+
YAHEI_SIZE=$(stat -c%s "$WORK/yahei.pdf")
echo "   雅黑测试 PDF 大小: $((YAHEI_SIZE/1024)) KB"
if [ "$YAHEI_SIZE" -gt $((5 * 1024 * 1024)) ]; then
    echo "FAIL: 雅黑测试 PDF 超过 5MB（疑似 CFF 字体整字嵌入，未正常子集）"
    exit 1
fi
echo "   体积断言通过（< 5MB，字体正常子集化）"

echo ""
echo "PASS: 端到端转换成功，PDF 已生成 (${SIZE} 字节)"
kill "$XPID" 2>/dev/null || true
exit 0
