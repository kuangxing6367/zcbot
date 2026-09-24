# -*- coding: utf-8 -*-
"""
启动 + 分发冒烟测试
====================
行为级测试：真调用被测函数，断言实际副作用/返回值（不做源码字符串匹配）。

运行：
    python tests/test_smoke.py
"""
import asyncio
import contextlib
import importlib.util
import inspect
import io
import os
import sys
import tempfile
import threading

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


class _RecordingApi:
    """只提供 acall 的 API 调用器（故意没有 set_group_kick 等具体方法）"""

    def __init__(self, result=None):
        self.result = result if result is not None else {'status': 'ok'}
        self.calls = []

    async def acall(self, action, **params):
        self.calls.append((action, params))
        return self.result


class _RecordingAdapter:
    """中立 send_text 适配器，记录调用"""

    def __init__(self):
        self.sent = []

    async def send_text(self, text, *, user_id=None, group_id=None,
                        source=None, bot=None):
        self.sent.append({'text': text, 'user_id': user_id,
                          'group_id': group_id, 'source': source})
        return {'status': 'ok', 'via': 'rec', 'text': text}


def _run_with_background_loop(fn, timeout=5):
    """在后台线程跑一个正在运行的 event loop，fn(loop) 在主线程执行"""
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    if not ready.wait(timeout):
        raise RuntimeError('event loop 未就绪')
    try:
        return fn(loop)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)
        loop.close()


def test_paths_and_repo_layout():
    import framework.core.runtime as rt
    import framework.core.base as b
    root_rt = _repo_root_from(rt.__file__)
    root_b = _repo_root_from(b.__file__)
    chk("runtime 模块根=仓库根", os.path.normpath(root_rt) == os.path.normpath(ROOT),
        f"got={root_rt}")
    chk("base 模块根=仓库根", os.path.normpath(root_b) == os.path.normpath(ROOT),
        f"got={root_b}")

    core_plugins = os.path.join(root_rt, 'core_plugins')
    chk("core_plugins 目录存在", os.path.isdir(core_plugins), core_plugins)
    chk("core_plugins 含 onebot_adapter/main.py",
        os.path.isfile(os.path.join(core_plugins, 'onebot_adapter', 'main.py')))
    n_main = sum(1 for n in os.listdir(core_plugins)
                 if os.path.isfile(os.path.join(core_plugins, n, 'main.py'))) \
        if os.path.isdir(core_plugins) else 0
    chk("官方插件 main.py ≥ 10 个", n_main >= 10, f"n={n_main}")

    # 默认 config 路径公式（dirname×3 + config.yaml）落在仓库根；
    # 文件本身在 .gitignore，不要求存在
    default_cfg = os.path.join(root_b, 'config.yaml')
    chk("默认 config 路径落在仓库根",
        os.path.normpath(os.path.dirname(default_cfg)) == os.path.normpath(ROOT),
        default_cfg)

    # 真调用 dirname×3 的路径方法（实例只需 config 字段）
    class _Mini:
        config = {}
    plugins_dir = b.Framework._get_plugins_dir(_Mini())
    dat_dir = b.Framework._get_plugins_dat_dir(_Mini())
    chk("_get_plugins_dir = 仓库根/plugins",
        os.path.normpath(plugins_dir) == os.path.normpath(os.path.join(ROOT, 'plugins')),
        plugins_dir)
    chk("_get_plugins_dat_dir = 仓库根/data/plugins_dat",
        os.path.normpath(dat_dir) == os.path.normpath(
            os.path.join(ROOT, 'data', 'plugins_dat')),
        dat_dir)

    # runtime._load_core_plugins 使用的 dirname×3 目录：真列目录
    rt_dir_expr = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(rt.__file__))), 'core_plugins')
    chk("runtime 拼出的 core_plugins 即仓库 core_plugins",
        os.path.isdir(rt_dir_expr) and os.path.samefile(rt_dir_expr, core_plugins),
        rt_dir_expr)

    # 插件合成包加载器真能按文件定位（spec_from_file_location 路径有效）
    main_path = os.path.join(core_plugins, 'onebot_adapter', 'main.py')
    spec = importlib.util.spec_from_file_location('_smoke_probe', main_path)
    chk("onebot_adapter main.py 可生成 spec", spec is not None and spec.loader is not None)


def test_dispatch_imports_no_nameerror():
    import framework.core.dispatch as d
    chk("dispatch.log_broker 可用", hasattr(d, 'log_broker'))
    chk("dispatch.asyncio 可用", hasattr(d, 'asyncio'))
    chk("dispatch.ProtocolAdapter 可用", hasattr(d, 'ProtocolAdapter'))
    chk("dispatch.HookPoints 可用", hasattr(d, 'HookPoints'))


def test_runtime_watchdog_imports():
    # 模块命名空间里必须真的绑定了这些名字（缺 import 会在这里暴露）
    import framework.core.runtime as r
    chk("runtime 命名空间含 asyncio", hasattr(r, 'asyncio'))
    chk("runtime 命名空间含 gc", hasattr(r, 'gc'))
    chk("runtime 命名空间含 importlib", hasattr(r, 'importlib'))


def test_db_conn_import_without_pymysql():
    import ast

    import framework.database.db_conn as m
    chk("_MYSQL_RECONNECT_ERRORS 在 db_conn", hasattr(m, '_MYSQL_RECONNECT_ERRORS'))
    chk("_MYSQL_RECONNECT_KEYWORDS 在 db_conn", hasattr(m, '_MYSQL_RECONNECT_KEYWORDS'))
    from framework.database import db as dbm
    chk("db.py re-export _MYSQL_RECONNECT_ERRORS",
        hasattr(dbm, '_MYSQL_RECONNECT_ERRORS'))
    chk("db.py re-export _MYSQL_RECONNECT_KEYWORDS",
        hasattr(dbm, '_MYSQL_RECONNECT_KEYWORDS'))

    # AST：模块顶层不允许 import pymysql（MySQL 可选依赖必须延迟）
    tree = ast.parse(open(m.__file__, encoding='utf-8').read())
    top_names = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            top_names.append(node.module or '')
    chk("db_conn 顶层无 pymysql import",
        not any(n == 'pymysql' or n.startswith('pymysql.') for n in top_names),
        top_names)

    # 行为：拦掉 pymysql 后模块仍可重新加载（不依赖该可选包）
    class _BlockPymysql:
        def find_spec(self, name, path=None, target=None):
            if name == 'pymysql' or name.startswith('pymysql.'):
                raise ImportError('pymysql blocked for smoke test')
            return None

    blocker = _BlockPymysql()
    sys.meta_path.insert(0, blocker)
    saved = sys.modules.pop('framework.database.db_conn', None)
    try:
        import importlib as _il
        reloaded = _il.import_module('framework.database.db_conn')
        chk("屏蔽 pymysql 后 db_conn 仍可导入",
            hasattr(reloaded, '_MYSQL_RECONNECT_ERRORS'))
    except ImportError as e:
        chk("屏蔽 pymysql 后 db_conn 仍可导入", False, repr(e))
    finally:
        sys.meta_path.remove(blocker)
        if saved is not None:
            sys.modules['framework.database.db_conn'] = saved


def test_config_file_exts_location():
    from framework.loader import base as lb
    from framework.loader import config as lc
    chk("loader.config 定义 _CONFIG_FILE_EXTS", hasattr(lc, '_CONFIG_FILE_EXTS'))
    chk("loader.base 导出 _PluginSourceLoader", hasattr(lb, '_PluginSourceLoader'))


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

    chk("fw.config_path 指向传入配置",
        os.path.normpath(fw.config_path) == os.path.normpath(cfg_path),
        fw.config_path)
    chk("fw._get_plugins_dir 落在仓库根",
        os.path.normpath(fw._get_plugins_dir()) == os.path.normpath(
            os.path.join(ROOT, 'plugins')),
        fw._get_plugins_dir())

    # 核心插件加载路径：全部禁用时应安静扫过目录（真跑 _load_core_plugins）
    try:
        fw._load_core_plugins()
        chk("_load_core_plugins 全禁用不抛异常", True)
    except Exception as e:
        chk("_load_core_plugins 全禁用不抛异常", False, repr(e))

    # 原始消息 handler 短路 + after_dispatch 钩子必须真的触发
    raw_seen = []
    after_seen = []

    def _raw(data, bot_name):
        raw_seen.append((data.get('message'), bot_name))
        return True  # 接管，跳过后续路由

    async def _after(event, bot_name):
        after_seen.append((event.get('message'), bot_name))

    fw.register_raw_message_handler('smoke', _raw)
    fw.hooks.register('event.after_dispatch', 'smoke_after', _after)

    echo = _RecordingAdapter()
    fw.services.register('protocol_adapter', echo)
    fw.services.register('api_caller', echo)

    async def _all():
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
        return await fw.reply_text(
            {'bot_name': 'smoke_bot', 'group_id': 67890, 'is_group': True},
            'pong')

    try:
        r = asyncio.run(_all())
        chk("raw handler 收到事件并短路",
            raw_seen == [('smoke-test-ping', 'smoke_bot')], repr(raw_seen))
        chk("event.after_dispatch 触发",
            after_seen == [('smoke-test-ping', 'smoke_bot')], repr(after_seen))
        chk("reply_text 真调用适配器 send_text",
            isinstance(r, dict) and r.get('via') == 'rec' and r.get('text') == 'pong',
            repr(r))
        chk("适配器记录了 group 回复",
            echo.sent and echo.sent[-1]['group_id'] == 67890
            and echo.sent[-1]['text'] == 'pong',
            repr(echo.sent))
    except Exception as e:
        chk("dispatch_event + reply_text 不抛异常", False, repr(e))


def test_adapter_for_source_routing():
    from framework.core.dispatch import FrameworkDispatchMixin
    from framework.messaging.protocol import ProtocolAdapter, ServiceRegistry

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
    chk("adapter_for_source 未知返回 None",
        reg.adapter_for_source('nope') is None)
    chk("adapter_for_source 空返回 None",
        reg.adapter_for_source(None) is None)

    # 第二个适配器：验证按 source 分流到不同实例
    b = _FakeAdapter()
    b.adapter_id = 'fake_tg_2'
    b.bot_name = 'tg_bot_2'
    reg.register('protocol_adapter_2', b)
    chk("adapter_for_source 分流到第二实例",
        reg.adapter_for_source('tg_bot_2') is b and reg.adapter_for_source('tg_bot') is a)

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

    captured = {}

    class _FakeWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, *a, **k):
            pass

        def __aiter__(self):
            async def _empty():
                return
                yield  # pragma: no cover
            return _empty()

    def _fake_connect(url, **kwargs):
        captured.clear()
        captured.update(kwargs)
        captured['_url'] = url
        return _FakeWS()

    adapter = m.WsClientAdapter(
        framework=object(), url='ws://127.0.0.1:9/ws', token='sekrit')

    async def _noop_recv(ws):
        return None

    adapter._recv_loop = _noop_recv
    orig_connect = m.websockets.connect
    orig_ver = m.websockets.__version__
    m.websockets.connect = _fake_connect
    try:
        for ver, expect_kw, forbid_kw in (
                ('14.1.0', 'additional_headers', 'extra_headers'),
                ('13.1.0', 'extra_headers', 'additional_headers'),
                ('12.0.0', 'extra_headers', 'additional_headers')):
            m.websockets.__version__ = ver
            asyncio.run(adapter._connect_once())
            chk(f"websockets {ver} 使用 {expect_kw}",
                expect_kw in captured and forbid_kw not in captured,
                repr({k: v for k, v in captured.items() if 'header' in k}))
            chk(f"websockets {ver} 带上 Bearer token",
                captured.get(expect_kw, {}).get('Authorization') == 'Bearer sekrit',
                repr(captured.get(expect_kw)))
            chk(f"websockets {ver} 连上目标 URL",
                captured.get('_url') == 'ws://127.0.0.1:9/ws', repr(captured.get('_url')))
        chk("连接后 _connected 置位", adapter._connected is True)
    except Exception as e:
        chk("_connect_once 行为", False, repr(e))
    finally:
        m.websockets.connect = orig_connect
        m.websockets.__version__ = orig_ver


def test_qq_official_msg_seq_and_async_read():
    import core_plugins.qq_official.main as m

    a, b = m._next_msg_seq(), m._next_msg_seq()
    chk("msg_seq 连续两次不同", a != b, f"{a}=={b}")
    chk("msg_seq 在 1..9999", 1 <= a <= 9999 and 1 <= b <= 9999, f"{a},{b}")

    # 真读文件
    with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + b'x' * 32)
        path = f.name
    try:
        got = m._read_file_bytes(path)
        chk("_read_file_bytes 读回内容", got == b'\x89PNG\r\n\x1a\n' + b'x' * 32,
            repr(got[:16] if got else None))
        chk("_read_file_bytes 缺文件返回 None",
            m._read_file_bytes(path + '.nope') is None)
    finally:
        os.unlink(path)

    # 真调 _upload_group_image：本地路径分支必须经 _read_file_bytes 读出字节再上传
    adapter = object.__new__(m.QQOfficialAdapter)
    api_calls = []

    def _fake_api(method, path_arg, **kw):
        api_calls.append((method, path_arg, kw))
        return {'file_info': 'FILE_INFO_SMOKE'}

    adapter._api_request = _fake_api

    png = b'\x89PNG\r\n\x1a\n' + b'payload'
    with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as f:
        f.write(png)
        img_path = f.name
    try:
        result = asyncio.run(
            adapter._upload_group_image('grp_openid', {'file': img_path}))
        chk("本地图片上传返回 file_info",
            result == {'file_info': 'FILE_INFO_SMOKE'}, repr(result))
        chk("上传真的发起了 POST files",
            len(api_calls) == 1 and api_calls[0][0] == 'POST'
            and api_calls[0][1] == '/v2/groups/grp_openid/files',
            repr([(c[0], c[1]) for c in api_calls]))
        files = api_calls[0][2].get('files') if api_calls else None
        chk("上传携带读到的原始字节",
            bool(files) and files.get('file', (None, None, None))[1] == png,
            repr(files))
    except Exception as e:
        chk("_upload_group_image 本地路径", False, repr(e))
    finally:
        os.unlink(img_path)

    # 缺文件 → 不发请求、返回 None
    api_calls.clear()
    result = asyncio.run(adapter._upload_group_image(
        'grp_openid', {'file': img_path + '.gone'}))
    chk("缺文件上传返回 None 且不调 API",
        result is None and api_calls == [], f"{result!r} {api_calls!r}")


def test_http_api_group_admin_behavior():
    import core_plugins.http_api.main as m

    results = []

    def _run(loop):
        # 成功：只有 acall，没有 set_group_kick 方法 → 旧代码会 AttributeError
        ok_api = _RecordingApi({'status': 'ok'})
        fw_ok = type('F', (), {})()
        fw_ok.services = type('S', (), {
            'get': staticmethod(lambda k: ok_api if k == 'api_caller' else None)
        })()
        fw_ok.loop = loop

        h = object.__new__(m.ApiHandler)
        h.framework = fw_ok
        h._send_json = lambda code, data: results.append(('kick_ok', code, data))
        h._handle_kick({'group_id': 1, 'user_id': 2})
        h._send_json = lambda code, data: results.append(('ban_ok', code, data))
        h._handle_ban({'group_id': 1, 'user_id': 2, 'duration': 60})
        h._send_json = lambda code, data: results.append(('unban_ok', code, data))
        h._handle_unban({'group_id': 1, 'user_id': 2})

        # 失败：status=failed → 必须 500，不能假成功
        bad_api = _RecordingApi({'status': 'failed', 'msg': '权限不足'})
        fw_bad = type('F', (), {})()
        fw_bad.services = type('S', (), {
            'get': staticmethod(lambda k: bad_api if k == 'api_caller' else None)
        })()
        fw_bad.loop = loop
        h2 = object.__new__(m.ApiHandler)
        h2.framework = fw_bad
        h2._send_json = lambda code, data: results.append(('kick_fail', code, data))
        h2._handle_kick({'group_id': 1, 'user_id': 2})

        # 缺参 → 400
        h._send_json = lambda code, data: results.append(('kick_badreq', code, data))
        h._handle_kick({'group_id': 1})

        return ok_api, bad_api

    try:
        ok_api, bad_api = _run_with_background_loop(_run)
        by = {tag: (code, data) for tag, code, data in results}

        chk("kick 成功走 acall 且 200",
            by.get('kick_ok', (None,))[0] == 200
            and ok_api.calls[0][0] == 'set_group_kick'
            and ok_api.calls[0][1] == {'group_id': 1, 'user_id': 2},
            repr(by.get('kick_ok')))
        chk("ban 成功走 acall 且 200",
            by.get('ban_ok', (None,))[0] == 200
            and ok_api.calls[1] == (
                'set_group_ban',
                {'group_id': 1, 'user_id': 2, 'duration': 60}),
            repr(by.get('ban_ok')))
        chk("unban 成功 duration=0",
            by.get('unban_ok', (None,))[0] == 200
            and ok_api.calls[2] == (
                'set_group_ban',
                {'group_id': 1, 'user_id': 2, 'duration': 0}),
            repr(by.get('unban_ok')))
        chk("kick 失败 status=failed → 500 带原因",
            by.get('kick_fail', (None,))[0] == 500
            and '权限不足' in str(by.get('kick_fail', (None, {}))[1]),
            repr(by.get('kick_fail')))
        chk("kick 缺参 → 400",
            by.get('kick_badreq', (None,))[0] == 400,
            repr(by.get('kick_badreq')))
        chk("全程未依赖 set_group_* 具体方法（只调 acall）",
            all(a in ('set_group_kick', 'set_group_ban')
                for a, _ in ok_api.calls + bad_api.calls))
    except Exception as e:
        chk("http_api 群管行为", False, repr(e))


def test_terminal_ban_kick_result():
    import framework.terminal.cmd_msg as cm
    from framework.terminal.command import terminal_commands

    outputs = {}

    def _run_case(name, api_result, args, expect_substr, forbid_substr):
        api = _RecordingApi(api_result)

        class _Svc:
            @staticmethod
            def get(key):
                return api if key == 'api_caller' else None

        class _Fw:
            services = _Svc()

        # register(fw) 闭包绑定 _Fw 实例
        cm.register(_Fw())
        handler = terminal_commands.get(name)
        if handler is None:
            chk(f"{name} 命令已注册", False, 'handler=None')
            return
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            asyncio.run(handler(args))
        out = buf.getvalue()
        outputs[name + args] = out
        chk(f"{name} 输出含「{expect_substr}」", expect_substr in out, repr(out))
        if forbid_substr:
            chk(f"{name} 不出现「{forbid_substr}」", forbid_substr not in out, repr(out))
        return api

    try:
        api = _run_case(
            'ban', {'status': 'failed', 'msg': '机器人权限不够'},
            'g:100 200 1', '禁言失败', '已禁言')
        chk("ban 失败仍调用了 acall",
            bool(api and api.calls and api.calls[0][0] == 'set_group_ban'),
            repr(api.calls if api else None))

        api = _run_case(
            'ban', {'status': 'ok'}, 'g:100 200 1', '已禁言', '禁言失败')
        chk("ban 成功参数正确",
            api.calls[0] == ('set_group_ban',
                             {'group_id': 100, 'user_id': 200, 'duration': 60}),
            repr(api.calls))

        api = _run_case(
            'kick', {'status': 'failed', 'msg': '目标是群主'},
            '100 200', '踢出失败', '已踢出')
        chk("kick 失败仍调用了 acall",
            bool(api and api.calls and api.calls[0][0] == 'set_group_kick'),
            repr(api.calls if api else None))

        api = _run_case(
            'kick', {'status': 'ok'}, '100 200', '已踢出', '踢出失败')
        chk("kick 成功参数正确",
            api.calls[0] == ('set_group_kick',
                             {'group_id': 100, 'user_id': 200}),
            repr(api.calls))

        api = _run_case(
            'unban', {'status': 'failed', 'msg': 'nope'},
            'g:100 200', '解禁失败', '已解除')
        chk("unban 失败参数 duration=0",
            api.calls[0] == ('set_group_ban',
                             {'group_id': 100, 'user_id': 200, 'duration': 0}),
            repr(api.calls))
    except Exception as e:
        chk("终端 ban/kick 行为", False, repr(e))


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
