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
  - 成功回收：同类型已有 idle 备用则干掉当前（回收），否则留作 idle 备用。
    备用数定死为 1（实测：0 会让持续负载每个请求冷启动、吞吐崩 2.7 倍；>1 收益 ~10%
    内存却翻倍——1 是唯一合理值，故不做配置）。无需 rebuild / watchdog / heartbeat。

环境变量：
    HOST / PORT        监听地址（默认 0.0.0.0:8080）
    MAX_FILE_MB        上传大小上限（默认 50MB）
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

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import Response

from rpcengine import MODULE_BY_EXT, SUPPORTED_EXTS
from worker import worker_main, INST_FILE

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "50"))
MAX_FILE = MAX_FILE_MB * 1024 * 1024
WORK = "/tmp/http_conv"
SPARE_PER_TYPE = 1  # 每类型保留的 idle 备用数，定死 1（0 崩吞吐、>1 收益低，详见模块注释）
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", "180"))
ODP_TIMEOUT = int(os.environ.get("ODP_TIMEOUT", "30"))
# 全局最大存活 worker 数（含 idle 备用）——内存预算护栏，防突发并发打爆内存。
# 0 = 不限制；超限时请求排队等额度，QUEUE_TIMEOUT 秒内等不到则 503。
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "3"))
QUEUE_TIMEOUT = int(os.environ.get("QUEUE_TIMEOUT", "60"))

MP_CTX = multiprocessing.get_context("fork")
# 阻塞读取 worker out_q 用的线程池（避免阻塞事件循环）；默认转换很快，线程迅速释放
_executor = ThreadPoolExecutor(max_workers=64, thread_name_prefix="dispatch")

app = FastAPI(title="wps-docx2pdf API", version="1.6.1")


@dataclass
class Worker:
    type: str                       # "wps" | "wpp" | "et"
    status: str                     # "busy" | "idle"
    in_q: multiprocessing.Queue    # 主->worker 任务投递
    out_q: multiprocessing.Queue   # worker->主 结果回传
    proc: multiprocessing.Process  # pid 取 w.proc.pid（is_alive/join 用官方封装）


WORKERS: list[Worker] = []
_pool_lock = asyncio.Lock()
# 额度信号量：计数 = 还可新建的 worker 数；acquire 新建时消耗、worker 销毁时归还。
# 语义 = 「池中存活 worker 数 ≤ MAX_WORKERS」。
_sem = asyncio.Semaphore(MAX_WORKERS) if MAX_WORKERS > 0 else None
# 条件变量（绑定池锁）：worker 变 idle / 额度释放时 notify，唤醒 acquire 排队者。
# 必须用它而不是纯信号量——否则 MAX≤SPARE 时唯一 worker 保留 idle 不释放额度，
# 排队者永远等不到额度（死等 503）。
_cond = asyncio.Condition(_pool_lock)


def _release_slot():
    """归还一个 worker 额度（worker 销毁/死亡时调用，避免额度泄漏）。"""
    if _sem is not None:
        _sem.release()


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
        with open(INST_FILE % w.proc.pid) as f:
            for p in f.read().split():
                try:
                    os.kill(int(p), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        try:
            os.remove(INST_FILE % w.proc.pid)
        except OSError:
            pass
    except FileNotFoundError:
        pass
    # 2) worker 进程本身（killpg 覆盖其进程组内残留，不误伤 API 主进程）
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
    """新建一个 worker 子进程（status=busy，因 warmup 未完成）。"""
    in_q = MP_CTX.Queue()
    out_q = MP_CTX.Queue()
    p = MP_CTX.Process(target=worker_main, args=(ext, in_q, out_q), daemon=True)
    p.start()
    w = Worker(type=ext, status="busy", in_q=in_q, out_q=out_q, proc=p)
    WORKERS.append(w)
    return w


def _reap_dead():
    """回收已退出（warmup 失败/被杀）的 worker，避免死进程残留在池中。
    调用方需持有 _pool_lock。被移除的死 worker 归还额度。"""
    dead = [w for w in WORKERS if not w.proc.is_alive()]
    for w in dead:
        WORKERS.remove(w)
        _release_slot()


async def acquire(ext: str) -> Worker:
    """取一个同类型 idle 且存活的 worker 复用（取出即标记 busy 防抢同一实例），
    否则申请额度并新建冷启动。池满（≥MAX_WORKERS）时在条件变量上排队，
    worker 变 idle / 额度释放都会被唤醒，QUEUE_TIMEOUT 超时抛 503。
    额度语义 = 池中存活 worker 数 ≤ MAX_WORKERS（idle 复用不新增）。"""
    deadline = time.time() + QUEUE_TIMEOUT
    async with _cond:
        while True:
            # 1) 先找可复用 idle（不消耗新额度）
            _reap_dead()
            for w in WORKERS:
                if w.type == ext and w.status == "idle" and w.proc.is_alive():
                    w.status = "busy"
                    return w
            # 2) 无 idle → 尝试拿额度（非阻塞；没有名额则等待被唤醒）
            #    注意 MAX_WORKERS=0（不限制）时 _sem 为 None，直接走"新建"路径。
            if _sem is None or not _sem.locked():
                if _sem is not None:
                    await _sem.acquire()
                # 拿到额度后再查一次 idle（等待期间可能刚出现空闲 worker，命中则归还额度）
                for w in WORKERS:
                    if w.type == ext and w.status == "idle" and w.proc.is_alive():
                        w.status = "busy"
                        _release_slot()
                        return w
                try:
                    return create_worker(ext)
                except Exception:
                    _release_slot()
                    raise
            # 3) 无 idle 且无额度 → 等 worker 状态变化（带超时）
            remaining = deadline - time.time()
            if remaining <= 0:
                raise HTTPException(
                    status_code=503,
                    detail=f"worker 池已满({MAX_WORKERS})，排队超 {QUEUE_TIMEOUT}s",
                )
            try:
                await asyncio.wait_for(_cond.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                raise HTTPException(
                    status_code=503,
                    detail=f"worker 池已满({MAX_WORKERS})，排队超 {QUEUE_TIMEOUT}s",
                )


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
            _release_slot()
            _cond.notify_all()


async def release_or_kill(worker: Worker, ext: str):
    """成功回收：同类型已有 idle 备用（存活）则干掉当前，否则留作 idle 备用。
    注意：必须只统计存活的 idle（死进程不能当备用），且先回收池中死进程。"""
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
        # 唤醒 acquire 排队者（idle 出现或额度释放都可能让其前进）
        _cond.notify_all()


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
        # 死 worker 移除释放了额度，通知 acquire 排队者（否则可能白等 503）
        _cond.notify_all()
        by_type = {}
        for w in WORKERS:
            d = by_type.setdefault(w.type, {"busy": 0, "idle": 0, "dead": 0})
            if not w.proc.is_alive():
                d["dead"] += 1
            else:
                d[w.status] = d.get(w.status, 0) + 1
    return {"status": "ok", "workers": by_type,
            "total": len(WORKERS), "max_workers": MAX_WORKERS}


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
