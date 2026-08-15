#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻 WPS 实例连续转换压测：验证 HTTP 常驻服务可行性
用法: python3 stress_test.py <input.docx>
行为: 初始化一次 getWpsApplication，连续转换 10 次（Open→SaveAs2→Close）
      每次打印耗时/内存/结果大小，最后统计失败率与内存增长。
"""
import sys, os, time, resource

os.environ.setdefault("DISPLAY", ":99")
os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root")
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/runtime-root/dbus")

from pywpsrpc.rpcwpsapi import createWpsRpcInstance, wpsapi
from pywpsrpc.common import S_OK, QtApp

N = 10  # 转换次数
src = sys.argv[1] if len(sys.argv) > 1 else "/data/input.docx"
outdir = "/tmp/stress_out"
os.makedirs(outdir, exist_ok=True)

def rss_mb():
    """当前进程 RSS (MB)"""
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    return 0.0

def main():
    qApp = QtApp(sys.argv)
    hr, rpc = createWpsRpcInstance()
    if hr != S_OK:
        print(f"FAIL createWpsRpcInstance: 0x{hr & 0xFFFFFFFF:08X}", flush=True)
        return 1
    rpc.setStartTimeout(15000000)

    hr, app = rpc.getWpsApplication()
    if hr != S_OK:
        print(f"FAIL getWpsApplication: 0x{hr & 0xFFFFFFFF:08X}", flush=True)
        return 1
    print(f"[init] WPS 实例就绪, RSS={rss_mb():.0f}MB", flush=True)

    hr, docs = app.get_Documents()
    if hr != S_OK:
        print(f"FAIL get_Documents: 0x{hr & 0xFFFFFFFF:08X}", flush=True)
        return 1

    ok_count = 0
    fail_msgs = []
    rss_before = rss_mb()
    for i in range(1, N + 1):
        t0 = time.time()
        out = os.path.join(outdir, f"out_{i:02d}.pdf")
        try:
            hr, doc = docs.Open(src)
            if hr != S_OK:
                fail_msgs.append(f"#{i} Open: 0x{hr & 0xFFFFFFFF:08X}")
                continue
            hr = doc.SaveAs2(out, FileFormat=wpsapi.wdFormatPDF)
            if hr != S_OK:
                fail_msgs.append(f"#{i} SaveAs2: 0x{hr & 0xFFFFFFFF:08X}")
            else:
                sz = os.path.getsize(out)
                if sz > 0:
                    ok_count += 1
            try:
                doc.Close(False)
            except Exception:
                pass
        except Exception as e:
            fail_msgs.append(f"#{i} 异常: {e}")
        dt = time.time() - t0
        print(f"[{i:02d}/{N}] {dt:.2f}s  RSS={rss_mb():.0f}MB  out={out}", flush=True)

    rss_after = rss_mb()
    print("=" * 50, flush=True)
    print(f"成功: {ok_count}/{N}", flush=True)
    print(f"RSS: {rss_before:.0f}MB → {rss_after:.0f}MB (增长 {rss_after - rss_before:.0f}MB)", flush=True)
    if fail_msgs:
        print("失败详情:")
        for m in fail_msgs:
            print("  ", m)
    # 用 os._exit 跳过清理段错误
    os._exit(0 if ok_count == N else 1)

if __name__ == "__main__":
    main()
