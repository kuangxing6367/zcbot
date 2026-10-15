# -*- coding: utf-8 -*-
"""延迟探针：量 事件(OneBot)→send_msg(回 OneBot) 的各段耗时。"""
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PYSIDE = os.path.dirname(ROOT)
sys.path.insert(0, HERE)
from onebot_fake import FakeOneBot, group_msg  # noqa: E402
from adapter_e2e import free_port, wait_tcp  # noqa: E402

KERNEL_EXE = os.path.join(ROOT, "go_kernel", "zcbot_kernel.exe")
# 协议端 exe：优先 iipc/rust_adapter（源码自带），回退旧布局 core_plugins/
RUST_EXE = next(
    (c for c in (
        os.path.join(ROOT, "rust_adapter", "target", "release", "zcbot_adapter.exe"),
        os.path.join(PYSIDE, "core_plugins", "IIPC_onebot_adapter",
                     "target", "release", "zcbot_adapter.exe"),
    ) if os.path.exists(c)),
    os.path.join(ROOT, "rust_adapter", "target", "release", "zcbot_adapter.exe"))

ipc_port, ob_port = free_port(), free_port()
tmp = tempfile.mkdtemp(prefix="zcbot_probe_")
env = dict(os.environ)
env["ZCBOT_PYTHON"] = sys.executable
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"

klog = open(os.path.join(tmp, "kernel.log"), "w", encoding="utf-8")
kernel = subprocess.Popen(
    [KERNEL_EXE, "--plugins-dir", os.path.join(ROOT, "plugins"),
     "--ipc-addr", "127.0.0.1:%d" % ipc_port, "--db", ":memory:",
     "--data-dir", os.path.join(tmp, "plugins_dat"), "--admin", "10099"],
    stdout=klog, stderr=subprocess.STDOUT, env=env)
rlog = open(os.path.join(tmp, "rust.log"), "w", encoding="utf-8")
rust = subprocess.Popen(
    [RUST_EXE, "--addr", "127.0.0.1:%d" % ipc_port, "--self-id", "rustbot",
     "--onebot-url", "ws://127.0.0.1:%d" % ob_port],
    stdout=rlog, stderr=subprocess.STDOUT, env=env)
ob = None
try:
    assert wait_tcp(ipc_port, 15), "kernel not ready"
    ob = FakeOneBot(port=ob_port).start()
    assert ob.connected.wait(15), "ws handshake timeout"
    print("[probe] 链路就绪，等插件注册 6s")
    time.sleep(6)
    t0 = time.time()
    ob.push_event(group_msg(666, 10001, "/ping"))
    got = None
    while time.time() - t0 < 20:
        try:
            v = ob.api_q.get(timeout=0.05)
        except Exception:
            continue
        el = time.time() - t0
        print("[probe] +%6.2fs api.call action=%s params=%s"
              % (el, v.get("action"), v.get("params")))
        if v.get("action") == "send_msg":
            got = el
            # 收完 pong 后再等 1s 看还有没有 echo:
            time.sleep(1)
            while True:
                try:
                    v2 = ob.api_q.get(timeout=0.2)
                except Exception:
                    break
                print("[probe] +%6.2fs api.call action=%s params=%s"
                      % (time.time() - t0, v2.get("action"), v2.get("params")))
            break
    print("[probe] 首条 send_msg 延迟: %s" % (("%.2fs" % got) if got else "超时"))
finally:
    if ob:
        ob.close()
    for p in (rust, kernel):
        if p and p.poll() is None:
            p.terminate()
    time.sleep(0.3)
    for p in (rust, kernel):
        if p and p.poll() is None:
            p.kill()
    klog.close()
    rlog.close()
    print("[probe] 日志目录: %s" % tmp)
