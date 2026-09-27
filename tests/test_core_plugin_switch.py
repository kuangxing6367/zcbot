# -*- coding: utf-8 -*-
"""core 插件开关回归测试：
1. config.yaml 的 core_plugins 段是权威开关，显式 false 必须禁用对应插件；
2. yaml 中 enabled 为字符串 "false" 时按 false 解析（bug 修复：bool("false")==True 曾致关闭失效）；
3. yaml 全 false 正常禁用；
4. config.yaml 段显式 true 可覆盖 yaml false（重新开启）。
运行：python tests/test_core_plugin_switch.py
"""
import os
import sys
import tempfile

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import framework.config as fc


def _fresh_yaml(enabled_value):
    """构造临时 core_plugins.yaml（monkeypatch 模块级路径，不触碰真实文件）"""
    tmp = tempfile.mkdtemp()
    yaml_path = os.path.join(tmp, 'core_plugins.yaml')
    installed = fc._scan_core_plugins()
    cp_yaml = {'core_plugins': {}}
    for n in installed:
        blk = dict(fc._CORE_PLUGIN_SCHEMA.get(n, {'enabled': True}))
        blk['enabled'] = enabled_value
        cp_yaml['core_plugins'][n] = blk
    with open(yaml_path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(cp_yaml, f, allow_unicode=True)
    fc.CORE_PLUGINS_YAML = yaml_path
    return installed, yaml_path


def _load_flags(main_cp):
    """运行 _autoload_core_plugins 并返回 {name: enabled}"""
    user_cfg = {'core_plugins': dict(main_cp)}
    fc._autoload_core_plugins(user_cfg)
    return user_cfg.get('core_plugins', {})


def test_config_yaml_authoritative():
    """config.yaml 段全 false + yaml 全 true → 全部禁用（关键回归）"""
    installed, _ = _fresh_yaml(True)
    flags = _load_flags({n: False for n in installed})
    loaded = [n for n in installed if flags.get(n, True) is not False]
    assert loaded == [], f"config.yaml 段全 false 仍加载: {loaded}"


def test_string_false_parsed():
    """yaml enabled: "false" 字符串 → 严格解析为禁用"""
    installed, _ = _fresh_yaml("false")
    flags = _load_flags({})
    loaded = [n for n in installed if flags.get(n, True) is not False]
    assert loaded == [], f"enabled='false' 字符串仍加载: {loaded}"


def test_yaml_false_disables():
    """yaml 全 false + config.yaml 无段 → 全部禁用"""
    installed, _ = _fresh_yaml(False)
    flags = _load_flags({})
    loaded = [n for n in installed if flags.get(n, True) is not False]
    assert loaded == [], f"yaml 全 false 仍加载: {loaded}"


def test_config_true_overrides_yaml_false():
    """config.yaml 段显式 true → 覆盖 yaml false 并重新启用"""
    installed, _ = _fresh_yaml(False)
    flags = _load_flags({n: True for n in installed})
    loaded = [n for n in installed if flags.get(n, True) is not False]
    assert len(loaded) == len(installed), f"config.yaml true 未全部覆盖: {loaded}"


def test_yaml_written_back_on_change():
    """开关变化需回写 yaml，保证磁盘状态与生效状态一致"""
    installed, yaml_path = _fresh_yaml(True)
    _load_flags({n: False for n in installed})
    with open(yaml_path, 'r', encoding='utf-8') as f:
        disk = yaml.safe_load(f).get('core_plugins', {})
    for n in installed:
        assert fc._as_bool(disk.get(n, {}).get('enabled', True)) is False, \
            f"yaml 未回写禁用: {n}"


if __name__ == '__main__':
    cases = [
        test_config_yaml_authoritative,
        test_string_false_parsed,
        test_yaml_false_disables,
        test_config_true_overrides_yaml_false,
        test_yaml_written_back_on_change,
    ]
    failed = 0
    for fn in cases:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    sys.exit(1 if failed else 0)