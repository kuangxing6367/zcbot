# -*- coding: utf-8 -*-
"""
终端运维面板测试（framework/terminal/panel.py，内置命令 tui）

只测纯渲染逻辑（_snapshot / _render_* / 工具函数）与菜单/滚动状态机，
不进入 ANSI 全屏循环：run() 需要交互式终端，CI/pytest 环境不适用。
运行：python -m pytest tests/test_terminal_panel.py -q
"""
import importlib.util
import os
import sys
import threading
import time
from collections import deque
from queue import Queue

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest


def _load_tui_module():
    """直接加载框架内置面板模块"""
    path = os.path.join(ROOT, 'framework', 'terminal', 'panel.py')
    spec = importlib.util.spec_from_file_location("terminal_panel_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope='module')
def tui_mod():
    return _load_tui_module()


class _FakeBuffer:
    def stats(self):
        return {'l1_items': 3, 'l1_bytes': 2048, 'l1_max_bytes': 512 * 1024,
                'l3_items': 0, 'l4_items': 0,
                'l2_pending': 0, 'overflow_to_sqlite': 5, 'dropped': 0}


class _FakeLoader:
    def get_loaded_plugins(self):
        return {
            'echo': {'meta': {'version': '1.0.0', 'desc': '回声测试'},
                     'priority': 50, 'yaml': {}},
            'admin': {'meta': {'version': '2.1', 'desc': '管理'},
                      'priority': 90, 'yaml': {}},
        }


class _FakeLogBroker:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = deque(maxlen=100)
        now = time.time()
        for i in range(6):
            self._cache.append({
                'seq': i, 'time': now - i, 'category': 'message' if i % 2 else 'system',
                'level': 'INFO' if i % 3 else 'WARN',
                'message': f'entry-{i}', 'detail': {}, 'source': 'test'})


class _FakeFW:
    _role = 'standard'
    _start_time = time.time() - 3725
    storage_mode = 'debug'
    db_debug_mode = True

    class _DB:
        pool_status = {'note': '本地 SQL 模拟'}
    db = _DB()
    _event_workers_count = 4
    _event_buffer = _FakeBuffer()

    class _SW:
        _reg_queue = Queue(maxsize=10)
        _cmd_hits = {1: 2}
        _kw_hits = {}
    stats_writer = _SW()
    _member_sync_queue = Queue()
    plugin_loader = _FakeLoader()
    log_broker = _FakeLogBroker()
    config = {'memory': {'limit_mb': 120}, 'core_plugins': {'session': False}}


def _make_panel(tui_mod):
    return tui_mod.TerminalPanel(_FakeFW(), refresh=1.0)


def test_fmt_helpers(tui_mod):
    assert tui_mod._fmt_uptime(3725) == '1h2m'
    assert tui_mod._fmt_uptime(None) == '-'
    assert tui_mod._fmt_bytes(2048) == '2.000KB'
    assert tui_mod._fmt_bytes('bad') == '-'
    assert tui_mod._fmt_bytes(1024 ** 3) == '1.000GB'


def test_snapshot_defensive(tui_mod):
    p = _make_panel(tui_mod)
    s = p._snapshot()
    assert s['role'] == 'standard'
    assert s['uptime'] == '1h2m'
    assert s['shard'] is True            # workers=4 → 分片模式
    assert s['buf']['l1_items'] == 3
    assert s['reg_q'] == 0
    assert len(s['plugins']) == 2


def test_render_all_views(tui_mod):
    p = _make_panel(tui_mod)
    for view, must_contain in (('overview', 'uptime'), ('messages', 'entry-'),
                               ('system', 'entry-')):
        p.view = view
        lines = p._render_overview() if view == 'overview' else \
            getattr(p, f'_render_{view}')()
        text = '\n'.join(lines)
        assert must_contain in text, f"视图 {view} 缺少关键字 {must_contain}"


def test_menu_structure_and_navigation(tui_mod):
    """主菜单/运维操作条目齐全；↑↓ 数字导航；两段式确认"""
    from framework.terminal.command import terminal_commands
    terminal_commands.register('reload', lambda args: print(f'重载 [{args}] 完成'))
    try:
        p = _make_panel(tui_mod)
        p._open('menu')
        labels = [it[0] for it in p._items]
        assert '状态总览' in labels and '插件管理（选中后 Enter 重载）' in labels \
            and '运维操作' in labels and '退出面板' in labels
        # 数字直达（菜单：1 实时监控 2 总览 3 插件 4 消息 5 系统 6 运维操作）
        p._dispatch('4')
        assert p.mode == 'messages'
        p._dispatch('esc')
        assert p.mode == 'menu'
        # 方向键移动选中
        p.sel = 0
        p._dispatch('down')
        assert p.sel == 1
        p._dispatch('up')
        assert p.sel == 0
        # 运维操作子菜单
        p._open('ops')
        ops_labels = [it[1] for it in p._items]
        assert 'cmd' in ops_labels and 'shell' in ops_labels and 'restart' in ops_labels
        # 插件页：Enter 触发二次确认，再 Enter 执行重载（捕获 stdout，不真打终端）
        p._open('plugins')
        assert 'echo' in p._plugin_names
        p.sel = p._plugin_names.index('echo')
        p._dispatch('enter')
        assert p._confirm == 'reload:echo'
        p._dispatch('enter')
        assert p.mode == 'result' and '重载 [echo]' in p._result
    finally:
        terminal_commands._commands.pop('reload', None)


def test_mouse_click_mapping(tui_mod):
    """鼠标点击行号映射到条目；两段式：首点选中、再点同项确认"""
    p = _make_panel(tui_mod)
    p._open('menu')
    p._body_lines(30)  # 模拟一次绘制，登记鼠标命中行
    assert p._hit_rows, "菜单必须登记鼠标命中行"
    some_row = next(iter(p._hit_rows))
    idx = p._hit_rows[some_row]
    p._dispatch(('mouse', some_row))
    assert p.sel == idx


def test_call_command_captures_stdout(tui_mod):
    from framework.terminal.command import terminal_commands
    calls = []
    terminal_commands.register('tui_test_cmd', lambda args: calls.append(args) or print('done'))
    try:
        p = _make_panel(tui_mod)
        out = p._call_command('tui_test_cmd', 'x')
        assert calls == ['x']
        assert 'done' in out
    finally:
        terminal_commands._commands.pop('tui_test_cmd', None)


def test_call_command_forwards_host_target_in_core(tui_mod):
    """双进程核心角色下面板调用 target=host 命令须经 IPC 转发宿主（回归：
    曾直接本地跑 handler，核心进程无用户插件导致 reload 显示成功却无效）"""
    from framework.terminal.command import terminal_commands

    local_calls = []
    remote_calls = []

    class _FakeServer:
        connected = True

        def request_host(self, method, params=None, timeout=30.0):
            remote_calls.append((method, params))
            return '宿主侧：重载完成'

    class _CoreFW(_FakeFW):
        _role = 'core'
        ipc_server = _FakeServer()

    terminal_commands.register(
        'tui_host_cmd',
        lambda args: local_calls.append(args) or print('本地不应执行'),
        target='host')
    try:
        p = tui_mod.TerminalPanel(_CoreFW(), refresh=1.0)
        out = p._call_command('tui_host_cmd', 'echo')
        assert local_calls == [], 'host 命令不能在核心进程本地执行'
        assert remote_calls and remote_calls[0][0] == 'terminal.exec'
        assert remote_calls[0][1] == {'name': 'tui_host_cmd', 'args': 'echo'}
        assert '宿主侧：重载完成' in out

        # 宿主未连接：清晰提示，不静默本地跑
        class _DownServer:
            connected = False

        class _CoreFW2(_FakeFW):
            _role = 'core'
            ipc_server = _DownServer()

        p2 = tui_mod.TerminalPanel(_CoreFW2(), refresh=1.0)
        out2 = p2._call_command('tui_host_cmd', 'echo')
        assert '宿主进程未连接' in out2 and local_calls == []
    finally:
        terminal_commands._commands.pop('tui_host_cmd', None)


def test_stops_when_framework_stops(tui_mod):
    """框架停机（_running=False）时 run() 立即退出，不卡解释器关停"""
    class _StopFW(_FakeFW):
        _running = False
    p = tui_mod.TerminalPanel(_StopFW(), refresh=0.2)
    # 非 TTY 下 _enter 返回 False 抛 RuntimeError；绕开终端直接验证主循环条件
    assert getattr(p.fw, '_running', True) is False


def test_default_view_and_monitor_render(tui_mod):
    """default_view 配置生效（非法值回退 monitor）；monitor 页仪表 + 线程表"""
    p1 = tui_mod.TerminalPanel(_FakeFW(), default_view='monitor')
    p2 = tui_mod.TerminalPanel(_FakeFW(), default_view='nonsense')
    assert p1.mode == 'monitor' and p2.mode == 'monitor'
    p3 = tui_mod.TerminalPanel(_FakeFW(), default_view='system')
    assert p3.mode == 'system'
    # monitor 渲染：仪表条 + Tasks 信息 + 线程表，内存 3 位小数
    body, items = p1._body_lines(30)
    text = '\n'.join(body)
    assert 'Tasks:' in text and '线程名' in text
    assert '事件缓冲' in text and '内存' in text
    assert 'uptime' in text
    assert '2.000KB' in text  # 字节值统一 3 位小数
    assert 'entry-' not in text  # 线程表页不混排日志


def test_render_tolerates_broken_fw(tui_mod):
    """字段缺失/异常对象不得让渲染抛异常（TUI 不能拖垮框架）"""
    class _Broken:
        pass
    p = tui_mod.TerminalPanel(_Broken(), refresh=1.0)
    p.view = 'overview'
    lines = p._render_overview()
    assert lines
    p._open('plugins')          # plugins 走菜单制：坏 loader → 空列表
    body, _items = p._body_lines(30)
    assert body is not None
    p.view = 'system'
    assert p._render_system() is not None


def test_reload_official_core_plugin(tmp_path, monkeypatch):
    """官方插件（core_plugins/）重载：原 reload 只认 plugins/ 目录必失败，现走专用线路

    官方插件启停的唯一权威是 core_plugins.yaml，而它被 .gitignore 忽略：开发机本地
    有这个文件（恰好启用了若干插件），干净环境/CI 里没有，于是「发现即禁用」，
    本用例会以「无已加载官方插件可测」失败。这里显式把 CORE_PLUGINS_YAML 指向
    临时文件并只启用 session，使任何环境下行为一致、不依赖本机 dev 配置。
    """
    import asyncio
    import contextlib
    import io

    import framework.config as _fw_config

    cps_yaml = tmp_path / 'core_plugins.yaml'
    cps_yaml.write_text(
        "core_plugins:\n  session:\n    enabled: true\n", encoding='utf-8')
    monkeypatch.setattr(_fw_config, 'CORE_PLUGINS_YAML', str(cps_yaml))

    cfg_path = tmp_path / 'config.yaml'
    cfg_path.write_text(
        "database:\n  type: debug\n"
        "log:\n  level: ERROR\n"
        "web:\n  host: 127.0.0.1\n  port: 0\n"
        "onebot:\n  enabled: false\n", encoding='utf-8')
    from framework.core import Framework
    from framework.terminal import register_builtins
    from framework.terminal.command import terminal_commands
    fw = Framework(config_path=str(cfg_path), role='standard')
    try:
        fw._load_core_plugins()
        register_builtins(fw)  # reload 为内置命令（官方+普通插件双线路重载）
        # 选一个已加载的官方插件（排除 ops 自身：重载会换掉命令注册者）
        core_plugins = [n for n, info in fw.plugin_loader._loaded_plugins.items()
                        if str(info.get('path', '')).replace(chr(92), '/').count('/core_plugins/')]
        assert core_plugins, "无已加载官方插件可测"
        target = core_plugins[0]
        h = terminal_commands.get('reload')
        assert h is not None, "内置 reload 命令未注册"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            h(target)
        out = buf.getvalue()
        assert '重载成功' in out, f"重载输出异常: {out}"
        # 重载后仍是官方插件身份且可取到
        assert target in fw.plugin_loader._loaded_plugins
        assert fw.plugin_loader.get_plugin_module(target) is not None
    finally:
        try:
            asyncio.run(fw.stop())
        except Exception:
            pass


def test_draw_renders_title_and_body(tui_mod, capsys):
    """完整 _draw 必须成功渲染：顶栏带版本与当前页名（防渲染层 NameError 被
    run() 静默吞掉、面板整屏不刷新的回归）"""
    p = _make_panel(tui_mod)
    p.mode = 'monitor'
    p._draw()
    out = capsys.readouterr().out
    assert '运维面板 · 实时监控' in out
    assert 'Tasks:' in out
    p.mode = 'menu'
    p._draw()
    out = capsys.readouterr().out
    assert '主菜单' in out and '状态总览' in out
