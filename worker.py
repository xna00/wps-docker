#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""worker 子进程：持有某组件（wps/wpp/et）的常驻 WPS 实例，单实例串行处理自身任务。

与主进程（FastAPI）的关系：
  - 主进程通过 in_q 投递任务、从 out_q 读取结果（每 worker 独立一对队列）
  - worker 进程内绝不共享 WPS 实例对象（跨进程不可行），只在自己的进程里持有一个实例
  - os.setsid() 让 worker 自成会话/进程组
  - 父进程（API）退出后，worker 自检 ppid 变化并自清理，避免孤儿 WPS 进程堆积

WPS 实例清理（防泄漏）：
  - WPS 会 double-fork 守护进程化，实际 wpsoffice 实例的 ppid 很快变 1（init），
    既不在 worker 进程树里，也收不到 killpg——因此 worker 在冷启动后用
    rpc.getProcessPid() 拿到"自己拉起的实例 PID"，写入 /tmp/wps_inst_<pid>.txt，
    并在退出时按记录精准击杀；API 在回收/超时强杀该 worker 时也会读取此文件补刀，
    确保每次用完即焚、零泄漏。
"""
import os
import queue
import multiprocessing

from rpcengine import RpcEngine

INST_FILE = "/tmp/wps_inst_%d.txt"  # %d = worker pid，记录其 WPS 实例 PID


def _persist_inst(engine):
    """把本 worker 的 WPS 实例 PID 落到文件，供 API 强杀时精准清理（WPS 已脱离进程树）。"""
    try:
        with open(INST_FILE % os.getpid(), "w") as f:
            f.write(" ".join(str(p) for p in engine._inst_pids))
    except Exception:
        pass


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

    # 冷启动成功，记录本 worker 拉起的 WPS 实例 PID
    _persist_inst(engine)

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
        # 任务后刷新实例 PID 记录（convert 中途重建会换实例）
        _persist_inst(engine)

    # 进程退出：按记录的实例 PID 精准清理本 worker 的 wpsoffice 残留（WPS 已脱离
    # 进程组，单纯随进程销毁 / killpg 清不掉，会泄漏 ~380MB/个），再退出自身。
    try:
        engine.kill_instances()
    except Exception:
        pass
    try:
        os.remove(INST_FILE % os.getpid())
    except OSError:
        pass
    os._exit(0)
