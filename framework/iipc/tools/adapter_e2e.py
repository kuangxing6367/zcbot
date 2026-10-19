# -*- coding: utf-8 -*-
"""适配器联调（全链路）：假 OneBot → Rust 适配器 → Go 内核 → Python 插件 → 回传。

拓扑：
    本脚本(FakeOneBot, WS 服务端) <──反向WS──> Rust 适配器 <──IIPC──> Go 内核 ─┬─> echo 插件
                                                                              └─> api_parity 插件

验证点（对齐原 zcbot 真实链路）：
  1. 适配器反向 WS 握手成功（accept + RFC 6455）
  2. OneBot 事件(群消息) → 内核 → echo 插件 → send_msg 回到 OneBot 端
     （其中 /ping 命令走 DB 往返后回 "pong"）
  3. api.call 的 echo 关联：get_login_info 请求到达 → 假实现回响应 →
     插件拿到返回值并用于回复（证明请求-响应闭环）
  4. 私聊事件：send_msg 带 user_id、不带 group_id（OneBot 语义）
  5. require_admin：非管理员被拒（无 send_msg），管理员放行
  6. 适配器保活 PING 被 PONG（读循环内自动处理，不单独断言）

用法：
    python tools/adapter_e2e.py            # 自动编译检查 + 全链路联调
"""
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # iipc/
PYSIDE = os.path.dirname(ROOT)                   # python_side/
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "py_sdk"))
from onebot_fake import FakeOneBot, group_msg, private_msg  # noqa: E402
from iipc_client import IIPCClient  # noqa: E402

KERNEL_EXE = os.path.join(ROOT, "go_kernel", "zcbot_kernel.exe")


def _find_rust():
    """协议端 exe：优先 iipc/rust_adapter（源码自带），回退旧布局 core_plugins/。"""
    for cand in (
        os.path.join(ROOT, "rust_adapter", "target", "release", "zcbot_adapter.exe"),
        os.path.join(PYSIDE, "core_plugins", "IIPC_onebot_adapter",
                     "target", "release", "zcbot_adapter.exe"),
    ):
        if os.path.exists(cand):
            return cand
    # 都没编译出来：给优先路径（调用方会打印“请先 cargo build”）
    return os.path.join(ROOT, "rust_adapter", "target", "release", "zcbot_adapter.exe")


RUST_EXE = _find_rust()
PLUGINS_DIR = os.path.join(ROOT, "plugins")
GO = os.environ.get("ZCBOT_GO_BIN") or r"E:/make_path/go/bin/go.exe"

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       (" -> " + str(detail)) if detail and not ok else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_plugins_ready(addr, needed, timeout=45):
    """轮询内核 plugin.list，直到指定插件全部完成注册（或超时）。

    固定 sleep 不可靠：插件进程要起解释器 + 载入 SDK，实测冷启动 4~7s，
    机器忙时更久。首条用例因此落空会被误读成功能回归，这里改成条件等待。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        cli = None
        try:
            cli = IIPCClient(addr, "dev-auth")
            r = cli.call("plugin.list", {}, timeout=3)
            ids = set((r or {}).get("plugins", []) and
                      [p.get("plugin_id") for p in (r or {}).get("plugins", [])])
            if needed.issubset(ids):
                return True
        except Exception:
            pass
        finally:
            if cli is not None:
                try:
                    cli.close()
                except Exception:
                    pass
        time.sleep(0.5)
    return False


def wait_tcp(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def drain(q, window=2.0):
    """收集一段时间窗口内到达的全部 API 请求。"""
    items = []
    deadline = time.time() + window
    while True:
        remain = deadline - time.time()
        if remain <= 0:
            break
        try:
            items.append(q.get(timeout=remain))
        except queue.Empty:
            break
    return items


def send_msgs(items):
    """从 API 请求列表里取 send_msg 的 params 列表。"""
    return [v.get("params", {}) for v in items if v.get("action") == "send_msg"]


def run_reverse_case():
    """场景 2：反向 WS 模式（--onebot-listen）：OneBot 实现主动连入适配器。

    验证适配器服务端侧：TcpListener → ws_accept（服务端握手）→ 读循环 →
    事件推进内核 / API 应答（服务端帧不掩码）。
    """
    ipc_port = free_port()
    ob_port = free_port()
    tmp = tempfile.mkdtemp(prefix="zcbot_adapter_rev_")
    print("\n---- 反向 WS 模式（--onebot-listen）----")
    print("IPC=%d OneBotWS=%d" % (ipc_port, ob_port))
    env = dict(os.environ)
    env["ZCBOT_PYTHON"] = sys.executable
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    klog = open(os.path.join(tmp, "kernel.log"), "w", encoding="utf-8")
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
            check("反向: 内核监听", False, "IPC 端口未就绪")
            return
        check("反向: 内核监听", True)
        rust = subprocess.Popen(
            [RUST_EXE, "--addr", "127.0.0.1:%d" % ipc_port, "--self-id", "rustbot",
             "--onebot-listen", "127.0.0.1:%d" % ob_port],
            stdout=rlog, stderr=subprocess.STDOUT, env=env)
        if not wait_tcp(ob_port, 15):
            check("反向: 适配器监听", False, "OneBot 端口未就绪")
            return
        check("反向: 适配器监听", True)
        # OneBot 实现主动连入（客户端语义：掩码帧）
        ob = FakeOneBot()
        ob.connect_to("127.0.0.1", ob_port)
        check("反向: WS 握手（服务端 accept）", ob.connected.is_set())
        time.sleep(4)
        ob.push_event(group_msg(777, 10001, "/ping"))
        items = drain(ob.api_q, 5)
        msgs = send_msgs(items)
        ok = any(p.get("message") == "pong" and p.get("group_id") == 777
                 for p in msgs)
        check("反向: /ping → send_msg pong", ok, msgs)
    finally:
        if ob:
            ob.close()
        for p in (rust, kernel):
            if p and p.poll() is None:
                p.terminate()
        time.sleep(0.5)
        for p in (rust, kernel):
            if p and p.poll() is None:
                p.kill()
        klog.close()
        rlog.close()


def main():
    if not os.path.exists(KERNEL_EXE):
        print("内核不存在，先构建: %s" % KERNEL_EXE)
        r = subprocess.run([GO, "build", "-o", KERNEL_EXE, "."],
                           cwd=os.path.dirname(KERNEL_EXE),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if r.returncode != 0:
            print("Go 构建失败:\n%s" % (r.stderr or r.stdout))
            return 1
    if not os.path.exists(RUST_EXE):
        print("适配器不存在: %s（请先 cargo build --release）" % RUST_EXE)
        return 1

    ipc_port = free_port()
    ob_port = free_port()
    tmp = tempfile.mkdtemp(prefix="zcbot_adapter_e2e_")
    print("IPC=%d OneBotWS=%d tmp=%s" % (ipc_port, ob_port, tmp))

    env = dict(os.environ)
    env["ZCBOT_PYTHON"] = sys.executable
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    klog = open(os.path.join(tmp, "kernel.log"), "w", encoding="utf-8")
    kernel = subprocess.Popen(
        [KERNEL_EXE, "--plugins-dir", PLUGINS_DIR, "--ipc-addr",
         "127.0.0.1:%d" % ipc_port, "--db", ":memory:",
         "--data-dir", os.path.join(tmp, "plugins_dat"), "--admin", "10099"],
        stdout=klog, stderr=subprocess.STDOUT, env=env,
    )
    rust_log = open(os.path.join(tmp, "rust.log"), "w", encoding="utf-8")
    rust = None
    ob = None
    try:
        # 1) 内核就绪
        if not wait_tcp(ipc_port, 15):
            check("内核监听", False, "IPC 端口未就绪")
            return 1
        check("内核监听", True)

        # 2) 起 Rust 适配器（客户端模式：主动连假 OneBot 的 WS 服务端）
        rust = subprocess.Popen(
            [RUST_EXE, "--addr", "127.0.0.1:%d" % ipc_port,
             "--self-id", "rustbot",
             "--onebot-url", "ws://127.0.0.1:%d" % ob_port],
            stdout=rust_log, stderr=subprocess.STDOUT, env=env,
        )

        # 3) 假 OneBot 起 WS 服务端（固定 ob_port），等适配器主动连入（accept + 握手）
        ob = FakeOneBot(port=ob_port).start()
        if not ob.connected.wait(15):
            check("WS 握手", False, "适配器未连入")
            return 1
        check("WS 握手", True)
        # 等插件注册：插件进程启动耗时随机器负载波动很大（本机实测 4~7s），
        # 固定 sleep 会在慢机器上让首条用例落空（表现为"第一条失败、后面全过"）。
        # 这里改成条件等待——轮询 plugin.list 直到插件真正注册完。
        if not wait_plugins_ready("127.0.0.1:%d" % ipc_port, {"echo"}, timeout=45):
            check("插件注册就绪", False, "等待超时，后续用例不可信")
        else:
            check("插件注册就绪", True)

        # 4) 群消息 "/ping" → echo 插件：命令命中执行 /ping（回 "pong" +
        #    DB 往返回 "db=..."）。1:1 语义（对齐框架 router.route）：
        #    **命令命中时不广播 message 事件**（只有未命中才广播），
        #    所以此处不应出现 "echo: /ping"。
        ob.push_event(group_msg(666, 10001, "/ping"))
        items = drain(ob.api_q, 4)
        msgs = send_msgs(items)
        ok = any(p.get("message") == "pong" for p in msgs)
        check("群消息 /ping → send_msg pong", ok, msgs)
        ok = not any(p.get("message") == "echo: /ping" for p in msgs)
        check("命令命中不广播 message（1:1 语义）", ok, msgs)

        # 5) api.call echo 关联：/info → get_login_info → 假实现自动应答 →
        #    插件拿到返回值并用于回复（证明请求-响应闭环）
        ob.push_event(group_msg(666, 10001, "/info"))
        req = ob.wait_api("get_login_info", 8)
        check("api.call get_login_info 到达", True)
        items = drain(ob.api_q, 4)
        msgs = send_msgs(items)
        ok = any("fakebot" in str(p.get("message", "")) for p in msgs)
        check("echo 响应回插件（info= 含 fakebot）", ok, msgs)

        # 6) 私聊事件：send_msg 带 user_id、不带 group_id
        ob.push_event(private_msg(10002, "hello"))
        items = drain(ob.api_q, 4)
        msgs = send_msgs(items)
        hit = [p for p in msgs if p.get("message") == "echo: hello"]
        ok = bool(hit) and hit[0].get("user_id") == 10002 \
            and "group_id" not in hit[0]
        check("私聊消息 → send_msg(user_id=10002, 无group_id)", ok, msgs)

        # 7) require_admin：非管理员被拒（回"权限不足"提示，对齐框架
        #    router_match 的 reply_text，不执行命令）；管理员放行
        ob.push_event(group_msg(666, 10001, "/secret"))
        items = drain(ob.api_q, 2.5)
        msgs = send_msgs(items)
        ok = any("权限不足" in str(p.get("message", "")) for p in msgs) \
            and not any(p.get("message") == "secret ok" for p in msgs)
        check("非管理员 /secret 回权限不足提示", ok, msgs)
        ob.push_event(group_msg(666, 10099, "/secret"))
        items = drain(ob.api_q, 4)
        msgs = send_msgs(items)
        ok = any(p.get("message") == "secret ok" for p in msgs)
        check("管理员 /secret 放行", ok, msgs)
    finally:
        if ob:
            ob.close()
        for p in (rust, kernel):
            if p and p.poll() is None:
                p.terminate()
        time.sleep(0.5)
        for p in (rust, kernel):
            if p and p.poll() is None:
                p.kill()
        klog.close()
        rust_log.close()

    print("\n==== %d PASS / %d FAIL ====" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项: %s" % ", ".join(FAIL))
        print("内核日志: %s" % os.path.join(tmp, "kernel.log"))
        print("适配器日志: %s" % os.path.join(tmp, "rust.log"))
    # 场景 2：反向 WS 模式（客户端模式全绿后再验，避免相互干扰）
    if not FAIL:
        run_reverse_case()
    print("\n==== 总计 %d PASS / %d FAIL ====" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项: %s" % ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
