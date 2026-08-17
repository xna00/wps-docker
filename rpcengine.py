#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WPS 三组件 RPC 引擎封装（wps/wpp/et），供 worker 子进程使用。

与 convert_docx2pdf.py 的 CLI 逻辑等价，但封装为进程内复用的 RpcEngine：
  - 懒初始化（首次 _ensure 拉起 WPS 进程）
  - warmup 带重试（消除冷启动偶发 E_FAIL 竞态；注意 getApplication 偶尔会永久阻塞，
    阻塞无法被重试循环捕获，只能靠调用方的超时 kill 兜底）
  - convert 自带一次重建重试（处理转换中途的实例异常）
"""
import os
import time

# 复用 entrypoint 注入的环境；仅当缺失时兜底（独立运行场景）
os.environ.setdefault("DISPLAY", ":99")
os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root")
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/runtime-root/dbus")

from pywpsrpc.rpcwpsapi import createWpsRpcInstance, wpsapi
from pywpsrpc.rpcwppapi import createWppRpcInstance, wppapi
from pywpsrpc.rpcetapi import createEtRpcInstance, etapi
from pywpsrpc.common import S_OK

# ---------------------------------------------------------------- 格式路由
# 与 convert_docx2pdf.py / 旧 http_server.py 的 MODULE_BY_EXT 保持一致
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
SUPPORTED_EXTS = sorted(MODULE_BY_EXT)


class RpcEngine:
    """某组件（wps/wpp/et）的引擎：进程内单实例复用（一个 worker 持一个实例）。"""

    def __init__(self, name):
        self.name = name
        self._app = None
        self._coll = None
        self._rpc = None

    def _ensure(self):
        if self._app is not None:
            return self._app
        if self.name == "wps":
            hr, rpc = createWpsRpcInstance()
        elif self.name == "wpp":
            hr, rpc = createWppRpcInstance()
        else:
            hr, rpc = createEtRpcInstance()
        if hr != S_OK:
            raise RuntimeError(f"{self.name} createRpcInstance: 0x{hr & 0xFFFFFFFF:08X}")
        rpc.setStartTimeout(15000000)
        if self.name == "wps":
            hr, app = rpc.getWpsApplication()
        elif self.name == "wpp":
            hr, app = rpc.getWppApplication()
        else:
            hr, app = rpc.getEtApplication()
        if hr != S_OK:
            raise RuntimeError(f"{self.name} getApplication: 0x{hr & 0xFFFFFFFF:08X}")
        if self.name == "wps":
            hr, coll = app.get_Documents()
        elif self.name == "wpp":
            hr, coll = app.get_Presentations()
        else:
            hr, coll = app.get_Workbooks()
        if hr != S_OK:
            raise RuntimeError(f"{self.name} get集合: 0x{hr & 0xFFFFFFFF:08X}")
        self._app, self._coll, self._rpc = app, coll, rpc
        return self._app

    def warmup(self, n=12, wait=1.0):
        """冷启动带重试。getApplication 偶发 E_FAIL（进程还没就绪），重试即可救回；
        若永久阻塞则不会走到下一次重试，靠调用方超时 kill 兜底。"""
        last = None
        for _ in range(n):
            try:
                self._ensure()
                return
            except Exception as e:
                last = e
                time.sleep(wait)
        raise RuntimeError(f"{self.name} warmup 失败（重试 {n} 次）: {last}")

    def convert(self, src: str, out: str) -> str:
        """转换一个文档 → pdf。失败（含中途实例异常）重建重试一次。"""
        for attempt in (1, 2):
            try:
                self._ensure()
                if self.name == "wps":
                    hr, doc = self._coll.Open(src)
                    if hr != S_OK:
                        raise RuntimeError(f"Open: 0x{hr & 0xFFFFFFFF:08X}")
                    try:
                        hr = doc.SaveAs2(out, FileFormat=wpsapi.wdFormatPDF)
                        if hr != S_OK:
                            hr = doc.ExportAsFixedFormat(out, wpsapi.wdExportFormatPDF)
                            if hr != S_OK:
                                raise RuntimeError(f"SaveAs2/Export: 0x{hr & 0xFFFFFFFF:08X}")
                    finally:
                        try:
                            doc.Close(False)
                        except Exception:
                            pass
                elif self.name == "wpp":
                    hr, pres = self._coll.Open(src)
                    if hr != S_OK:
                        raise RuntimeError(f"Open: 0x{hr & 0xFFFFFFFF:08X}")
                    try:
                        hr = pres.ExportAsFixedFormat(out, wppapi.ppFixedFormatTypePDF)
                        if hr != S_OK:
                            raise RuntimeError(f"ExportAsFixedFormat: 0x{hr & 0xFFFFFFFF:08X}")
                    finally:
                        try:
                            pres.Close()
                        except Exception:
                            pass
                else:  # et
                    hr, wb = self._coll.Open(src)
                    if hr != S_OK:
                        raise RuntimeError(f"Open: 0x{hr & 0xFFFFFFFF:08X}")
                    try:
                        hr = wb.ExportAsFixedFormat(etapi.xlTypePDF, Filename=out)
                        if hr != S_OK:
                            raise RuntimeError(f"ExportAsFixedFormat: 0x{hr & 0xFFFFFFFF:08X}")
                    finally:
                        try:
                            wb.Close(False)
                        except Exception:
                            pass
                if os.path.exists(out) and os.path.getsize(out) > 0:
                    return out
                raise RuntimeError("PDF 未生成或为空")
            except Exception as e:
                if attempt == 1:
                    print(f"[warn] {self.name} 转换失败(尝试重建): {e}", flush=True)
                    self.reset()
                else:
                    raise RuntimeError(f"{self.name} 转换失败: {e}") from e

    def reset(self):
        self._app = None
        self._coll = None
        self._rpc = None
