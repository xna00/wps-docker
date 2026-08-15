#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pywpsrpc HTTP 转换服务：常驻 WPS 三组件(wps/wpp/et)，POST /convert 上传文档返回 PDF

支持格式按「扩展名 -> 组件」路由：
    wps(Writer):       docx doc wps rtf txt xml html htm mht mhtml odt uot uof dot dotx
    wpp(Presentation): pptx ppt dps pot potx odp uop pps ppsx
    et(Spreadsheet):   xlsx xls et csv ods uos xlt xltx ett prn dif

说明: 仅限内网使用，无鉴权（如部署到公网请自行加网关/反向代理鉴权）

环境变量:
    HOST / PORT    监听地址（默认 0.0.0.0:8080）
    MAX_FILE_MB    上传大小上限（默认 50MB）

接口:
    GET  /health   健康检查（含各组件实例状态）
    POST /convert  multipart 上传 file=<文档> → application/pdf
"""
import os, sys, time, threading, tempfile, shutil

os.environ.setdefault("DISPLAY", ":99")
os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root")
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/runtime-root/dbus")

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import Response
from pywpsrpc.rpcwpsapi import createWpsRpcInstance, wpsapi
from pywpsrpc.rpcwppapi import createWppRpcInstance, wppapi
from pywpsrpc.rpcetapi import createEtRpcInstance, etapi
from pywpsrpc.common import S_OK, QtApp

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "50"))
MAX_FILE = MAX_FILE_MB * 1024 * 1024
WORK = "/tmp/http_conv"

# ---------------------------------------------------------------- 格式路由
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

app = FastAPI(title="wps-docx2pdf API", version="1.1")

# ---------------------------------------------------------------- 组件引擎
class RpcEngine:
    """某组件（wps/wpp/et）的全局单例：懒初始化 + 崩溃重建 + 锁串行化"""

    def __init__(self, name):
        self.name = name
        self._lock = threading.Lock()
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

    def convert(self, src: str, out: str) -> str:
        """转换一个文档 → pdf（线程安全）"""
        with self._lock:
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

    def status(self):
        return "ready" if self._app is not None else "not_initialized"

engines = {m: RpcEngine(m) for m in ("wps", "wpp", "et")}

# ---------------------------------------------------------------- 接口
@app.get("/health")
def health():
    return {"status": "ok", "wps": engines["wps"].status(),
            "wpp": engines["wpp"].status(), "et": engines["et"].status()}

@app.post("/convert")
async def convert(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="未提供文件名")
    ext = file.filename.lower().rsplit(".", 1)[-1] if "." in file.filename else ""
    module = MODULE_BY_EXT.get(ext)
    if not module:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 .{ext}，支持: {', '.join(SUPPORTED_EXTS)}",
        )

    data = await file.read()
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="空文件")
    if len(data) > MAX_FILE:
        raise HTTPException(status_code=413, detail=f"文件超过 {MAX_FILE_MB}MB 上限")

    os.makedirs(WORK, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="conv_", dir=WORK)
    src = os.path.join(workdir, "input" + os.path.splitext(file.filename)[1].lower())
    out = os.path.join(workdir, "output.pdf")
    try:
        with open(src, "wb") as f:
            f.write(data)
        t0 = time.time()
        engines[module].convert(src, out)
        elapsed = time.time() - t0
        with open(out, "rb") as f:
            pdf = f.read()
        print(f"[ok] {file.filename} -> {len(pdf)}B 耗时{elapsed:.2f}s", flush=True)
        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="output.pdf"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"[err] {file.filename}: {e}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
