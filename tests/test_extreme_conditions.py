# -*- coding: utf-8 -*-
"""ZCBOT 极端 / 边界条件加固测试。

覆盖框架在「畸形输入 / 坏数据 / 越权键 / 未知语言 / 命令树防呆」等极端情形下的行为：
不崩溃、不越权写文件、安全回退。

运行：
    pytest tests/test_extreme_conditions.py -v
    python tests/test_extreme_conditions.py
"""
import argparse
import os
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from framework import i18n  # noqa: E402
from framework.config import load_config  # noqa: E402
from framework.cli import build_parser, main as cli_main  # noqa: E402
from framework.cli.commands.config import do_config  # noqa: E402
from framework.cli.utils import (_read_meta, render_table, set_core_plugin_enabled,
                                 _yaml_get_enabled)  # noqa: E402


# --------------------------------------------------------------------------
# 1) i18n：未知语言 / 非法语言安全回退
# --------------------------------------------------------------------------
def test_i18n_unknown_language_fallback():
    prev = i18n.get_language()
    try:
        i18n.set_language("this_is_not_a_lang")
        assert i18n.get_language() in i18n.available_languages()
        out = i18n.t("framework.initializing")
        assert isinstance(out, str) and out
    finally:
        i18n.set_language(prev)


def test_i18n_set_language_invalid_does_not_crash():
    prev = i18n.get_language()
    try:
        for bad in ("", None, "!!!!!!!!", "zh_CN", "ENGLISH"):
            i18n.set_language(bad)  # 不应抛异常
    finally:
        i18n.set_language(prev)


# --------------------------------------------------------------------------
# 2) 插件元信息提取（ast）：畸形 / 缺省 / 非法都能安全返回 {}
# --------------------------------------------------------------------------
def _write_tmp(content, suffix=".py"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_read_meta_syntax_error_returns_empty():
    p = _write_tmp("def (::: not valid python\n__plugin_meta__ = {bad")
    try:
        assert _read_meta(p) == {}
    finally:
        os.remove(p)


def test_read_meta_missing_returns_empty():
    p = _write_tmp("X = 1\ny = 2\n")
    try:
        assert _read_meta(p) == {}
    finally:
        os.remove(p)


def test_read_meta_dict_literal():
    p = _write_tmp(
        "__plugin_meta__ = {\n"
        "  'name': 'Demo',\n"
        "  'version': '0.1.0',\n"
        "  'priority': 50,\n"
        "}\n"
    )
    try:
        meta = _read_meta(p)
        assert meta.get("name") == "Demo"
        assert meta.get("priority") == 50
    finally:
        os.remove(p)


def test_read_meta_non_dict_returns_empty():
    p = _write_tmp("__plugin_meta__ = 'a string, not a dict'\n")
    try:
        assert _read_meta(p) == {}
    finally:
        os.remove(p)


# --------------------------------------------------------------------------
# 3) render_table：含非 ASCII / 空值 / 超长不崩
# --------------------------------------------------------------------------
def test_render_table_mixed():
    rows = [["id", "v", "说明中文"], ["x", None, ""], ["y", "1", "a" * 50]]
    out = render_table(rows, ["ID", "V", "D"])
    assert "ID" in out and "说明中文" in out


# --------------------------------------------------------------------------
# 4) core_plugins.yaml 原地改写：创建 / 切换 enabled，且不破坏其它条目
# --------------------------------------------------------------------------
def test_core_plugin_enabled_roundtrip():
    d = tempfile.mkdtemp()
    yaml_path = os.path.join(d, "core_plugins.yaml")
    base = (
        "core_plugins:\n"
        "  discord:\n"
        "    enabled: false\n"
        "    token: ''\n"
        "  aiwriter:\n"
        "    enabled: true\n"
        "    base_url: https://x\n"
    )
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(base)
    ok, _ = set_core_plugin_enabled(yaml_path, "discord", True)
    assert ok
    assert _yaml_get_enabled(yaml_path, "core_plugins", "discord", False) is True
    # aiwriter 不受影响
    assert _yaml_get_enabled(yaml_path, "core_plugins", "aiwriter", False) is True
    # 关闭 discord
    set_core_plugin_enabled(yaml_path, "discord", False)
    assert _yaml_get_enabled(yaml_path, "core_plugins", "discord", True) is False
    # 注释 / 其它字段仍保留
    text = open(yaml_path, encoding="utf-8").read()
    assert "base_url: https://x" in text
    assert "token: ''" in text


def test_core_plugin_enabled_new_entry():
    d = tempfile.mkdtemp()
    yaml_path = os.path.join(d, "core_plugins.yaml")
    ok, _ = set_core_plugin_enabled(yaml_path, "new_adapter", True)
    assert ok
    assert _yaml_get_enabled(yaml_path, "core_plugins", "new_adapter", False) is True


# --------------------------------------------------------------------------
# 5) config set：拒绝越权键（路径穿越 / 注入），正常键可写
# --------------------------------------------------------------------------
def _make_config(tmp, body="language: zh\n"):
    path = os.path.join(tmp, "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def test_config_set_rejects_path_traversal():
    d = tempfile.mkdtemp()
    _make_config(d)
    args = argparse.Namespace(action="set", key="../evil", value=["x"])
    rc = do_config(args, d)
    assert rc == 2
    assert "../evil" not in open(os.path.join(d, "config.yaml"), encoding="utf-8").read()


def test_config_set_rejects_slash_key():
    d = tempfile.mkdtemp()
    _make_config(d)
    args = argparse.Namespace(action="set", key="a/b", value=["x"])
    assert do_config(args, d) == 2


def test_config_set_rejects_dot_prefix_key():
    d = tempfile.mkdtemp()
    _make_config(d)
    args = argparse.Namespace(action="set", key=".hidden", value=["x"])
    assert do_config(args, d) == 2


def test_config_set_normal_writes():
    d = tempfile.mkdtemp()
    _make_config(d)
    args = argparse.Namespace(action="set", key="language", value=["en"])
    assert do_config(args, d) == 0
    assert "language: en" in open(os.path.join(d, "config.yaml"), encoding="utf-8").read()


def test_config_get_missing_key():
    d = tempfile.mkdtemp()
    _make_config(d)
    args = argparse.Namespace(action="get", key="not_there")
    assert do_config(args, d) == 0


# --------------------------------------------------------------------------
# 6) load_config：畸形 YAML 安全回退（不崩溃，备份+默认）
# --------------------------------------------------------------------------
def test_load_config_malformed_yaml():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write("::: this is : not : valid : yaml: [unbalanced\n")
    cfg = load_config(path)  # 不应抛异常
    assert isinstance(cfg, dict)
    assert os.path.isfile(path + ".bak")  # 损坏文件应被备份


def test_load_config_missing_file_generates_default():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "nope.yaml")
    cfg = load_config(path)
    assert isinstance(cfg, dict)
    assert os.path.isfile(path)  # 自动生成默认配置


# --------------------------------------------------------------------------
# 7) CLI 命令树防呆：未知 group 被拒；--help / --version 正常退出
# --------------------------------------------------------------------------
def test_cli_rejects_unknown_group():
    with pytest.raises(SystemExit) as ei:
        build_parser().parse_args(["totally-unknown-group"])
    assert ei.value.code == 2


def test_cli_help_exits_zero():
    with pytest.raises(SystemExit) as ei:
        cli_main(["--help"])
    assert ei.value.code == 0


def test_cli_version_exits_zero():
    with pytest.raises(SystemExit) as ei:
        cli_main(["--version"])
    assert ei.value.code == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
