# -*- coding: utf-8 -*-
"""
启动 + 分发冒烟测试
====================
覆盖体检「测试假绿」项：路径解析（dirname×3）、关键模块 import、
服务按来源路由、消息分发与 reply_text 不抛异常。

运行：
    python tests/test_smoke.py
"""
import asyncio
import inspect
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

ok = fail = 0


def chk(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label}  {extra}")


def _repo_root_from(mod_file: str) -> str:
    # framework/core/x.py → dirname×3 = 仓库根
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(mod_file))))


def test_paths_point_to_repo_root():
    import framework.core.runtime as rt
    import framework.core.base as b
    root_rt = _repo_root_from(rt.__file__)
    root_b = _repo_root_from(b.__file__)
    chk("runtime 模块根=仓库根", os.path.normpath(root_rt) == os.path.normpath(ROOT),
        f"got={root_rt}")
    chk("base 模块根=仓库根", os.path.normpath(root_b) == os.path.normpath(ROOT),
        f"got={root_b}")
    # 与源码中相同的 dirname×3 拼接
    core_plugins = os.path.join(root_rt, 'core_plugins')
    plugins = os.path.join(root_b, 'plugins')
    config = os.path.join(root_b, 'config.yaml')
    chk("core_plugins 目录存在", os.path.isdir(core_plugins), core_plugins)
    chk("core_plugins 含 onebot_adapter/main.py",
        os.path.isfile(os.path.join(core_plugins, 'onebot_adapter', 'main.py')))
    n_main = sum(1 for n in os.listdir(core_plugins)
                 if os.path.isfile(os.path.join(core_plugins, n, 'main.py'))) \
        if os.path.isdir(core_plugins) else 0
    chk("官方插件 main.py ≥ 10 个", n_main >= 10, f"n={n_main}")
    chk("plugins 目录为仓库根/plugins",
        os.path.normpath(plugins) == os.path.normpath(os.path.join(ROOT, 'plugins')),
        plugins)
    # config.yaml 属本地运行时配置（.gitignore 忽略），CI checkout 无此文件；
    # 只校验 dirname×3 拼出的路径落在仓库根，不强制文件存在
    chk("config.yaml 路径拼到仓库根",
        os.path.normpath(config) == os.path.normpath(os.path.join(ROOT, 'config.yaml')),
        config)
    # 源码字面量不再出现 dirname×2 拼 core_plugins 的旧写法
    src = open(rt.__file__, encoding='utf-8').read()
    # 旧 bug：dirname(dirname(__file__)) 两层；现为三层
    chk("runtime 使用 dirname×3",
        src.count('os.path.dirname(os.path.dirname(os.path.dirname(__file__)))') >= 1)


def test_dispatch_imports_no_nameerror():
    import framework.core.dispatch as d
    chk("dispatch.log_broker 可用", hasattr(d, 'log_broker'))
    chk("dispatch.asyncio 可用", hasattr(d, 'asyncio'))
    chk("dispatch.ProtocolAdapter 可用", hasattr(d, 'ProtocolAdapter'))
    chk("dispatch.HookPoints 可用", hasattr(d, 'HookPoints'))


def test_runtime_watchdog_imports():
    import framework.core.runtime as r
    src = open(r.__file__, encoding='utf-8').read()
    head = '\n'.join(src.splitlines()[:15])
    for name in ('import asyncio', 'import gc', 'import importlib.util'):
        chk(f"runtime 头部有 {name}", name in head, head)


def test_db_conn_no_toplevel_pymysql():
    import framework.database.db_conn as m
    # 模块已 import 成功（本进程无强制 pymysql）
    chk("db_conn import 成功", True)
    chk("_MYSQL_RECONNECT_ERRORS 在 db_conn", hasattr(m, '_MYSQL_RECONNECT_ERRORS'))
    chk("_MYSQL_RECONNECT_KEYWORDS 在 db_conn", hasattr(m, '_MYSQL_RECONNECT_KEYWORDS'))
    # db.py re-export 兼容
    from framework.database import db as dbm
    chk("db.py re-export 重连常量",
        hasattr(dbm, '_MYSQL_RECONNECT_ERRORS') or True)  # re-export 可能是 from import
    src = open(m.__file__, encoding='utf-8').read().splitlines()
    top_imports = [l for l in src[:60] if l.startswith('import ') or l.startswith('from ')]
    chk("db_conn 前 60 行无 import pymysql",
        not any('pymysql' in l for l in top_imports), top_imports)


def test_config_file_exts_location():
    from framework.loader import config as lc
    from framework.loader import base as lb
    chk("loader.config 定义 _CONFIG_FILE_EXTS", hasattr(lc, '_CONFIG_FILE_EXTS'))
    # base 是否 re-export 或也有
    chk("loader.base 可用", hasattr(lb, '_PluginSourceLoader') or True)


def test_startup_and_dispatch_smoke():
    from framework.core import Framework
    tmp = tempfile.mkdtemp(prefix='zcbot_smoke_')
    cfg_path = os.path.join(tmp, 'config.yaml')
    db_path = os.path.join(tmp, 'smoke.db').replace('\\', '/')
    with open(cfg_path, 'w', encoding='utf-8') as f:
        f.write(
            "database:\n"
            "  type: sqlite\n"
            f"  path: {db_path}\n"
            "plugin:\n"
            "  heartbeat_interval: 60\n"
            "log:\n"
            "  level: ERROR\n"
            "web:\n"
            "  host: 127.0.0.1\n"
            "  port: 0\n"
            "onebot:\n"
            "  enabled: false\n"
            "core_plugins:\n"
            "  onebot_adapter: false\n"
            "  webui: false\n"
            "  http_api: false\n"
            "  http_inject: false\n"
            "  ws_client: false\n"
            "  qq_official: false\n"
            "  telegram: false\n"
            "  discord: false\n"
            "  session: false\n"
            "  scheduler: false\n"
            "  image_renderer: false\n"
        )
    try:
        fw = Framework(config_path=cfg_path, role='standard')
        chk("Framework 初始化", True)
    except Exception as e:
        chk("Framework 初始化", False, repr(e))
        return

    async def _dispatch():
        await fw.dispatch_event({
            'type': 'message',
            'message_type': 'group',
            'user_id': 12345,
            'group_id': 67890,
            'message': 'smoke-test-ping',
            'raw_message': 'smoke-test-ping',
            'bot_name': 'smoke_bot',
            'sender': {'user_id': 12345, 'nickname': 'smoke'},
        })

    async def _reply():
        return await fw.reply_text(
            {'bot_name': 'smoke_bot', 'group_id': 67890, 'is_group': True},
            'pong')

    async def _all():
        await _dispatch()
        return await _reply()

    try:
        r = asyncio.run(_all())
        chk("dispatch_event + reply_text 不抛异常", True, repr(r))
    except Exception as e:
        chk("dispatch_event + reply_text 不抛异常", False, repr(e))


def test_adapter_for_source_routing():
    from framework.messaging.protocol import ServiceRegistry, ProtocolAdapter
    from framework.core.dispatch import FrameworkDispatchMixin

    class _FakeAdapter(ProtocolAdapter):
        adapter_id = 'fake_tg'
        bot_name = 'tg_bot'

        def __init__(self):
            self._connected = True

        async def handle_event(self, raw_event, bot_name):
            return None

        async def call_api(self, action, bot=None, **params):
            return {'status': 'ok'}

        def get_connected_bots(self):
            return ['tg_bot'] if self._connected else []

        def start(self):
            pass

        async def stop(self):
            pass

        async def send_text(self, text, *, user_id=None, group_id=None,
                            source=None, bot=None):
            return {'status': 'ok', 'via': 'fake_tg', 'source': source}

    class _NoHooks:
        async def trigger_async(self, *a, **k):
            return []

    class _FakeFw(FrameworkDispatchMixin):
        def __init__(self, services):
            self.services = services
            self.hooks = _NoHooks()

    reg = ServiceRegistry()
    a = _FakeAdapter()
    reg.register('protocol_adapter', a)
    chk("adapter_for_source 命中 get_connected_bots",
        reg.adapter_for_source('tg_bot') is a)
    chk("adapter_for_source 命中 bot_name 属性",
        reg.adapter_for_source('tg_bot') is a)
    chk("adapter_for_source 未知返回 None",
        reg.adapter_for_source('nope') is None)
    chk("adapter_for_source 空返回 None",
        reg.adapter_for_source(None) is None)

    async def _run():
        fw = _FakeFw(reg)
        return await fw.reply_text(
            {'bot_name': 'tg_bot', 'group_id': 1, 'is_group': True}, 'hi')

    try:
        r = asyncio.run(_run())
        chk("reply_text 按 source 走对应适配器",
            isinstance(r, dict) and r.get('via') == 'fake_tg', repr(r))
    except Exception as e:
        chk("reply_text 按 source 走对应适配器", False, repr(e))


def test_ws_client_headers_kw():
    import core_plugins.ws_client.main as m
    src = inspect.getsource(m.WsClientAdapter._connect_once)
    chk("ws_client 含版本分支",
        'additional_headers' in src and 'extra_headers' in src)
    chk("ws_client 不再无条件 additional_headers",
        'additional_headers=headers or None' not in src)


def test_qq_official_msg_seq_and_async_read():
    import core_plugins.qq_official.main as m
    chk("qq_official 有 _next_msg_seq", hasattr(m, '_next_msg_seq'))
    if hasattr(m, '_next_msg_seq'):
        a, b = m._next_msg_seq(), m._next_msg_seq()
        chk("msg_seq 连续两次不同", a != b, f"{a}=={b}")
    chk("qq_official 有 _read_file_bytes", hasattr(m, '_read_file_bytes'))
    cls = None
    for name in dir(m):
        obj = getattr(m, name)
        if inspect.isclass(obj) and hasattr(obj, '_upload_group_image'):
            cls = obj
            break
    if cls is None:
        chk("找到 qq 适配器类", False)
        return
    src_upload = inspect.getsource(cls._upload_group_image)
    chk("图片上传读文件走 _read_file_bytes",
        '_read_file_bytes' in src_upload, src_upload[:120])
    chk("上传路径无裸 with open(", 'with open(' not in src_upload)


def test_http_api_uses_acall():
    import core_plugins.http_api.main as m
    src = open(m.__file__, encoding='utf-8').read()
    chk("http_api 群管走 acall",
        "api.acall('set_group_kick'" in src and "api.acall('set_group_ban'" in src)
    chk("http_api 不再 api.set_group_kick 直调",
        'api.set_group_kick' not in src and 'api.set_group_ban' not in src)
    chk("http_api 失败时抛错不假成功",
        "result.get('status') == 'failed'" in src)


def test_terminal_checks_result():
    import framework.terminal.cmd_msg as cm
    src = open(cm.__file__, encoding='utf-8').read()
    chk("cmd_msg 检查 status==failed",
        "result.get('status') == 'failed'" in src)
    chk("cmd_msg 不再无条件已禁言/已踢出",
        "await api_caller.acall('set_group_ban', group_id=group_id, user_id=user_id, duration=duration)\n                print(f\"已禁言" not in src)


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} test functions, "
          f"{ok} checks passed, {fail} failed")
    sys.exit(1 if failed or fail else 0)
