#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WPS 三组件 RPC 引擎封装（wps/wpp/et），供 worker 子进程使用。

- 冷启动串行化：跨进程文件锁把并发冷启动争用变成「一次一个」
  （getApplication 是同步 RPC，返回即实例就绪，无需时间间隔）
- warmup 带重试；getApplication 偶发永久阻塞时线程预算超时自杀释放锁
- 实例清理：冷启动前后 /proc 快照 diff 认领本 worker 的全部 WPS 进程
  （主实例 + launcher stub，WPS double-fork 脱离进程树，必须按 PID 清理）
"""
import fcntl
import os
import signal
import threading
import time

# 复用 entrypoint 注入的环境；仅当缺失时兜底（独立运行场景）
os.environ.setdefault("DISPLAY", ":99")
os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root")
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/runtime-root/dbus")

COLDSTART_LOCK_PATH = os.environ.get("COLDSTART_LOCK", "/tmp/wps_coldstart.lock")
COLDSTART_STAGGER = float(os.environ.get("COLDSTART_STAGGER", "0"))    # 极端环境微调用
COLDSTART_BUDGET = float(os.environ.get("COLDSTART_BUDGET", "60"))    # 冷启动硬上限（秒）

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


def _list_wps_pids():
    """列出所有 WPS 系列进程 PID，供冷启动后 diff 认领（串行化前提下不会误吞）。"""
    pids = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            comm = open("/proc/%s/comm" % name).read().strip()
        except Exception:
            continue
        if comm in ("wpsoffice", "wps", "et", "wpp"):
            pids.append(int(name))
    return pids


class RpcEngine:
    """某组件（wps/wpp/et）的引擎：进程内单实例复用（一个 worker 持一个实例）。"""

    def __init__(self, name):
        self.name = name
        self._app = None
        self._coll = None
        self._rpc = None
        self._inst_pids = set()  # 本 worker 拉起的 WPS 实例 PID（正常单元素）

    def kill_instances(self):
        """按记录 PID 击杀本 worker 的 WPS 实例（已脱离进程树，必须按 PID）。"""
        for p in list(self._inst_pids):
            try:
                os.kill(p, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self._inst_pids.clear()

    def _create_instance(self):
        """拉起 WPS 实例（createRpcInstance + getApplication + get集合）。可能永久阻塞。"""
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
        return app, coll, rpc

    def _ensure(self):
        if self._app is not None:
            return self._app
        # 跨进程串行化冷启动：一次一个，避免并发争用 Kingsoft 守护进程
        lockf = None
        try:
            try:
                lockf = open(COLDSTART_LOCK_PATH, "w")
                fcntl.flock(lockf, fcntl.LOCK_EX)
            except OSError as e:
                print(f"[warn] {self.name} 冷启动锁获取失败，降级为无锁（可能竞态）: {e}",
                      flush=True)
                lockf = None
            time.sleep(COLDSTART_STAGGER)
            baseline = set(_list_wps_pids())  # 冷启动前已有进程（其它 worker 的）
            # getApplication 偶发永久阻塞：线程 + 预算超时兜底，超时自杀释放锁
            box = {}
            t = threading.Thread(target=self._run_create, args=(box,), daemon=True)
            t.start()
            t.join(COLDSTART_BUDGET)
            if t.is_alive():
                print(f"[fatal] {self.name} 冷启动永久阻塞（>{COLDSTART_BUDGET:.0f}s），"
                      f"自杀释放锁", flush=True)
                os._exit(1)
            if "err" in box:
                raise box["err"]
            app, coll, rpc = box["val"]
            self._app, self._coll, self._rpc = app, coll, rpc
            # 认领冷启动期间新出现的全部 WPS 进程（实例 + launcher stub，一并清理）
            self._inst_pids = set(_list_wps_pids()) - baseline
            return self._app
        finally:
            if lockf is not None:
                try:
                    fcntl.flock(lockf, fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    lockf.close()
                except Exception:
                    pass

    def _run_create(self, box):
        try:
            box["val"] = self._create_instance()
        except Exception as e:
            box["err"] = e

    def warmup(self, n=3, wait=1.0):
        """冷启动带重试（getApplication 偶发 E_FAIL 重试可救）。"""
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
        self.kill_instances()  # 先按 PID 杀掉旧实例，避免重建时泄漏
        self._app = None
        self._coll = None
        self._rpc = None
