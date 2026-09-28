# -*- coding: utf-8 -*-
"""core 插件开关回归测试（清单驱动语义）：
1. core_plugins.yaml 是唯一权威：yaml false 必须禁用（含字符串 "false"）；
2. config.yaml 段已废弃：显式 false 不再压制 yaml true，显式 true 也不再
   覆盖 yaml false —— 官方插件启停只由 core_plugins.yaml 决定；
3. 新发现（yaml 缺失块）插件 → 补块 enabled=false（发现即禁用，绝不静默启用）；
4. 已卸载插件 → 自动从清单移除并回写。
运行：python tests/test_core_plugin_switch.py
"""
import os
import sys
import tempfile

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import framework.config as fc  # noqa: E402


def _fresh_yaml(enabled_value, only=None):
    """构造临时 core_plugins.yaml（monkeypatch 模块级路径，不触碰真实文件）。
    only=None 表示全量插件；否则只包含指定插件（模拟 yaml 缺失其它插件）。"""
    tmp = tempfile.mkdtemp()
    yaml_path = os.path.join(tmp, 'core_plugins.yaml')
    installed = fc._scan_core_plugins()
    if only is None:
        names = installed
    else:
        names = [n for n in installed if n in only]
    cp_yaml = {'core_plugins': {}}
    for n in names:
        blk = dict(fc._CORE_PLUGIN_SCHEMA.get(n, {'enabled': True}))
        blk['enabled'] = enabled_value
        cp_yaml['core_plugins'][n] = blk
    with open(yaml_path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(cp_yaml, f, allow_unicode=True)
    fc.CORE_PLUGINS_YAML = yaml_path
    return installed, yaml_path


def _load_flags(user_cfg):
    """运行 _autoload_core_plugins 并返回 {name: enabled}"""
    fc._autoload_core_plugins(user_cfg)
    return user_cfg.get('core_plugins', {})


def _loaded_names(flags, installed):
    """yaml 推导出的 enabled 为真 的插件名"""
    return [n for n in installed if flags.get(n) is True]


def test_yaml_false_disables():
    """yaml 全 false + config.yaml 无段 → 全部禁用"""
    installed, _ = _fresh_yaml(False)
    flags = _load_flags({})
    assert _loaded_names(flags, installed) == [], \
        f"yaml 全 false 仍加载: {_loaded_names(flags, installed)}"


def test_string_false_parsed():
    """yaml enabled: "false" 字符串 → 严格解析为禁用"""
    installed, _ = _fresh_yaml("false")
    flags = _load_flags({})
    assert _loaded_names(flags, installed) == [], \
        f"enabled='false' 字符串仍加载: {_loaded_names(flags, installed)}"


def test_config_no_longer_overrides_yaml_false():
    """config.yaml 段已废弃：显式 true 不再覆盖 yaml false（反转旧权威行为）"""
    installed, _ = _fresh_yaml(False)
    flags = _load_flags({'core_plugins': {n: True for n in installed}})
    assert _loaded_names(flags, installed) == [], \
        f"config.yaml true 仍覆盖 yaml false: {_loaded_names(flags, installed)}"


def test_config_false_does_not_disable_yaml_true():
    """config.yaml 段已废弃：显式 false 不再压制 yaml true（yaml 为准）"""
    installed, _ = _fresh_yaml(True)
    flags = _load_flags({'core_plugins': {n: False for n in installed}})
    assert _loaded_names(flags, installed) == installed, \
        f"config.yaml false 压制了 yaml true: {_loaded_names(flags, installed)}"


def test_new_plugin_default_disabled():
    """已安装但 yaml 缺失的插件 → 补块且 enabled 强制 False（发现即禁用）"""
    installed, yaml_path = _fresh_yaml(True, only=['html_assembler'])
    missing = [n for n in installed if n != 'html_assembler']
    assert missing, "前置条件失败：仓库至少应有两个官方插件"
    flags = _load_flags({})
    # 已列出插件按 yaml true 加载
    assert flags.get('html_assembler') is True
    # 缺失插件一律禁用（不静默启用）
    assert all(flags.get(n) is False for n in missing), \
        f"缺失插件被静默启用: {[n for n in missing if flags.get(n)]}"
    # 且已回写 yaml（磁盘状态与生效状态一致）
    with open(yaml_path, 'r', encoding='utf-8') as f:
        disk = yaml.safe_load(f).get('core_plugins', {})
    assert all(fc._as_bool(disk[n]['enabled']) is False for n in missing), \
        f"新插件未回写为禁用: {disk}"


def test_uninstalled_plugin_removed():
    """yaml 中已卸载的插件块 → 自动移除（清单与安装状态一致）"""
    _, yaml_path = _fresh_yaml(True)
    # 手工塞一个"幽灵"块
    with open(yaml_path, 'r', encoding='utf-8') as f:
        cp = yaml.safe_load(f)
    cp['core_plugins']['ghost_plugin_zzz'] = {'enabled': True}
    with open(yaml_path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(cp, f, allow_unicode=True)
    _load_flags({})
    with open(yaml_path, 'r', encoding='utf-8') as f:
        disk = yaml.safe_load(f).get('core_plugins', {})
    assert 'ghost_plugin_zzz' not in disk, "已卸载插件块未被清理"


if __name__ == '__main__':
    cases = [
        test_yaml_false_disables,
        test_string_false_parsed,
        test_config_no_longer_overrides_yaml_false,
        test_config_false_does_not_disable_yaml_true,
        test_new_plugin_default_disabled,
        test_uninstalled_plugin_removed,
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