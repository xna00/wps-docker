#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pywpsrpc RPC 转换最终版：getWpsApplication → get_Documents → Open → SaveAs2 PDF"""
import sys, os, time

# 沿用 entrypoint 注入的环境；仅当缺失时兜底（独立运行场景）
os.environ.setdefault("DISPLAY", ":99")
os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root")
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/runtime-root/dbus")

from pywpsrpc.rpcwpsapi import createWpsRpcInstance, wpsapi
from pywpsrpc.common import S_OK, QtApp

def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "/workspace/docx2pdf/input/demo.docx"
    out = sys.argv[2] if len(sys.argv) > 2 else "/workspace/docx2pdf/output/demo_rpc.pdf"

    qApp = QtApp(sys.argv)
    hr, rpc = createWpsRpcInstance()
    print(f"createWpsRpcInstance: 0x{hr & 0xFFFFFFFF:08X}", flush=True)
    if hr != S_OK:
        return 1

    rpc.setStartTimeout(15000000)  # 微秒
    t0 = time.time()
    hr, app = rpc.getWpsApplication()
    print(f"getWpsApplication: 0x{hr & 0xFFFFFFFF:08X}, 耗时 {time.time()-t0:.1f}s", flush=True)
    if hr != S_OK:
        return 1

    hr, docs = app.get_Documents()
    print(f"get_Documents: 0x{hr & 0xFFFFFFFF:08X}", flush=True)
    if hr != S_OK:
        return 1

    hr, doc = docs.Open(src)
    print(f"Documents.Open: 0x{hr & 0xFFFFFFFF:08X}", flush=True)
    if hr != S_OK:
        return 1

    hr = doc.SaveAs2(out, FileFormat=wpsapi.wdFormatPDF)
    print(f"SaveAs2(PDF): 0x{hr & 0xFFFFFFFF:08X}", flush=True)
    if hr != S_OK:
        hr = doc.ExportAsFixedFormat(out, wpsapi.wdExportFormatPDF)
        print(f"ExportAsFixedFormat: 0x{hr & 0xFFFFFFFF:08X}", flush=True)

    try:
        doc.Close(False)
    except Exception:
        pass
    ok = os.path.exists(out) and os.path.getsize(out) > 0
    print(f"完成: {out} ({os.path.getsize(out) if ok else 0} 字节)", flush=True)
    # 用 os._exit 跳过 Python 退出清理（SDK 清理可能段错误，不影响已写出的 PDF）
    os._exit(0 if ok else 1)

if __name__ == "__main__":
    main()
