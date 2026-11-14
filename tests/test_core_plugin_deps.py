"""官方插件（core_plugins/）依赖声明与自愈登记。

回归的线上问题：webui 插件 requirements.txt 明写 flask，却因为
`PluginDepsMixin._read_requirements_txt` 只查 `plugins_dir/<name>/`，
core 插件永远读不到依赖清单；`_load_core_plugins` 的 except 分支也只打日志、
不登记 `_missing_deps`，于是 `_auto_heal_plugin_deps` 全程看不到它，
插件失效而启动日志只留一行 ERROR。

直接 `python tests/test_core_plugin_deps.py` 运行，不依赖 pytest。
"""

import os
import shutil
import sys
import threading
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from framework.core.runtime import FrameworkRuntimeMixin as CoreRuntime
from framework.deps import PluginDepsMixin

_CORE_DIR = os.path.join(_ROOT, 'core_plugins')
_TMP_PLUGIN = 'zzz_test_missing_dep'


def test_core_plugin_requirements_readable():
    """官方插件的 requirements.txt 必须能读到（回退到 core_plugins/）"""
    o = types.SimpleNamespace(plugins_dir=os.path.join(_ROOT, 'plugins'))
    deps = PluginDepsMixin._read_requirements_txt(o, 'webui')
    assert deps, 'core 插件的 requirements.txt 读不到'
    names = [d.split('>')[0].split('=')[0].strip().lower() for d in deps]
    assert 'flask' in names, f'未解析出 flask: {deps}'
    return f'读到 {len(deps)} 条: {deps}'


def test_user_plugin_requirements_unchanged():
    """用户插件路径行为不变：不存在则返回空列表"""
    o = types.SimpleNamespace(plugins_dir=os.path.join(_ROOT, 'plugins'))
    assert PluginDepsMixin._read_requirements_txt(o, 'no_such_plugin_xyz') == []
    return '不存在的插件返回 []'


def test_missing_dep_registered_on_load_failure():
    """core 插件因缺模块加载失败时，必须登记到 _missing_deps 供自愈接管"""
    plugin_dir = os.path.join(_CORE_DIR, _TMP_PLUGIN)
    os.makedirs(plugin_dir, exist_ok=True)
    with open(os.path.join(plugin_dir, 'main.py'), 'w', encoding='utf-8') as f:
        f.write('import zzz_not_installed_xyz  # noqa: F401\n'
                'def register(ctx):\n    pass\n')
    try:
        fake = types.SimpleNamespace(
            config={'core_plugins': {_TMP_PLUGIN: True}},
            _role='standard',
            plugin_loader=types.SimpleNamespace(
                _loaded_plugins={},
                _missing_deps={},
                _lock=threading.Lock(),
            ),
        )
        CoreRuntime._load_core_plugins(fake, only={_TMP_PLUGIN})
        bucket = fake.plugin_loader._missing_deps.get(_TMP_PLUGIN)
        assert bucket, '缺依赖的 core 插件没有登记到 _missing_deps'
        assert 'zzz_not_installed_xyz' in bucket, f'登记内容不对: {bucket}'
        assert _TMP_PLUGIN not in fake.plugin_loader._loaded_plugins
        return f'已登记: {bucket}'
    finally:
        shutil.rmtree(plugin_dir, ignore_errors=True)


def test_loaded_plugin_skipped_on_retry():
    """自愈后重试加载时，已加载的插件不能重复 exec/register（幂等）"""
    plugin_dir = os.path.join(_CORE_DIR, _TMP_PLUGIN)
    os.makedirs(plugin_dir, exist_ok=True)
    calls = os.path.join(plugin_dir, 'calls.txt')
    with open(os.path.join(plugin_dir, 'main.py'), 'w', encoding='utf-8') as f:
        f.write('import os\n'
                f'with open(r"{calls}", "a") as fp:\n'
                '    fp.write("x")\n'
                'def register(ctx):\n    pass\n')
    try:
        fake = types.SimpleNamespace(
            config={'core_plugins': {_TMP_PLUGIN: True}},
            _role='standard',
            plugin_loader=types.SimpleNamespace(
                # 模拟「该插件已加载成功」：重试时必须在进入 exec 之前跳过
                _loaded_plugins={_TMP_PLUGIN: {'module': None}},
                _missing_deps={},
                _lock=threading.Lock(),
            ),
        )
        CoreRuntime._load_core_plugins(fake, only={_TMP_PLUGIN})
        assert not os.path.isfile(calls), '已加载的插件被重复执行了模块代码'
        return '已加载插件被跳过'
    finally:
        shutil.rmtree(plugin_dir, ignore_errors=True)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    ok = 0
    for t in tests:
        try:
            msg = t()
            print(f'  PASS  {t.__name__}  ({msg})')
            ok += 1
        except Exception as e:
            print(f'  FAIL  {t.__name__}: {e}')
    print(f'\n{ok}/{len(tests)} passed')
    return 0 if ok == len(tests) else 1


if __name__ == '__main__':
    sys.exit(main())
