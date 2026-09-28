# -*- coding: utf-8 -*-
"""
官方运维插件（ops）

以 core_plugins 官方插件线路提供终端扩展命令，不侵入 framework/terminal 源码：
  - restart   重启框架（单进程 os.execv 原地重启）
  - reload    重载插件（修复内置 reload 的 bug：plugin_loader 本无
              reload_plugin/reload_all 方法，旧命令每次都静默失败）
  - shell     执行本机 shell 命令（带超时 / 输出截断，语法错误不卡死终端）
  - dbdump    导出数据库快照（复用 tools/export_db_snapshot.py，与 debug sim 同构）

容错约定（用户要求）：
  1. 命令 handler 一律为同步函数（/api/terminal/exec 直接同步调用，
     async handler 会返回未 await 的协程导致异常）；
  2. 每个 handler 自带 try/except + 超时 + 输出截断，任何语法错误或
     子进程异常只打印错误文本，不向线程/事件循环抛出；
  3. restart 防御双进程宿主角色：host 进程 execv 成 main.py 会变成
     第二个核心进程（与现存 core 冲突），因此 host 角色拒绝执行并提示。

启用（core_plugins.yaml，唯一权威）：
  ops:
    enabled: true
    shell_timeout: 10     # shell 命令超时（秒）
    max_output: 4000      # 命令输出截断长度

双进程说明：ops 不带 process 标记 → 单进程 standard 加载、双进程宿主加载。
核心进程终端输入 reload 走内置转发链路（target=host）到宿主执行，同样生效；
restart / shell / dbdump 在核心终端不可用时（双进程），请通过 Web 面板
（/api/restart）或把 ops 加入 config.yaml 的 dual_process.core_plugins 名单。
"""
import asyncio
import os
import subprocess
import sys
import threading
import time

from framework.terminal.command import terminal_commands

__plugin_meta__ = {
    "name": "ops",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "终端运维扩展：重启 / 重载插件 / 执行 shell / 导出数据库快照",
    "priority": 90,
    "official": True,
}


def _truncate(text: str, max_len: int) -> str:
    """输出截断，超过上限时提示省略，保证终端/API 不会收到超长回显"""
    text = text or ""
    if len(text) <= max_len:
        return text
    head = max_len // 2
    tail = max_len - head - 11
    return (f"{text[:head]}\n......[输出已截断，共 {len(text)} 字符]......\n{text[-tail:]}")


def register(ctx):
    """注册终端命令（官方插件线路，修改 framework/terminal 源码之外）"""
    fw = ctx._framework
    cfg = fw.config.get('ops', {}) or {}
    _shell_timeout = float(cfg.get('shell_timeout', 10) or 10)
    _max_output = int(cfg.get('max_output', 4000) or 4000)

    # ---------- restart ----------
    def cmd_restart(args):
        """重启框架: restart"""
        role = getattr(fw, '_role', 'standard')
        if role == 'host':
            print("[ops] 宿主进程不能独立重启框架（会与核心进程冲突），"
                  "请在核心进程或 Web 面板（/api/restart）执行重启")
            return

        def _do_restart():
            import time as _t
            _t.sleep(1)
            try:
                loop = getattr(fw, 'loop', None)
                if loop is not None and loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(fw.stop(), loop)
                    fut.result(timeout=15)
                else:
                    asyncio.run(fw.stop())
            except Exception:
                pass
            # os.execv 用当前解释器原地重载 main.py（与 framework_ops 一致）
            python = sys.executable
            main_py = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                'main.py')
            os.chdir(os.path.dirname(main_py))
            os.execv(python, [python, main_py] + sys.argv[1:])

        print("框架正在重启（1 秒后执行，当前会话将中断）...")
        threading.Thread(target=_do_restart, daemon=False, name="ops-restart").start()

    # ---------- reload（修复内置 reload 的 bug） ----------
    def _reload_one(loader, name):
        loader.unload_plugin(name)
        if not loader.load_plugin(name):
            return False
        try:
            loader.register_commands(name)
        except Exception:
            pass
        try:
            fw.router._invalidate_cache()
        except Exception:
            pass
        return True

    def cmd_reload(args):
        """重载插件: reload [插件名]（缺省重载所有已加载插件）"""
        loader = fw.plugin_loader
        try:
            with loader._lock:
                loaded = list(loader._loaded_plugins.keys())
        except Exception:
            loaded = []
        target = args.strip()
        try:
            if target:
                ok = _reload_one(loader, target)
                print(f"插件 [{target}] 重载{'成功' if ok else '失败'}")
                return
            if not loaded:
                print("没有已加载的插件")
                return
            ok_list, fail_list = [], []
            for name in loaded:
                if _reload_one(loader, name):
                    ok_list.append(name)
                else:
                    fail_list.append(name)
            print(f"重载完成：成功 {len(ok_list)}/{len(loaded)}"
                  + (f"，失败: {', '.join(fail_list)}" if fail_list else ""))
        except Exception as e:
            print(f"重载失败: {e}")

    # ---------- shell（语法错误不卡死终端 / 不崩线程） ----------
    def cmd_shell(args):
        """执行 shell 命令: shell <命令>"""
        cmd = args.strip()
        if not cmd:
            print("用法: shell <命令>，如: shell echo hello")
            return
        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=_shell_timeout,
                encoding='utf-8', errors='replace')
        except subprocess.TimeoutExpired:
            print(f"[ops] 命令超时（>{_shell_timeout}s），已终止: {cmd}")
            return
        except FileNotFoundError as e:
            print(f"[ops] 命令不存在: {e}")
            return
        except Exception as e:
            print(f"[ops] 执行异常: {e}")
            return
        out = (proc.stdout or '') + (proc.stderr or '')
        prefix = f"[退出码 {proc.returncode}]" if proc.returncode else ""
        body = _truncate(out, _max_output)
        print(f"{prefix}\n{body}" if prefix else body)

    # ---------- dbdump（复用生产导出工具） ----------
    def cmd_dbdump(args):
        """导出数据库快照: dbdump [--dir 目录] [--db 库路径]"""
        tool = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'tools', 'export_db_snapshot.py')
        argv = [sys.executable, tool] + (args.split() or [])
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=60,
                encoding='utf-8', errors='replace')
        except subprocess.TimeoutExpired:
            print("[ops] 快照导出超时（>60s），已终止")
            return
        except Exception as e:
            print(f"[ops] 快照导出异常: {e}")
            return
        out = (proc.stdout or '') + (proc.stderr or '')
        body = _truncate(out, _max_output)
        print(f"[退出码 {proc.returncode}]\n{body}" if proc.returncode else body)

    terminal_commands.register("restart", cmd_restart, "重启框架")
    terminal_commands.register("reload", cmd_reload, "重载插件: reload [插件名]",
                               aliases=["rl"])
    terminal_commands.register("shell", cmd_shell, "执行 shell 命令: shell <命令>",
                               aliases=["sh"])
    terminal_commands.register("dbdump", cmd_dbdump, "导出数据库快照: dbdump [--dir 目录]")
    ctx.log("终端运维命令已注册: restart / reload / shell / dbdump")