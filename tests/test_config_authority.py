# -*- coding: utf-8 -*-
"""配置权威划分：config.yaml 管运行参数，core_plugins.yaml 管启停

回归口径：
- config.yaml 的 web.host/port 改动必须生效（不再被 core_plugins.yaml 覆盖）
- core_plugins.yaml 里与 config.yaml 重名的键要被收敛移除
- 只在 core_plugins.yaml 里存在的插件私有键仍然生效
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
import framework.config as C


def _with_yaml(cps: dict):
    """把 core_plugins.yaml 指向临时文件，返回 (path, 恢复函数)"""
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, 'core_plugins.yaml')
    with open(path, 'w', encoding='utf-8') as f:
        yaml.safe_dump({'core_plugins': cps}, f, allow_unicode=True, sort_keys=False)
    old = C.CORE_PLUGINS_YAML
    C.CORE_PLUGINS_YAML = path
    return path, lambda: setattr(C, 'CORE_PLUGINS_YAML', old)


def test_config_yaml_wins_for_web_port():
    """config.yaml 的 web.port 必须生效（此前被 core_plugins.yaml 静默覆盖）"""
    path, restore = _with_yaml({
        'webui': {'enabled': True, 'host': '1.1.1.1', 'port': 9999},
    })
    try:
        config = {'web': {'host': '127.0.0.1', 'port': 8080,
                          'session_timeout': 3600}}
        C._autoload_core_plugins(config)
        assert config['web']['port'] == 8080, config['web']
        assert config['web']['host'] == '127.0.0.1', config['web']
        assert config['web']['session_timeout'] == 3600
        return 'config.yaml 的 web.port 生效'
    finally:
        restore()


def test_duplicate_keys_removed_from_core_yaml():
    """重复键要从 core_plugins.yaml 中收掉，不留两处可改"""
    path, restore = _with_yaml({
        'webui': {'enabled': True, 'host': '1.1.1.1', 'port': 9999},
    })
    try:
        C._autoload_core_plugins({'web': {'host': '127.0.0.1', 'port': 8080}})
        data = yaml.safe_load(open(path, encoding='utf-8'))
        blk = data['core_plugins']['webui']
        assert 'host' not in blk and 'port' not in blk, blk
        assert blk.get('enabled') is True, blk
        return 'yaml 中重复键已移除，enabled 保留'
    finally:
        restore()


def test_plugin_private_keys_still_work():
    """config.yaml 没有的插件私有键仍从 core_plugins.yaml 生效"""
    path, restore = _with_yaml({
        'onebot_adapter': {'enabled': True, 'listen_host': '0.0.0.0',
                           'listen_port': 7000, 'access_token': 'abc',
                           'max_pending_events': 8192},
    })
    try:
        config = {'onebot': {'listen_port': 6830, 'access_token': ''}}
        C._autoload_core_plugins(config)
        ob = config['onebot']
        assert ob['listen_port'] == 6830, ob        # config.yaml 赢
        assert ob['access_token'] == '', ob         # config.yaml 赢
        assert ob['listen_host'] == '0.0.0.0', ob   # 仅 yaml 有 → 保留
        assert ob['max_pending_events'] == 8192, ob  # 仅 yaml 有 → 保留
        return '私有键保留、同名键以 config.yaml 为准'
    finally:
        restore()


def test_enabled_still_from_core_yaml():
    """启停仍然只认 core_plugins.yaml"""
    path, restore = _with_yaml({
        'webui': {'enabled': False, 'host': '127.0.0.1', 'port': 8080},
    })
    try:
        config = {'web': {'host': '127.0.0.1', 'port': 8080}}
        C._autoload_core_plugins(config)
        assert config['core_plugins']['webui'] is False
        return 'enabled 仍以 core_plugins.yaml 为准'
    finally:
        restore()


def test_no_conflict_keys_untouched():
    """config.yaml 没有对应段时，yaml 里的键一个都不能删"""
    path, restore = _with_yaml({
        'webui': {'enabled': True, 'host': '10.0.0.9', 'port': 9000},
    })
    try:
        config = {}
        C._autoload_core_plugins(config)
        assert config['web']['port'] == 9000, config['web']
        assert config['web']['host'] == '10.0.0.9', config['web']
        return '无 config 段时 yaml 值完整生效'
    finally:
        restore()


if __name__ == '__main__':
    ok = fail = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                print(f'PASS {name}: {fn()}')
                ok += 1
            except Exception as e:
                print(f'FAIL {name}: {type(e).__name__}: {e}')
                fail += 1
    print(f'\n{ok} passed, {fail} failed')
    sys.exit(1 if fail else 0)
