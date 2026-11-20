"""启动环境与横幅测试：验证真实配置路径/数据库信息只输出一份。"""
import logging

from framework import environment
from framework.bootstrap.app import _emit_startup_environment


def test_snapshot_uses_explicit_config(tmp_path):
    cfg_path = tmp_path / 'custom.yaml'
    cfg = {
        '_config_path': str(cfg_path),
        'database': {'type': 'sqlite', 'path': str(tmp_path / 'bot.db')},
    }
    info = environment.snapshot(config=cfg)
    assert info['config_path'] == str(cfg_path)
    assert info['db_type'] == 'sqlite'
    assert info['db_path'] == str(tmp_path / 'bot.db')


def test_format_block_has_required_labels(tmp_path):
    info = environment.snapshot(config={
        '_config_path': str(tmp_path / 'custom.yaml'),
        'database': {'type': 'sqlite', 'path': str(tmp_path / 'bot.db')},
    })
    block = environment.format_block(info)
    for label in ('[运行环境]', 'Python', '环境类型', '容器', '特权身份',
                  '系统', '主机 / PID', '工作目录', '配置文件', '数据库', '内存', '磁盘'):
        assert label in block
    assert str(tmp_path / 'custom.yaml') in block
    assert str(tmp_path / 'bot.db') in block
    assert 'None' not in block
    assert '未检测到' in block


def test_startup_banner_emitted_once(tmp_path, monkeypatch, capsys, caplog):
    cfg_path = str(tmp_path / 'custom.yaml')
    cfg = {
        '_config_path': cfg_path,
        'database': {'type': 'sqlite', 'path': str(tmp_path / 'bot.db')},
        'framework': {'startup_info': True, 'security': {'warn_if_root': False}},
    }
    monkeypatch.setattr('framework.config.load_config', lambda path=None: cfg, raising=False)
    monkeypatch.setattr(environment, 'is_privileged', lambda: (False, ''))

    with caplog.at_level(logging.INFO, logger='zcbot'):
        _emit_startup_environment(cfg_path)

    out = capsys.readouterr().out
    assert out.count('[运行环境]') == 1
    assert cfg_path in out
    assert len([r for r in caplog.records if '[运行环境]' in r.message]) == 1
    assert not any('安全警告' in r.message for r in caplog.records)


def test_startup_root_warning_respects_switch(tmp_path, monkeypatch, caplog):
    cfg = {
        '_config_path': str(tmp_path / 'custom.yaml'),
        'framework': {'startup_info': False, 'security': {'warn_if_root': True}},
    }
    monkeypatch.setattr('framework.config.load_config', lambda path=None: cfg, raising=False)
    monkeypatch.setattr(environment, 'is_privileged', lambda: (True, 'uid=0'))

    with caplog.at_level(logging.WARNING, logger='zcbot'):
        _emit_startup_environment(str(tmp_path / 'custom.yaml'))

    assert any('安全警告' in r.message for r in caplog.records)
