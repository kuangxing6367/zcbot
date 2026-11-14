"""插件声明式依赖（__plugin_meta__['requires']）回归测试。

覆盖：
  1. 静态解析：字面量 meta 能读出 requires；动态构造的 meta 安全返回空
  2. 归一化：list / 逗号字符串 / dict / 非法名字
  3. 拓扑排序：被依赖方排前面；缺少依赖 / 循环依赖被拒绝且不传递加载
  4. 已加载插件视为满足（可以 requires 官方插件）
  5. 运行时拦截：单插件加载时依赖缺失直接拒绝（不 import、不 register）
  6. 真实 Framework：load_all 按依赖顺序加载，依赖缺失的插件被跳过

运行：python tests/test_plugin_requires.py   或   pytest tests/test_plugin_requires.py
"""
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.loader.graph import (
    PluginGraphMixin,
    normalize_requires,
    parse_meta_literal,
)

_FAILED = []


def check(cond, msg):
    if cond:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}")
        _FAILED.append(msg)


class _Stub(PluginGraphMixin):
    """只带依赖图能力的桩加载器（不触发真实 import / DB）"""

    def __init__(self, plugins_dir):
        self.plugins_dir = plugins_dir
        self._loaded_plugins = {}
        self._lock = threading.Lock()
        self._declared_meta_cache = {}
        self._dep_errors = {}
        self._graph_lock = threading.Lock()


def _mk_plugin(root, name, meta_line):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'main.py'), 'w', encoding='utf-8') as f:
        f.write("__plugin_meta__ = %s\n\n\ndef register(ctx):\n    pass\n" % meta_line)
    return d


# ── 1. 静态解析 ─────────────────────────────────────────────
def test_parse_literal():
    print("[1] 静态解析 __plugin_meta__")
    tmp = tempfile.mkdtemp(prefix='zcbot_req_')
    try:
        d = _mk_plugin(tmp, 'lit', "{'name': 'lit', 'requires': ['a', 'b']}")
        meta = parse_meta_literal(os.path.join(d, 'main.py'))
        check(meta.get('requires') == ['a', 'b'], "字面量 dict 能读出 requires")

        d2 = os.path.join(tmp, 'dyn')
        os.makedirs(d2, exist_ok=True)
        with open(os.path.join(d2, 'main.py'), 'w', encoding='utf-8') as f:
            f.write("import os\n__plugin_meta__ = {'name': os.getcwd()}\n")
        check(parse_meta_literal(os.path.join(d2, 'main.py')) == {},
              "动态 meta 解析失败时安全返回空（向后兼容）")

        # 带注解的写法也认
        d3 = _mk_plugin(tmp, 'ann', "{}")
        with open(os.path.join(d3, 'main.py'), 'w', encoding='utf-8') as f:
            f.write("__plugin_meta__: dict = {'requires': 'x'}\n")
        check(parse_meta_literal(os.path.join(d3, 'main.py')).get('requires') == 'x',
              "AnnAssign 写法也能解析")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_normalize():
    print("[2] requires 归一化")
    check(normalize_requires(['a', 'b']) == ['a', 'b'], "list 原样")
    check(normalize_requires('a, b c') == ['a', 'b', 'c'], "逗号/空白分隔字符串")
    check(normalize_requires({'a': '>=1.0', 'b': '*'}) == ['a', 'b'], "dict 取键")
    check(normalize_requires(['a', 'a']) == ['a'], "去重")
    check(normalize_requires(None) == [] and normalize_requires('') == [], "空值返回空")
    check(normalize_requires([123, '>=1.0']) == [], "非法名字被过滤")


# ── 2. 拓扑排序 ─────────────────────────────────────────────
def test_order():
    print("[3] 拓扑排序")
    tmp = tempfile.mkdtemp(prefix='zcbot_req_')
    try:
        _mk_plugin(tmp, 'user', "{'requires': ['dep']}")
        _mk_plugin(tmp, 'dep', "{}")
        loader = _Stub(tmp)
        order, problems = loader.resolve_load_order(['user', 'dep'])
        check(order == ['dep', 'user'], f"被依赖方排在前面（{order}）")
        check(problems == {}, "无缺失依赖")

        # 声明顺序反过来也一样
        order2, _ = loader.resolve_load_order(['dep', 'user'])
        check(order2 == ['dep', 'user'], f"声明顺序无关（{order2}）")

        # 三层链
        _mk_plugin(tmp, 'top', "{'requires': ['user']}")
        order3, problems3 = loader.resolve_load_order(['top', 'user', 'dep'])
        check(order3 == ['dep', 'user', 'top'], f"三层依赖链顺序正确（{order3}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missing():
    print("[4] 缺失依赖被拒绝")
    tmp = tempfile.mkdtemp(prefix='zcbot_req_')
    try:
        _mk_plugin(tmp, 'needy', "{'requires': ['ghost']}")
        _mk_plugin(tmp, 'child', "{'requires': ['needy']}")
        _mk_plugin(tmp, 'ok', "{}")
        loader = _Stub(tmp)
        order, problems = loader.resolve_load_order(['needy', 'child', 'ok'])
        check('needy' not in order, "缺失依赖的插件不在加载序列")
        check('needy' in problems and 'ghost' in problems['needy'],
              f"原因指明缺失的名字（{problems.get('needy')}）")
        check('child' in problems, "依赖被拒的插件同样被拒（不传递加载）")
        check(order == ['ok'], f"无关插件照常加载（{order}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cycle():
    print("[5] 循环依赖不死锁")
    tmp = tempfile.mkdtemp(prefix='zcbot_req_')
    try:
        _mk_plugin(tmp, 'p', "{'requires': ['q']}")
        _mk_plugin(tmp, 'q', "{'requires': ['p']}")
        _mk_plugin(tmp, 'solo', "{}")
        loader = _Stub(tmp)
        order, problems = loader.resolve_load_order(['p', 'q', 'solo'])
        check(order == ['solo'], f"只加载无环插件（{order}）")
        check('p' in problems and 'q' in problems, "环上两个插件都被拒绝")
        check('循环依赖' in problems.get('p', ''), f"原因标注循环（{problems.get('p')}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_loaded_satisfies():
    print("[6] 已加载插件视为满足")
    tmp = tempfile.mkdtemp(prefix='zcbot_req_')
    try:
        _mk_plugin(tmp, 'user', "{'requires': ['llm_core']}")
        loader = _Stub(tmp)
        check(loader.check_requires('user') == ['llm_core'], "未加载时报告缺失")
        loader._loaded_plugins['llm_core'] = {'meta': {}, 'priority': 50}
        check(loader.check_requires('user') == [], "已加载（含官方插件）后满足")
        order, problems = loader.resolve_load_order(['user'])
        check(order == ['user'] and not problems, "排序阶段同样认可已加载依赖")
        check(loader.declared_requires('user') == ['llm_core'], "declared_requires 可读出")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 3. 真实加载器拦截 ───────────────────────────────────────
def test_runtime_guard():
    print("[7] 单插件加载时依赖缺失直接拒绝")
    from types import SimpleNamespace

    from framework.loader import PluginLoader

    tmp = tempfile.mkdtemp(prefix='zcbot_req_')
    try:
        _mk_plugin(tmp, 'needy', "{'requires': ['ghost']}")
        fake_fw = SimpleNamespace(db=None, config={'plugin': {}}, services=None)
        loader = PluginLoader(tmp, fake_fw)
        ok = loader.load_plugin('needy')
        check(ok is False, "load_plugin 返回 False")
        err = loader.get_dep_error('needy')
        check('ghost' in err, f"记录了可读原因（{err}）")

        # 依赖补齐后可正常通过校验（此处只看守卫，不触发真实 import）
        loader._loaded_plugins['ghost'] = {'meta': {}, 'priority': 50}
        check(loader.check_requires('needy') == [], "依赖就位后 check_requires 放行")
        loader.clear_dep_error('needy')
        check(loader.get_dep_error('needy') == '', "clear_dep_error 清空原因")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 4. 真实 Framework 端到端 ────────────────────────────────
def test_framework_e2e():
    print("[8] 真实 Framework：load_all 按依赖顺序加载")
    from framework.core import Framework

    tmp = tempfile.mkdtemp(prefix='zcbot_req_fw_')
    try:
        home = os.path.join(tmp, 'home')
        os.makedirs(home, exist_ok=True)
        cfg = os.path.join(home, 'config.yaml')
        db = os.path.join(tmp, 'req.db').replace('\\', '/')
        with open(cfg, 'w', encoding='utf-8') as f:
            f.write(
                f"database:\n  type: sqlite\n  path: {db}\n"
                "log:\n  level: ERROR\n"
                "core_plugins: {}\n"
            )
        plugins_dir = os.path.join(home, 'plugins')
        os.makedirs(plugins_dir, exist_ok=True)
        _mk_plugin(plugins_dir, 'aa_dep', "{'name': 'aa_dep'}")
        _mk_plugin(plugins_dir, 'zz_user', "{'name': 'zz_user', 'requires': ['aa_dep']}")
        _mk_plugin(plugins_dir, 'bb_orphan', "{'name': 'bb_orphan', 'requires': ['nope']}")

        # 插件目录由 get_home() 决定：用 ZCBOT_HOME 把家目录指到临时目录，
        # 避免误加载仓库自带的 plugins/（那里有 llm_core 等真实插件）。
        _old_home = os.environ.get('ZCBOT_HOME')
        os.environ['ZCBOT_HOME'] = home
        try:
            fw = Framework(config_path=cfg, role='standard')
            loaded = fw.plugin_loader.load_all()
        finally:
            if _old_home is None:
                os.environ.pop('ZCBOT_HOME', None)
            else:
                os.environ['ZCBOT_HOME'] = _old_home

        check('aa_dep' in loaded and 'zz_user' in loaded,
              f"依赖链两个插件都加载成功（{loaded}）")
        check('bb_orphan' not in loaded, "依赖缺失的插件未被加载")
        check('nope' in fw.plugin_loader.get_dep_error('bb_orphan'),
              "拦截原因可查询")
        check(loaded.index('aa_dep') < loaded.index('zz_user'),
              f"加载顺序满足依赖（{loaded}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for fn in (test_parse_literal, test_normalize, test_order, test_missing,
               test_cycle, test_loaded_satisfies, test_runtime_guard,
               test_framework_e2e):
        fn()
    print()
    if _FAILED:
        print(f"FAILED {len(_FAILED)} 项：")
        for m in _FAILED:
            print("  -", m)
        return 1
    print("ALL PASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
