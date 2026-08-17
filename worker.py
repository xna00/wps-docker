#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""worker 子进程：持有某组件（wps/wpp/et）的常驻 WPS 实例，单实例串行处理自身任务。

与主进程（FastAPI）的关系：
  - 主进程通过 in_q 投递任务、从 out_q 读取结果（每 worker 独立一对队列）
  - worker 进程内绝不共享 WPS 实例对象（跨进程不可行），只在自己的进程里持有一个实例
  - os.setsid() 让 worker 自成会话/进程组，主进程 killpg 时只清掉本 worker 子树
    （含其拉起的 wpsoffice 残留），不会误伤 API 主进程
  - 父进程（API）退出后，worker 自检 ppid 变化并自清理，避免孤儿 WPS 进程堆积
"""
import os
import shutil
import signal
import queue
import multiprocessing

from rpcengine import RpcEngine


def worker_main(ext: str, in_q: multiprocessing.Queue, out_q: multiprocessing.Queue):
    # 脱离父进程会话，确保 killpg 只作用于本 worker 子树（不误伤 API 主进程）
    try:
        os.setsid()
    except Exception:
        pass
    parent_pid = os.getppid()

    engine = RpcEngine(ext)
    try:
        engine.warmup()
    except Exception as e:
        # 冷启动彻底失败：本 worker 不可用，直接退出。
        # 主进程会因收不到结果而超时，随后 kill（已退出，noop）并从池中移除。
        print(f"[worker:{ext}] warmup 失败，退出: {e}", flush=True)
        return

    while True:
        # 父进程已退出 -> 自清理（避免孤儿 WPS 进程）
        if os.getppid() != parent_pid:
            break
        try:
            task = in_q.get(timeout=1)
        except queue.Empty:
            continue
        if task is None:  # 哨兵：显式退出
            break

        task_id = task["id"]
        src = task["src"]
        out = task["out"]
        try:
            engine.convert(src, out)
            with open(out, "rb") as f:
                pdf = f.read()
            out_q.put({"id": task_id, "ok": True, "pdf": pdf, "bytes": len(pdf)})
        except Exception as e:
            out_q.put({"id": task_id, "ok": False, "error": str(e)})

    # 进程退出：WPS 实例随进程销毁（含其拉起的 wpsoffice 子进程）
    os._exit(0)
