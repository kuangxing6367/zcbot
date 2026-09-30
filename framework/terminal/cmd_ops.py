# -*- coding: utf-8 -*-
"""
终端命令：运维扩展（restart / shell / dbdump）

框架内置线路（原 core_plugins/ops 官方插件迁入，插件形态已删除）。
容错约定：handler 一律同步函数（/api/terminal/exec 直接同步调用，async
会返回未 await 的协程）；自带 try/except + 超时 + 输出截断，任何子进程
异常只打印错误文本，不向线程/事件循环抛出。
reload（含别名 rl）在 cmd_plugin.py，本模块不重复注册。

配置（config.yaml 的 ops 段，全部可省略）：
  ops:
    shell_timeout: 10     # shell 命令超时（秒）
    max_output: 4000      # 命令输出截断长度
"""
import asyncio
import os
import subprocess
import sys
import threading

from .command import terminal_commands

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _truncate(text: str, max_len: int) -> str:
    """输出截断，超过上限时提示省略，避免终端/API 收到超长回显"""
    text = text or ""
    if len(text) <= max_len:
        return text
    head = max_len // 2
    tail = max_len - head - 11
    return (f"{text[:head]}\n......[输出已截断，共 {len(text)} 字符]......\n{text[-tail:]}")


def register(fw):
    """注册本组终端命令"""
    cfg = fw.config.get('ops', {}) or {}
    _shell_timeout = float(cfg.get('shell_timeout', 10) or 10)
    _max_output = int(cfg.get('max_output', 4000) or 4000)

    def cmd_restart(args):
        """重启框架: restart"""
        if getattr(fw, '_role', 'standard') == 'host':
            print("宿主进程不能独立重启框架（会与核心进程冲突），"
                  "请在核心进程或 Web 面板（/api/restart）执行重启")
            return

        def _do_restart():
            import time as _t
            _t.sleep(1)
            try:
                loop = getattr(fw, 'loop', None)
                if loop is not None and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        fw.stop(), loop).result(timeout=15)
                else:
                    asyncio.run(fw.stop())
            except Exception:
                pass
            # os.execv 用当前解释器原地重载 main.py（与 /api/restart 一致）
            python = sys.executable
            main_py = os.path.join(_ROOT, 'main.py')
            os.chdir(os.path.dirname(main_py))
            os.execv(python, [python, main_py] + sys.argv[1:])

        print("框架正在重启（1 秒后执行，当前会话将中断）...")
        threading.Thread(target=_do_restart, daemon=False, name="ops-restart").start()

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
            print(f"[shell] 命令超时（>{_shell_timeout}s），已终止: {cmd}")
            return
        except FileNotFoundError as e:
            print(f"[shell] 命令不存在: {e}")
            return
        except Exception as e:
            print(f"[shell] 执行异常: {e}")
            return
        out = (proc.stdout or '') + (proc.stderr or '')
        prefix = f"[退出码 {proc.returncode}]" if proc.returncode else ""
        body = _truncate(out, _max_output)
        print(f"{prefix}\n{body}" if prefix else body)

    def cmd_dbdump(args):
        """导出数据库快照: dbdump [--dir 目录] [--db 库路径]"""
        tool = os.path.join(_ROOT, 'tools', 'export_db_snapshot.py')
        if not os.path.isfile(tool):
            print(f"[dbdump] 导出工具不存在: {tool}")
            return
        argv = [sys.executable, tool] + (args.split() or [])
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=60,
                encoding='utf-8', errors='replace')
        except subprocess.TimeoutExpired:
            print("[dbdump] 快照导出超时（>60s），已终止")
            return
        except Exception as e:
            print(f"[dbdump] 快照导出异常: {e}")
            return
        out = (proc.stdout or '') + (proc.stderr or '')
        body = _truncate(out, _max_output)
        print(f"[退出码 {proc.returncode}]\n{body}" if proc.returncode else body)

    terminal_commands.register("restart", cmd_restart, "重启框架")
    terminal_commands.register("shell", cmd_shell, "执行 shell 命令: shell <命令>",
                               aliases=["sh"])
    terminal_commands.register("dbdump", cmd_dbdump, "导出数据库快照: dbdump [--dir 目录]")
