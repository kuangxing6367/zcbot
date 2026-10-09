# -*- coding: utf-8 -*-
"""终端运维面板（交互式菜单，framework/terminal 内置命令 tui）

框架自带能力（非插件）：零第三方依赖（psutil 可选）。
协议说明：界面交互（仪表条/线程表/功能键条/增量刷新）参考 htop（GPLv2+）
与 NTop（GPLv3）的交互布局思想；zcbot 为 MIT/Apache-2.0 双协议，与 GPL 不
兼容，未复制、未派生上述项目任何源码，本模块为独立实现。
- 渲染：ANSI 备用屏（Windows 经 SetConsoleMode 开启 VT，POSIX 原生支持）；
- 按键：Windows msvcrt / POSIX termios+select；方向键导航、数字直达；
- 鼠标：SGR 1000/1006 上报，点击选中、再点同项确认（两段式防误触）；
- 数据：进程内运行时对象只读快照，字段缺失安全降级；动作复用内置运维命令。

结构：主菜单 → 状态页（总览/消息/系统）与插件管理（选中重载）、运维操作
（重载/快照/重启/Shell）。run() 阻塞至退出；框架停机（fw._running=False）时
面板自动退出，不阻塞解释器关停。
"""
import atexit
import contextlib
import io
import os
import shutil
import sys
import threading
import time
from datetime import datetime

_RESET = '\x1b[0m'
_BOLD = '\x1b[1m'
_DIM = '\x1b[2m'
_CYAN = '\x1b[36m'
_GREEN = '\x1b[32m'
_YELLOW = '\x1b[33m'
_RED = '\x1b[31m'
_INVERSE = '\x1b[7m'
_GREEN_BG = '\x1b[42m'
_BLACK = '\x1b[30m'

__version__ = '1.1.0'

# 页面注册表：(数字键, mode, 标题) —— 标题同时用于主菜单条目与 _draw 顶栏
_VIEWS = [('2', 'overview', '总览'), ('3', 'plugins', '插件'),
          ('4', 'messages', '消息'), ('5', 'system', '系统')]
_VIEW_NAMES = {'menu': '主菜单', 'monitor': '实时监控', 'overview': '总览',
               'plugins': '插件管理', 'messages': '消息日志', 'system': '系统日志',
               'ops': '运维操作', 'result': '执行结果'}


def _term_size():
    """真实终端尺寸（绕过 COLUMNS/LINES 环境变量——窗口改大小后环境变量不变，
    shutil.get_terminal_size 会优先读它们，导致面板不跟随 resize）"""
    try:
        sz = os.get_terminal_size(sys.__stdout__.fileno())
        return sz.columns, sz.lines
    except Exception:
        return shutil.get_terminal_size((80, 24))


def _dwidth(s: str) -> int:
    """显示宽度：CJK/全角字符按 2 列计（终端等宽字体下的实际占位）"""
    w = 0
    for ch in s:
        w += 2 if ('\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f'
                   or '\uff00' <= ch <= '\uffef') else 1
    return w


def _pad(s: str, width: int) -> str:
    """按显示宽度右补空格（str.ljust 会把中文当 1 列，导致表头错位溢出）"""
    return s + ' ' * max(0, width - _dwidth(s))


def _clip(s: str, width: int, ellipsis_width: int = 0) -> str:
    """按显示宽度截断，超宽时以 … 收尾"""
    if _dwidth(s) <= width:
        return s
    limit = max(0, width - 2)  # '…' 占 1~2 列
    out, w = [], 0
    for ch in s:
        cw = _dwidth(ch)
        if w + cw > limit:
            break
        out.append(ch)
        w += cw
    return ''.join(out) + '…'


def _fmt_bytes(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return '-'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f"{n:.0f}{unit}" if unit == 'B' else f"{n:.3f}{unit}"
        n /= 1024
    return '-'


def _fmt_uptime(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return '-'
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, sec = divmod(r, 60)
    if d:
        return f"{d}d{h}h{m}m"
    if h:
        return f"{h}h{m}m"
    return f"{m}m{sec}s"


class TerminalPanel:
    """交互式运维面板。run() 阻塞直到退出；必须在交互式终端中运行。

    default_view：进入面板时的初始页。monitor = 实时监控
    （上：仪表 + Tasks 信息，下：线程表）；其余为对应菜单页。
    """

    def __init__(self, framework, refresh: float = 1.0, tail: int = 30,
                 default_view: str = 'monitor'):
        self.fw = framework
        self.refresh = max(0.2, float(refresh or 1.0))
        self.tail = max(10, int(tail))
        dv = str(default_view or 'monitor')
        self.mode = dv if dv in ('monitor', 'menu', 'overview', 'plugins',
                                 'messages', 'system') else 'monitor'
        self.sel = 0                # 当前菜单选中下标
        self._items = []            # 当前可选条目 [(label, action, payload)]
        self._hit_rows = {}         # 终端行号 -> 条目下标（鼠标点击映射）
        self._confirm = ''          # 二次确认标记（'reload:<名>' / 'restart'）
        self._result = ''           # 动作输出（result 页显示）
        self._plugin_names = []
        self._scroll = 0
        self._saved_termios = None
        self._saved_input_mode = None
        self._stop = False
        self._leave_registered = False
        self._last_size = None
        self._last_lines = None
        self.version = __version__

    # ── 终端进入 / 退出 ──────────────────────────────────

    def _enter(self) -> bool:
        try:
            if not sys.stdin or not sys.stdin.isatty() or not sys.stdout:
                return False
        except Exception:
            return False
        if os.name == 'nt':
            if not self._enable_windows_vt():
                return False
            self._set_windows_input_mode()
        else:
            try:
                import termios
                import tty
                self._saved_termios = termios.tcgetattr(0)
                tty.setraw(0)
            except Exception:
                self._saved_termios = None
                return False
        # 备用屏 + 隐藏光标 + 鼠标上报（SGR 模式）
        sys.stdout.write('\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h\x1b[2J')
        sys.stdout.flush()
        # 进程被 Ctrl+C / 异常终止时也必须恢复终端（否则备用屏那一帧永远
        # 糊在终端顶部，看起来像"标题不更新"的残影）
        if not self._leave_registered:
            atexit.register(self._leave)
            self._leave_registered = True
        return True

    def _leave(self):
        try:
            sys.stdout.write('\x1b[0m\x1b[?1006l\x1b[?1000l\x1b[?25h\x1b[?1049l')
            sys.stdout.flush()
        except Exception:
            pass
        self._restore_windows_input_mode()
        if self._saved_termios is not None:
            try:
                import termios
                termios.tcsetattr(0, termios.TCSADRAIN, self._saved_termios)
            except Exception:
                pass
            self._saved_termios = None

    def _set_windows_input_mode(self):
        """关掉 Windows 控制台的 processed / line / echo 输入模式。

        - ENABLE_PROCESSED_INPUT：关掉后 Ctrl+C 变成普通按键字节（0x03），
          不再触发系统信号把整个进程干掉（面板/界面自己处理退出）；
        - ENABLE_LINE_INPUT：关掉后按键即时送达，不必等回车；
        - ENABLE_ECHO_INPUT：关掉后按键不回显（界面自绘光标，避免重影）。
        """
        self._saved_input_mode = None
        if os.name != 'nt':
            return
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-10)          # STD_INPUT_HANDLE
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                new_mode = mode.value & ~0x0001 & ~0x0002 & ~0x0004
                if k.SetConsoleMode(h, new_mode):
                    self._saved_input_mode = (h, mode.value)
        except Exception:
            self._saved_input_mode = None

    def _restore_windows_input_mode(self):
        if getattr(self, '_saved_input_mode', None):
            try:
                import ctypes
                h, mode = self._saved_input_mode
                ctypes.windll.kernel32.SetConsoleMode(h, mode)
            except Exception:
                pass
        self._saved_input_mode = None

    @staticmethod
    def _enable_windows_vt() -> bool:
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if not k.GetConsoleMode(h, ctypes.byref(mode)):
                return False
            return bool(k.SetConsoleMode(h, mode.value | 0x0004))
        except Exception:
            return False

    # ── 按键读取（归一化）────────────────────────────────

    def _read_byte(self, timeout: float):
        if os.name == 'nt':
            import msvcrt
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if msvcrt.kbhit():
                    return msvcrt.getch()
                time.sleep(0.01)
            return None
        import select
        try:
            r, _, _ = select.select([sys.stdin], [], [], timeout)
            if r:
                return os.read(0, 1)
        except (OSError, ValueError):
            self._stop = True
        return None

    def _read_escape_seq(self) -> bytes:
        """已读到 \\x1b，继续拼完整转义序列（方向键 / SGR 鼠标）"""
        seq = bytearray(b'\x1b')
        for _ in range(32):
            b = self._read_byte(0.05)
            if b is None:
                break
            seq += b
            if seq.startswith(b'\x1b[<') and seq[-1:] in (b'M', b'm'):
                break  # SGR 鼠标事件完结
            if len(seq) >= 3 and seq[1:2] == b'[' and seq[-1:].isalpha():
                break  # CSI 序列完结
        return bytes(seq)

    def _poll_key(self, timeout: float):
        """读一个按键并归一化：
        返回 'up'/'down'/'enter'/'esc'/'quit'/数字字符，或 ('mouse', 行号)。"""
        b = self._read_byte(timeout)
        if b is None:
            return None
        if b == b'\x1b':
            seq = self._read_escape_seq()
            if seq in (b'\x1b[A', b'\xe0H'):
                return 'up'
            if seq in (b'\x1b[B', b'\xe0P'):
                return 'down'
            if seq == b'\x1b':
                return 'esc'
            if seq.startswith(b'\x1b[<') and seq[-1:] in (b'M', b'm'):
                try:
                    parts = seq[3:-1].decode('ascii', 'ignore').split(';')
                    btn, _x, y = int(parts[0]), int(parts[1]), int(parts[2])
                    if seq[-1:] == b'M' and btn == 0:
                        return ('mouse', y)
                except (IndexError, ValueError):
                    return None
            return None
        if b in (b'\r', b'\n'):
            return 'enter'
        if b in (b'\x03', b'q', b'Q'):
            return 'quit'
        if os.name == 'nt' and b[:1] in (b'\x00', b'\xe0'):
            nxt = self._read_byte(0.05)
            if nxt == b'H':
                return 'up'
            if nxt == b'P':
                return 'down'
            if nxt == b'I':
                return 'pgup'
            if nxt == b'Q':
                return 'pgdn'
            return None
        try:
            ch = b.decode('ascii')
        except UnicodeDecodeError:
            return None
        if ch.isdigit():
            return ch
        if ch in ('m', 'M'):
            return 'm'
        return None

    # ── 主循环 ───────────────────────────────────────────

    def run(self):
        if not self._enter():
            raise RuntimeError("TUI 需要交互式终端（stdin/stdout 为 TTY 且支持 ANSI）")
        try:
            while not self._stop:
                if not getattr(self.fw, '_running', True):
                    break  # 框架停机：面板自动退出，不阻塞解释器关停
                try:
                    self._draw()
                except Exception:
                    pass  # 渲染失败不退出，下轮重试
                try:
                    key = self._poll_key(self.refresh)
                except KeyboardInterrupt:
                    break
                if key == 'quit' or key is None and self._stop:
                    break
                self._dispatch(key)
        except KeyboardInterrupt:
            pass
        finally:
            self._leave()

    def _dispatch(self, key):
        if key is None:
            return
        if isinstance(key, tuple) and key[0] == 'mouse':
            idx = self._hit_rows.get(key[1])
            if idx is None:
                return
            if idx == self.sel and self.mode in ('menu', 'ops', 'plugins'):
                self._activate(idx)       # 两段式：点已选中项 = 确认
            else:
                self.sel = idx
            return
        if self.mode in ('menu', 'ops', 'plugins'):
            if key == 'up':
                self.sel = max(0, self.sel - 1)
                return
            if key == 'down':
                self.sel = min(max(0, len(self._items) - 1), self.sel + 1)
                return
            if key == 'enter':
                if 0 <= self.sel < len(self._items):
                    self._activate(self.sel)
                return
            if key == 'esc':
                if self.mode != 'menu':
                    self._open('menu')
                else:
                    self._confirm = ''
                return
            if isinstance(key, str) and key.isdigit():
                i = int(key) - 1
                if 0 <= i < len(self._items):
                    self._activate(i)
                return
        elif self.mode in ('messages', 'system') and                 key in ('up', 'down', 'pgup', 'pgdn'):
            step = self.tail if key in ('pgup', 'pgdn') else 1
            self._scroll = max(0, self._scroll + (step if key in ('up', 'pgup') else -step))
            return
        else:  # monitor / 状态页 / 结果页
            if key == 'esc' or key == 'm':
                self._open('menu')
                return
            if isinstance(key, str) and key.isdigit():
                n = int(key)
                pages = ['monitor', 'overview', 'plugins', 'messages', 'system']
                if 1 <= n <= len(pages):
                    self._open(pages[n - 1])

    # ── 菜单数据 ─────────────────────────────────────────

    def _build_items(self):
        if self.mode == 'menu':
            return [('实时监控', 'page', 'monitor'),
                    ('状态总览', 'page', 'overview'),
                    ('插件管理（选中后 Enter 重载）', 'page', 'plugins'),
                    ('消息日志', 'page', 'messages'),
                    ('系统日志', 'page', 'system'),
                    ('运维操作', 'menu', 'ops'),
                    ('退出面板', 'quit', None)]
        if self.mode == 'ops':
            return [('重载全部插件', 'cmd', 'reload'),
                    ('导出数据库快照', 'cmd', 'dbdump'),
                    ('执行 Shell 命令', 'shell', None),
                    ('重启框架', 'restart', None),
                    ('返回主菜单', 'page', 'menu')]
        if self.mode == 'plugins':
            return [(name, 'plugin', name) for name in self._plugin_names]
        return []

    def _open(self, mode):
        self.mode = mode
        self.sel = 0
        self._scroll = 0   # 日志页滚动偏移（0=最新；↑/PgUp 看更早历史）
        self._confirm = ''
        if mode == 'plugins':
            self._plugin_names = self._plugin_list()
        self._items = self._build_items()

    def _plugin_list(self):
        try:
            plugins = self.fw.plugin_loader.get_loaded_plugins()
            return [n for n, _ in sorted(plugins.items(),
                                         key=lambda kv: -kv[1].get('priority', 0))]
        except Exception:
            return []

    def _activate(self, idx):
        if not (0 <= idx < len(self._items)):
            return
        label, action, payload = self._items[idx]
        if action == 'page':
            self._open(payload if payload != 'menu' else 'menu')
        elif action == 'menu':
            self._open(payload)
        elif action == 'quit':
            self._stop = True
        elif action == 'plugin':
            # 二次确认后重载单个插件（复用内置 reload 命令）
            tag = f'reload:{payload}'
            if self._confirm == tag:
                self._confirm = ''
                self._result = self._call_command('reload', payload)
                self.mode = 'result'
            else:
                self._confirm = tag
        elif action == 'cmd':
            self._result = self._call_command(payload, '')
            self.mode = 'result'
        elif action == 'shell':
            self._result = self._prompt_shell()
            self.mode = 'result'
        elif action == 'restart':
            if self._confirm == 'restart':
                self._leave()
                self._call_command('restart', '')
                self._stop = True
            else:
                self._confirm = 'restart'

    def _call_command(self, name: str, args: str) -> str:
        """调用已注册的终端命令：按命令归属进程路由，捕获输出为面板结果。

        双进程模式下面板运行在核心进程，宿主侧命令（reload/enable/disable/tasks，
        target=host）须经 IPC 转发到宿主执行——与终端 REPL（input.py）的路由保持
        一致，否则会在核心进程本地空跑（核心不加载用户插件），面板显示成功却无实际效果。
        """
        from framework.terminal.command import terminal_commands
        target = terminal_commands.get_target(name)
        role = getattr(self.fw, '_role', 'standard')

        if role == 'core' and target in ('host', 'both'):
            remote = self._call_remote(name, args)
            if target == 'host':
                return remote
            local = self._run_local(name, args)
            return (f"{local}\n--- 宿主进程 ---\n{remote}").strip()

        return self._run_local(name, args)

    def _run_local(self, name: str, args: str) -> str:
        """在本进程执行同步终端命令，捕获其 stdout"""
        from framework.terminal.command import terminal_commands
        handler = terminal_commands.get(name)
        if handler is None:
            return f"命令 [{name}] 未注册"
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                handler(args)
        except Exception as e:
            buf.write(f"执行异常: {e}")
        return buf.getvalue().rstrip() or '（无输出）'

    def _call_remote(self, name: str, args: str) -> str:
        """经 IPC 把命令转发到宿主进程执行，返回其输出文本"""
        server = getattr(self.fw, 'ipc_server', None)
        if server is None or not getattr(server, 'connected', False):
            return f"[{name}] 宿主进程未连接，无法执行（其状态在宿主进程）"
        try:
            text = server.request_host(
                'terminal.exec', {'name': name, 'args': args})
        except Exception as e:
            return f"[{name}] 转发到宿主进程失败: {e}"
        return (str(text).rstrip() if text else '') or '（无输出）'

    def _prompt_shell(self) -> str:
        """退出备用屏，在真实终端读一行命令并捕获输出，再回面板"""
        self._leave()
        try:
            print("shell（空行取消）> ", end='', flush=True)
            line = ''
            try:
                line = input().strip()
            except (EOFError, KeyboardInterrupt):
                return '（已取消）'
            if not line:
                return '（已取消）'
            from framework.terminal.command import terminal_commands
            handler = terminal_commands.get('shell')
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    handler(line)
            except Exception as e:
                buf.write(f"执行异常: {e}")
            return buf.getvalue().rstrip() or '（无输出）'
        finally:
            if not self._enter():
                self._stop = True

    # ── 渲染 ─────────────────────────────────────────────

    def _draw(self):
        """差分渲染：光标定位逐行覆盖（不整屏清写，避免 Windows 终端闪烁）；
        仅终端尺寸变化时才整屏清除重排。"""
        cols, rows = _term_size()
        size_changed = (cols, rows) != self._last_size
        self._last_size = (cols, rows)
        self._hit_rows = {}
        body, items = self._body_lines(rows)
        self._items = items if items is not None else self._items
        footer = self._footer()
        lines = body[:rows - 3]
        lines.append(_DIM + '-' * cols + _RESET)
        lines.append(footer)
        view_name = _VIEW_NAMES.get(self.mode, '运维面板')
        buf = [_BOLD +
               f" 运维面板 · {view_name}   "
               f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}" + _RESET]
        buf += [ln[:cols - 1] for ln in lines]
        # 组装输出：定位到每行行首覆盖写，行尾清残留（\x1b[K）
        parts = []
        if size_changed:
            parts.append('\x1b[2J')
        prev = self._last_lines or []
        for i in range(max(len(buf), len(prev))):
            parts.append(f'\x1b[{i + 1};1H')
            if i < len(buf):
                parts.append(buf[i])
            parts.append('\x1b[0m\x1b[K')
        self._last_lines = buf
        sys.stdout.write(''.join(parts))
        sys.stdout.flush()

    def _body_lines(self, rows):
        if self.mode == 'monitor':
            return self._render_monitor(rows), None
        if self.mode == 'overview':
            return self._render_overview(), None
        if self.mode == 'messages':
            return self._render_messages(), None
        if self.mode == 'system':
            return self._render_system(), None
        if self.mode == 'result':
            lines = ['', f" {_BOLD}执行结果{_RESET}", '']
            lines += [f" {ln}" for ln in self._result.splitlines()[:rows - 8]]
            return lines, None
        # 菜单类页面（menu / ops / plugins）：可选中条目
        if self.mode == 'plugins':
            head = ['', f" {_BOLD}已加载插件{_RESET} {_DIM}（↑↓ 选中，Enter 重载，Esc 返回）{_RESET}", '']
            names = self._plugin_names
            self._items = self._build_items()
            lines = list(head)
            self.sel = min(self.sel, max(0, len(self._items) - 1))
            base_row = 5  # 头(标题1+空1+说明1+空1)后从第 5 行起，_draw 里还有标题行占 1 行
            for i, (label, _a, _p) in enumerate(self._items):
                row_y = base_row + i
                if row_y > rows - 3:
                    break  # 超出可视区（_draw 会裁剪），不登记鼠标命中
                self._hit_rows[row_y] = i
                cursor = f'{_INVERSE}{label}{_RESET}' if i == self.sel else f' {label} '
                lines.append(f" {cursor}")
            return lines, self._items
        # menu / ops
        self._items = self._build_items()
        self.sel = min(self.sel, max(0, len(self._items) - 1))
        lines = ['']
        if self.mode == 'menu':
            lines += self._status_strip()
            lines.append('')
        else:
            lines.append(f" {_BOLD}运维操作{_RESET} {_DIM}（Enter 执行，Esc 返回）{_RESET}")
            lines.append('')
        base_row = len(lines) + 2  # 标题行占 1，_draw 的 buf 首行即第 1 行
        for i, (label, _a, _p) in enumerate(self._items):
            if base_row + i > rows - 3:
                break
            self._hit_rows[base_row + i] = i
            mark = f'{i + 1}.'
            cursor = f'{_INVERSE} {mark} {label} {_RESET}' if i == self.sel \
                else f' {mark} {label} '
            lines.append(f' {cursor}')
        if self._confirm:
            lines.append('')
            lines.append(f' {_YELLOW}再次按 Enter 确认执行：{_RESET}'
                         f'{_BOLD}{self._confirm}{_RESET}')
        return lines, self._items

    def _footer(self):
        if self.mode in ('menu', 'ops', 'plugins'):
            keys = '↑↓ 选择   Enter 确认   数字直达   Esc 返回   q 退出'
        elif self.mode == 'monitor':
            chips = [('1', '总览'), ('2', '插件'), ('3', '消息'), ('4', '系统'),
                     ('m', '菜单'), ('q', '退出')]
            keys = ' '.join(f'{_INVERSE} {k} {_RESET}{_BOLD}{lb}{_RESET}'
                            for k, lb in chips)
        else:
            keys = 'Esc 返回菜单   数字键切页   q 退出'
        if self.mode in ('messages', 'system'):
            keys = '↑↓/PgUp PgDn 滚动历史   Esc 返回菜单   q 退出'
        if self.mode == 'result':
            keys = '任意键返回'
        return f' {_DIM}{keys}{_RESET}'

    def _status_strip(self) -> list:
        """主菜单顶部精简状态条（总览页有完整版）"""
        s = self._snapshot()
        b = s['buf']
        rss = f"{s['rss']:.3f}MB" if s['rss'] is not None else '-'
        warn = _YELLOW if s['rss'] and isinstance(s['mem_limit'], (int, float)) \
            and s['rss'] > s['mem_limit'] * 0.8 else _GREEN
        return [
            f" {_DIM}uptime {_RESET}{s['uptime']} {_DIM}| PID {_RESET}{s['pid']} "
            f"{_DIM}| RSS {_RESET}{warn}{rss}{_RESET} "
            f"{_DIM}| 存储 {_RESET}{s['storage_mode']}",
            f" {_DIM}事件缓冲 {_RESET}L1 {b.get('l1_items', '-')}/{_fmt_bytes(b.get('l1_bytes'))} "
            f"{_DIM}| L2 {_RESET}{b.get('l2_pending', '-')} {_DIM}| 溢出 {_RESET}"
            f"{b.get('overflow_to_sqlite', '-')} {_DIM}| 丢弃 {_RESET}{b.get('dropped', 0)} "
            f"{_DIM}| 插件 {_RESET}{len(s['plugins'])}",
        ]

    # ── 数据快照（防御式只读）────────────────────────────

    def _snapshot(self) -> dict:
        fw = self.fw
        snap = {}
        snap['role'] = getattr(fw, '_role', 'standard')
        snap['pid'] = os.getpid()
        start = getattr(fw, '_start_time', None)
        snap['uptime'] = _fmt_uptime(time.time() - start) if start else '-'
        snap['storage_mode'] = getattr(fw, 'storage_mode', '-')
        snap['db_debug'] = bool(getattr(fw, 'db_debug_mode', False))
        db = getattr(fw, 'db', None)
        snap['db_repr'] = type(db).__name__ if db is not None else '-'
        pool = getattr(db, 'pool_status', None)
        snap['db_note'] = (pool or {}).get('note', '') if isinstance(pool, dict) else ''
        eq = getattr(fw, '_event_workers_count', None)
        snap['workers'] = eq if eq is not None else '-'
        snap['shard'] = eq > 1 if isinstance(eq, int) else False
        buf = getattr(fw, '_event_buffer', None)
        snap['buf'] = buf.stats() if buf is not None and hasattr(buf, 'stats') else {}
        sw = getattr(fw, 'stats_writer', None)
        snap['reg_q'] = sw._reg_queue.qsize() if sw is not None and \
            hasattr(sw, '_reg_queue') else '-'
        snap['cmd_hits'] = len(getattr(sw, '_cmd_hits', {}) or {})
        snap['kw_hits'] = len(getattr(sw, '_kw_hits', {}) or {})
        msq = getattr(fw, '_member_sync_queue', None)
        snap['member_q'] = msq.qsize() if msq is not None else '-'
        loader = getattr(fw, 'plugin_loader', None)
        try:
            snap['plugins'] = loader.get_loaded_plugins() if loader is not None else {}
        except Exception:
            snap['plugins'] = {}
        lb = getattr(fw, 'log_broker', None)
        if lb is None:
            from framework.log_broker import log_broker as lb
        snap['log_cache_n'] = len(getattr(lb, '_cache', ()))
        snap['_lb'] = lb
        mem = getattr(fw, 'config', {}).get('memory', {}) if hasattr(fw, 'config') else {}
        snap['mem_limit'] = mem.get('limit_mb', '-')
        try:
            import psutil
            rss_bytes = psutil.Process().memory_info().rss
            snap['rss'] = rss_bytes / 1024 / 1024          # MB（总览页文本用）
            snap['rss_bytes'] = rss_bytes                  # 字节（内存仪表用）
        except Exception:
            snap['rss'] = None
            snap['rss_bytes'] = None
        # 内存上限：优先运行时 watchdog 的值（config.memory.limit_mb，默认 120）
        limit_mb = getattr(fw, '_memory_limit_mb', None)
        if not isinstance(limit_mb, (int, float)) or limit_mb <= 0:
            limit_mb = snap['mem_limit'] if isinstance(snap['mem_limit'], (int, float)) else None
        snap['rss_limit_bytes'] = limit_mb * 1024 * 1024 \
            if isinstance(limit_mb, (int, float)) and limit_mb > 0 else None
        return snap

    def _log_tail(self, lb, categories=None, n=None, scroll: int = 0) -> list:
        """取日志窗口：scroll=0 最新 n 条；scroll>0 回看更早历史"""
        n = n or self.tail
        try:
            with lb._lock:
                rows = list(lb._cache)
        except Exception:
            return []
        if categories is not None:
            rows = [r for r in rows if r.get('category') in categories]
        end = max(n, len(rows) - int(scroll))
        return rows[max(0, end - n):end]

    # ── 状态页渲染（复用 v1）─────────────────────────────

    def _meter(self, label: str, used, total, width: int = 34) -> str:
        """仪表条：'|' 填充、加粗边框、暗色阴影、绿/黄/红分段；
        数值文本右对齐画在条内（同 htop BarMeterMode 的绘制方式）"""
        try:
            u = float(used)
        except (TypeError, ValueError):
            u = None
        has_total = isinstance(total, (int, float)) and total > 0
        pct = 0.0
        if u is not None and has_total:
            pct = max(0.0, min(1.0, u / float(total)))
        if u is None:
            val_txt = '-'
        elif has_total:
            val_txt = f"{_fmt_bytes(u)}/{_fmt_bytes(float(total))}"
        else:
            val_txt = _fmt_bytes(u)
        txt = (f"{val_txt} {pct * 100:.1f}%" if has_total else val_txt)
        tc = min(max(_dwidth(txt), 1), width - 4)
        txt = ' ' * max(0, tc - _dwidth(txt)) + txt[:tc]
        fill_area = max(1, width - tc)
        filled = int(round(pct * fill_area))
        color = _GREEN if pct < 0.6 else (_YELLOW if pct < 0.85 else _RED)
        bar = (color + '|' * filled + _RESET + _DIM + '|' * (fill_area - filled)
               + _RESET + _BOLD + _CYAN + txt + _RESET)
        return f" {_CYAN}{label}{_RESET}{_BOLD}[{bar}]{_RESET}"

    def _thread_rows(self, max_rows: int) -> list:
        """线程表：框架相关的线程排前"""
        cur = threading.current_thread().name
        rows = []
        for t in threading.enumerate():
            name = t.name
            if name == cur:
                kind, rank = '面板', 0
            elif 'event-worker' in name or 'distributor' in name:
                kind, rank = '事件', 1
            elif 'zcdb' in name:
                kind, rank = '数据库', 2
            elif 'flush' in name or 'sqlsim' in name:
                kind, rank = '缓冲', 3
            elif 'scheduler' in name or 'apscheduler' in name:
                kind, rank = '调度', 4
            elif 'terminal' in name or 'ipc' in name:
                kind, rank = '终端', 5
            elif 'stats' in name or 'member' in name:
                kind, rank = '统计', 6
            else:
                kind, rank = '一般', 9
            rows.append((rank, name, '是' if t.daemon else '否', kind))
        rows.sort(key=lambda r: (r[0], r[1]))
        return rows[:max_rows]

    def _render_monitor(self, rows) -> list:
        """实时监控：上半屏仪表 + Tasks 信息，
        下半屏线程表（绿底表头），底部功能键条"""
        cols, _ = _term_size()
        s = self._snapshot()
        b = s['buf']
        lines = []
        # ── 仪表区（字节值统一 _fmt_bytes 格式化，3 位小数）──
        lines.append(self._meter('事件缓冲', b.get('l1_bytes', 0),
                                 b.get('l1_max_bytes') or None))
        lines.append(f"   L1 {b.get('l1_items', 0)} 条 | "
                     f"L4 {b.get('l4_items', 0)} | L3 {b.get('l3_items', 0)} | "
                     f"L2 待处理 {b.get('l2_pending', 0)}")
        lines.append(f"   溢出 {b.get('overflow_to_sqlite', 0)} | "
                     f"丢弃 {b.get('dropped', 0)}")
        lines.append('')
        lines.append(self._meter('内存    ', s['rss_bytes'], s['rss_limit_bytes']))
        lines.append('')
        # ── Tasks / 运行信息 ──
        thr = threading.enumerate()
        n_daemon = sum(1 for t in thr if t.daemon)
        lines.append(f"Tasks: {len(thr)} thr; {n_daemon} daemon"
                     f"{_CYAN}     uptime: {s['uptime']}{_RESET}"
                     f"   PID: {s['pid']} ({s['role']})"
                     f"{_DIM}   面板 v{self.version}{_RESET}")
        lines.append(f"存储: {s['storage_mode']} ({s['db_repr']})"
                     f"{_CYAN}   workers: {s['workers']}"
                     f"{'（会话分片）' if s['shard'] else ''}{_RESET}")
        lines.append(f"注册队列 {s['reg_q']} | "
                     f"命中 cmd {s['cmd_hits']} / kw {s['kw_hits']} | "
                     f"插件 {len(s['plugins'])}")
        lines.append('')
        # ── 线程表（中文双宽字符按显示宽度对齐，防表头溢出）──
        table_h = max(3, rows - 3 - len(lines) - 3)
        name_w = max(20, min(44, cols - 22))
        lines.append(_GREEN_BG + _BLACK + ' ' +
                     _pad('线程名', name_w) + _pad('守护', 5) + '类别 ' + _RESET)
        for rank, name, daemon, kind in self._thread_rows(table_h - 1):
            color = _CYAN if kind == '事件' else (_DIM if kind == '一般' else '')
            lines.append(f' {color}{_clip(name, name_w, name_w)}{_RESET} '
                         f'{_pad(daemon, 5)}{kind}')
        return lines

    def _render_overview(self) -> list:
        s = self._snapshot()
        b = s['buf']
        rss = f"{s['rss']:.3f}MB" if s['rss'] is not None else '-'
        limit = s['mem_limit']
        rss_txt = rss + (f" / 上限 {limit}MB" if isinstance(limit, (int, float)) else '')
        warn = _YELLOW if s['rss'] and isinstance(limit, (int, float)) \
            and s['rss'] > limit * 0.8 else _GREEN
        return [
            '',
            f" {_CYAN}运行{_RESET}    uptime {s['uptime']} | 角色 {s['role']} "
            f"| PID {s['pid']} | RSS {warn}{rss_txt}{_RESET}",
            f" {_CYAN}存储{_RESET}    mode={s['storage_mode']} | {s['db_repr']}"
            f"{'（调试模拟）' if s['db_debug'] else ''}",
            f" {_CYAN}事件{_RESET}    workers={s['workers']}"
            f"{'（会话分片并行）' if s['shard'] else ''} | "
            f"L1 {b.get('l1_items', '-')}/{_fmt_bytes(b.get('l1_bytes'))} | "
            f"L4 {b.get('l4_items', '-')} | L3 {b.get('l3_items', '-')} | "
            f"L2 待处理 {b.get('l2_pending', '-')} | 溢出 {b.get('overflow_to_sqlite', '-')} | "
            f"丢弃 {(b.get('dropped') or 0)}",
            f" {_CYAN}统计{_RESET}    注册队列 {s['reg_q']} | "
            f"命中待写 命令 {s['cmd_hits']} / 关键词 {s['kw_hits']} | "
            f"群成员同步队列 {s['member_q']}",
            f" {_CYAN}插件{_RESET}    已加载 {len(s['plugins'])} | 日志缓存 {s['log_cache_n']} 条",
        ]

    def _render_messages(self) -> list:
        s = self._snapshot()
        rows = self._log_tail(s['_lb'], categories=('message',),
                              scroll=self._scroll)
        lines = ['', f" {_BOLD}最近消息（{len(rows)}）{_RESET}"]
        for r in rows:
            t = time.strftime('%H:%M:%S', time.localtime(r.get('time', 0)))
            lvl = r.get('level', '')
            color = _GREEN if lvl == 'INFO' else _YELLOW
            lines.append(f" {_DIM}{t}{_RESET} {color}{lvl:<5}{_RESET} "
                         f"{str(r.get('message', ''))[:90]}")
        if not rows:
            lines.append(f" {_DIM}（暂无消息日志）{_RESET}")
        return lines

    def _render_system(self) -> list:
        s = self._snapshot()
        rows = self._log_tail(s['_lb'], scroll=self._scroll)
        rows = [r for r in rows if r.get('category') != 'message'][-self.tail:]
        lines = ['', f" {_BOLD}系统 / 插件 / 连接日志（{len(rows)}）{_RESET}"]
        for r in rows:
            t = time.strftime('%H:%M:%S', time.localtime(r.get('time', 0)))
            lvl = r.get('level', '')
            color = {'ERROR': _RED, 'WARN': _YELLOW, 'INFO': _GREEN}.get(lvl, _DIM)
            src = r.get('source') or r.get('category', '')
            lines.append(f" {_DIM}{t}{_RESET} {color}{lvl:<5}{_RESET} "
                         f"[{str(src)[:12]}] {str(r.get('message', ''))[:80]}")
        if not rows:
            lines.append(f" {_DIM}（暂无系统日志）{_RESET}")
        return lines


def register(fw):
    """注册 tui 面板命令（由 builtins.register_builtins 调用）"""
    from .command import terminal_commands

    cfg = fw.config.get('terminal', {}) or {}

    def cmd_tui(args):
        """终端运维面板: tui（数字键/方向键/鼠标切页，q 退出）"""
        try:
            from .context import is_remote_session
            if is_remote_session():
                print("[tui] 这是远程文本通道（调试控制台 / HTTP），没有 TTY，无法承载全屏面板。\n"
                      "      请在 bot 所在终端直接输入 tui")
                return
        except Exception:
            pass
        try:
            panel = TerminalPanel(
                fw,
                refresh=float(cfg.get('panel_refresh', 1.0) or 1.0),
                default_view=str(cfg.get('panel_default_view', 'monitor') or 'monitor'))
            panel.run()
        except RuntimeError as e:
            print(f"[tui] {e}")
        except Exception as e:
            print(f"[tui] 面板异常退出: {e}")

    terminal_commands.register("tui", cmd_tui, "终端运维面板: tui（q 退出）")
