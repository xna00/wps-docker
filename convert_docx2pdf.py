#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pywpsrpc RPC 转换（CLI 模式）：按扩展名路由到 wps/wpp/et 三组件，转 PDF

与 http_server.py 共用同一套「扩展名 -> 组件」路由，保证 CLI 与 API 行为一致：
    wps(Writer):       docx doc wps rtf txt xml html htm mht mhtml odt uot uof dot dotx
    wpp(Presentation): pptx ppt dps pot potx odp uop pps ppsx
    et(Spreadsheet):   xlsx xls et csv ods uos xlt xltx ett prn dif

用法：
    python3 convert_docx2pdf.py <输入> <输出.pdf>
"""
import sys, os, multiprocessing

# 沿用 entrypoint 注入的环境；仅当缺失时兜底（独立运行场景）
os.environ.setdefault("DISPLAY", ":99")
os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root")
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/runtime-root/dbus")

# 转换超时（秒）：WPS 对个别格式（已知 .odp）导出 PDF 存在挂起 bug，
# Open 返回成功但 ExportAsFixedFormat 永久阻塞。超时兜底，避免 CLI 无限挂死。
CONVERT_TIMEOUT = 180

from pywpsrpc.common import S_OK, QtApp

# 与 http_server.py 的 MODULE_BY_EXT 保持一致
MODULE_BY_EXT = {
    # Writer（wpsapi）
    "docx": "wps", "doc": "wps", "wps": "wps", "rtf": "wps", "txt": "wps",
    "xml": "wps", "html": "wps", "htm": "wps", "mht": "wps", "mhtml": "wps",
    "odt": "wps", "uot": "wps", "uof": "wps", "dot": "wps", "dotx": "wps",
    # Presentation（wppapi）
    "pptx": "wpp", "ppt": "wpp", "dps": "wpp", "pot": "wpp", "potx": "wpp",
    "odp": "wpp", "uop": "wpp", "pps": "wpp", "ppsx": "wpp",
    # Spreadsheet（etapi）
    "xlsx": "et", "xls": "et", "et": "et", "csv": "et", "ods": "et",
    "uos": "et", "xlt": "et", "xltx": "et", "ett": "et", "prn": "et", "dif": "et",
}


class ConvertError(Exception):
    pass


def _wps(src, out):
    from pywpsrpc.rpcwpsapi import createWpsRpcInstance, wpsapi
    hr, rpc = createWpsRpcInstance()
    if hr != S_OK:
        raise ConvertError(f"createWpsRpcInstance: 0x{hr & 0xFFFFFFFF:08X}")
    rpc.setStartTimeout(15000000)
    hr, app = rpc.getWpsApplication()
    if hr != S_OK:
        raise ConvertError(f"getWpsApplication: 0x{hr & 0xFFFFFFFF:08X}")
    hr, coll = app.get_Documents()
    if hr != S_OK:
        raise ConvertError(f"get_Documents: 0x{hr & 0xFFFFFFFF:08X}")
    hr, doc = coll.Open(src)
    if hr != S_OK:
        raise ConvertError(f"Documents.Open: 0x{hr & 0xFFFFFFFF:08X}")
    try:
        hr = doc.SaveAs2(out, FileFormat=wpsapi.wdFormatPDF)
        if hr != S_OK:
            hr = doc.ExportAsFixedFormat(out, wpsapi.wdExportFormatPDF)
    finally:
        try:
            doc.Close(False)
        except Exception:
            pass
    if hr != S_OK:
        raise ConvertError(f"SaveAs2/Export: 0x{hr & 0xFFFFFFFF:08X}")


def _wpp(src, out):
    from pywpsrpc.rpcwppapi import createWppRpcInstance, wppapi
    hr, rpc = createWppRpcInstance()
    if hr != S_OK:
        raise ConvertError(f"createWppRpcInstance: 0x{hr & 0xFFFFFFFF:08X}")
    rpc.setStartTimeout(15000000)
    hr, app = rpc.getWppApplication()
    if hr != S_OK:
        raise ConvertError(f"getWppApplication: 0x{hr & 0xFFFFFFFF:08X}")
    hr, coll = app.get_Presentations()
    if hr != S_OK:
        raise ConvertError(f"get_Presentations: 0x{hr & 0xFFFFFFFF:08X}")
    hr, pres = coll.Open(src)
    if hr != S_OK:
        raise ConvertError(f"Presentations.Open: 0x{hr & 0xFFFFFFFF:08X}")
    try:
        hr = pres.ExportAsFixedFormat(out, wppapi.ppFixedFormatTypePDF)
    finally:
        try:
            pres.Close()
        except Exception:
            pass
    if hr != S_OK:
        raise ConvertError(f"ExportAsFixedFormat: 0x{hr & 0xFFFFFFFF:08X}")


def _et(src, out):
    from pywpsrpc.rpcetapi import createEtRpcInstance, etapi
    hr, rpc = createEtRpcInstance()
    if hr != S_OK:
        raise ConvertError(f"createEtRpcInstance: 0x{hr & 0xFFFFFFFF:08X}")
    rpc.setStartTimeout(15000000)
    hr, app = rpc.getEtApplication()
    if hr != S_OK:
        raise ConvertError(f"getEtApplication: 0x{hr & 0xFFFFFFFF:08X}")
    hr, coll = app.get_Workbooks()
    if hr != S_OK:
        raise ConvertError(f"get_Workbooks: 0x{hr & 0xFFFFFFFF:08X}")
    hr, wb = coll.Open(src)
    if hr != S_OK:
        raise ConvertError(f"Workbooks.Open: 0x{hr & 0xFFFFFFFF:08X}")
    try:
        hr = wb.ExportAsFixedFormat(etapi.xlTypePDF, Filename=out)
    finally:
        try:
            wb.Close(False)
        except Exception:
            pass
    if hr != S_OK:
        raise ConvertError(f"ExportAsFixedFormat: 0x{hr & 0xFFFFFFFF:08X}")


def convert(module, src, out):
    """用指定组件转换一个文档 -> pdf（失败抛 ConvertError，由调用方决定是否重试）"""
    if module == "wps":
        _wps(src, out)
    elif module == "wpp":
        _wpp(src, out)
    else:  # et
        _et(src, out)
    if not (os.path.exists(out) and os.path.getsize(out) > 0):
        raise ConvertError(f"PDF 未生成或为空: {out}")


def _convert_worker(module, src, out):
    """子进程入口：在干净的进程内存里建 QtApp 并执行转换。

    QtApp 必须在 fork 之后创建（Qt 对象跨 fork 不安全），
    这也是把转换整体放进子进程的原因。
    """
    qApp = QtApp(sys.argv)  # noqa: F841 — 保持引用防 GC，进程退出时无需清理
    convert(module, src, out)


def _run_with_timeout(module, src, out):
    """带超时执行转换。超时返回 False；转换失败抛 ConvertError。"""
    ctx = multiprocessing.get_context("fork")
    p = ctx.Process(target=_convert_worker, args=(module, src, out))
    p.start()
    p.join(CONVERT_TIMEOUT)
    if p.is_alive():
        # 转换进程仍活着 = WPS 挂起（如 .odp 导出 bug）。强杀并报告超时。
        p.terminate()
        p.join(5)
        if p.is_alive():
            p.kill()
            p.join(2)
        return False
    if p.exitcode != 0:
        raise ConvertError(f"转换子进程异常退出 (exit={p.exitcode})")
    return True


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "/data/input.docx"
    out = sys.argv[2] if len(sys.argv) > 2 else "/data/output.pdf"

    if not os.path.exists(src):
        print(f"[err] 输入文件不存在: {src}", flush=True)
        return 2

    ext = src.lower().rsplit(".", 1)[-1] if "." in src else ""
    module = MODULE_BY_EXT.get(ext)
    if not module:
        print(f"[err] 不支持的文件类型 .{ext}（支持: {', '.join(sorted(MODULE_BY_EXT))}）", flush=True)
        return 2

    print(f"[info] 路由组件: {module}  输入: {src}  输出: {out}", flush=True)

    # WPS 冷启动偶发失败，重试一次；超时（疑似格式导出 bug）则直接失败
    last = None
    for attempt in (1, 2):
        try:
            ok = _run_with_timeout(module, src, out)
            if not ok:
                print(
                    f"[err] 转换超时（>{CONVERT_TIMEOUT}s）。疑似 WPS 无法导出该格式"
                    f"（已知 .odp 会挂起），请改用 .pptx/.docx/.xlsx 等格式",
                    flush=True,
                )
                os._exit(1)
            last = None
            break
        except ConvertError as e:
            last = e
            if attempt == 1:
                print(f"[warn] 转换失败，重试一次: {e}", flush=True)
            else:
                print(f"[err] 转换失败: {e}", flush=True)

    if last is not None:
        os._exit(1)

    print(f"完成: {out} ({os.path.getsize(out)} 字节)", flush=True)
    # 用 os._exit 跳过 Python 退出清理（SDK 清理可能段错误，不影响已写出的 PDF）
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
