# -*- coding: utf-8 -*-
"""aiwriter（终端 AI 智能体）聚焦回归测试（离线，不联网）。

覆盖：
1. 加载路径：框架 loader 以「顶层模块」加载 main.py 不炸（相对导入 + 同目录兜底），
   register 注册终端命令 `ai`（别名 agent，target=core），unregister 摘除。
2. 工具集 ToolRegistry：能力闸门（readonly/workspace/full）、specs 按模式过滤、
   路径越界拒绝、read/write/edit/glob/grep 行为、edit 唯一性语义、shell 闸门。
3. Agent 循环：工具调用闭环（事件流 + 文件落地）、工具异常回灌、max_rounds 收口。
4. SessionStore：裁剪、build_messages、压缩（注入 summarize）。
5. prompt.build：按工具集拼装工具指引。
6. 终端命令：ai reset 清记忆、未配 api_key 提示、一次性执行（桩 stream_chat）。

运行：
    python -m pytest tests/test_aiwriter.py -v
    python tests/test_aiwriter.py
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core_plugins.aiwriter import agent as aw_agent  # noqa: E402
from core_plugins.aiwriter import main as aw_main  # noqa: E402
from core_plugins.aiwriter import prompt as aw_prompt  # noqa: E402
from core_plugins.aiwriter import session as aw_session  # noqa: E402
from core_plugins.aiwriter.tools import (  # noqa: E402
    PermissionDenied, ToolError, ToolRegistry)

_ORIG_STREAM = aw_agent.stream_chat


# ── 桩 ──────────────────────────────────────────────────────────────────
class _FakeCtx:
    def __init__(self, fw, data_dir):
        self._framework = fw
        self._data_dir = data_dir
        self.logs = []

    def get_data_dir(self):
        return self._data_dir

    def log(self, msg, level='info'):
        self.logs.append((level, msg))


class _FakeFw:
    def __init__(self, config=None):
        self.config = config or {}


def _ws(mode="workspace", allow_exec=False):
    d = tempfile.mkdtemp(prefix="aiwriter_ws_")
    return ToolRegistry(d, mode=mode, allow_exec=allow_exec), d


def _mk_stream(responses):
    calls = {"n": 0}

    def fake(cfg, messages, tools):
        i = calls["n"]
        calls["n"] += 1
        for item in responses[min(i, len(responses) - 1)]:
            yield item

    return fake


def _capture(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **k)
    return buf.getvalue()


def _register(cfg=None):
    d = tempfile.mkdtemp(prefix="aiwriter_reg_")
    ctx = _FakeCtx(_FakeFw(cfg or {"aiwriter": {"api_key": "sk-x"}}), d)
    aw_main.register(ctx)
    return ctx, d


# ── 1. 加载路径 ─────────────────────────────────────────────────────────

def test_framework_loader_path_and_terminal_command():
    """框架 loader（顶层模块）加载 main.py 必须成功，并注册终端命令 ai。"""
    spec = importlib.util.spec_from_file_location(
        "core_plugin_aiwriter_t", os.path.join(ROOT, "core_plugins", "aiwriter", "main.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["core_plugin_aiwriter_t"] = mod
    spec.loader.exec_module(mod)  # 不抛 ImportError 即通过
    assert mod.__plugin_meta__["process"] == "core"

    from framework.terminal import terminal_commands
    d = tempfile.mkdtemp(prefix="aiwriter_loader_")
    mod.register(_FakeCtx(_FakeFw({"aiwriter": {"api_key": "sk"}}), d))
    try:
        assert terminal_commands.get("code") is not None
        assert terminal_commands.get("oc") is not None
        assert terminal_commands.get_target("code") == "core"
    finally:
        mod.unregister()
    assert terminal_commands.get("code") is None


def test_framework_loader_no_top_level_module_collision():
    """框架顶层模块加载时，不得依赖/污染通用顶层名（agent/tools/llm…）。

    回归：plugins/llm_core 也有 agent.py / tools.py，会把短名 agent/tools
    写进 sys.modules；旧实现用 `from agent import Agent` 兜底会撞上它，
    报 cannot import name 'Agent' from 'plugin_llm_core.agent'。
    """
    import types
    occupied = ('agent', 'tools', 'llm', 'session', 'prompt')
    saved = {n: sys.modules.get(n) for n in occupied}
    for n in occupied:
        sys.modules[n] = types.ModuleType(n)          # 占名，模拟其它插件
    pkg = "core_plugin_aiwriter_col"
    try:
        spec = importlib.util.spec_from_file_location(
            pkg, os.path.join(ROOT, "core_plugins", "aiwriter", "main.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[pkg] = mod
        spec.loader.exec_module(mod)
        assert mod._PKG == pkg
        tui_mod = mod._load_tui()
        assert tui_mod.__name__.startswith(pkg)        # 从本插件包解析，未撞车
        assert mod.Agent.__module__.startswith(pkg)
        # 未污染顶层短名
        assert sys.modules['agent'].__name__ == 'agent'
    finally:
        for k in [k for k in list(sys.modules) if k == pkg or k.startswith(pkg + '.')]:
            sys.modules.pop(k, None)
        for n, orig in saved.items():
            if orig is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = orig


def test_terminal_command_remove_helper():
    from framework.terminal.command import TerminalCommand
    reg = TerminalCommand()
    reg.register("zzz", lambda a: None, "d", aliases=["zz"], target="core")
    assert reg.get("zzz") is not None and reg.get("zz") is not None
    assert reg.remove("zzz") is True
    assert reg.get("zzz") is None and reg.get("zz") is None
    assert reg.remove("zzz") is False


# ── 2. 工具集 ───────────────────────────────────────────────────────────

def test_tool_caps_by_mode():
    assert ToolRegistry("x", mode="readonly").allowed_caps() == {"read"}
    assert ToolRegistry("x", mode="workspace").allowed_caps() == {"read", "write"}
    assert ToolRegistry("x", mode="full", allow_exec=False).allowed_caps() == {"read", "write"}
    assert ToolRegistry("x", mode="full", allow_exec=True).allowed_caps() == {"read", "write", "exec"}


def test_tool_specs_filtered_by_mode():
    names_ro = {s["name"] for s in ToolRegistry("x", mode="readonly").specs()}
    assert names_ro == {"read", "list", "glob", "grep",
                        "webfetch", "websearch", "db_query", "plugin_list"}
    names_ws = {s["name"] for s in ToolRegistry("x", mode="workspace").specs()}
    assert {"write", "edit"} <= names_ws
    assert not ({"shell", "python", "db_execute", "plugin_action"} & names_ws)
    names_full = {s["name"] for s in ToolRegistry("x", mode="full", allow_exec=True).specs()}
    assert {"shell", "python", "db_execute", "plugin_action"} <= names_full


def test_tool_path_boundary_rejects_escape():
    reg, _ = _ws()
    try:
        reg.call("read", {"path": "../outside.txt"})
    except ToolError:
        pass
    else:
        raise AssertionError("越界路径必须被拒绝")


def test_tool_write_read_edit_roundtrip():
    reg, _ = _ws(mode="workspace")
    assert "已写入" in reg.call("write", {"path": "sub/a.txt", "content": "hello\nworld\n"})
    read = reg.call("read", {"path": "sub/a.txt"})
    assert "1\thello" in read and "2\tworld" in read
    # edit 唯一命中
    assert "替换 1 处" in reg.call("edit", {"path": "sub/a.txt",
                                          "oldString": "world", "newString": "ZCBOT"})
    assert "ZCBOT" in reg.call("read", {"path": "sub/a.txt"})
    # newString == oldString 拒绝
    try:
        reg.call("edit", {"path": "sub/a.txt", "oldString": "ZCBOT", "newString": "ZCBOT"})
    except ToolError:
        pass
    else:
        raise AssertionError("newString 与 oldString 相同必须报错")
    # oldString 未命中
    try:
        reg.call("edit", {"path": "sub/a.txt", "oldString": "NOPE", "newString": "x"})
    except ToolError:
        pass
    else:
        raise AssertionError("未命中的 oldString 必须报错")


def test_tool_edit_multi_match_requires_replace_all():
    reg, _ = _ws(mode="workspace")
    reg.call("write", {"path": "m.txt", "content": "a\na\n"})
    try:
        reg.call("edit", {"path": "m.txt", "oldString": "a", "newString": "b"})
    except ToolError:
        pass
    else:
        raise AssertionError("多处命中且未 replaceAll 必须报错")
    assert "替换 2 处" in reg.call("edit", {"path": "m.txt", "oldString": "a",
                                          "newString": "b", "replaceAll": True})


def test_tool_glob_and_grep():
    reg, _ = _ws(mode="workspace")
    reg.call("write", {"path": "pkg/mod.py", "content": "def target():\n    return 1\n"})
    reg.call("write", {"path": "pkg/readme.md", "content": "target here\n"})
    g = reg.call("glob", {"pattern": "**/*.py"})
    assert "mod.py" in g
    hits = reg.call("grep", {"pattern": "def target", "include": "*.py"})
    assert "mod.py:1" in hits


def test_tool_permission_denied_on_write_in_readonly():
    reg, _ = _ws(mode="readonly")
    try:
        reg.call("write", {"path": "x.txt", "content": "y"})
    except PermissionDenied:
        pass
    else:
        raise AssertionError("readonly 写必须 PermissionDenied")


def test_tool_shell_gated():
    ws, _ = _ws(mode="workspace")
    try:
        ws.call("shell", {"command": "echo hi"})
    except PermissionDenied:
        pass
    else:
        raise AssertionError("workspace 模式 shell 必须拒绝")
    full, _ = _ws(mode="full", allow_exec=True)
    out = full.call("shell", {"command": "echo zcbot_probe"})
    assert "zcbot_probe" in out


def test_tool_unknown_name_raises():
    reg, _ = _ws()
    try:
        reg.call("nope", {})
    except ToolError:
        pass
    else:
        raise AssertionError("未知工具必须报错")


# ── 3. Agent 循环 ───────────────────────────────────────────────────────

def test_agent_loop_tool_then_final():
    reg, d = _ws(mode="workspace")
    events = []
    responses = [
        [("tool", {"name": "write",
                   "arguments": json.dumps({"path": "a.txt", "content": "hi"})})],
        [("text", "写好了")],
    ]
    aw_agent.stream_chat = _mk_stream(responses)
    try:
        agent = aw_agent.Agent({"max_rounds": 6}, reg,
                               on_event=lambda k, p=None: events.append((k, p)))
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "写文件"}]
        final = agent.run(msgs)
    finally:
        aw_agent.stream_chat = _ORIG_STREAM
    assert final == "写好了"
    kinds = [k for k, _ in events]
    assert "tool_start" in kinds and "tool_end" in kinds and "text" in kinds
    assert any(k == "tool_end" and p["ok"] for k, p in events if k == "tool_end")
    with open(os.path.join(d, "a.txt"), encoding="utf-8") as f:
        assert f.read() == "hi"


def test_agent_tool_error_fed_back():
    reg, _ = _ws(mode="readonly")
    events = []
    responses = [
        [("tool", {"name": "write",
                   "arguments": json.dumps({"path": "a.txt", "content": "x"})})],
        [("text", "放弃了")],
    ]
    aw_agent.stream_chat = _mk_stream(responses)
    try:
        agent = aw_agent.Agent({"max_rounds": 6}, reg,
                               on_event=lambda k, p=None: events.append((k, p)))
        final = agent.run([{"role": "user", "content": "写文件"}])
    finally:
        aw_agent.stream_chat = _ORIG_STREAM
    assert final == "放弃了"
    ends = [p for k, p in events if k == "tool_end"]
    assert ends and ends[0]["ok"] is False


def test_agent_max_rounds_forces_stop():
    reg, _ = _ws(mode="readonly")
    aw_agent.stream_chat = _mk_stream(
        [[("tool", {"name": "read", "arguments": json.dumps({"path": "x"})})]])
    try:
        agent = aw_agent.Agent({"max_rounds": 2}, reg)
        final = agent.run([{"role": "user", "content": "loop"}])
    finally:
        aw_agent.stream_chat = _ORIG_STREAM
    assert "最大轮数" in final


# ── 4. SessionStore ─────────────────────────────────────────────────────

def test_session_append_and_build_messages():
    d = tempfile.mkdtemp(prefix="aiwriter_sess_")
    store = aw_session.SessionStore(os.path.join(d, "s.json"), keep_turns=1)
    store.append_turn("k", "u1", "a1")
    store.append_turn("k", "u2", "a2")
    # 追加不提前裁剪（「保留最近若干轮」交给 compact 按需处理）
    hist = store.history("k")
    assert hist == [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"},
                    {"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"}]
    msgs = store.build_messages("k", "SYS", "u3")
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[-1] == {"role": "user", "content": "u3"}
    assert len(msgs) == 6


def test_session_compaction_with_injected_summarizer():
    d = tempfile.mkdtemp(prefix="aiwriter_sess2_")
    store = aw_session.SessionStore(os.path.join(d, "s.json"), keep_turns=1, max_chars=200)
    for i in range(5):
        store.append_turn("k", "u%d" % i + "x" * 100, "a%d" % i + "y" * 100)
    assert store.needs_compaction("k") is True
    seen = {}

    def _summ(older):
        seen["n"] = len(older)
        return "早期共 %d 条" % len(older)

    store.compact("k", _summ)
    hist = store.history("k")
    assert hist[0]["role"] == "assistant" and "摘要" in hist[0]["content"]
    assert seen["n"] > 0
    # 摘要失败 → 退化为纯裁剪，不抛
    store2 = aw_session.SessionStore(os.path.join(d, "s2.json"), keep_turns=1, max_chars=200)
    for i in range(5):
        store2.append_turn("k", "u%d" % i + "x" * 100, "a%d" % i + "y" * 100)
    store2.compact("k", lambda older: (_ for _ in ()).throw(RuntimeError("boom")))
    assert len(store2.history("k")) == 2


# ── 5. prompt ───────────────────────────────────────────────────────────

def test_prompt_build_tool_guidance():
    p_full = aw_prompt.build(["read", "write", "edit", "shell"])
    assert "AI 智能体" in p_full
    assert "shell" in p_full and "edit" in p_full
    p_ro = aw_prompt.build(["read", "grep"])
    assert "工具使用" not in p_ro


# ── 5b. 配置 / 框架装配 ─────────────────────────────────────────────────

def test_load_cfg_tolerates_bool_enabled():
    """core_plugins.aiwriter 的值是布尔开关（非配置字典）时不得炸。"""
    fw = _FakeFw({"core_plugins": {"aiwriter": True}, "aiwriter": {"model": "m2"}})
    assert aw_main._load_cfg(fw)["model"] == "m2"
    cfg = aw_main._load_cfg(_FakeFw({"core_plugins": {"aiwriter": False}}))
    assert cfg["model"] == "deepseek-chat"


def test_framework_core_plugin_loader_wires_terminal_command():
    """走框架真实 core 插件加载路径：aiwriter 加载后注册 code 终端命令。"""
    import threading

    from framework.core.runtime import FrameworkRuntimeMixin
    from framework.messaging.protocol import ServiceRegistry
    from framework.terminal import terminal_commands

    d = tempfile.mkdtemp(prefix="aiwriter_fwload_")

    class _PL:
        def __init__(self):
            self.plugins_dat_dir = d
            self._lock = threading.Lock()
            self._loaded_plugins = {}

        def get_plugin_module(self, n):
            return None

    class _FW(FrameworkRuntimeMixin):
        def __init__(self):
            self.config = {"core_plugins": {"aiwriter": True},
                           "aiwriter": {"api_key": "sk"}}
            self._role = "standard"
            self.plugin_loader = _PL()
            self.services = ServiceRegistry()
            self.db = None
            self.loop = None

    terminal_commands.remove("code")
    _FW()._load_core_plugins()
    try:
        assert terminal_commands.get("code") is not None
        assert terminal_commands.get_target("code") == "core"
        assert terminal_commands.get("ai") is None      # 不再有 /ai 式命令
    finally:
        terminal_commands.remove("code")


def test_bootstrap_standalone_cli():
    """独立 CLI 启动：不启 bot 即可初始化插件状态并打开界面。"""
    import framework.config as fc

    orig_load = fc.load_config
    orig_yaml = fc.CORE_PLUGINS_YAML
    orig_root = aw_main._ROOT
    d = tempfile.mkdtemp(prefix="aiwriter_cli_")
    fc.load_config = lambda p=None: {"aiwriter": {"api_key": "sk", "mode": "workspace"}}
    fc.CORE_PLUGINS_YAML = os.path.join(d, "core_plugins.yaml")
    aw_main._ROOT = d
    try:
        fw, plugin = aw_main.bootstrap(None)
        assert fw._running is True
        assert plugin._TOOLS is not None and plugin._STORE is not None
        assert os.path.isdir(fw.plugin_loader.plugins_dat_dir)
        assert callable(aw_main.run_cli)
        from framework.terminal import terminal_commands
        assert terminal_commands.get("code") is not None
    finally:
        aw_main._ROOT = orig_root
        fc.load_config = orig_load
        fc.CORE_PLUGINS_YAML = orig_yaml
        aw_main.unregister()


# ── 6. 界面（Textual）────────────────────────────────────────────────────

def _mk_plugin(config=None):
    """注册插件（把 core_plugins.yaml 落点改到临时目录），返回 (module, fw, dir, restore)。

    缺省把 aiwriter.workspace 指到临时目录——否则工作区默认是框架根目录，
    测试会往仓库里写文件。
    """
    import framework.config as fc

    import core_plugins.aiwriter.main as m
    d = tempfile.mkdtemp(prefix="aiwriter_app_")
    cfg = dict(config or {"aiwriter": {"api_key": "sk-x", "mode": "workspace"}})
    blk = dict(cfg.get("aiwriter") or {})
    blk.setdefault("workspace", os.path.join(d, "ws"))
    cfg["aiwriter"] = blk
    fw = _FakeFw(cfg)
    m.register(_FakeCtx(fw, d))
    orig_yaml = fc.CORE_PLUGINS_YAML
    fc.CORE_PLUGINS_YAML = os.path.join(d, "core_plugins.yaml")

    def _restore():
        fc.CORE_PLUGINS_YAML = orig_yaml
        m.unregister()

    return m, fw, d, _restore


def _mk_app(config=None):
    """构造 Textual AgentApp（headless 可测）。"""
    from core_plugins.aiwriter import tui as t
    m, fw, d, restore = _mk_plugin(config)
    return t.AgentApp(m), m, fw, d, restore


def test_code_command_registered():
    m, fw, d, restore = _mk_plugin()
    try:
        from framework.terminal import terminal_commands
        assert terminal_commands.get("code") is not None
        assert terminal_commands.get("oc") is not None
        assert terminal_commands.get_target("code") == "core"
    finally:
        restore()


def test_persist_cfg_writes_yaml_and_memory():
    m, fw, d, restore = _mk_plugin()
    try:
        m._CFG["model"] = "m-x"
        m._CFG["mode"] = "readonly"
        m._persist_cfg(fw, m._CFG)
        import yaml
        with open(os.path.join(d, "core_plugins.yaml"), encoding="utf-8") as f:
            blk = yaml.safe_load(f)["core_plugins"]["aiwriter"]
        assert blk["model"] == "m-x" and blk["mode"] == "readonly"
        assert fw.config["aiwriter"]["model"] == "m-x"
    finally:
        restore()


def test_scaffold_plugin_valid_invalid_exists():
    m, fw, d, restore = _mk_plugin()
    orig_root = m._ROOT
    m._ROOT = tempfile.mkdtemp(prefix="aiwriter_root_")
    try:
        ok, _ = m._scaffold_plugin("my_plug")
        assert ok and os.path.isfile(os.path.join(m._ROOT, "plugins", "my_plug", "main.py"))
        assert not m._scaffold_plugin("my_plug")[0]      # 已存在
        assert not m._scaffold_plugin("1bad")[0]         # 非法名
        assert not m._scaffold_plugin("")[0]
    finally:
        m._ROOT = orig_root
        restore()


def test_cmd_code_rejected_in_remote_session():
    """经远程通道（调试控制台 / HTTP）调用 code 必须拒绝并给指引，不去 spawn 子进程。"""
    m, fw, d, restore = _mk_plugin()
    try:
        from framework.terminal.context import remote_session
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with remote_session():
                m._cmd_code('')
        out = buf.getvalue()
        assert '无法承载全屏界面' in out and 'python main.py code' in out
    finally:
        restore()


def test_app_mounts_and_shows_status():
    import asyncio
    app, m, fw, d, restore = _mk_app(
        {"aiwriter": {"api_key": "sk", "model": "demo-m", "mode": "workspace"}})
    try:
        async def _go():
            async with app.run_test() as pilot:
                await pilot.pause()
                assert "demo-m" in app.top_text
        asyncio.run(_go())
    finally:
        restore()


def test_app_enter_submits_and_streams():
    import asyncio
    app, m, fw, d, restore = _mk_app()
    aw_agent.stream_chat = _mk_stream([
        [("tool", {"name": "write",
                   "arguments": json.dumps({"path": "out.txt", "content": "ok"})})],
        [("text", "完成")],
    ])
    lines = []
    try:
        async def _go():
            async with app.run_test() as pilot:
                await pilot.pause()
                orig = app._log
                app._log = lambda content: (lines.append(str(content)), orig(content))[1]
                app.query_one("#prompt").value = "写个 out.txt"
                await pilot.press("enter")
                for _ in range(60):
                    await pilot.pause()
                    if not app._busy:
                        break
        asyncio.run(_go())
    finally:
        aw_agent.stream_chat = _ORIG_STREAM
        restore()
    assert app._busy is False
    assert any("写个 out.txt" in l for l in lines)
    assert any("完成" in l for l in lines)
    assert os.path.isfile(os.path.join(m._workspace_dir(), "out.txt"))
    with open(os.path.join(d, "sessions.json"), encoding="utf-8") as f:
        data = json.load(f)
    assert data["version"] == 2
    msgs = data["sessions"]["terminal"]["messages"]
    assert msgs[-1]["role"] == "assistant"


def test_app_slash_model_updates_cfg():
    import asyncio
    app, m, fw, d, restore = _mk_app()
    try:
        async def _go():
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one("#prompt").value = "/model m-9"
                await pilot.press("enter")
                await pilot.pause()
        asyncio.run(_go())
        assert m._CFG["model"] == "m-9"
    finally:
        restore()


def test_app_f2_opens_settings():
    import asyncio
    from core_plugins.aiwriter.tui import SettingsScreen
    app, m, fw, d, restore = _mk_app()
    try:
        async def _go():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("f2")
                await pilot.pause()
                assert isinstance(app.screen, SettingsScreen)
        asyncio.run(_go())
    finally:
        restore()



# ── 7. 扩展工具（DB / 插件 / Python / 网络）────────────────────────────

class _FakeHost:
    def __init__(self):
        self.calls = []

    def db_query(self, sql):
        self.calls.append(('query', sql))
        return [{'id': 1, 'name': 'x'}]

    def db_execute(self, sql):
        self.calls.append(('exec', sql))
        return 3

    def plugins(self):
        return ['aiwriter', 'webui']

    def plugin_action(self, action, name):
        self.calls.append((action, name))
        return '已 %s %s' % (action, name)

    def memory_add(self, text):
        self.calls.append(('remember', text))
        return '已记住'

    def plugin_new(self, name):
        self.calls.append(('plugin_new', name))
        return True, '已生成 plugins/%s' % name


def _full_ws(host=None):
    d = tempfile.mkdtemp(prefix="aiwriter_full_")
    return ToolRegistry(d, mode="full", allow_exec=True, host=host), d


def test_tool_db_query_guard_and_result():
    host = _FakeHost()
    reg, _ = _full_ws(host)
    out = reg.call("db_query", {"sql": "SELECT * FROM t"})
    assert "id" in out and host.calls == [('query', 'SELECT * FROM t')]
    for bad in ("DELETE FROM t", "update t set a=1", "drop table t"):
        try:
            reg.call("db_query", {"sql": bad})
        except ToolError:
            pass
        else:
            raise AssertionError("db_query 必须拒绝写语句：%s" % bad)


def test_tool_db_execute_and_plugin_tools():
    host = _FakeHost()
    reg, _ = _full_ws(host)
    assert "3" in reg.call("db_execute", {"sql": "DELETE FROM t"})
    assert "aiwriter" in reg.call("plugin_list", {})
    assert "enable" in reg.call("plugin_action", {"action": "enable", "name": "webui"})
    assert ('enable', 'webui') in host.calls
    try:
        reg.call("plugin_action", {"action": "boom", "name": "x"})
    except ToolError:
        pass
    else:
        raise AssertionError("非法 action 必须报错")


def test_tool_host_absent_reports_clearly():
    reg, _ = _full_ws(None)          # 无宿主（独立 CLI）
    for name, args in (("db_query", {"sql": "SELECT 1"}),
                       ("plugin_list", {}),
                       ("plugin_action", {"action": "enable", "name": "x"})):
        try:
            reg.call(name, args)
        except ToolError as e:
            assert "宿主" in str(e)
        else:
            raise AssertionError("%s 无宿主时必须报错" % name)


def test_tool_python_exec_and_gate():
    reg, _ = _full_ws()
    assert "2" in reg.call("python", {"code": "print(1 + 1)"})
    noexec = ToolRegistry(tempfile.mkdtemp(prefix="aiwriter_noexec_"),
                          mode="full", allow_exec=False)
    try:
        noexec.call("python", {"code": "print(1)"})
    except PermissionDenied:
        pass
    else:
        raise AssertionError("allow_exec=False 时 python 必须拒绝")


def test_tool_webfetch_rejects_non_http():
    reg, _ = _full_ws()
    try:
        reg.call("webfetch", {"url": "file:///etc/passwd"})
    except ToolError:
        pass
    else:
        raise AssertionError("非 http/https 必须拒绝")


# ── 8. 人格（agent）─────────────────────────────────────────────────────

def test_tool_remember_and_plugin_new():
    host = _FakeHost()
    reg, _ = _full_ws(host)
    assert '已记住' in reg.call('remember', {'text': '用户偏好 X'})
    assert '已生成' in reg.call('plugin_new', {'name': 'my_plug'})
    assert ('remember', '用户偏好 X') in host.calls
    assert ('plugin_new', 'my_plug') in host.calls


def test_llm_stream_emits_reason_and_thinking_payload():
    """深度思考：reasoning_content 等要转成 reason 事件；thinking 开关要进请求体。"""
    import json

    import core_plugins.aiwriter.llm as L

    chunks = [
        {"choices": [{"delta": {"reasoning_content": "先想想"}}]},
        {"choices": [{"delta": {"content": "答案"}}]},
    ]
    body = b"".join(("data: " + json.dumps(c, ensure_ascii=False) + "\n\n").encode("utf-8")
                    for c in chunks) + b"data: [DONE]\n\n"

    class FakeResp:
        def __iter__(self):
            for ln in body.split(b"\n\n"):
                if ln:
                    yield ln + b"\n\n"

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp()

    orig = L.urllib.request.urlopen
    L.urllib.request.urlopen = fake_urlopen
    try:
        out = list(L.stream_chat(
            {"base_url": "http://x/v1", "api_key": "k", "model": "m", "thinking": True}, [], []))
    finally:
        L.urllib.request.urlopen = orig
    assert ("reason", "先想想") in out
    assert ("text", "答案") in out
    assert captured["body"].get("thinking") == {"type": "enabled"}


def test_app_display_toggles():
    """/tools /detail /think 三个显示开关要能切并落盘。"""
    import asyncio
    app, m, fw, d, restore = _mk_app()
    try:
        async def _go():
            async with app.run_test() as pilot:
                await pilot.pause()
                for cmd in ("/think on", "/tools off", "/detail on"):
                    app.query_one("#prompt").value = cmd
                    await pilot.press("enter")
                    await pilot.pause()
        asyncio.run(_go())
        assert m._CFG["thinking"] is True
        assert m._CFG["show_tools"] is False
        assert m._CFG["tool_detail"] is True
    finally:
        restore()


def test_reason_and_tool_rendering():
    """思考默认折叠成一行摘要；工具调用默认只显示名字与成败（防刷屏）。"""
    import asyncio
    app, m, fw, d, restore = _mk_app()
    try:
        lines = []

        async def _go():
            async with app.run_test() as pilot:
                await pilot.pause()
                orig = app._log
                app._log = lambda c: (lines.append(str(c)), orig(c))[1]
                app._on_event("reason", "想" * 300)
                app._on_event("text", "答案")
                app._on_event("tool_start", {"name": "write", "args": {"path": "a"}})
                app._on_event("tool_end", {"name": "write", "ok": True, "result": "ok"})
        asyncio.run(_go())
        joined = "\n".join(lines)
        assert "💭 思考" in joined and "共 300 字" in joined      # 折叠成摘要
        assert "⚙ write" in joined
        assert "✔" in joined
        assert "{'path': 'a'}" not in joined                      # 默认不展开参数
    finally:
        restore()


def test_app_new_command_clears_session():
    """/new 必须真的清掉会话（历史归档与长期记忆不受影响）。"""
    import asyncio
    app, m, fw, d, restore = _mk_app()
    try:
        m._STORE.append_turn(m._session_key(), "u", "a")
        m._STORE.save()
        assert m._STORE.history(m._session_key())
        async def _go():
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one('#prompt').value = '/new'
                await pilot.press('enter')
                await pilot.pause()
        asyncio.run(_go())
        assert m._STORE.history(m._session_key()) == []
    finally:
        restore()


def test_persist_session_toggle():
    """persist_session=false 时，重启即全新会话；true 时保留。"""
    import framework.config as fc

    import core_plugins.aiwriter.main as m
    d = tempfile.mkdtemp(prefix="aiwriter_persist_")
    orig = fc.CORE_PLUGINS_YAML
    fc.CORE_PLUGINS_YAML = os.path.join(d, "core_plugins.yaml")
    try:
        m.register(_FakeCtx(_FakeFw({"aiwriter": {"api_key": "sk", "persist_session": True}}), d))
        m._STORE.append_turn(m._session_key(), "u", "a")
        m._STORE.save()
        m.unregister()
        m.register(_FakeCtx(_FakeFw({"aiwriter": {"api_key": "sk", "persist_session": True}}), d))
        assert m._STORE.history(m._session_key()) != []          # 保留
        m.unregister()
        m.register(_FakeCtx(_FakeFw({"aiwriter": {"api_key": "sk", "persist_session": False}}), d))
        assert m._STORE.history(m._session_key()) == []          # 清空
        m.unregister()
    finally:
        fc.CORE_PLUGINS_YAML = orig


def test_plugin_brief_and_memory_injected():
    """插件开发要点（取自仓库文档）+ 长期记忆都要进系统提示。"""
    m, fw, d, restore = _mk_plugin()
    try:
        brief = m._load_plugin_brief()
        assert '__plugin_meta__' in brief and 'ctx.command' in brief
        assert m._append_memory('用户偏好：插件名用全小写下划线')[0]
        p = m._system_prompt()
        assert '# ZCBOT 插件开发' in p and '__plugin_meta__' in p
        assert '# 长期记忆' in p and '小写下划线' in p
        assert m._clear_memory()[0] is True
        assert '# 长期记忆' not in m._system_prompt()
    finally:
        restore()


def test_history_archive_and_read():
    m, fw, d, restore = _mk_plugin()
    try:
        m._archive_turn('u1', 'a1')
        m._archive_turn('u2', 'a2')
        rows = m._read_history(10)
        assert [r['user'] for r in rows] == ['u2', 'u1']      # 新 → 旧
        assert rows[0]['assistant'] == 'a2'
        assert m._history_path().endswith('history.jsonl')
    finally:
        restore()


def test_app_memory_command():
    import asyncio
    app, m, fw, d, restore = _mk_app()
    try:
        async def _go():
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one('#prompt').value = '/memory add 用户偏好：插件名全小写'
                await pilot.press('enter')
                await pilot.pause()
        asyncio.run(_go())
        assert '小写' in m._load_memory()
    finally:
        restore()


def test_agents_default_and_crud():
    m, fw, d, restore = _mk_plugin()
    try:
        assert [a['name'] for a in m._agents()] == ['build']      # 默认人格
        assert m._active_agent()['name'] == 'build'
        ok, _ = m._add_agent('review', '你只做代码审查')
        assert ok and m._CFG['agent'] == 'review'
        assert m._active_agent()['prompt'] == '你只做代码审查'
        assert not m._add_agent('review')[0]                      # 重名
        assert m._set_agent('build')[0] and m._CFG['agent'] == 'build'
        ok, msg = m._set_agent('nope')
        assert not ok and 'nope' in msg
        assert m._set_agent_prompt('review', '新提示')[0]
        assert [a for a in m._agents() if a['name'] == 'review'][0]['prompt'] == '新提示'
        assert m._del_agent('review')[0]
        assert [a['name'] for a in m._agents()] == ['build']
    finally:
        restore()


def test_default_workspace_is_framework_root():
    """工作区缺省 = 框架根目录（直接在项目里干活）；临时目录 = data/tmp。"""
    m, fw, d, restore = _mk_plugin({"aiwriter": {"api_key": "sk", "workspace": ""}})
    try:
        assert m._workspace_dir() == m._ROOT
        assert m._temp_dir() == os.path.join(m._ROOT, "data", "tmp")
        assert os.path.isdir(m._temp_dir())
        p = m._system_prompt()
        assert m._ROOT in p and m._temp_dir() in p
        # 显式指定 workspace 时以配置为准
        assert m._workspace_dir({"workspace": d}) == os.path.abspath(d)
    finally:
        restore()


def test_prompt_includes_agent_persona():
    p = aw_prompt.build(['read'], '你是只读审查员。')
    assert '# 人格' in p and '只读审查员' in p
    assert '# 人格' not in aw_prompt.build(['read'])


def test_app_sidebar_and_tab_cycle_mode():
    import asyncio
    app, m, fw, d, restore = _mk_app(
        {"aiwriter": {"api_key": "sk", "mode": "readonly", "allow_exec": False}})
    try:
        async def _go():
            async with app.run_test() as pilot:
                await pilot.pause()
                from textual.widgets import ListView
                assert len(app.query_one('#agents', ListView).children) == 1
                assert 'build' in app.top_text
                await pilot.press('tab')
                await pilot.pause()
                assert m._CFG['mode'] == 'workspace'
                await pilot.press('tab')
                await pilot.pause()
                assert m._CFG['mode'] == 'full' and m._CFG['allow_exec'] is True
                await pilot.press('tab')
                await pilot.pause()
                assert m._CFG['mode'] == 'readonly' and m._CFG['allow_exec'] is False
        asyncio.run(_go())
    finally:
        restore()


def test_app_agent_command_opens_editor():
    """/agent new 打开人格编辑弹窗；弹窗保存后创建并切换人格。"""
    import asyncio
    from core_plugins.aiwriter.tui import AgentScreen
    app, m, fw, d, restore = _mk_app()
    try:
        async def _go():
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one('#prompt').value = '/agent new'
                await pilot.press('enter')
                await pilot.pause()
                assert isinstance(app.screen, AgentScreen)
                # 填名 + 提示后保存
                app.screen.query_one('#f-name').value = 'reviewer'
                app.screen.query_one('#f-prompt').text = '你只做代码审查'
                await pilot.click('#save')
                await pilot.pause()
        asyncio.run(_go())
        assert m._CFG['agent'] == 'reviewer'
        a = m._active_agent()
        assert a['name'] == 'reviewer' and a['prompt'] == '你只做代码审查'
    finally:
        restore()


def test_session_store_multisession_and_v1_migration():
    """会话存储：多会话（含元数据）+ 旧格式自动迁移。"""
    from core_plugins.aiwriter.session import SessionStore
    d = tempfile.mkdtemp(prefix="aiwriter_sess_")
    p = os.path.join(d, "sessions.json")
    # v1 旧格式
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"terminal": [{"role": "user", "content": "旧"},
                                {"role": "assistant", "content": "答"}]}, f)
    s = SessionStore(p)
    assert [x["key"] for x in s.sessions()] == ["terminal"]
    assert [m["content"] for m in s.history("terminal")] == ["旧", "答"]

    s.ensure("会话", name="会话")
    s.append_turn("会话", "u", "a")
    s.set_meta("会话", model="deepseek-reasoner", mode="full")
    s.save()
    assert s.meta("会话") == {"name": "会话", "model": "deepseek-reasoner", "mode": "full"}
    assert s.new_key("会话") == "会话-2"                   # 撞名自动加后缀
    s.rename("会话", "改过名")
    assert s.meta("会话")["name"] == "改过名"
    # 重载：v2 往返
    s2 = SessionStore(p)
    assert s2.meta("会话")["model"] == "deepseek-reasoner"
    assert s2.history("会话")[-1]["content"] == "a"
    assert s2.delete("会话") is True and s2.exists("会话") is False


def test_session_switch_delete_and_override():
    """切换 / 删除 / 会话级模型覆盖。"""
    m, fw, d, restore = _mk_plugin()
    try:
        assert m._session_key() == "terminal"
        ok, key = m._new_session("调试会话")
        assert ok and key == "调试会话" and m._session_key() == "调试会话"
        m._configure_session("调试会话", model="deepseek-reasoner", mode="full")
        eff = m._effective_cfg()
        assert eff["model"] == "deepseek-reasoner" and eff["mode"] == "full"
        assert eff["allow_exec"] is True
        assert len(m._sessions()) == 2
        assert m._delete_session("terminal")[0] is True
        assert m._session_key() == "调试会话"                 # 删的不是当前会话
        assert m._delete_session("调试会话")[0] is True       # 删掉当前会话 → 自动切走
        assert m._session_key() != "调试会话"
        assert m._delete_session("不存在")[0] is False
    finally:
        restore()


def test_app_sessions_command_and_screen():
    """/sessions 打开弹窗；弹窗内可新建 / 进入。"""
    import asyncio
    from core_plugins.aiwriter.tui import SessionScreen
    app, m, fw, d, restore = _mk_app()
    try:
        async def _go():
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one("#prompt").value = "/sessions"
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SessionScreen)
                await pilot.click("#snew")             # 新建并进入
                await pilot.pause()
                assert len(m._sessions()) == 2
                await pilot.press("escape")
                await pilot.pause()
        asyncio.run(_go())
        assert m._session_key() != "terminal"
    finally:
        restore()


def test_app_session_cmd_create_switch():
    import asyncio
    app, m, fw, d, restore = _mk_app()
    try:
        async def _go():
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one("#prompt").value = "/session new 插件开发"
                await pilot.press("enter")
                await pilot.pause()
        asyncio.run(_go())
        assert m._session_key() == "插件开发"
    finally:
        restore()


def test_standalone_mode_db_is_readonly_and_usable():
    """独立 CLI（python main.py code）也要能查库：只读可用，写被拒并给指引。"""
    import pytest
    # 独立模式以只读方式连真实业务库（data/zcbot.db）；CI 干净环境无业务库，跳过。
    if not os.path.isfile(os.path.join(ROOT, 'data', 'zcbot.db')):
        pytest.skip("独立模式只读查库需要本地 data/zcbot.db（CI 干净环境无业务库）")
    import core_plugins.aiwriter.main as m
    fw, mod = m.bootstrap()
    try:
        assert mod._TOOLS.host.standalone is True
        assert fw.db is not None, "独立模式应能打开 config.yaml 里的库（只读）"
        out = mod._TOOLS.call("db_query", {"sql": "SELECT name FROM sqlite_master WHERE type='table'", "limit": 3})
        assert "(" not in out[:1]
        # 写语句在工具层就被挡
        try:
            mod._TOOLS.call("db_query", {"sql": "DELETE FROM plugins"})
        except ToolError:
            pass
        else:
            raise AssertionError("db_query 必须拒绝写语句")
        # db_execute 在独立模式被只读连接拒绝，且错误里带指引
        try:
            mod._TOOLS.call("db_execute", {"sql": "UPDATE plugins SET name=name"})
        except ToolError as e:
            assert "只读" in str(e) or "不能写库" in str(e)
        else:
            raise AssertionError("独立模式 db_execute 必须拒绝")
        # 插件：列举可用；管理动作给指引且不抛
        assert isinstance(mod._TOOLS.call("plugin_list", {}), str)
        msg = mod._TOOLS.call("plugin_action", {"action": "reload", "name": "aiwriter"})
        assert "独立模式" in msg
        # 长期记忆仍可用（不依赖框架宿主）
        assert "已记入" in mod._TOOLS.call("remember", {"text": "独立模式用例"})
    finally:
        mod.unregister()


def test_agent_screen_rejects_bad_name():
    import asyncio
    from core_plugins.aiwriter.tui import AgentScreen
    app, m, fw, d, restore = _mk_app()
    try:
        async def _go():
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one('#prompt').value = '/agent new'
                await pilot.press('enter')
                await pilot.pause()
                app.screen.query_one('#f-name').value = '1 bad/name'
                await pilot.click('#save')
                await pilot.pause()
                assert isinstance(app.screen, AgentScreen)      # 未通过校验，弹窗仍在
        asyncio.run(_go())
    finally:
        restore()


def test_app_agent_add_quick():
    """/agent add <名> 直接建（不开弹窗）。"""
    import asyncio
    app, m, fw, d, restore = _mk_app()
    try:
        async def _go():
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one('#prompt').value = '/agent add quick'
                await pilot.press('enter')
                await pilot.pause()
        asyncio.run(_go())
        assert m._CFG['agent'] == 'quick'
        assert 'quick' in [a['name'] for a in m._agents()]
    finally:
        restore()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
