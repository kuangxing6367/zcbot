# -*- coding: utf-8 -*-
"""html_assembler 官方插件测试：引擎行为 + 官方插件装载（register/自测命令）"""
import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(ROOT, 'core_plugins', 'html_assembler')

_counter = [0]


def _load(filename: str, mod_name: str):
    """按 runtime._load_core_plugins 的同款方式装载（合成模块名 + spec_from_file）"""
    _counter[0] += 1
    name = f"{mod_name}_{_counter[0]}"
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(PLUGIN_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def engine():
    return _load('engine.py', 'test_ha_engine')


PNG_1PX = base64_png = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
    '0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082')

# ── 引擎：占位符 ─────────────────────────────────────────────


def test_variable_escape(engine):
    out = engine.render_html("<b>{{ name }}</b>", data={"name": "<i>x&y</i>"})
    assert out == "<b>&lt;i&gt;x&amp;y&lt;/i&gt;</b>"


def test_variable_no_escape_and_none(engine):
    out = engine.render_html("{{ a }}|{{ b }}|{{ c }}",
                             data={"a": "<hr>", "b": None},
                             escape_html=False)
    assert out == "<hr>||{{ c }}"   # b 存在但为 None → 空串；c 缺失 → keep 保留占位符


def test_nested_and_list_index(engine):
    data = {"a": {"b": [{"c": "ok"}]}, "items": [{"name": "n0"}, {"name": "n1"}]}
    out = engine.render_html("{{ a.b.0.c }} {{ items.1.name }}", data=data)
    assert out == "ok n1"


def test_raw_insert(engine):
    out = engine.render_html("{{ raw:snippet }}",
                             data={"snippet": "<hr><i>原文</i>"})
    assert out == "<hr><i>原文</i>"


def test_img_bytes_png_and_svg(engine):
    png = engine.embed_image(PNG_1PX)
    assert png.startswith("data:image/png;base64,")
    svg = engine.embed_image(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    assert svg.startswith("data:image/svg+xml;base64,")


def test_img_data_uri_passthrough(engine):
    uri = "data:image/gif;base64,R0lGOD"
    assert engine.embed_image(uri) == uri


def test_img_placeholder(engine):
    out = engine.render_html('<img src="{{ img:logo }}">',
                             images={"logo": PNG_1PX})
    assert out.startswith('<img src="data:image/png;base64,iVBOR')


# ── 引擎：缺失策略 ───────────────────────────────────────────


def test_on_missing_keep_default(engine):
    assert engine.render_html("x {{ nope }} y") == "x {{ nope }} y"


def test_on_missing_empty(engine):
    assert engine.render_html("x {{ nope }} y", on_missing="empty") == "x  y"


def test_on_missing_raise(engine):
    with pytest.raises(KeyError):
        engine.render_html("{{ nope }}", on_missing="raise")


def test_missing_img_follows_policy(engine):
    out = engine.render_html('{{ img:gone }}', images={}, on_missing="empty")
    assert out == ""


# ── 引擎：上限（内存可控） ────────────────────────────────────


def test_template_too_large(engine):
    with pytest.raises(engine.TemplateTooLarge):
        engine.render_html("x" * 100, max_template_bytes=10)


def test_image_too_large(engine):
    with pytest.raises(engine.ImageTooLarge):
        engine.render_html("{{ img:big }}", images={"big": PNG_1PX},
                           max_image_bytes=10)


def test_output_too_large(engine):
    with pytest.raises(engine.OutputTooLarge):
        engine.render_html("{{ raw:big }}", data={"big": "y" * 100},
                           escape_html=False, max_output_bytes=10)


def test_too_many_images(engine):
    a = engine.HtmlAssembler(max_images=1)
    a.add_image("a", PNG_1PX)
    with pytest.raises(engine.TooManyImages):
        a.add_image("b", PNG_1PX)


def test_unknown_opt_rejected(engine):
    with pytest.raises(TypeError):
        engine.HtmlAssembler(nope=1)


# ── 引擎：文件读写与统计 ─────────────────────────────────────


def test_load_template_utf8_and_gbk(engine, tmp_path):
    utf8 = tmp_path / "u.html"
    utf8.write_text("中文utf8", encoding="utf-8")
    assert engine.load_template(str(utf8)) == "中文utf8"

    gbk = tmp_path / "g.html"
    gbk.write_bytes("中文gbk".encode("gbk"))
    assert engine.load_template(str(gbk)) == "中文gbk"


def test_render_to_file_and_stats(engine, tmp_path):
    out = str(tmp_path / "sub" / "page.html")
    path = engine.render_to_file("<p>{{ k }}</p>", out,
                                 data={"k": "v"}, images={"i": PNG_1PX})
    assert path == out
    text = open(out, encoding="utf-8").read()
    assert text == "<p>v</p>"


def test_stats_counters(engine):
    a = engine.HtmlAssembler()
    a.set_template("{{ a }} {{ gone }} <img src=\"{{ img:i }}\">")
    a.set_many({"a": "1"})
    a.add_image("i", PNG_1PX)
    a.render()
    st = a.last_stats
    assert (st["replaced"], st["missing"], st["images"]) == (1, 1, 1)
    assert st["output_bytes"] > 0


# ── 官方插件装载：register / 自测命令 ────────────────────────


class _Services:
    def __init__(self):
        self._s = {}

    def register(self, name, svc):
        self._s[name] = svc

    def get(self, name, default=None):
        return self._s.get(name, default)


class _Framework:
    def __init__(self, cfg=None):
        self._cfg = cfg or {}
        self.config = self          # 框架对象自身带 .get，config 即同源
        self.services = _Services()

    def get(self, key, default=None):
        return self._cfg.get(key, default)


class _Ctx:
    """最小 ctx 桩：覆盖 register/handler 用到的面"""

    def __init__(self, fw, datadir):
        self._framework = fw
        self._datadir = str(datadir)
        self.commands = []
        self.sent = []

    def log(self, msg, level="info"):
        pass

    def command(self, pattern, handler, **kw):
        self.commands.append((pattern, handler, kw))

    def get_data_dir(self):
        os.makedirs(self._datadir, exist_ok=True)
        return self._datadir

    def send_msg(self, **kw):
        self.sent.append(kw)


def test_plugin_register_and_service(tmp_path, monkeypatch):
    main = _load('main.py', 'test_ha_main')
    fw = _Framework({"html_assembler": {"enabled": True, "max_images": 8}})
    ctx = _Ctx(fw, tmp_path / "dat")
    main.register(ctx)

    # 命令已注册 + 服务已登记
    assert [(p, h) for p, h, _ in ctx.commands] and ctx.commands[0][0] == "/html_asm"
    svc = fw.services.get("html_assembler")
    assert svc is not None and callable(svc.render_html)

    # 配置已进引擎默认值
    assert main.engine.get_defaults()["max_images"] == 8

    # 模块级门面可用（跨插件契约）
    assert main.render_html("{{ x }}", data={"x": 1}) == "1"


def test_plugin_config_reaches_engine(tmp_path):
    main = _load('main.py', 'test_ha_main')
    fw = _Framework({"html_assembler": {"enabled": True, "max_images": 1}})
    ctx = _Ctx(fw, tmp_path / "dat")
    main.register(ctx)
    a = main.HtmlAssembler()
    a.add_image("a", PNG_1PX)
    with pytest.raises(main.engine.TooManyImages):
        a.add_image("b", PNG_1PX)


def test_html_asm_command_renders_demo(tmp_path):
    main = _load('main.py', 'test_ha_main')
    fw = _Framework({})
    ctx = _Ctx(fw, tmp_path / "dat")
    main.register(ctx)
    main.ctx = ctx                      # runtime 装载时会把 ctx 注入模块全局

    ev = types.SimpleNamespace(user_id=1, group_id=10086, message="/html_asm")
    _, handler, _ = ctx.commands[0]
    handler(ev, types.SimpleNamespace(group=lambda i: None))

    assert ctx.sent and "html_asm_demo.html" in ctx.sent[0]["message"]
    page = open(os.path.join(ctx._datadir, "html_asm_demo.html"),
                encoding="utf-8").read()
    assert "HTML 装配引擎自测" in page and "data:image/svg+xml;base64," in page


def test_core_ctx_data_dir_windows_safe(tmp_path):
    """官方插件 ctx 名带 "core:" 前缀，get_data_dir 不得把冒号带进路径（Windows 非法）"""
    from framework.ctx import PluginContext
    fw = types.SimpleNamespace(
        plugin_loader=types.SimpleNamespace(plugins_dat_dir=str(tmp_path)),
        db=None)
    c = PluginContext("core:html_assembler", fw)
    d = c.get_data_dir()
    assert d == os.path.join(str(tmp_path), "core_html_assembler")
    assert os.path.isdir(d)
