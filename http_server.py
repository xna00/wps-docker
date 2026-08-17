#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pywpsrpc HTTP 转换服务（v1.6.1 worker 池架构）

设计（最终拍板）：
  - 主进程 FastAPI（async）：只做 IO 收发与调度，绝不碰 RPC。
  - worker 子进程：持有某组件（wps/wpp/et）的常驻 WPS 实例（handler），
    单实例串行处理自身任务。每 worker 独立一对队列（in_q/out_q）。
  - 动态 worker：每来一个请求，acquire 一个 worker（优先复用同类型 idle 实例，
    否则新建冷启动）；占用即标记 busy，并发度 = idle 数，同类全忙则排到下次。
  - 请求级超时：主进程阻塞读取 worker 的 out_q（带超时循环），超时即
    kill 该 worker 整个进程组（连残留 wpsoffice 一起清）→ 返回 504。
  - 成功回收：检查同类型 idle 数量，≥ SPARE_PER_TYPE 则干掉当前（回收），
    否则留作 idle 备用。无需 rebuild / 固定容量池 / 独立 watchdog / heartbeat。

环境变量：
    HOST / PORT        监听地址（默认 0.0.0.0:8080）
    MAX_FILE_MB        上传大小上限（默认 50MB）
    SPARE_PER_TYPE     每类型最多保留的 idle 备用 worker 数（默认 1；0 = 空闲零常驻）
    TASK_TIMEOUT       通用转换超时秒（默认 180）
    ODP_TIMEOUT        .odp 专项超时秒（已知必挂死，默认 30）

接口：
    GET  /health   健康检查（含各类型 worker 的 busy/idle 计数）
    POST /convert  multipart 上传 file=<文档> → application/pdf
"""
import asyncio
import multiprocessing
import os
import queue
import signal
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import Response

from rpcengine import MODULE_BY_EXT, SUPPORTED_EXTS
from worker import worker_main, INST_FILE

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "50"))
MAX_FILE = MAX_FILE_MB * 1024 * 1024
WORK = "/tmp/http_conv"
SPARE_PER_TYPE = int(os.environ.get("SPARE_PER_TYPE", "1"))
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", "180"))
ODP_TIMEOUT = int(os.environ.get("ODP_TIMEOUT", "30"))

MP_CTX = multiprocessing.get_context("fork")
# 阻塞读取 worker out_q 用的线程池（避免阻塞事件循环）；默认转换很快，线程迅速释放
_executor = ThreadPoolExecutor(max_workers=64, thread_name_prefix="dispatch")

app = FastAPI(title="wps-docx2pdf API", version="1.6.1")


@dataclass
class Worker:
    type: str                       # "wps" | "wpp" | "et"
    status: str                     # "busy" | "idle"
    pid: int                        # worker 子进程 pid（killpg 用）
    in_q: multiprocessing.Queue    # 主->worker 任务投递
    out_q: multiprocessing.Queue   # worker->主 结果回传
    proc: multiprocessing.Process


WORKERS: list[Worker] = []
_pool_lock = asyncio.Lock()


def task_timeout_for(ext: str) -> int:
    return ODP_TIMEOUT if ext == "odp" else TASK_TIMEOUT


def kill_worker(w: Worker):
    """精准清理一个 worker：按记录的 PID 杀它的 WPS 实例 + killpg 杀 worker 本身。

    关键：WPS 会 double-fork 守护进程化，实例脱离 worker 进程树/进程组，
    killpg(worker) 打不到它——实例 PID 由 worker 冷启动后用 rpc.getProcessPid()
    记录在 INST_FILE，这里按 PID 精准击杀，再清理 worker 进程本身。
    """
    # 1) 按 worker 记录的实例 PID 精准击杀（核心，覆盖 double-fork 的 wpsoffice）
    try:
        with open(INST_FILE % w.pid) as f:
            for p in f.read().split():
                try:
                    os.kill(int(p), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        try:
            os.remove(INST_FILE % w.pid)
        except OSError:
            pass
    except FileNotFoundError:
        pass
    # 2) worker 进程本身（killpg 覆盖其进程组内残留，不误伤 API 主进程）
    try:
        pgid = os.getpgid(w.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        w.proc.join(3)
    except Exception:
        pass


def create_worker(ext: str) -> Worker:
    """新建一个 worker 子进程（status=busy，因 warmup 未完成）。"""
    in_q = MP_CTX.Queue()
    out_q = MP_CTX.Queue()
    p = MP_CTX.Process(target=worker_main, args=(ext, in_q, out_q), daemon=True)
    p.start()
    w = Worker(type=ext, status="busy", pid=p.pid, in_q=in_q, out_q=out_q, proc=p)
    WORKERS.append(w)
    return w


def _reap_dead():
    """回收已退出（warmup 失败/被杀）的 worker，避免死进程残留在池中。
    调用方需持有 _pool_lock。"""
    dead = [w for w in WORKERS if not w.proc.is_alive()]
    for w in dead:
        WORKERS.remove(w)


async def acquire(ext: str) -> Worker:
    """取一个同类型 idle 且存活的 worker 复用（取出即标记 busy 防抢同一实例），
    否则新建冷启动。该临界区需原子：查找 + 标记 + 新建都在锁内。"""
    async with _pool_lock:
        _reap_dead()
        for w in WORKERS:
            if w.type == ext and w.status == "idle" and w.proc.is_alive():
                w.status = "busy"
                return w
        return create_worker(ext)


def _await_result(out_q: multiprocessing.Queue, timeout: int):
    """在 worker 专用 out_q 上取结果，带超时循环。
    worker 挂死/崩溃则不返回直到超时；返回 result dict（含 ok/id），永不返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline + 0.5:
        try:
            return out_q.get(timeout=1)
        except queue.Empty:
            continue
    return {"id": None, "ok": False, "error": "WORKER_TIMEOUT"}


async def dispatch(worker: Worker, task: dict, timeout: int):
    loop = asyncio.get_running_loop()
    worker.in_q.put(task)
    return await loop.run_in_executor(_executor, _await_result, worker.out_q, timeout)


async def remove_worker(w: Worker):
    async with _pool_lock:
        if w in WORKERS:
            WORKERS.remove(w)


async def release_or_kill(worker: Worker, ext: str):
    """成功回收：同类型 idle 且存活数 ≥ SPARE_PER_TYPE 则干掉当前，否则留作 idle 备用。
    注意：必须只统计存活的 idle（死进程不能当备用），且先回收池中死进程。"""
    async with _pool_lock:
        _reap_dead()
        others = [w for w in WORKERS
                  if w.type == ext and w.status == "idle" and w.proc.is_alive()
                  and w is not worker]
        if len(others) >= SPARE_PER_TYPE:
            kill_worker(worker)
            WORKERS.remove(worker)
        else:
            worker.status = "idle"


@app.on_event("startup")
async def on_startup():
    os.makedirs(WORK, exist_ok=True)
    # 预热：每类型保留 SPARE_PER_TYPE 个 idle 备用 worker，减少常规负载的冷启动。
    # warmup 在 worker 进程内异步进行，标记 idle 后立即可被复用（首个任务会等 warmup）。
    # 错开 2s 创建：避免多个 worker 同时冷启动撞竞态（实验证实并发冷启动有 E_FAIL/挂死风险）。
    for ext in ("wps", "wpp", "et"):
        for _ in range(SPARE_PER_TYPE):
            w = create_worker(ext)
            w.status = "idle"
            if SPARE_PER_TYPE > 1 or ext != "et":
                await asyncio.sleep(2)


@app.on_event("shutdown")
async def on_shutdown():
    async with _pool_lock:
        ws = list(WORKERS)
    for w in ws:
        kill_worker(w)
    _executor.shutdown(wait=False)


import atexit
@atexit.register
def _cleanup_atexit():
    for w in list(WORKERS):
        try:
            kill_worker(w)
        except Exception:
            pass


@app.get("/health")
async def health():
    async with _pool_lock:
        _reap_dead()
        by_type = {}
        for w in WORKERS:
            d = by_type.setdefault(w.type, {"busy": 0, "idle": 0, "dead": 0})
            if not w.proc.is_alive():
                d["dead"] += 1
            else:
                d[w.status] = d.get(w.status, 0) + 1
    return {"status": "ok", "workers": by_type, "spare_per_type": SPARE_PER_TYPE}


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

        timeout = task_timeout_for(ext)
        worker = await acquire(module)
        ok_holder = {"ok": False}
        try:
            task = {"id": str(uuid.uuid4()), "src": src, "out": out}
            result = await dispatch(worker, task, timeout)
            if not result["ok"]:
                if result.get("error") == "WORKER_TIMEOUT":
                    raise HTTPException(
                        status_code=504,
                        detail=f"转换超时（>{timeout}s），疑似 WPS 无法导出该格式"
                                f"（已知 .odp 会挂死），请改用 .pptx/.docx/.xlsx 等格式",
                    )
                raise HTTPException(status_code=500, detail=result.get("error", "转换失败"))
            ok_holder["ok"] = True
            pdf = result["pdf"]
            print(f"[ok] {file.filename} -> {len(pdf)}B", flush=True)
            return Response(
                content=pdf,
                media_type="application/pdf",
                headers={"Content-Disposition": 'attachment; filename="output.pdf"'},
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            if not ok_holder["ok"]:
                # 失败（超时/worker 错误）：本 worker 可能已挂死或实例损坏 -> 干掉
                kill_worker(worker)
                await remove_worker(worker)
            else:
                # 成功：按 spare 策略保留或回收
                await release_or_kill(worker, module)
            shutil.rmtree(workdir, ignore_errors=True)
    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
