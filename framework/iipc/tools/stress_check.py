# -*- coding: utf-8 -*-
"""压力 / 故障注入 / 异常路径验收（tools/stress_check.py）

e2e_check.py 与 adapter_e2e.py 全是"happy path 功能断言"（自证式：我的实现
对着我的断言）。本脚本补的是此前**完全没做过**的一类：让系统挨打，看它
会不会死、死了能不能自己爬起来。

  A. 畸形输入    非 JSON / 空帧 / 无 post_type / 64KB 超长 / 无 echo 响应
                 → 适配器与内核不得崩，且之后链路仍可用
  B. 吞吐与延迟  连发 100 条消息：丢失率 + P50/P95/P99
  C. OneBot 掉线 服务端主动断连 → 适配器 2s 重连策略 → 链路恢复可用
  D. 插件被杀    kill 插件进程 → 内核 plugin_mgr 自动重启 → 命令仍可用
  E. 内核异常路径 未知 method / 坏 SQL → 返回 error 而非崩溃，后续调用仍成功

用法：
    python tools/stress_check.py
退出码 0 = 全部通过。
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
IIPC = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(IIPC, "py_sdk"))

from onebot_fake import FakeOneBot, group_msg, ws_write_frame, OP_TEXT  # noqa: E402
from iipc_client import IIPCClient  # noqa: E402

KERNEL_EXE = os.path.join(IIPC, "go_kernel", "zcbot_kernel.exe")
RUST_EXE = os.path.join(IIPC, "rust_adapter", "target", "release",
                        "zcbot_adapter.exe")
PLUGINS_DIR = os.path.join(IIPC, "plugins")
AUTH = "dev-auth"

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       (" -> " + str(detail)) if detail else ""))
    return bool(ok)


from portutil import free_port, wait_tcp  # noqa: E402,F401  端口一律动态分配


def alive(p):
    return p is not None and p.poll() is None


def wait_api(ob, action, timeout=10):
    """等指定 action 的 API 请求。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            v = ob.api_q.get(timeout=max(0.1, deadline - time.time()))
        except Exception:
            break
        if v.get("action") == action:
            return v
    return None


def wait_send(ob, predicate, timeout=10):
    """等一条满足条件的 send_msg。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            v = ob.api_q.get(timeout=max(0.1, deadline - time.time()))
        except Exception:
            break
        if v.get("action") == "send_msg" and predicate(v.get("params", {})):
            return v.get("params", {})
    return None


def _drain_q(ob):
    while True:
        try:
            ob.api_q.get_nowait()
        except Exception:
            return


def _collect(ob, sent_at, timeout=30):
    """收集 echo 回显，返回 {tag: 收到时刻}。"""
    recv_at = {}
    deadline = time.time() + timeout
    while time.time() < deadline and len(recv_at) < len(sent_at):
        try:
            v = ob.api_q.get(timeout=max(0.1, deadline - time.time()))
        except Exception:
            break
        if v.get("action") != "send_msg":
            continue
        m = str(v.get("params", {}).get("message", ""))
        if m.startswith("echo: soak") and m[len("echo: "):] in sent_at:
            recv_at[m[len("echo: "):]] = v.get("_recv_ts", time.time())
    return recv_at


def light_load(ob, n=20, interval=0.3):
    """轻载：限速发送，队列不积压。返回 (延迟列表, 丢失数, n)。"""
    _drain_q(ob)
    sent_at = {}
    for i in range(n):
        tag = "soak%d" % i
        sent_at[tag] = time.time()
        ob.push_event(group_msg(666, 10001, tag))
        time.sleep(interval)
    recv_at = _collect(ob, sent_at, timeout=15)
    lat = sorted(recv_at[t] - sent_at[t] for t in recv_at)
    seq = [(int(t[4:]), round(recv_at[t] - sent_at[t], 3))
           for t in sorted(recv_at, key=lambda x: int(x[4:]))]
    print("  逐条延迟(序号,秒): %s" % seq[:8])
    return lat, n - len(recv_at), n


def saturate(ob, n=200):
    """饱和吞吐：不限速灌 n 条，返回 条/秒（按首尾回显时间差算稳态）。"""
    _drain_q(ob)
    sent_at = {}
    t0 = time.time()
    for i in range(n):
        tag = "soak%d" % i
        sent_at[tag] = time.time()
        ob.push_event(group_msg(666, 10001, tag))
    recv_at = _collect(ob, sent_at, timeout=60)
    if len(recv_at) < 2:
        return 0.0
    # 端到端口径：第一条"发出" → 最后一条"回复到达"。
    # 不能用首尾回复的时间差——发送侧 5ms 批量 flush 会把多条合并，
    # 那样算出来的是"产出速率"而非"系统吞吐"，会虚高一个量级。
    span = max(recv_at.values()) - t0
    return (len(recv_at) / span if span > 0 else 0.0), len(recv_at), span


def kernel_pids(klog_path, plugin="echo"):
    """从内核日志解析插件进程 pid（取最后一次拉起）。"""
    pids = []
    try:
        with open(klog_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                mark = "已拉起插件进程 %s (pid=" % plugin
                if mark in line:
                    try:
                        pids.append(int(line.split(mark)[1].split(")")[0]))
                    except Exception:
                        pass
    except Exception:
        pass
    return pids


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--soak-n", type=int, default=200,
                    help="饱和吞吐样本数（默认 200；加大可看是否线性）")
    a = ap.parse_args()

    for exe, what in ((KERNEL_EXE, "内核"), (RUST_EXE, "Rust 协议端")):
        if not os.path.exists(exe):
            print("缺少 %s: %s（先跑 run_all_bench.py 构建）" % (what, exe))
            return 2

    ipc_port = free_port()
    ob_port = free_port()
    tmp = tempfile.mkdtemp(prefix="zcbot_stress_")
    print("IPC=%d OneBotWS=%d tmp=%s" % (ipc_port, ob_port, tmp))

    env = dict(os.environ)
    env["ZCBOT_PYTHON"] = sys.executable
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    klog_path = os.path.join(tmp, "kernel.log")
    klog = open(klog_path, "w", encoding="utf-8")
    kernel = subprocess.Popen(
        [KERNEL_EXE, "--plugins-dir", PLUGINS_DIR, "--ipc-addr",
         "127.0.0.1:%d" % ipc_port, "--db", ":memory:",
         "--data-dir", os.path.join(tmp, "plugins_dat"), "--admin", "10099"],
        stdout=klog, stderr=subprocess.STDOUT, env=env)
    rlog = open(os.path.join(tmp, "rust.log"), "w", encoding="utf-8")
    rust = None
    ob = None
    try:
        if not wait_tcp(ipc_port, 15):
            check("内核监听", False, "IPC 端口未就绪")
            return 1
        check("内核监听", True)

        rust = subprocess.Popen(
            [RUST_EXE, "--addr", "127.0.0.1:%d" % ipc_port,
             "--self-id", "rustbot", "--onebot-url",
             "ws://127.0.0.1:%d" % ob_port],
            stdout=rlog, stderr=subprocess.STDOUT, env=env)
        ob = FakeOneBot(port=ob_port).start()
        if not ob.connected.wait(15):
            check("WS 握手", False, "适配器未连入")
            return 1
        check("WS 握手", True)
        time.sleep(4)  # 等插件进程被内核拉起并注册

        # ── A. 畸形输入 ──────────────────────────────────────────
        malformed = [
            ("非 JSON 文本", b"this is not json {{{"),
            ("空帧", b""),
            ("合法 JSON 但无 post_type", json.dumps({"foo": "bar"}).encode()),
            ("post_type 但字段残缺", json.dumps({"post_type": "message"}).encode()),
            ("64KB 超长帧", b"x" * 65536),
            ("无 echo 的响应帧", json.dumps(
                {"status": "ok", "retcode": 0, "data": {}}).encode()),
        ]
        for name, payload in malformed:
            try:
                ws_write_frame(ob.sock, OP_TEXT, payload)
            except Exception as e:
                print("  [注入 %s] 发送失败: %s" % (name, e))
            time.sleep(0.3)
        time.sleep(1.0)
        ok = alive(kernel) and alive(rust)
        check("畸形输入后内核/适配器存活", ok,
              "kernel=%s rust=%s" % (alive(kernel), alive(rust)))

        ob.push_event(group_msg(666, 10001, "/ping"))
        got = wait_send(ob, lambda p: p.get("message") == "pong", 8)
        check("畸形输入后链路仍可用（/ping → pong）", got is not None, got)

        # ── B1. 轻载延迟（队列不积压时的真实单条体验）──────────────
        lat, lost, N = light_load(ob, 20, 0.3)
        pct = lambda q: lat[min(len(lat) - 1, int(len(lat) * q))] if lat else -1
        print("  轻载: %d/%d 收到；P50=%.3fs P95=%.3fs P99=%.3fs"
              % (N - lost, N, pct(0.50), pct(0.95), pct(0.99)))
        check("轻载零丢失（%d/%d）" % (N - lost, N), lost == 0, "丢失 %d" % lost)
        check("轻载 P95 延迟 <0.5s", 0 <= pct(0.95) < 0.5, "P95=%.3fs" % pct(0.95))

        # ── B2. 饱和吞吐（不限速灌满，测稳态条/秒）────────────────
        thr, got, span = saturate(ob, a.soak_n)
        print("  饱和吞吐: %.1f 条/秒（%d/%d 收到，端到端 span=%.2fs）"
              % (thr, got, a.soak_n, span))
        check("饱和吞吐 >1000 条/秒", thr > 1000, "%.1f 条/秒" % thr)
        # 直接对抗"api.call 响应丢失 → 内核干等 15s"这类卡顿：
        # 正常 span≈0.25s，一旦丢响应就会跳到 15s 量级。
        check("突发无 15s 级卡顿（span<3s）", span < 3.0, "span=%.2fs" % span)

        # ── C. OneBot 侧掉线 → 适配器重连 ────────────────────────
        before = ob.conn_count
        ob.drop_conn()
        ok = False
        for _ in range(150):
            if ob.conn_count > before and ob.sock is not None:
                ok = True
                break
            time.sleep(0.1)
        check("OneBot 掉线后适配器自动重连", ok,
              "conn_count %d→%d" % (before, ob.conn_count))
        if ok:
            time.sleep(1.0)
            ob.push_event(group_msg(666, 10001, "/ping"))
            got = wait_send(ob, lambda p: p.get("message") == "pong", 10)
            check("重连后链路恢复（/ping → pong）", got is not None, got)

        # ── D. 插件进程被杀 → 内核重启 ────────────────────────────
        pids = kernel_pids(klog_path, "echo")
        if not pids:
            check("取到插件 pid", False, "内核日志无拉起记录")
        else:
            victim = pids[-1]
            subprocess.run(["taskkill", "/PID", str(victim), "/F"],
                           capture_output=True)
            restarted = False
            deadline = time.time() + 20
            while time.time() < deadline:
                if len(kernel_pids(klog_path, "echo")) > len(pids):
                    restarted = True
                    break
                time.sleep(0.5)
            check("插件被 kill 后内核重启插件", restarted,
                  "pid=%d 日志序列=%s" % (victim, kernel_pids(klog_path, "echo")))
            if restarted:
                time.sleep(4)
                ob.push_event(group_msg(666, 10001, "/ping"))
                got = wait_send(ob, lambda p: p.get("message") == "pong", 12)
                check("重启后命令仍可用（/ping → pong）", got is not None, got)

        # ── E. 内核异常路径 ───────────────────────────────────────
        try:
            c = IIPCClient("127.0.0.1:%d" % ipc_port, AUTH)
            c.call("adapter.register", {"self_id": "probe"})
            err = None
            try:
                c.call("no.such.method", {"x": 1}, timeout=5)
            except Exception as e:
                err = str(e)
            check("未知 method 返回 error 而非崩溃", err is not None, err)
            err = None
            try:
                c.call("db.query", {"sql": "SELECT * FROM no_such_table"},
                       timeout=5)
            except Exception as e:
                err = str(e)
            check("坏 SQL 返回 error 而非崩溃", err is not None, err)
            ok = False
            try:
                r = c.call("db.query", {"sql": "SELECT 1 AS x"}, timeout=5)
                ok = bool(r.get("rows")) if isinstance(r, dict) else r is not None
            except Exception as e:
                print("  正常查询异常: %s" % e)
            check("异常调用后内核仍正常服务", ok)
        except Exception as e:
            check("内核异常路径探测可连接", False, str(e))
        check("全场景后内核仍存活", alive(kernel))
    finally:
        if ob:
            ob.close()
        for p in (rust, kernel):
            if alive(p):
                p.terminate()
        time.sleep(0.5)
        for p in (rust, kernel):
            if alive(p):
                p.kill()
        klog.close()
        rlog.close()

    print("\n==== %d PASS / %d FAIL ====" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项: %s" % ", ".join(FAIL))
        print("日志目录: %s" % tmp)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
