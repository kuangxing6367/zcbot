"""模块级 ctx 注入契约 + pip 安装态修复的回归测试。

起因是两份外部实测报告的复核结论：

1. 插件 handler 直接裸用模块级 ``ctx``，框架在加载期执行 ``module.ctx = ctx``
   注入。运行时成立，但没有声明、没有类型 —— 静态检查刷 F821、IDE 无法补全。
   这里把「四处注入点存在 + 六个模块有类型化占位 + 注入后 handler 真能跑」
   锁成契约，防止后续重构把隐式契约改坏而没人发现。
2. v1.21.1 之后那轮「装完就能用」的修复（版本自报、迁移备份、状态目录布局、
   CLI 入口回退、核心依赖、wheel 排除）当时一个回归测试都没有，只有注释。
   这里逐条补上，避免改动静默把上手路径打回原形。

不依赖网络、真实数据库服务或 ruff 可执行文件；所有路径由 tmp_path / __file__ 派生。
"""
import ast
import importlib.util
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 模块级裸用 ctx 的插件（ruff F821 的 202 处集中在这 6 个文件）
CTX_MODULES = [
    'core_plugins/echo/main.py',
    'core_plugins/html_assembler/main.py',
    'core_plugins/image_renderer/main.py',
    'framework/iipc/plugins/api_surface/main.py',
    'framework/iipc/plugins/official_blacklist/main.py',
    'framework/iipc/plugins/official_signin/main.py',
]

# 框架加载期注入 ctx 的全部位置（少一处就会有插件在运行时 NameError）
INJECTION_SITES = [
    'framework/loader/base.py',
    'framework/core/runtime.py',
    'framework/iipc/py_sdk/sdk.py',
    'framework/terminal/cmd_plugin.py',
]


def _parse(rel):
    path = ROOT / rel
    return ast.parse(path.read_text(encoding='utf-8')), path


def _load_module(rel):
    path = ROOT / rel
    name = '_reg_' + re.sub(r'\W', '_', rel)
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── 契约一：注入点 ────────────────────────────────────────────────────────

@pytest.mark.parametrize('rel', INJECTION_SITES)
def test_ctx_injection_site_exists(rel):
    """四处加载路径都必须把 ctx 写进插件模块全局。"""
    tree, _ = _parse(rel)
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == 'ctx'
                    and isinstance(t.value, ast.Name)
                    and t.value.id in ('module', 'mod')
                    for t in n.targets)]
    assert hits, f'{rel} 缺少 module.ctx = ctx 注入'


@pytest.mark.parametrize('rel', CTX_MODULES)
def test_bare_ctx_module_declares_typed_placeholder(rel):
    """每个裸用 ctx 的模块都要有类型化占位，且类型名来自 TYPE_CHECKING 导入。"""
    tree, path = _parse(rel)
    declared = [n for n in tree.body
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'ctx' for t in n.targets)
                and isinstance(n.value, ast.Call)
                and getattr(n.value.func, 'id', None) == 'cast']
    assert declared, f'{rel} 缺少模块级 ctx = cast("PluginContext", None) 声明'
    src = path.read_text(encoding='utf-8')
    assert 'TYPE_CHECKING' in src and 'PluginContext' in src, \
        f'{rel} 的类型声明没有配套 TYPE_CHECKING 导入，IDE 仍然补全不了'


@pytest.mark.parametrize('rel', CTX_MODULES)
def test_ctx_placeholder_is_none_before_injection(rel):
    """占位在导入期必须是 None：不能自己造一个假 ctx 抢在框架注入前生效。"""
    mod = _load_module(rel)
    assert getattr(mod, 'ctx', 'MISSING') is None, f'{rel} 的 ctx 占位不是 None'


def test_echo_handler_works_with_injected_ctx():
    """端到端：register → 注入 → handler 用裸 ctx 发消息，整条链路真的通。"""
    mod = _load_module('core_plugins/echo/main.py')
    assert mod.ctx is None
    sent, registered = [], []

    fake_ctx = SimpleNamespace(
        command=lambda *a, **kw: registered.append(a[0]),
        log=lambda *a, **kw: None,
        send_msg=lambda **kw: sent.append(kw),
    )
    mod.register(fake_ctx)
    mod.ctx = fake_ctx          # 等价于框架加载期的 module.ctx = ctx

    class Match:
        def group(self, i):
            return '你好'

    mod.handle_echo(SimpleNamespace(user_id='10001', group_id=None), Match())
    assert registered == ['/echo', '/ping']
    assert sent and sent[-1]['message'] == '你好'


# ── 契约二：pip 安装态修复 ────────────────────────────────────────────────

def test_version_falls_back_to_installed_metadata(monkeypatch):
    """wheel 里没有 VERSION 文件，zb -v 必须读 dist-info 而不是报 0.0.0。"""
    from framework.cli import utils as cli_utils
    from framework.migration import version as vmod

    monkeypatch.setattr(vmod, '_read_version_file', lambda: (None, 'no VERSION'))
    monkeypatch.setattr(vmod, '_read_installed_metadata', lambda: ('9.8.7', 'metadata'))
    assert vmod.current_version().raw == '9.8.7'
    assert cli_utils.read_version() == '9.8.7'


def test_version_sources_reports_installed_metadata(monkeypatch):
    """探测报告要能看见第三路来源，否则装包态的版本漂移无从排查。"""
    from framework.migration import version as vmod

    monkeypatch.setattr(vmod, '_read_installed_metadata', lambda: ('9.8.7', 'metadata'))
    sources = vmod.version_sources()
    assert sources['installed_metadata']['raw'] == '9.8.7'
    assert sources['installed_metadata']['parsed']['version'] == '9.8.7'


def test_backup_snapshot_resolves_relative_db_path_under_home(tmp_path, monkeypatch):
    """工作目录 ≠ home 时，destructive 迁移的备份仍要落在真实库旁边。"""
    home = tmp_path / 'home'
    db_file = home / 'data' / 'zcbot.db'
    db_file.parent.mkdir(parents=True)
    db_file.write_bytes(b'fake-sqlite-db')
    cwd = tmp_path / 'cwd'
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    import framework.config as config_mod
    monkeypatch.setattr(config_mod, 'get_home', lambda: str(home))

    from framework.database import migrations as mig

    class FakeDB:
        db_type = 'sqlite'
        config = {'path': 'data/zcbot.db'}

        def execute(self, sql, params=()):
            # 假的 database 没有真 SQLite；走 backup_snapshot 的 copy2 降级分支
            raise RuntimeError('no real sqlite connection')

    result = mig.backup_snapshot(FakeDB(), tag='reg')
    assert result['ok'] is True, result
    assert Path(result['path']).read_bytes() == b'fake-sqlite-db'
    assert Path(result['path']).parent == db_file.parent / 'backups'
    assert not (cwd / 'data').exists(), '备份/库文件被错误地按 cwd 解析'


def test_event_buffer_paths_resolve_under_home(tmp_path, monkeypatch):
    """L2/L3 落盘路径同样按 home 解析，不能和数据库/配置劈成两处状态。"""
    monkeypatch.setenv('ZCBOT_HOME', str(tmp_path / 'home'))
    from framework.core.event_buffer import EventBuffer

    buf = EventBuffer({'buffer': {'l2_mode': 'off'}}, 'data')
    home = str(tmp_path / 'home')
    assert buf.sqlite_path == os.path.join(home, 'data', 'event_buffer.db')
    assert buf.l2_path == os.path.join(home, 'data', 'event_buffer.l2.log')


def test_do_init_uses_state_dir_not_cwd(tmp_path, monkeypatch):
    """全新安装时 zb init 直接落 <home>/data，避免在 cwd 造内核不读的旧布局。"""
    home = tmp_path / 'home'
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.setenv('ZCBOT_HOME', str(home))
    monkeypatch.chdir(work)

    from framework.cli.commands import project as proj

    assert proj.do_init(SimpleNamespace(name=None), str(work)) == 0
    assert (home / 'data' / 'config.yaml').is_file()
    assert (home / 'data' / 'plugins').is_dir()
    assert not (work / 'config.yaml').exists()
    assert not (work / 'plugins').exists()


def test_do_run_falls_back_to_packaged_entry(tmp_path, monkeypatch):
    """无本地 startup.py 时回退包内入口；显式 -f 不存在则必须 rc=2。"""
    from framework.cli.commands import run as run_mod

    work = tmp_path / 'work'
    work.mkdir()
    calls = []
    monkeypatch.setattr(run_mod.subprocess, 'call',
                        lambda cmd, cwd=None: calls.append((cmd, cwd)) or 0)

    args = SimpleNamespace(file=None, python_path=None, reload=False)
    assert run_mod.do_run(args, str(work)) == 0
    assert calls and os.path.isfile(calls[0][0][1]), '回退入口不是真实文件'
    assert calls[0][1] == str(work), 'cwd 仍应是用户的工作目录'

    explicit = SimpleNamespace(file='nope.py', python_path=None, reload=False)
    assert run_mod.do_run(explicit, str(work)) == 2


def test_packaging_contract_web_stack_and_excludes():
    """默认路径会加载 webui，flask 三件套必须在核心依赖；开发期工具不能进 wheel。"""
    pyproject = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    dep_block = re.search(r'^dependencies\s*=\s*\[(.*?)^\]', pyproject, re.MULTILINE | re.DOTALL)
    assert dep_block, 'pyproject.toml 找不到 [project] dependencies'
    for pkg in ('"flask>=2.3.0"', '"flask-cors>=4.0.0"', '"waitress>=3.0.0"'):
        assert pkg in dep_block.group(1), f'{pkg} 掉出核心依赖'

    requirements = (ROOT / 'requirements.txt').read_text(encoding='utf-8')
    for pkg in ('flask>=2.3.0', 'flask-cors>=4.0.0', 'waitress>=3.0.0'):
        assert pkg in requirements, f'{pkg} 没同步进 requirements.txt'

    assert 'framework.iipc.tools*' in pyproject
    assert '*.tests*' in pyproject


def test_plugin_discovery_uses_kernel_core_plugins_dir(tmp_path, monkeypatch):
    """CLI 的插件列表/启用态必须看内核那份清单，而不是 cwd 下的同名副本。"""
    home = tmp_path / 'home'
    (home / 'data').mkdir(parents=True)
    (home / 'data' / 'core_plugins.yaml').write_text(
        'core_plugins:\n  echo:\n    enabled: true\n', encoding='utf-8')
    work = tmp_path / 'work'
    (work / 'data').mkdir(parents=True)
    # 故意在 cwd 放一份相反的配置：旧实现只看 working_dir，会读成 False
    (work / 'data' / 'core_plugins.yaml').write_text(
        'core_plugins:\n  echo:\n    enabled: false\n', encoding='utf-8')
    monkeypatch.setenv('ZCBOT_HOME', str(home))

    from framework.cli import utils as cli_utils

    ids = {pid for pid, _main, kind in cli_utils._iter_plugin_dirs(str(work))
           if kind == 'core'}
    assert 'echo' in ids and 'webui' in ids
    assert cli_utils.plugin_enabled_state(str(work), 'echo', 'core') is True
    assert cli_utils.plugin_enabled_state(str(work), 'not_exists', 'core') is False
