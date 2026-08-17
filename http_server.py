#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pywpsrpc HTTP 转换服务：FastAPI 调度 + worker 子进程池（每进程一个 WPS 实例）。

弹性模型：请求创建 worker → 用完保留 1 个 idle（SPARE 定死 1）→ 超限排队。
环境变量：HOST/PORT MAX_FILE_MB TASK_TIMEOUT MAX_WORKERS QUEUE_TIMEOUT
"""
import asyncio
import atexit
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

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import Response

from rpcengine import MODULE_BY_EXT, SUPPORTED_EXTS
from worker import worker_main, INST_FILE

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "50"))
MAX_FILE = MAX_FILE_MB * 1024 * 1024
WORK = "/tmp/http_conv"
os.makedirs(WORK, exist_ok=True)
SPARE_PER_TYPE = 1  # 每类型保留 idle 数：0 崩吞吐、>1 收益低，定死 1
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", "180"))  # 转换超时秒（兜底挂死）
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "3"))      # 全局存活 worker 上限，0=不限制
QUEUE_TIMEOUT = int(os.environ.get("QUEUE_TIMEOUT", "60"))  # 池满排队上限，超时 503
# 已知 WPS 导入挂死的格式（实测 odp/ods 挂死；uop 与 odp 同族高风险），入口直接拒绝
REJECTED_EXTS = {"odp", "ods", "uop"}

MP_CTX = multiprocessing.get_context("fork")
# 阻塞读 worker out_q 的线程池（multiprocessing.Queue.get 不能进事件循环）
_executor = ThreadPoolExecutor(max_workers=64, thread_name_prefix="dispatch")

app = FastAPI(title="wps-docx2pdf API", version="1.6.1")


@dataclass
class Worker:
    type: str                       # "wps" | "wpp" | "et"
    status: str                     # "busy" | "idle"
    in_q: multiprocessing.Queue    # 主->worker 任务投递
    out_q: multiprocessing.Queue   # worker->主 结果回传
    proc: multiprocessing.Process  # pid 取 w.proc.pid


WORKERS: list[Worker] = []
_pool_lock = asyncio.Lock()
# 额度信号量：计数 = 可新建的 worker 数（存活 worker ≤ MAX_WORKERS）
_sem = asyncio.Semaphore(MAX_WORKERS) if MAX_WORKERS > 0 else None
# 条件变量：worker 变 idle / 额度释放时 notify 唤醒排队者。
# 必须用 Condition 而非纯信号量——idle 不释放额度，排队者只能靠"出现 idle"唤醒。
_cond = asyncio.Condition(_pool_lock)


def _release_slot():
    """归还一个 worker 额度（worker 销毁/死亡时调用）。"""
    if _sem is not None:
        _sem.release()


_POOL_FULL_MSG = f"worker 池已满({MAX_WORKERS})，排队超 {QUEUE_TIMEOUT}s"


def _take_idle(ext: str):
    """取一个同类型 idle 且存活的 worker 并标记 busy；无则 None。调用方需持锁。"""
    for w in WORKERS:
        if w.type == ext and w.status == "idle" and w.proc.is_alive():
            w.status = "busy"
            return w
    return None


def kill_worker(w: Worker):
    """按记录 PID 杀 WPS 实例 + killpg 杀 worker。

    WPS 会 double-fork 守护进程化（实例脱离进程树），killpg 打不到它，
    实例 PID 由 worker 冷启动后记录在 INST_FILE，这里按 PID 精准击杀。"""
    inst_file = INST_FILE % w.proc.pid
    try:
        with open(inst_file) as f:
            for p in f.read().split():
                try:
                    os.kill(int(p), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        os.remove(inst_file)
    except FileNotFoundError:
        pass
    try:
        pgid = os.getpgid(w.proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        w.proc.join(3)
    except Exception:
        pass


def create_worker(ext: str) -> Worker:
    """新建 worker 子进程（status=busy，warmup 未完成）。"""
    in_q = MP_CTX.Queue()
    out_q = MP_CTX.Queue()
    p = MP_CTX.Process(target=worker_main, args=(ext, in_q, out_q), daemon=True)
    p.start()
    w = Worker(type=ext, status="busy", in_q=in_q, out_q=out_q, proc=p)
    WORKERS.append(w)
    return w


def _reap_dead():
    """移除已退出的 worker 并归还额度。调用方需持有 _pool_lock。"""
    dead = [w for w in WORKERS if not w.proc.is_alive()]
    for w in dead:
        WORKERS.remove(w)
        _release_slot()


async def acquire(ext: str) -> Worker:
    """取同类型 idle worker 复用，否则申请额度新建；池满排队（QUEUE_TIMEOUT 超时 503）。
    额度语义 = 存活 worker 数 ≤ MAX_WORKERS（idle 复用不新增）。"""
    deadline = time.time() + QUEUE_TIMEOUT
    async with _cond:
        while True:
            _reap_dead()
            if w := _take_idle(ext):
                return w
            # 无 idle → 拿额度新建（MAX=0 时 _sem 为 None，直接新建）
            if _sem is None or not _sem.locked():
                if _sem is not None:
                    await _sem.acquire()
                if w := _take_idle(ext):  # 等待期间可能刚出现 idle，命中则归还额度
                    _release_slot()
                    return w
                try:
                    return create_worker(ext)
                except Exception:
                    _release_slot()
                    raise
            # 无 idle 且无额度 → 池满。若额度被他类型 idle 占满（如 3 类型各 1 idle = MAX=3），
            # 牺牲一个他类型 idle 腾额度（备用缓存可重建，~0.3s 冷启动），否则同类型并发
            # 请求会被锁死排队（idle 不释放额度，只能等第一个完成 → 假串行）
            if _sem is not None and _sem.locked():
                victim = next((w for w in WORKERS
                               if w.type != ext and w.status == "idle" and w.proc.is_alive()),
                              None)
                if victim is not None:
                    kill_worker(victim)
                    WORKERS.remove(victim)
                    _release_slot()
                    continue
            remaining = deadline - time.time()
            if remaining <= 0:
                raise HTTPException(status_code=503, detail=_POOL_FULL_MSG)
            try:
                await asyncio.wait_for(_cond.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                raise HTTPException(status_code=503, detail=_POOL_FULL_MSG)


def _await_result(out_q: multiprocessing.Queue, timeout: int):
    """阻塞读 worker 结果，带超时循环（超时返回 WORKER_TIMEOUT，永不返回 None）。"""
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
            _release_slot()
            _cond.notify_all()


async def release_or_kill(worker: Worker, ext: str):
    """成功回收：同类型已有 idle 则杀当前，否则留作 idle 备用。"""
    async with _pool_lock:
        _reap_dead()
        others = [w for w in WORKERS
                  if w.type == ext and w.status == "idle" and w.proc.is_alive()
                  and w is not worker]
        if others:
            kill_worker(worker)
            WORKERS.remove(worker)
            _release_slot()
        else:
            worker.status = "idle"
        _cond.notify_all()  # 唤醒 acquire 排队者（idle 出现或额度释放）


@app.on_event("shutdown")
async def on_shutdown():
    async with _pool_lock:
        ws = list(WORKERS)
    for w in ws:
        kill_worker(w)
    _executor.shutdown(wait=False)


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
        _cond.notify_all()  # 死 worker 移除释放了额度，通知排队者
        by_type = {}
        for w in WORKERS:
            d = by_type.setdefault(w.type, {"busy": 0, "idle": 0, "dead": 0})
            if not w.proc.is_alive():
                d["dead"] += 1
            else:
                d[w.status] = d.get(w.status, 0) + 1
    return {"status": "ok", "workers": by_type,
            "total": len(WORKERS), "max_workers": MAX_WORKERS}


async def _save_upload(file: UploadFile, src: str):
    """流式写盘 + 边写边限流（避免大文件整份读进内存、超限尽早中断）。"""
    size = 0
    with open(src, "wb") as f:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE:
                raise HTTPException(status_code=413,
                                    detail=f"文件超过 {MAX_FILE_MB}MB 上限")
            f.write(chunk)
    if size == 0:
        raise HTTPException(status_code=400, detail="空文件")


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
    if ext in REJECTED_EXTS:
        raise HTTPException(
            status_code=415,
            detail=f".{ext} 已知无法转换（WPS 导入该格式会挂死），请转存为 docx/pptx/xlsx",
        )

    workdir = tempfile.mkdtemp(prefix="conv_", dir=WORK)
    src = os.path.join(workdir, "input" + os.path.splitext(file.filename)[1].lower())
    out = os.path.join(workdir, "output.pdf")
    try:
        await _save_upload(file, src)

        timeout = TASK_TIMEOUT
        worker = await acquire(module)
        succeeded = False
        try:
            task = {"id": str(uuid.uuid4()), "src": src, "out": out}
            result = await dispatch(worker, task, timeout)
            if not result["ok"]:
                if result.get("error") == "WORKER_TIMEOUT":
                    raise HTTPException(
                        status_code=504,
                        detail=f"转换超时（>{timeout}s），疑似 WPS 无法导出该格式，"
                                f"请改用 .docx/.pptx/.xlsx 等格式",
                    )
                raise HTTPException(status_code=500, detail=result.get("error", "转换失败"))
            pdf = result["pdf"]
            print(f"[ok] {file.filename} -> {len(pdf)}B", flush=True)
            succeeded = True
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
            if succeeded:
                await release_or_kill(worker, module)
            else:
                # 失败（超时/worker 错误）：worker 可能已挂死或实例损坏，干掉
                kill_worker(worker)
                await remove_worker(worker)
            shutil.rmtree(workdir, ignore_errors=True)
    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
