"""IIPC 鉴权回归测试（tests/test_iipc_auth.py）

覆盖三层，既跑纯 Python 逻辑，也打真实 Go 内核做黑盒验证：
  1) resolve_auth 令牌解析：用户显式设置优先、自动生成高熵、写回环境（子进程可见）、幂等；
  2) 真实内核：正确令牌握手 + 高危 RPC 可用；错误令牌 / 不握手的高危 RPC 被明确拒绝；
  3) 拒绝行为在内核日志里可见（读内核 stdout 文本，不只用 caplog）。

测试铁律：
  - 所有路径由 __file__ / tmp_path 推导，不写死绝对路径、不依赖 cwd；
  - 不写死时区（本测试不解析时间戳）；
  - 内核一律 --db :memory: + --plugins-dir/--data-dir 指向 tmp_path 空目录，
    日志走 stdout 捕获，绝不污染真实项目目录；
  - 日志断言读内核 stdout，不依赖 caplog。

同时支持 pytest 收集与 ``python tests/test_iipc_auth.py`` 脚本式直跑（不硬依赖 pytest）。
"""
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK_DIR = ROOT / "framework" / "iipc" / "py_sdk"
GO_DIR = ROOT / "framework" / "iipc" / "go_kernel"
KERNEL = GO_DIR / ("zcbot_kernel.exe" if os.name == "nt" else "zcbot_kernel")
if str(SDK_DIR) not in sys.path:
    sys.path.insert(0, str(SDK_DIR))

from iipc_client import ENV_NAME, IIPCClient, resolve_auth  # noqa: E402

# ───────────────────────── 1) 令牌解析（纯 Python，确定性） ─────────────────────────

def test_resolve_auth_user_env_wins():
    """用户显式设置 ZCBOT_IIPC_AUTH 时沿用；显式参数优先级更高。"""
    saved = os.environ.get(ENV_NAME)
    try:
        os.environ[ENV_NAME] = "user-explicit-token-123"
        assert resolve_auth() == "user-explicit-token-123"
        assert resolve_auth("param-override") == "param-override"
        # 显式设置时不应被改写，保证同进程派生的所有子进程看到同一个值
        assert os.environ[ENV_NAME] == "user-explicit-token-123"
    finally:
        if saved is None:
            os.environ.pop(ENV_NAME, None)
        else:
            os.environ[ENV_NAME] = saved


def test_resolve_auth_generates_high_entropy_and_visible():
    """未设置时生成高熵令牌、写回 os.environ（子进程可见）、幂等、重新生成不同。"""
    saved = os.environ.get(ENV_NAME)
    try:
        os.environ.pop(ENV_NAME, None)
        t1 = resolve_auth()
        assert isinstance(t1, str) and len(t1) >= 32, "令牌必须是高熵长串，而非固定短串"
        assert os.environ.get(ENV_NAME) == t1, "自动生成的令牌必须写回环境，子进程才可见"
        assert resolve_auth() == t1, "二次调用应幂等（读回同一个值）"
        os.environ.pop(ENV_NAME, None)
        t2 = resolve_auth()
        assert t2 != t1, "重新生成应是全新的随机令牌"
    finally:
        if saved is None:
            os.environ.pop(ENV_NAME, None)
        else:
            os.environ[ENV_NAME] = saved


# ───────────────────────── 2/3) 真实 Go 内核黑盒 ─────────────────────────

def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _start_kernel(tmp_path, token):
    """在 127.0.0.1 随机端口起一个只监听回环、数据全在 tmp_path/:memory: 的内核。"""
    plugins = tmp_path / "empty_plugins"      # 空目录：不拉任何插件
    plugins.mkdir()
    dat = tmp_path / "dat"
    dat.mkdir()
    port = _free_port()
    env = dict(os.environ)
    env["ZCBOT_IIPC_AUTH"] = token
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(
        [str(KERNEL), "--plugins-dir", str(plugins),
         "--ipc-addr", "127.0.0.1:%d" % port, "--db", ":memory:",
         "--data-dir", str(dat), "--log-level", "debug"],
        cwd=str(GO_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    lines = []
    threading.Thread(target=lambda: [lines.append(l) for l in proc.stdout],
                     daemon=True).start()
    deadline = time.time() + 15
    while time.time() < deadline:
        if any("IIPC 服务端监听" in l for l in lines):
            break
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    if proc.poll() is not None:
        raise AssertionError("内核启动即退出（日志）: %s" % "".join(lines)[-800:])
    if not any("IIPC 服务端监听" in l for l in lines):
        raise AssertionError("内核 15s 未就绪（日志）: %s" % "".join(lines)[-800:])
    return proc, port, lines


def test_kernel_rejects_unauthenticated(tmp_path):
    """正确令牌放行；错误令牌 / 裸连接高危 RPC 被明确拒绝，且内核日志留痕。"""
    if not KERNEL.exists():
        print("SKIP: 内核未编译 %s（先 go build）" % KERNEL)
        return
    token = secrets.token_hex(32)
    proc, port, klog = _start_kernel(tmp_path, token)
    addr = "127.0.0.1:%d" % port
    try:
        # 1) 正确令牌：握手成功，随后高危 RPC（db.query）正常返回
        cli = IIPCClient(addr, token)
        try:
            res = cli.call("db.query", {"sql": "SELECT 1 AS v"}, timeout=5)
            assert isinstance(res, dict), "正确令牌的 RPC 应有正常响应: %r" % (res,)
        finally:
            cli.close()

        # 2) 错误令牌：客户端必须抛出带明确信息的 PermissionError
        try:
            bad = IIPCClient(addr, "wrong-token")
            bad.close()
            raise AssertionError("错误令牌竟未被拒绝")
        except PermissionError as e:
            assert ("鉴权" in str(e)) or ("auth" in str(e).lower()), \
                "失败信息应明确指向鉴权: %s" % e

        # 3) 裸连接不握手直接发高危 RPC → 明确 unauthorized，绝不能静默放行
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            req = {"t": "req", "id": 7, "method": "db.query",
                   "params": {"sql": "SELECT 1"}}
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            s.settimeout(5)
            data = b""
            while b"\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            text = data.decode("utf-8", "replace")
            assert "unauthorized" in text.lower() or "鉴权" in text, \
                "未授权请求应被明确拒绝，而不是静默放行: %r" % text
            assert '"result"' not in text, \
                "未授权请求不应拿到数据库结果: %r" % text
        finally:
            s.close()

        # 4) 拒绝行为在内核日志里可见（读内核 stdout，不只用 caplog）
        time.sleep(0.5)
        blob = "".join(klog)
        assert ("鉴权失败" in blob) or ("未授权" in blob) or ("未完成鉴权" in blob), \
            "内核日志应记录拒绝事件: %r" % blob[-500:]
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    import traceback

    funcs = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in funcs:
        try:
            if "tmp_path" in fn.__code__.co_varnames:
                with tempfile.TemporaryDirectory(prefix="zcbot_iipc_auth_") as d:
                    fn(Path(d))
            else:
                fn()
            print("PASS", name)
        except Exception:
            failed += 1
            print("FAIL", name)
            traceback.print_exc()
    print("\n%d/%d passed" % (len(funcs) - failed, len(funcs)))
    sys.exit(1 if failed else 0)
