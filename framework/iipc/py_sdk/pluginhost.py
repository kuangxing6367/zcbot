#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pluginhost：由 Go 内核拉起的单一进程，负责发现并以 fork(Linux)/spawn(Windows) 启动各 Python 插件。

内存设计（对应用户关切）：
  - Linux：先 import SDK + 公共依赖，再对每个插件 os.fork()；子进程经 COW 共享父进程只读页，
    把"每插件一份解释器/SDK"的常驻内存大幅压低（这是 spawn 做不到的）。
  - Windows：无 os.fork，回退 subprocess spawn（与内核逐插件直拉等价，付每进程全价）。
带看门狗重启与优雅关闭；fork 路径需在 Linux 验证，Windows 仅验证 spawn 回退路径。
"""
import argparse
import os
import signal
import subprocess
import sys
import time


def log(msg):
    print("[pluginhost] %s" % msg, flush=True)


HAVE_FORK = hasattr(os, "fork")


def discover(plugins_dir):
    out = []
    try:
        for e in sorted(os.listdir(plugins_dir)):
            d = os.path.join(plugins_dir, e)
            if os.path.isdir(d) and os.path.exists(os.path.join(d, "main.py")):
                out.append(e)
    except OSError as e:
        log("读取插件目录失败: %s" % e)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugins-dir", required=True)
    ap.add_argument("--sdk-dir", required=True)
    ap.add_argument("--iipc-addr", required=True)
    ap.add_argument("--iipc-auth", default="dev-auth")
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args()

    if a.sdk_dir not in sys.path:
        sys.path.insert(0, a.sdk_dir)

    # 预加载 SDK + 公共依赖（Linux fork 的 COW 共享来源；spawn 模式无此收益但无害）
    run_plugin = None
    try:
        import sdk  # noqa: F401
        import iipc_client  # noqa: F401
        from sdk import run_plugin as _rp
        run_plugin = _rp
        log("已预加载 SDK（fork 模式子进程将 COW 共享只读页）")
    except Exception as e:
        log("预加载 SDK 失败: %s（将仅用 spawn）" % e)

    plugins = discover(a.plugins_dir)
    log("发现插件: %s（fork=%s）" % (plugins, HAVE_FORK))

    children = {}  # id -> {"pid": int} 或 {"proc": Popen}

    def launch(pid_id):
        pdir = os.path.join(a.plugins_dir, pid_id)
        if not os.path.exists(os.path.join(pdir, "main.py")):
            log("插件 %s 无 main.py，跳过" % pid_id)
            return
        if HAVE_FORK and run_plugin is not None:
            try:
                pid = os.fork()
            except OSError as e:
                log("fork %s 失败: %s，回退 spawn" % (pid_id, e))
                pid = None
            if pid == 0:
                # 子进程：复用预加载 SDK 运行插件，退出用 os._exit 避免跑父进程清理
                try:
                    run_plugin(pdir, pid_id, a.iipc_addr, a.iipc_auth)
                except Exception as e:
                    sys.stderr.write("[pluginhost child %s] %s\n" % (pid_id, e))
                finally:
                    os._exit(0)
            elif pid is not None:
                children[pid_id] = {"pid": pid}
                log("fork 启动插件 %s (pid=%d)" % (pid_id, pid))
                return
        # spawn 回退（Windows 或 fork 失败）
        entry_py = os.path.join(a.sdk_dir, "entry.py")
        env = dict(os.environ)
        env["ZCBOT_PYTHON"] = a.python
        try:
            proc = subprocess.Popen(
                [a.python, entry_py, "--plugin-dir", pdir,
                 "--iipc-addr", a.iipc_addr, "--plugin-id", pid_id,
                 "--iipc-auth", a.iipc_auth],
                env=env,
            )
        except Exception as e:
            log("spawn 插件 %s 失败: %s" % (pid_id, e))
            return
        children[pid_id] = {"proc": proc}
        log("spawn 启动插件 %s (pid=%d)" % (pid_id, proc.pid))

    for p in plugins:
        launch(p)

    stopping = {"v": False}

    def handle_term(*_):
        stopping["v"] = True

    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGINT, handle_term)

    # 看门狗：周期检查子进程，退出则重启
    while not stopping["v"]:
        time.sleep(1.0)
        for pid_id in list(children.keys()):
            info = children.get(pid_id)
            if not info:
                continue
            if "pid" in info:
                pid = info["pid"]
                try:
                    wpid, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    wpid = pid
                if wpid != 0:
                    log("插件 %s (fork pid=%d) 退出，重启" % (pid_id, pid))
                    launch(pid_id)
            else:
                proc = info["proc"]
                if proc.poll() is not None:
                    log("插件 %s (spawn) 退出 rc=%s，重启" % (pid_id, proc.returncode))
                    launch(pid_id)

    # 优雅关闭
    for pid_id, info in list(children.items()):
        if "pid" in info:
            try:
                os.kill(info["pid"], signal.SIGTERM)
            except Exception:
                pass
        else:
            try:
                info["proc"].terminate()
            except Exception:
                pass
    log("pluginhost 退出")


if __name__ == "__main__":
    main()
