"""回归：``plugin.dir`` 的相对路径必须以状态目录为基准（而不是仓库根目录）

线上现象：llm_load 每次自检都把 16 个文件释放到**仓库根**的 ``plugins/llm_core``，
启动迁移刚把 ``plugins/`` 搬进 ``data/``，下一轮启动又被建出来，布局探测永久停在
root/mixed，自动迁移怎么都收不了尾。

根因在模板与解析：``config.yaml`` 里写着 ``plugin.dir: plugins``，而解析层把相对
路径拼在 ``home_dir()`` 上——于是插件目录被钉死在代码目录，绕过整个 ``data/`` 布局。

这里的用例把解析规则钉住：
  · 相对路径 → ``<home>/data/<dir>``
  · 存量部署（data 下没有、根目录有同名目录）→ 回退旧位置，不把插件"弄丢"
  · 迁移搬完（data 下有了）→ 自动切新位置，不再来回跳
  · 绝对路径原样使用

运行：python tests/test_plugin_dir_resolution.py 或 pytest tests/test_plugin_dir_resolution.py
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework import paths
from framework.migration import detector, migrator


def _with_home():
    """临时 home（ZCBOT_HOME 决定 home_dir/state_dir），返回 (home, 清理函数)"""
    old = os.environ.get('ZCBOT_HOME')
    home = tempfile.mkdtemp(prefix='zcbot_home_')

    def cleanup():
        if old is None:
            os.environ.pop('ZCBOT_HOME', None)
        else:
            os.environ['ZCBOT_HOME'] = old
        shutil.rmtree(home, ignore_errors=True)

    os.environ['ZCBOT_HOME'] = home
    return home, cleanup


def test_empty_config_uses_state_dir():
    home, cleanup = _with_home()
    try:
        assert paths.home_dir() == home
        assert paths.resolve_user_plugins_dir('') == os.path.join(home, 'data', 'plugins')
    finally:
        cleanup()


def test_relative_dir_is_relative_to_state_dir():
    home, cleanup = _with_home()
    try:
        os.makedirs(os.path.join(home, 'data', 'plugins'))
        got = paths.resolve_user_plugins_dir('plugins')
        assert got == os.path.join(home, 'data', 'plugins'), got
        # 代码目录（仓库根）不该再被选中
        assert got != os.path.join(home, 'plugins')
    finally:
        cleanup()


def test_legacy_root_fallback_keeps_plugins_working():
    """存量部署：data 下还没有、根目录已有 plugins → 先用旧位置，别把插件弄丢"""
    home, cleanup = _with_home()
    try:
        os.makedirs(os.path.join(home, 'plugins', 'hello'))
        got = paths.resolve_user_plugins_dir('plugins')
        assert got == os.path.join(home, 'plugins'), got
    finally:
        cleanup()


def test_fallback_converges_after_migration():
    """迁移把 plugins 搬进 data/ 之后 → 自动切新位置（收敛，不来回跳）"""
    home, cleanup = _with_home()
    try:
        os.makedirs(os.path.join(home, 'data', 'plugins'))
        os.makedirs(os.path.join(home, 'plugins'))
        got = paths.resolve_user_plugins_dir('plugins')
        assert got == os.path.join(home, 'data', 'plugins'), got
        # 再调用一次仍是新位置（解析是纯函数，不留状态）
        assert paths.resolve_user_plugins_dir('plugins') == got
    finally:
        cleanup()


def test_absolute_path_untouched():
    _, cleanup = _with_home()
    try:
        target = os.path.join(tempfile.gettempdir(), 'zcbot_external_plugins')
        assert paths.resolve_user_plugins_dir(target) == target
    finally:
        cleanup()


def test_default_dir_matches_state_layout():
    """空配置与 ``plugins`` 配置在干净 home 下应指向同一处（模板写不写都一样）"""
    home, cleanup = _with_home()
    try:
        implicit = paths.resolve_user_plugins_dir('')
        explicit = paths.resolve_user_plugins_dir('plugins')
        assert implicit == explicit == os.path.join(home, 'data', 'plugins')
    finally:
        cleanup()


def test_empty_root_plugins_is_not_layout_evidence():
    """空的根 plugins/ 不该被当成「旧布局还在用」的证据"""
    home, cleanup = _with_home()
    try:
        os.makedirs(os.path.join(home, 'plugins'))
        os.makedirs(os.path.join(home, 'data'))
        report = detector.probe(home)
        assert report['evidence']['root_plugins'] is False, report['evidence']
        assert report['layout'] != detector.LAYOUT_ROOT, report['layout']
        assert report['needs_migration'] is False
    finally:
        cleanup()


def test_empty_plugins_dir_skipped_in_plan():
    """空壳 plugins/ 不参与搬家，避免和 data/plugins 撞成 conflict"""
    home, cleanup = _with_home()
    try:
        os.makedirs(os.path.join(home, 'plugins'))
        os.makedirs(os.path.join(home, 'data', 'plugins'))
        plan = migrator.plan(home)
        item = next(a for a in plan if a['key'] == 'plugins')
        assert item['kind'] == 'skip-empty', item
    finally:
        cleanup()


if __name__ == '__main__':
    failed = []
    for name, fn in sorted(globals().items()):
        if not name.startswith('test_'):
            continue
        try:
            fn()
            print(f'  PASS  {name}')
        except AssertionError as e:
            print(f'  FAIL  {name}: {e}')
            failed.append(name)
    print(f"\n{'=' * 56}")
    if failed:
        print(f'FAILED {len(failed)}: {", ".join(failed)}')
        sys.exit(1)
    print('ALL PASS')
