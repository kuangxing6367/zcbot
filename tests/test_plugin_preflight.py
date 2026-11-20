"""启动期插件预检：整目录扫描只告警、不阻断启动。"""
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.loader import plugin_preflight as pf
from framework.loader import source_scan as ss


def _make_plugin(root, name, body):
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / 'main.py').write_text(body, encoding='utf-8')
    return plugin_dir


def _cfg(**kwargs):
    section = {'enabled': True}
    section.update(kwargs)
    return {'plugins': {'source_scan': section}}


def test_disabled_skips_scanning(tmp_path, monkeypatch):
    user_dir = tmp_path / 'plugins'
    _make_plugin(user_dir, 'bad', 'import time\ntime.sleep(3)\n')
    monkeypatch.setattr(pf, 'plugin_dirs', lambda config=None: [('插件目录', str(user_dir))])

    summary = pf.run(_cfg(enabled=False))
    assert summary['enabled'] is False
    assert summary['issues'] == []
    assert summary['plugins'] == 0


def test_hits_are_reported_not_raised(tmp_path, monkeypatch):
    user_dir = tmp_path / 'plugins'
    _make_plugin(user_dir, 'blocking', 'import time\ntime.sleep(3)\n')
    _make_plugin(user_dir, 'clean', 'x = 1\n')
    monkeypatch.setattr(pf, 'plugin_dirs', lambda config=None: [('插件目录', str(user_dir))])

    summary = pf.run(_cfg())
    assert summary['enabled'] is True
    assert summary['plugins'] == 2
    assert len(summary['issues']) == 1
    hit = summary['issues'][0]
    assert hit['plugin'] == 'blocking'
    assert hit['kind'] == 'blocking_import'
    assert hit['file'].endswith('plugins/blocking/main.py')


def test_warn_mode_is_default_and_does_not_block(tmp_path, monkeypatch):
    """默认 warn：命中照样返回汇总，模块没有「拒绝加载」这条路径。"""
    user_dir = tmp_path / 'plugins'
    _make_plugin(user_dir, 'bad', 'import os\nos._exit(0)\n')
    monkeypatch.setattr(pf, 'plugin_dirs', lambda config=None: [('插件目录', str(user_dir))])

    summary = pf.run({'plugins': {'source_scan': {'enabled': True}}})
    assert summary['issues'], '默认配置下必须报出同步退出'
    assert ss.scan_mode({'plugins': {'source_scan': {}}}) == 'warn'


def test_allowlist_filters_report(tmp_path, monkeypatch):
    user_dir = tmp_path / 'plugins'
    _make_plugin(user_dir, 'trusted', 'import time\ntime.sleep(3)\n')
    _make_plugin(user_dir, 'other', 'import time\ntime.sleep(3)\n')
    monkeypatch.setattr(pf, 'plugin_dirs', lambda config=None: [('插件目录', str(user_dir))])

    summary = pf.run(_cfg(allowlist=['trusted']))
    assert [i['plugin'] for i in summary['issues']] == ['other']


def test_max_report_truncates(tmp_path, monkeypatch):
    user_dir = tmp_path / 'plugins'
    for idx in range(4):
        _make_plugin(user_dir, f'p{idx}', 'import time\ntime.sleep(1)\n')
    monkeypatch.setattr(pf, 'plugin_dirs', lambda config=None: [('插件目录', str(user_dir))])

    summary = pf.run(_cfg(max_report=2))
    assert len(summary['issues']) == 4
    assert summary['truncated'] == 2


def test_missing_directory_is_non_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, 'plugin_dirs',
                        lambda config=None: [('插件目录', str(tmp_path / 'nope'))])
    summary = pf.run(_cfg())
    assert summary['plugins'] == 0
    assert summary['issues'] == []


def test_log_lines_carry_file_and_kind(tmp_path, monkeypatch, caplog):
    user_dir = tmp_path / 'plugins'
    _make_plugin(user_dir, 'bad', 'import time\ntime.sleep(3)\n')
    monkeypatch.setattr(pf, 'plugin_dirs', lambda config=None: [('插件目录', str(user_dir))])

    with caplog.at_level(logging.WARNING, logger='zcbot'):
        pf.run(_cfg())
    text = '\n'.join(r.getMessage() for r in caplog.records)
    assert '[插件预检]' in text
    assert 'bad/main.py' in text
    assert '[blocking_import]' in text
