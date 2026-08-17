#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""worker 子进程：持有某组件（wps/wpp/et）的常驻 WPS 实例，串行处理自身任务。

主进程通过 in_q 投递任务、out_q 读结果；worker 自成会话（killpg 不误伤主进程）。
WPS 实例会 double-fork 脱离进程树，故冷启动后按 /proc 快照 diff 记录实例 PID
到 INST_FILE，退出/被强杀时按 PID 精准击杀，避免 ~380MB/实例 的泄漏。
"""
import os
import queue
import multiprocessing

from rpcengine import RpcEngine

INST_FILE = "/tmp/wps_inst_%d.txt"  # %d = worker pid，记录其 WPS 实例 PID


def _persist_inst(engine):
    """把本 worker 的 WPS 实例 PID 落到文件，供 API 强杀时精准清理。"""
    try:
        with open(INST_FILE % os.getpid(), "w") as f:
            f.write(" ".join(str(p) for p in engine._inst_pids))
    except Exception:
        pass


def worker_main(ext: str, in_q: multiprocessing.Queue, out_q: multiprocessing.Queue):
    try:
        os.setsid()
    except Exception:
        pass
    parent_pid = os.getppid()

    engine = RpcEngine(ext)
    try:
        engine.warmup()
    except Exception as e:
        # 冷启动彻底失败：退出，主进程会因收不到结果而超时清理
        print(f"[worker:{ext}] warmup 失败，退出: {e}", flush=True)
        return

    _persist_inst(engine)

    while True:
        if os.getppid() != parent_pid:  # 主进程已退出 -> 自清理
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
            try:
                out_q.put({"id": task_id, "ok": False, "error": str(e)})
            except Exception:
                break  # 主进程已死（BrokenPipe）：退出走实例清理，避免泄漏
        _persist_inst(engine)  # convert 中途重建会换实例，刷新记录

    try:
        engine.kill_instances()  # 按记录 PID 清理 WPS 实例（脱离进程树，必须按 PID）
    except Exception:
        pass
    try:
        os.remove(INST_FILE % os.getpid())
    except OSError:
        pass
    os._exit(0)
