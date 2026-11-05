#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IIPC 启动器（zcbot_go/python_side/iipc/launcher.py）

拓扑（对应用户定稿）：
    python main.py -iipc
      └─ 本启动器：日志总出口 + 终端交互（不承载业务）
          ├─ Go 内核（iipc/go_kernel/zcbot_kernel[.exe]）：事件总线 / SQLite / 插件编排
          ├─ Rust 协议端（core_plugins/IIPC_onebot_adapter/）：OneBot 协议层，连入内核
          └─ Python 插件（iipc/plugins/，或 --plugins-dir 指定）：每插件独立进程

日志：全部子进程输出汇到本进程，经 iipc_log 按 config.yaml 的 log: 段统一格式
落盘（与 zcbot 框架同一个 log.file）；插件侧日志经 IIPC log.batch → 内核 → 本出口。

命令：status | plugins | logs | send <文本> | quit
"""
import argparse
import datetime
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_root():
    """框架/项目根：目录里含 config.yaml 的最近上级。

    自适应两种布局（同一份源码，放哪都能跑）：
      - <repo>/framework/iipc/  → 根 = <repo>（合并进真 zcbot 框架）
      - <python_side>/iipc/     → 根 = <python_side>（zcbot_go 实验树）
    """
    up1 = os.path.dirname(HERE)
    up2 = os.path.dirname(up1)
    for cand in (up2, up1):
        if os.path.exists(os.path.join(cand, "config.yaml")):
            return cand
    return up1


ROOT = _find_root()
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "py_sdk"))

import iipc_log  # noqa: E402
from iipc_client import resolve_auth  # noqa: E402


def _load_iipc_config():
    """读 config.yaml 的 `iipc:` 段（IIPC 链路的可配置项，端口为主）。

    设计口径：
      - 端口**不写死在代码里**——开发机上往往同时跑着主框架（WebUI :8080、
        OneBot 反向 WS :6830 等），IIPC 必须能让用户自己挑端口。
      - 优先级：**命令行显式 > config.yaml > 环境变量 > 代码内置默认**。
      - config.yaml 里没有 `iipc:` 段时，行为与旧版完全一致（不做任何假设）。
    """
    defaults = {
        "ipc_addr": os.environ.get("ZCBOT_IIPC_ADDR", "127.0.0.1:18730"),
        "db": ":memory:",
        "admin": os.environ.get("ZCBOT_ADMINS", ""),
        "plugins_dir": "",
        "onebot_url": os.environ.get("ZCBOT_ONEBOT_URL", ""),
        "onebot_listen": os.environ.get("ZCBOT_ONEBOT_LISTEN", ""),
        "onebot_token": os.environ.get("ZCBOT_ONEBOT_TOKEN", ""),
        "no_rust": False,
        "max_rss_mb": 1024,
    }
    try:
        import yaml
        path = os.path.join(ROOT, "config.yaml")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                cfg = (yaml.safe_load(f) or {}).get("iipc") or {}
            for k in defaults:
                v = cfg.get(k)
                if v is None or v == "":
                    continue
                defaults[k] = v
    except Exception as e:      # 配置读不了就用内置默认，绝不让启动器挂在这
        print("[iipc] config.yaml 的 iipc 段读取失败，用默认值: %r" % e,
              file=sys.stderr, flush=True)
    return defaults


KERNEL_DIR = os.path.join(HERE, "go_kernel")
RUST_DIR = os.path.join(HERE, "rust_adapter")   # 协议端源码自带（以源码方式分发）


def _bin_ext():
    """按平台决定二进制扩展名：Windows 用 .exe，Linux/macOS 无扩展名。"""
    return ".exe" if (os.name == "nt" or sys.platform.startswith("win")) else ""


KERNEL_EXE = os.path.join(KERNEL_DIR, "zcbot_kernel" + _bin_ext())
RUST_EXE = os.path.join(RUST_DIR, "target", "release", "zcbot_adapter" + _bin_ext())
PLUGINS = os.path.join(HERE, "plugins")
PLUGINHOST = os.path.join(HERE, "py_sdk", "pluginhost.py")
PY = sys.executable
IPC_ADDR = os.environ.get("ZCBOT_IIPC_ADDR", "127.0.0.1:18730")

# 内核/Rust 日志行：`2026-10-14 20:08:56 INFO [pm] 消息...`
_KLOG_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+(\w+)\s+\[([^\]]+)\]\s*(.*)$")
# 无时间戳但带 [组件] 前缀的行（如插件 stdout 经内核直通）
_COMP_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)$")

procs = {}                 # name -> subprocess.Popen
procs_lock = threading.Lock()
stop = threading.Event()


def _find_go():
    """定位 go：环境变量 > 常见安装路径 > PATH。"""
    for cand in (
        os.environ.get("ZCBOT_GO_BIN"),
        r"E:/make_path/go/bin/go.exe",
        shutil.which("go"),
        shutil.which("go.exe"),
    ):
        if cand and os.path.exists(cand):
            return cand
    return "go"


def _find_cargo():
    """定位 cargo：环境变量 > PATH > 常见安装位置。找不到时回退 "cargo"（让错误显形）。"""
    home = os.path.expanduser("~")
    for cand in (
        os.environ.get("ZCBOT_CARGO"),
        shutil.which("cargo"),
        os.path.join(home, ".cargo", "bin", "cargo.exe"),
        os.path.join(home, ".cargo", "bin", "cargo"),
        r"E:/make_path/rust/.cargo/bin/cargo",
    ):
        if cand and os.path.exists(cand):
            return cand
    return "cargo"


GO_BIN = _find_go()
CARGO = _find_cargo()


def log(tag, msg, level="INFO"):
    iipc_log.emit(tag, level, msg)


def _relay(tag, line):
    """把子进程日志行解析成 (组件, 级别, 消息) 后走统一出口；解析不了整体透传。"""
    m = _KLOG_RE.match(line)
    if m:
        lvl, comp, msg = m.group(1), m.group(2), m.group(3)
        if not msg:
            msg = line
        return comp, lvl, msg
    # 无时间戳但带 [组件] 前缀（如插件 stdout 直通）：提取组件
    m2 = _COMP_RE.match(line)
    if m2:
        lvl = "WARNING" if re.search(r"error|失败|panic", line, re.I) else "INFO"
        return m2.group(1), lvl, m2.group(2)
    # Rust 等无前缀行：含 error/失败 字样按 WARNING，其余 INFO
    lvl = "WARNING" if re.search(r"error|失败|panic", line, re.I) else "INFO"
    return tag, lvl, line


def pump(name, stream, on_line=None):
    try:
        for line in iter(stream.readline, ""):
            line = line.rstrip("\n").rstrip("\r")
            if not line:
                continue
            comp, lvl, msg = _relay(name, line)
            iipc_log.emit(comp, lvl, msg)
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:
                    pass
    except Exception as e:
        log(name, "日志泵异常(已停止该进程日志转发): %s" % e, "ERROR")
    finally:
        try:
            stream.close()
        except Exception:
            pass


def spawn(name, argv, cwd=None, env=None, on_line=None):
    """启动子进程并把其合并输出泵到统一日志出口。失败返回 None（不抛）。"""
    try:
        p = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError as e:
        log("launcher", "启动 %s 失败: 找不到 %s (%s)" % (name, argv[0], e), "ERROR")
        return None
    except Exception as e:
        log("launcher", "启动 %s 失败: %s" % (name, e), "ERROR")
        return None
    with procs_lock:
        procs[name] = p
    threading.Thread(target=pump, args=(name, p.stdout, on_line), daemon=True).start()
    return p


def _newest_source_mtime(root, patterns):
    """源码树里匹配 patterns 的最新 mtime；无匹配返回 0。"""
    import glob
    newest = 0.0
    for pat in patterns:
        for path in glob.glob(os.path.join(root, "**", pat), recursive=True):
            try:
                newest = max(newest, os.path.getmtime(path))
            except OSError:
                continue
    return newest


# 参与「是否需要重编」判定的源码文件。测试文件不算：改 *_test.go 不影响二进制。
_KERNEL_SRC_GLOBS = ("*.go", "go.mod", "go.sum")
_RUST_SRC_GLOBS = ("*.rs", "Cargo.toml", "Cargo.lock")


def _binary_is_stale(exe, src_root, patterns):
    """exe 存在但比源码旧 → 陈旧。

    旧实现是「exe 存在就直接 return True」，结果源码改了（尤其是**协议变更**，
    如 IIPC 握手新增 auth 帧）但二进制没重编，新客户端与旧内核握手超时，
    表现为「连不上、没报错原因」，排查成本极高。
    """
    if not os.path.exists(exe):
        return True
    try:
        exe_mtime = os.path.getmtime(exe)
    except OSError:
        return True
    return _newest_source_mtime(src_root, patterns) > exe_mtime + 1


def _skip_build_requested():
    """允许部署方显式关掉「按源码新鲜度重编」——例如机器上没有 Go/Rust 工具链，
    只用随包分发的预编译二进制。设置后仍会检查 exe 是否存在。
    """
    return os.environ.get("ZCBOT_IIPC_SKIP_BUILD", "").strip() not in ("", "0", "false", "False")


def build_kernel():
    if os.path.exists(KERNEL_EXE) and _skip_build_requested():
        log("launcher", "跳过 Go 内核重编（ZCBOT_IIPC_SKIP_BUILD=1），直接使用 %s"
            % KERNEL_EXE)
        return True
    if not _binary_is_stale(KERNEL_EXE, KERNEL_DIR, _KERNEL_SRC_GLOBS):
        return True
    if os.path.exists(KERNEL_EXE):
        log("launcher", "Go 内核二进制比源码旧（协议可能已变），重新编译 -> %s"
            % KERNEL_EXE)
    else:
        log("launcher", "编译 Go 内核 -> %s" % KERNEL_EXE)
    r = subprocess.run([GO_BIN, "build", "-o", KERNEL_EXE, "."],
                       cwd=KERNEL_DIR, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        if os.path.exists(KERNEL_EXE):
            # 没有工具链/编译失败时不让整个链路起不来：退回已有二进制，
            # 但必须说清楚风险（协议可能不匹配，握手会失败）。
            log("launcher", "Go 编译失败，退回已有内核二进制（若源码改过协议，"
                "握手可能失败）：\n%s" % (r.stderr or r.stdout), "ERROR")
            return True
        log("launcher", "Go 编译失败:\n%s" % (r.stderr or r.stdout), "ERROR")
        return False
    return True


def build_rust():
    if os.path.exists(RUST_EXE) and _skip_build_requested():
        log("launcher", "跳过 Rust 协议端重编（ZCBOT_IIPC_SKIP_BUILD=1），直接使用 %s"
            % RUST_EXE)
        return True
    if not _binary_is_stale(RUST_EXE, os.path.join(RUST_DIR, "src"),
                            _RUST_SRC_GLOBS) and \
            not _binary_is_stale(RUST_EXE, RUST_DIR, ("Cargo.toml", "Cargo.lock")):
        return True
    if os.path.exists(RUST_EXE):
        log("launcher", "Rust 协议端二进制比源码旧（协议可能已变），重新编译 -> %s"
            % RUST_EXE)
    else:
        log("launcher", "编译 Rust 协议端 -> %s" % RUST_EXE)
    # 尊重部署方自己的环境，不注入任何本机代理。
    # 旧实现无条件 setdefault 127.0.0.1:7892（开发机 Clash 端口）：没有该代理的
    # 服务器上首次 build 拉取 crate 会全部走死代理 → Rust 编译失败并降级为
    # 「无协议端」，send 类动作全挂。需要代理时由部署方自行设置 http_proxy。
    env = dict(os.environ)
    r = subprocess.run([CARGO, "build", "--release"],
                       cwd=RUST_DIR, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        if os.path.exists(RUST_EXE):
            # 与 kernel 同口径：编译不了就用已有二进制，但明确说清协议可能不匹配。
            log("launcher", "Rust 编译失败，退回已有协议端二进制（若源码改过协议，"
                "握手可能失败）:\n%s" % (r.stderr or r.stdout)[-2000:], "ERROR")
            return True
        log("launcher", "Rust 编译失败（将降级：无协议端，send 类动作会失败）:\n%s"
            % (r.stderr or r.stdout)[-2000:], "ERROR")
        return False
    return True


def sweep_plugins():
    """清扫内核/pluginhost 拉起的孤儿插件进程（Windows 无进程组，需显式清理）。

    注意：本机 Win32_Process.CommandLine 可能为空导致 LIKE 匹配不到——
    主防线是插件侧"连接断开即自杀"（实测生效），此处仅兜底。
    """
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter "
             "\"CommandLine LIKE '%entry.py%' OR CommandLine LIKE '%pluginhost.py%'\" "
             "| ForEach-Object { $_.Terminate() }"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def shutdown():
    if stop.is_set():
        return
    stop.set()
    with procs_lock:
        names = list(procs.keys())
    for name in ("rust", "kernel"):  # 先协议层后内核
        with procs_lock:
            p = procs.get(name)
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
    time.sleep(0.5)
    with procs_lock:
        for p in procs.values():
            if p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
    sweep_plugins()
    log("launcher", "已停止全部子进程")


def iipc_client():
    from iipc_client import IIPCClient
    return IIPCClient(IPC_ADDR, resolve_auth())


def _mem_watchdog(max_mb):
    """子进程 RSS 监控（防御性）。

    铁律：只告警 + 记录，**不杀任何进程**（看门狗误杀比内存溢出更致命）。
    psutil 不可用则静默跳过。max_mb<=0 表示关闭。
    """
    if max_mb <= 0:
        return
    try:
        import psutil
    except Exception:
        log("launcher", "psutil 不可用，RSS 监控未启用（不影响运行）", "WARNING")
        return

    def loop():
        proc = psutil.Process(os.getpid())
        client = None
        while not stop.wait(30):
            try:
                total = proc.memory_info().rss / 1024 / 1024
                parts = []
                with procs_lock:
                    items = list(procs.items())
                for n, p in items:
                    if p.poll() is not None:
                        continue
                    try:
                        rss = psutil.Process(p.pid).memory_info().rss / 1024 / 1024
                        parts.append("%s=%.0fMB" % (n, rss))
                        total += rss
                    except Exception:
                        pass
                # 各插件进程（内核逐插件直拉，每个是一个独立 Python 解释器，真正的内存大户）：
                # 此前 launcher 监控只扫自身 procs（仅 kernel+rust），完全看不到它们，给出假安全感。
                # 现经内核 debug.proc_stats 拿 PID，逐个累加到总 RSS，并在告警里点名。
                try:
                    if client is None or not client.is_alive():
                        client = iipc_client()
                    stats = client.call("debug.proc_stats", {}, timeout=5)
                    if isinstance(stats, dict):
                        for pl in (stats.get("plugins") or []):
                            pid = pl.get("pid") or 0
                            if pid <= 0:
                                continue
                            try:
                                rss = psutil.Process(pid).memory_info().rss / 1024 / 1024
                                parts.append("%s=%.0fMB" % (pl.get("plugin_id"), rss))
                                total += rss
                            except Exception:
                                pass
                except Exception:
                    pass  # 内核未就绪/断开时跳过插件部分，不影响 kernel+rust 监控
                if total > max_mb:
                    log("memwatch",
                        "子进程总 RSS %.0fMB 超阈值 %dMB（%s）——仅告警，不杀进程"
                        % (total, max_mb, ", ".join(parts)), "WARNING")
            except Exception:
                pass  # 监控异常不干扰主流程

    threading.Thread(target=loop, daemon=True, name="iipc-memwatch").start()


def repl():
    log("launcher", "终端就绪。命令: status | plugins | logs | send <文本> | quit")
    client = None
    while not stop.is_set():
        try:
            line = input("zcbot> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        parts = line.split(None, 1)
        op = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if op in ("quit", "exit"):
            break
        elif op == "status":
            with procs_lock:
                snapshot = list(procs.items())
            for n, p in snapshot:
                if p.poll() is None:
                    log("launcher", "%s: 运行中 (pid=%d)" % (n, p.pid))
                else:
                    log("launcher", "%s: 已退出 (rc=%s)" % (n, p.returncode), "WARNING")
        elif op == "plugins":
            try:
                for d in sorted(os.listdir(PLUGINS)):
                    if os.path.isdir(os.path.join(PLUGINS, d)):
                        log("launcher", "插件目录: %s" % d)
            except OSError as e:
                log("launcher", "读取插件目录失败: %s" % e, "ERROR")
        elif op == "logs":
            log("launcher", "日志文件: %s%s" % (
                iipc_log._load_log_config(ROOT)["file"] or "(未配置，仅控制台)",
                " (已启用)" if iipc_log.file_enabled() else ""))
        elif op == "send":
            if not arg:
                log("launcher", "用法: send <文本>")
                continue
            if client is None:
                try:
                    client = iipc_client()
                except Exception as e:
                    log("launcher", "连接内核失败: %s" % e, "ERROR")
                    continue
            try:
                client.call("debug.inject", {
                    "type": "message",
                    "data": {"message": arg, "user_id": 10001, "self_id": "mainpy"},
                })
                log("launcher", "已注入事件: %s" % arg)
            except Exception as e:
                log("launcher", "注入失败: %s" % e, "ERROR")
        elif op == "help":
            log("launcher", "status | plugins | logs | send <文本> | quit")
        else:
            log("launcher", "未知命令: %s（help 查看）" % op, "WARNING")
    shutdown()


def main(argv=None):
    """启动 IIPC 全栈（阻塞到终端退出）。

    argv=None 时读 sys.argv[1:]（命令行直跑）；被框架入口调用时传显式列表，
    例如 main(["--iipc", "--admin", "10001"])。

    参数优先级：命令行 > config.yaml 的 `iipc:` 段 > 环境变量 > 内置默认。
    端口类参数（ipc_addr / onebot_url / onebot_listen）建议写进 config.yaml，
    避免与本机其它服务抢端口。
    """
    global IPC_ADDR
    cfg = _load_iipc_config()

    ap = argparse.ArgumentParser(description="zcbot IIPC 启动器")
    ap.add_argument("-iipc", "--iipc", action="store_true",
                    help="以 IIPC 模式启动全栈（Go 内核 + Rust 协议端 + 插件）并进入终端")
    ap.add_argument("--no-rust", action="store_true", help="不启动 Rust 协议端")
    ap.add_argument("--plugin-host", action="store_true",
                    help="用 pluginhost（fork/spawn）拉起插件（默认由内核逐插件直拉）")
    ap.add_argument("--plugins-dir", default=None,
                    help="插件目录（默认 config iipc.plugins_dir，再默认 iipc/plugins）")
    ap.add_argument("--db", default=None, help="SQLite DSN（默认 config iipc.db / :memory:）")
    ap.add_argument("--admin", default=None,
                    help="管理员 user_id 列表（逗号分隔），用于命令级 require_admin 鉴权")
    ap.add_argument("--max-rss-mb", type=int, default=None,
                    help="子进程总 RSS 告警阈值 MB（只告警不杀；0=关闭）")
    ap.add_argument("--onebot-url", default=None,
                    help="OneBot 正向 WS 地址 ws://host:port/path（客户端模式，适配器主动连）")
    ap.add_argument("--onebot-listen", default=None,
                    help="OneBot 反向 WS 监听 [ip:]port（服务端模式，OneBot 实现连入；不传则用适配器默认 127.0.0.1:6701）")
    ap.add_argument("--onebot-token", default=None,
                    help="OneBot access-token（可选）")
    ap.add_argument("--ipc-addr", default=None,
                    help="内核 IPC 监听 [ip:]port（默认 config iipc.ipc_addr / 127.0.0.1:18730）")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])

    if not a.iipc:
        log("launcher", "用法: python main.py -iipc")
        return

    # 命令行未显式给出时，回落到 config.yaml / 环境变量 / 内置默认
    def pick(cli_val, cfg_key, fallback=None):
        if cli_val not in (None, ""):
            return cli_val
        v = cfg.get(cfg_key)
        return v if v not in (None, "") else fallback

    a.plugins_dir = pick(a.plugins_dir, "plugins_dir", PLUGINS)
    a.db = pick(a.db, "db", ":memory:")
    a.admin = pick(a.admin, "admin", "")
    a.onebot_url = pick(a.onebot_url, "onebot_url", "")
    a.onebot_listen = pick(a.onebot_listen, "onebot_listen", "")
    a.onebot_token = pick(a.onebot_token, "onebot_token", "")
    a.max_rss_mb = pick(a.max_rss_mb, "max_rss_mb", 1024)
    a.no_rust = bool(a.no_rust or cfg.get("no_rust"))
    IPC_ADDR = pick(a.ipc_addr, "ipc_addr", "127.0.0.1:18730")

    # 日志总出口初始化（读 config.yaml log: 段；失败用默认值）
    logger = iipc_log.setup_logging(ROOT)
    conf = iipc_log._load_log_config(ROOT)
    log("launcher", "日志级别 %s，文件 %s"
        % (conf["level"], conf["file"] or "(仅控制台)"))
    log("launcher", "IIPC 配置: ipc=%s db=%s onebot=%s"
        % (IPC_ADDR, a.db, a.onebot_url or (a.onebot_listen or "反向WS默认")))

    # 生成/读取高熵共享令牌并写入本进程环境：kernel / rust / 各插件子进程
    # 均以 exec 继承 ZCBOT_IIPC_AUTH，令牌不进命令行参数（避免进程列表泄露）。
    _had_auth = bool(os.environ.get("ZCBOT_IIPC_AUTH"))
    _auth = resolve_auth()
    log("launcher", "IIPC 鉴权令牌已就绪（%s，长度 %d，不打印明文）"
        % ("沿用用户显式设置" if _had_auth else "自动生成高熵令牌", len(_auth)))

    signal.signal(signal.SIGTERM, lambda *_: shutdown())

    if not build_kernel():
        log("launcher", "内核构建失败，中止", "ERROR")
        return

    kernel_ready = threading.Event()
    plugins_ready = threading.Event()

    def on_kernel_line(line):
        if "IIPC 服务端监听" in line:
            kernel_ready.set()
        # 插件注册行（内核转发的插件 stdout）："[plugin echo] 已注册，订阅事件=[...]"
        if "已注册，订阅事件" in line:
            plugins_ready.set()

    env = dict(os.environ)
    env["ZCBOT_PYTHON"] = PY
    env["PYTHONUNBUFFERED"] = "1"
    # 编码统一：内核/插件 stdout 一律 UTF-8，避免 Windows GBK 区域设置下透传乱码
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 显式注入共享令牌（os.environ 里已有，这里再钉一次确保 kernel 拿到）
    env["ZCBOT_IIPC_AUTH"] = _auth
    kargv = [KERNEL_EXE, "--plugins-dir", a.plugins_dir,
             "--ipc-addr", IPC_ADDR, "--db", a.db,
             "--data-dir", os.path.join(ROOT, "plugins_dat")]
    if a.admin:
        kargv += ["--admin", a.admin]
    if a.plugin_host:
        kargv += ["--plugin-host", PLUGINHOST]
    spawn("kernel", kargv, env=env, on_line=on_kernel_line)

    if not kernel_ready.wait(15):
        log("launcher", "内核 15s 未就绪，请查看上方内核日志", "ERROR")
        shutdown()
        return
    log("launcher", "内核就绪 @ %s" % IPC_ADDR)

    # Rust 协议端后台准备：首次启动可能要全量编译（十余秒），不能阻塞 REPL。
    # 编译期间事件注入/DB 链路已可用；动作回传（send）在 Rust 就绪后生效。
    def _prepare_rust():
        if a.no_rust:
            return
        if build_rust():
            rargv = [RUST_EXE, "--addr", IPC_ADDR, "--self-id", "rustbot"]
            if a.onebot_url:
                rargv += ["--onebot-url", a.onebot_url]
            elif a.onebot_listen:
                rargv += ["--onebot-listen", a.onebot_listen]
            # 两者都不传：Rust 侧默认反向 WS 127.0.0.1:6701
            if a.onebot_token:
                rargv += ["--token", a.onebot_token]
            spawn("rust", rargv, env=env)
            log("launcher", "Rust 协议端已就绪（OneBot: %s）"
                % (a.onebot_url or (a.onebot_listen or "反向WS默认 127.0.0.1:6701")))
        else:
            log("launcher", "Rust 协议端不可用：send 类动作会失败，事件注入仍可用", "WARNING")

    threading.Thread(target=_prepare_rust, daemon=True, name="rust-prepare").start()

    # 等插件注册完成再开终端：否则启动后立即 send 的事件会早于订阅到达被丢弃
    if plugins_ready.wait(10):
        log("launcher", "插件已就绪")
    else:
        log("launcher", "警告：10s 内未见插件注册（继续启动；此时 send 可能丢事件）", "WARNING")

    _mem_watchdog(a.max_rss_mb)
    repl()


if __name__ == "__main__":
    main()
