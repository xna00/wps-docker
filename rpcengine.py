#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WPS 三组件 RPC 引擎封装（wps/wpp/et），供 worker 子进程使用。

与 convert_docx2pdf.py 的 CLI 逻辑等价，但封装为进程内复用的 RpcEngine：
  - 懒初始化（首次 _ensure 拉起 WPS 进程）
  - 冷启动串行化：_ensure 用跨进程文件锁 + 错峰，把并发冷启动的 Kingsoft 守护进程
    争用变成「一次一个」，根治并发 E_FAIL / 永久阻塞（实验一已证实并发冷启动会失败）
  - warmup 带重试（消除冷启动偶发 E_FAIL 竞态；永久阻塞由 _ensure 内部自杀释放锁兜底）
  - convert 自带一次重建重试（处理转换中途的实例异常）
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

# ---------------------------------------------------------------- 冷启动串行化
# 多个 worker 进程会并发冷启动（getApplication 连接全局 Kingsoft 守护进程）。
# 并发争用会触发 E_FAIL / 永久阻塞（实验一已证实），必须以「跨进程文件锁」串行化，
# 并错峰 ≥2s，把争用变成「一次一个、互不干扰」。仅真实冷启动（_app is None）才加锁，
# 复用路径直接返回，零开销。
COLDSTART_LOCK_PATH = os.environ.get("COLDSTART_LOCK", "/tmp/wps_coldstart.lock")
COLDSTART_STAGGER = float(os.environ.get("COLDSTART_STAGGER", "2.0"))  # 错峰间隔（秒）
COLDSTART_BUDGET = float(os.environ.get("COLDSTART_BUDGET", "60"))    # 单实例冷启动硬上限（秒）


def kill_subtree(root_pid: int):
    """SIGKILL root_pid 及其全部后代进程（按 /proc ppid 树遍历）。

    关键背景：WPS 启动后会自行 setsid 脱离 worker 进程组（ppid 变 1、独立 pgid），
    因此单纯 killpg(worker) 清不掉它拉起的 wpsoffice 实例，会泄漏（~380MB/个）。
    必须在 worker 还活着时按 PID 树收集其全部后代并逐个击杀——无论 WPS 怎么 setsid，
    只要 worker 还活着，wpsoffice 的 ppid 链仍指向本 worker，即可精准清理。
    """
    try:
        with open("/proc/%d/stat" % root_pid):
            pass
    except FileNotFoundError:
        return
    children = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name) as f:
                data = f.read()
            # stat 第4字段是 ppid；comm 可能含空格/括号，从 ')' 之后切最稳
            ppid = int(data[data.index(")") + 1:].split()[1])
        except Exception:
            continue
        children.setdefault(ppid, []).append(int(name))
    stack = [root_pid]
    seen = set()
    pids = []
    while stack:
        cur = stack.pop()
        for c in children.get(cur, []):
            if c not in seen:
                seen.add(c)
                pids.append(c)
                stack.append(c)
    # 先杀后代，最后杀根（根死后无法再遍历）
    for p in pids + [root_pid]:
        try:
            os.kill(p, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

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
    """列出当前所有 WPS 系列进程 PID（wpsoffice / wps / et / wpp）。
    用于 worker 冷启动后按 PID 精确认领自己拉起的实例——WPS 会 double-fork 守护进程化，
    实际实例的 ppid 很快变成 1（init），无法靠进程树反查，只能按 PID 记录。"""
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
        self._inst_pids = set()  # 本 worker 拉起的 WPS 实例 PID（用于精准清理，防泄漏）

    def _track_instances(self, baseline):
        """与冷启动前的 WPS PID 集合做差，把本 worker 新拉起的实例 PID 记入 _inst_pids。
        冷启动是全局串行化的（文件锁），故不会误吞其它 worker 的实例。"""
        self._inst_pids |= (set(_list_wps_pids()) - baseline)

    def kill_instances(self):
        """按记录的 PID 精准击杀本 worker 的 WPS 实例（WPS 已脱离进程树，必须按 PID）。"""
        for p in list(self._inst_pids):
            try:
                os.kill(p, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self._inst_pids.clear()

    def _create_instance(self):
        """真正拉起 WPS 实例（createRpcInstance + getApplication + get集合）。
        仅在持有冷启动锁且已错峰后调用；可能永久阻塞（getApplication 偶发）。"""
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
        # 跨进程串行化冷启动：同一时刻只有一个 worker 在拉起实例，其余排队。
        # 错峰避免 Kingsoft 守护进程被连续请求冲垮。
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
            # getApplication 偶发永久阻塞：用线程 + 预算超时兜底，超时即自杀释放锁，
            # 避免整把锁被永久占用、拖垮后续所有冷启动。
            baseline = set(_list_wps_pids())  # 冷启动前已有的 WPS 进程（其它 worker 的）
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
            # 记录本 worker 新拉起的实例 PID（WPS double-fork 到 init，必须按 PID 跟踪清理）
            self._track_instances(baseline)
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
        """冷启动带重试。_ensure 内部已用跨进程文件锁串行化 + 错峰，消除并发争用；
        getApplication 偶发 E_FAIL（进程还没就绪）时重试即可救回。若永久阻塞，
        _ensure 内部会自杀释放锁，由调用方超时 kill 兜底。"""
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
