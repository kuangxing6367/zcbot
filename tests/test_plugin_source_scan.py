"""插件源码 AST 静态扫描：只拦真正危险的调用，不误伤嵌套同步函数。"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.loader import source_scan as ss


def _write(tmp_path, name, source):
    path = tmp_path / name
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(source)
    return path


def test_module_level_exit_and_blocking_are_detected(tmp_path):
    path = _write(tmp_path, 'main.py', '''\
import os
import sys
import time
sys.exit(0)
time.sleep(3)
''')
    issues = ss.scan_file(path)
    kinds = {i.kind for i in issues}
    assert 'exit' in kinds
    assert 'blocking_import' in kinds
    assert all(i.line >= 4 for i in issues)


def test_raise_systemexit_at_module_level_is_detected(tmp_path):
    path = _write(tmp_path, 'main.py', 'raise SystemExit(1)\n')
    issues = ss.scan_file(path)
    assert [i.kind for i in issues] == ['exit']


def test_async_direct_blocking_detected_but_nested_sync_not(tmp_path):
    path = _write(tmp_path, 'main.py', '''\
import asyncio
import time

async def handler(ctx):
    time.sleep(1)
    def inner():
        time.sleep(2)
    await asyncio.sleep(0)

def sync_handler():
    time.sleep(3)
''')
    issues = ss.scan_file(path)
    assert len(issues) == 1
    assert issues[0].kind == 'blocking_handler'
    assert issues[0].line == 5


def test_class_body_and_decorator_are_module_scope(tmp_path):
    path = _write(tmp_path, 'main.py', '''\
import time

def deco(fn):
    return fn

@deco
class Thing:
    time.sleep(1)

    def method(self):
        time.sleep(2)
''')
    issues = ss.scan_file(path)
    kinds = [i.kind for i in issues]
    assert 'blocking_import' in kinds
    assert kinds.count('blocking_import') == 1  # 方法体里的不算


def test_allowlist_and_switches(tmp_path):
    path = _write(tmp_path, 'main.py', 'import time\ntime.sleep(1)\n')
    cfg = {'plugins': {'source_scan': {
        'enabled': True,
        'mode': 'block',
        'block_on_import_side_effects': True,
        'allowlist': ['trusted'],
    }}}
    assert ss.scan_plugin('trusted', path, cfg) == []
    assert len(ss.scan_plugin('other', path, cfg)) == 1

    cfg['plugins']['source_scan']['enabled'] = False
    assert ss.scan_plugin('other', path, cfg) == []
    cfg['plugins']['source_scan']['enabled'] = True
    cfg['plugins']['source_scan']['block_on_import_side_effects'] = False
    assert ss.scan_plugin('other', path, cfg) == []


def test_async_blocking_switch_independent(tmp_path):
    path = _write(tmp_path, 'main.py',
                  'import time\nasync def h():\n    time.sleep(1)\n')
    cfg = {'plugins': {'source_scan': {
        'enabled': True,
        'mode': 'block',
        'block_on_blocking_handler': False,
    }}}
    assert ss.scan_file(path, cfg) == []


def test_warn_mode_ignores_block_switches(tmp_path):
    """warn 模式下细项开关不生效：告警要能看全，不能只报一半。"""
    body = ('import time\n'
            'time.sleep(1)\n'
            'async def h():\n'
            '    time.sleep(1)\n')
    path = _write(tmp_path, 'main.py', body)
    cfg = {'plugins': {'source_scan': {
        'enabled': True,
        'mode': 'warn',
        'block_on_import_side_effects': False,
        'block_on_blocking_handler': False,
    }}}
    kinds = sorted(i.kind for i in ss.scan_file(path, cfg))
    assert kinds == ['blocking_handler', 'blocking_import']


def test_mode_defaults_to_warn(tmp_path):
    path = _write(tmp_path, 'main.py', 'import time\ntime.sleep(1)\n')
    assert ss.scan_mode({}) == 'warn'
    assert ss.scan_mode({'plugins': {'source_scan': {'mode': 'BLOCK'}}}) == 'block'
    assert ss.scan_mode({'plugins': {'source_scan': {'mode': 'nonsense'}}}) == 'warn'
    assert len(ss.scan_plugin('p', path, {})) == 1


def test_sync_wait_detection(tmp_path):
    """thread/process/event/lock/queue/future 这类对象的同步等特要报出来。"""
    body = '''import threading

async def handler():
    ev = threading.Event()
    ev.wait()
    thread_obj = threading.Thread(target=print)
    thread_obj.join()
    fut = pool.submit(print)
    fut.result()
'''
    path = _write(tmp_path, 'main.py', body)
    hits = [i for i in ss.scan_file(path) if i.kind == 'blocking_handler']
    assert len(hits) == 3, [str(i) for i in hits]


def test_wait_with_timeout_is_allowed(tmp_path):
    body = '''import threading

async def handler():
    ev = threading.Event()
    ev.wait(5)
    t = threading.Thread(target=print)
    t.join(1.0)
'''
    path = _write(tmp_path, 'main.py', body)
    assert ss.scan_file(path) == []


def test_scan_tree_counts_entry_points(tmp_path):
    root = tmp_path / 'plugins'
    (root / 'alpha').mkdir(parents=True)
    (root / 'alpha' / 'main.py').write_text(
        'import time\ntime.sleep(1)\n', encoding='utf-8')
    (root / 'beta').mkdir()
    (root / 'beta' / 'main.py').write_text('x = 1\n', encoding='utf-8')
    (root / 'gamma').mkdir()  # 没有 main.py，不算插件入口
    files, issues = ss.scan_tree(root)
    assert files == 2
    assert len(issues) == 1


def test_syntax_error_is_non_fatal(tmp_path):
    path = _write(tmp_path, 'main.py', 'def broken(:\n')
    assert ss.scan_file(path) == []


def test_clean_plugin_has_no_issues(tmp_path):
    path = _write(tmp_path, 'main.py', '''\
async def h(ctx):
    await ctx.sleep(0)

def register(ctx):
    ctx.command('x')(h)
''')
    assert ss.scan_file(path) == []


def test_exit_inside_async_handler_is_detected(tmp_path):
    """handler 在核心进程内运行，async 里的 os._exit 会带走整个框架。"""
    for body, kind in (
        ("import os\nasync def h(ctx):\n    os._exit(1)\n", 'exit_handler'),
        ("import sys\nasync def h(ctx):\n    sys.exit(1)\n", 'exit_handler'),
        ("async def h(ctx):\n    raise SystemExit(1)\n", 'exit_handler'),
    ):
        path = _write(tmp_path, 'main.py', body)
        issues = ss.scan_file(path)
        assert [i.kind for i in issues] == [kind], (body, issues)


def test_hard_exit_inside_plain_function_is_detected(tmp_path):
    """普通函数里的 os._exit 被 handler 调用时同样致命；sys.exit 可被捕获，不报。"""
    hard = _write(tmp_path, 'hard.py', "import os\ndef helper(ctx):\n    os._exit(1)\n")
    assert [i.kind for i in ss.scan_file(hard)] == ['exit_function']

    soft = _write(tmp_path, 'soft.py', "import sys\ndef helper(ctx):\n    sys.exit(1)\n")
    assert ss.scan_file(soft) == []


def test_script_entry_and_main_guard_may_exit(tmp_path):
    """脚本入口与 __main__ 守卫块只在直接运行时执行，允许自行退出。"""
    for body in (
        "import os\ndef main():\n    os._exit(1)\n",
        "import os\ndef cli():\n    os._exit(1)\n",
        "import os\nif __name__ == '__main__':\n    os._exit(1)\n",
        "import os\nif '__main__' == __name__:\n    os._exit(1)\n",
    ):
        path = _write(tmp_path, 'main.py', body)
        assert ss.scan_file(path) == [], body
