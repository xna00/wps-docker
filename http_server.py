#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pywpsrpc HTTP 转换服务：常驻 WPS 实例，POST /convert 上传 docx 返回 PDF

说明: 仅限内网使用，无鉴权（如部署到公网请自行加网关/反向代理鉴权）

环境变量:
    HOST / PORT    监听地址（默认 0.0.0.0:8080）
    MAX_FILE_MB    上传大小上限（默认 50MB）

接口:
    GET  /health   健康检查（含 WPS 实例状态）
    POST /convert  multipart 上传 file=<docx> → application/pdf
"""
import os, sys, time, threading, tempfile, shutil

os.environ.setdefault("DISPLAY", ":99")
os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root")
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/runtime-root/dbus")

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import Response
from pywpsrpc.rpcwpsapi import createWpsRpcInstance, wpsapi
from pywpsrpc.common import S_OK, QtApp

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "50"))
MAX_FILE = MAX_FILE_MB * 1024 * 1024
WORK = "/tmp/http_conv"

app = FastAPI(title="wps-docx2pdf API", version="1.0")

# ---------------------------------------------------------------- WPS 管理
class WpsEngine:
    """全局单例：懒初始化 + 崩溃重建 + 锁串行化"""

    def __init__(self):
        self._lock = threading.Lock()
        self._app = None
        self._docs = None
        self._rpc = None

    def _ensure(self):
        if self._app is not None:
            return self._app
        hr, rpc = createWpsRpcInstance()
        if hr != S_OK:
            raise RuntimeError(f"createWpsRpcInstance: 0x{hr & 0xFFFFFFFF:08X}")
        rpc.setStartTimeout(15000000)
        hr, wps_app = rpc.getWpsApplication()
        if hr != S_OK:
            raise RuntimeError(f"getWpsApplication: 0x{hr & 0xFFFFFFFF:08X}")
        hr, docs = wps_app.get_Documents()
        if hr != S_OK:
            raise RuntimeError(f"get_Documents: 0x{hr & 0xFFFFFFFF:08X}")
        self._app, self._docs, self._rpc = wps_app, docs, rpc
        return self._app

    def convert(self, src: str, out: str) -> str:
        """转换一个 docx → pdf（线程安全）"""
        with self._lock:
            # 重建尝试：初次失败后丢弃旧实例重建一次
            for attempt in (1, 2):
                try:
                    wps_app = self._ensure()
                    hr, doc = self._docs.Open(src)
                    if hr != S_OK:
                        raise RuntimeError(f"Open: 0x{hr & 0xFFFFFFFF:08X}")
                    try:
                        hr = doc.SaveAs2(out, FileFormat=wpsapi.wdFormatPDF)
                        if hr != S_OK:
                            # 兜底：旧版 API
                            hr = doc.ExportAsFixedFormat(out, wpsapi.wdExportFormatPDF)
                            if hr != S_OK:
                                raise RuntimeError(f"SaveAs2/Export: 0x{hr & 0xFFFFFFFF:08X}")
                    finally:
                        try:
                            doc.Close(False)
                        except Exception:
                            pass
                    if os.path.exists(out) and os.path.getsize(out) > 0:
                        return out
                    raise RuntimeError("PDF 未生成或为空")
                except Exception as e:
                    # 第一次失败：假设实例异常，丢弃重建
                    if attempt == 1:
                        print(f"[warn] 转换失败(尝试重建): {e}", flush=True)
                        self.reset()
                    else:
                        raise RuntimeError(f"转换失败: {e}") from e

    def reset(self):
        self._app = None
        self._docs = None
        self._rpc = None

    def status(self):
        return "ready" if self._app is not None else "not_initialized"

engine = WpsEngine()

# ---------------------------------------------------------------- 接口
@app.get("/health")
def health():
    return {"status": "ok", "wps": engine.status()}

@app.post("/convert")
async def convert(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="仅支持 .docx 文件")

    data = await file.read()
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="空文件")
    if len(data) > MAX_FILE:
        raise HTTPException(status_code=413, detail=f"文件超过 {MAX_FILE_MB}MB 上限")

    os.makedirs(WORK, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="conv_", dir=WORK)
    src = os.path.join(workdir, "input.docx")
    out = os.path.join(workdir, "output.pdf")
    try:
        with open(src, "wb") as f:
            f.write(data)
        t0 = time.time()
        engine.convert(src, out)
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
