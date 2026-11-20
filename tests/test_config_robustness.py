"""配置健壮性：坏配置不毁文件、未知键点名、只读 --check、启动失败退出码 2。

旧实现把语法错误的 config.yaml 覆盖成默认模板（token / 数据库路径 / 管理员
名单一起丢失），且对 schema 之外的键一声不吭。这里锁定新的行为边界：
语法/安全未知键一律拒绝启动，只有显式授权才降级到内置默认值。
"""
import hashlib
import os
import subprocess
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import framework.config as config_mod
from framework.config import (
    _DEFAULT_CONFIG,
    ConfigLoadError,
    ConfigValidationError,
    _best_effort_crash_config,
    load_config,
    report_config_load_error,
    validate_config,
)
from framework.crash_guard import count_crash_markers, should_abort_boot


@pytest.fixture()
def isolated_core_plugins(tmp_path, monkeypatch):
    """_autoload_core_plugins 会扫描并同步插件 yaml，测试里指到临时文件。"""
    monkeypatch.setattr(config_mod, 'CORE_PLUGINS_YAML',
                        str(tmp_path / 'core_plugins.yaml'))
    return tmp_path


def _default_cfg():
    return yaml.safe_load(_DEFAULT_CONFIG)


def _clean_env(tmp_path):
    return {
        **os.environ,
        'ZCBOT_ALLOW_DEFAULT_CONFIG': '0',
        'ZCBOT_AUTO_MIGRATE': '0',
        'ZCBOT_TOKEN_KEY': '',
        'TMPDIR': str(tmp_path),
    }


def test_broken_yaml_never_falls_back_and_keeps_original_bytes(
        tmp_path, isolated_core_plugins, caplog):
    """旧断言期待 load_config 返回默认配置；新契约是抛 ConfigLoadError。"""
    cfg_path = tmp_path / 'config.yaml'
    raw = b'framework:\n  name: x\n bad-indent: true\nwebui:\n  security: true\n'
    cfg_path.write_bytes(raw)
    before = hashlib.md5(cfg_path.read_bytes()).hexdigest()

    with pytest.raises(ConfigLoadError) as exc_info:
        load_config(str(cfg_path))

    # 入口 reporter 复用同一份错误块与原文副本，并写入崩溃计数。
    with caplog.at_level('CRITICAL', logger='zcbot'):
        report_config_load_error(exc_info.value, str(tmp_path), str(cfg_path))

    after = hashlib.md5(cfg_path.read_bytes()).hexdigest()
    assert before == after, '原配置字节必须完全不变'
    assert cfg_path.read_bytes() == raw

    exc = exc_info.value
    assert exc.line != '未知'
    assert exc.column != '未知'
    copies = list(tmp_path.glob('config.yaml.broken.*'))
    assert len(copies) == 1, copies
    assert copies[0].read_bytes() == raw
    assert os.path.abspath(exc.copy_path) == str(copies[0])

    blob = '\n'.join(r.getMessage() for r in caplog.records)
    assert any(r.levelname == 'CRITICAL' for r in caplog.records)
    assert '原配置文件未被修改' in blob
    assert str(cfg_path) in blob
    assert '行' in blob and '列' in blob
    assert copies[0].name in blob


def test_explicit_default_fallback_prints_banner(tmp_path, isolated_core_plugins,
                                                  caplog):
    cfg_path = tmp_path / 'config.yaml'
    cfg_path.write_text(
        'framework:\n  config:\n    on_error: default\n  bad: [\n',
        encoding='utf-8', newline='')
    with caplog.at_level('CRITICAL', logger='zcbot'):
        cfg = load_config(str(cfg_path))
    assert cfg.get('database', {}).get('path')
    blob = '\n'.join(r.getMessage() for r in caplog.records)
    assert '[当前运行的不是你的配置文件]' in blob
    assert 'database.type / path' in blob
    assert 'access_token 是否为空' in blob


def test_env_flag_allows_default_fallback(tmp_path, isolated_core_plugins,
                                          monkeypatch):
    cfg_path = tmp_path / 'config.yaml'
    cfg_path.write_text('framework:\n  bad: [\n', encoding='utf-8', newline='')
    monkeypatch.setenv('ZCBOT_ALLOW_DEFAULT_CONFIG', '1')
    cfg = load_config(str(cfg_path))
    assert cfg.get('web', {}).get('port') == 8080


def test_unknown_key_names_typo_and_unknown_section():
    cfg = _default_cfg()
    cfg['webui'] = {'securty': True}
    cfg['foo'] = {'bar': 1}
    issues = validate_config(cfg)
    blob = '\n'.join(issues)
    assert 'webui.securty' in blob
    assert 'webui.security' in blob
    assert '[foo]' in blob


def test_unknown_security_key_is_error_level():
    cfg = _default_cfg()
    cfg['webui'] = {'securty': True}
    issues = validate_config(cfg)
    assert any(getattr(i, 'level', '') == 'error' and 'webui.securty' in i
               for i in issues)


def test_strict_unknown_keys_false_downgrades_to_warning():
    cfg = _default_cfg()
    cfg['framework']['config']['strict_unknown_keys'] = False
    cfg['webui'] = {'securty': True}
    issues = validate_config(cfg)
    hit = [i for i in issues if 'webui.securty' in i]
    assert hit and all(getattr(i, 'level', '') == 'warning' for i in hit)


def test_security_human_check_is_known_config():
    cfg = _default_cfg()
    cfg['security']['human_check'] = {'enabled': False}
    issues = validate_config(cfg)
    assert not any('human_check' in i for i in issues), issues


def _run_check(path):
    proc = subprocess.run(
        [sys.executable, '-m', 'framework.config', '--check', str(path)],
        cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
    return proc.returncode, proc.stdout + proc.stderr


def test_check_cli_return_codes(tmp_path):
    good = tmp_path / 'good.yaml'
    good.write_text(yaml.safe_dump(_default_cfg(), allow_unicode=True,
                                   sort_keys=False), encoding='utf-8', newline='')
    rc, out = _run_check(good)
    assert rc == 0, out
    assert '配置检查通过' in out

    bad = tmp_path / 'bad.yaml'
    bad.write_text('framework:\n  securty: true\nfoo: 1\n',
                   encoding='utf-8', newline='')
    rc, out = _run_check(bad)
    assert rc == 2, out
    assert 'framework.securty' in out
    assert 'foo' in out


def test_startup_check_config_forwards(tmp_path):
    good = tmp_path / 'good.yaml'
    good.write_text(yaml.safe_dump(_default_cfg(), allow_unicode=True,
                                   sort_keys=False), encoding='utf-8', newline='')
    proc = subprocess.run(
        [sys.executable, 'startup.py', '--check-config', str(good)],
        cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace',
        env=_clean_env(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert '配置检查通过' in proc.stdout


def test_startup_refuses_broken_config_with_exit_code_2(tmp_path):
    cfg_path = tmp_path / 'config.yaml'
    crash_log = tmp_path / 'error.log'
    cfg_path.write_text(
        'framework:\n'
        '  crash:\n'
        '    enabled: true\n'
        '    safe_mode: false\n'
        '    max_restarts: 3\n'
        f'    path: {crash_log}\n'
        '  securty: true\n'
        'webui:\n'
        ' security: [\n',
        encoding='utf-8', newline='')
    proc = subprocess.run(
        [sys.executable, 'startup.py', str(cfg_path)],
        cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace',
        env=_clean_env(tmp_path), timeout=60)
    blob = proc.stdout + proc.stderr
    assert proc.returncode == 2, blob
    assert '框架未启动' in blob
    assert '配置文件语法错误' in blob
    assert '原配置文件未被修改' in blob
    assert cfg_path.read_bytes().startswith(b'framework:')
    assert crash_log.is_file(), '配置错误必须计入崩溃计数'
    assert '配置错误' in crash_log.read_text(encoding='utf-8')


def test_config_error_crash_count_reaches_safe_mode_threshold(tmp_path,
                                                              isolated_core_plugins):
    cfg_path = tmp_path / 'config.yaml'
    crash_log = tmp_path / 'error.log'
    cfg_path.write_text(
        'framework:\n'
        '  crash:\n'
        '    enabled: true\n'
        '    safe_mode: false\n'
        '    max_restarts: 2\n'
        f'    path: {crash_log}\n'
        '  bad: [\n',
        encoding='utf-8', newline='')
    with pytest.raises(ConfigLoadError) as exc_info:
        load_config(str(cfg_path))
    exc = exc_info.value
    crash_cfg = _best_effort_crash_config(str(cfg_path), str(tmp_path))
    assert crash_cfg['framework']['crash']['safe_mode'] is False

    report_config_load_error(exc, str(tmp_path), str(cfg_path))
    assert count_crash_markers(crash_cfg, str(tmp_path)) == 1
    assert not should_abort_boot(crash_cfg, str(tmp_path))

    report_config_load_error(exc, str(tmp_path), str(cfg_path))
    assert count_crash_markers(crash_cfg, str(tmp_path)) == 2
    assert should_abort_boot(crash_cfg, str(tmp_path))


def test_security_level_bare_off_is_rejected(tmp_path):
    """``security.level: off`` 不加引号会被 YAML 解析成布尔 False。

    这是真实踩到的坑：管理员以为关掉了蜜罐，框架却静默回退到 medium 继续
    拦截，日志只有一条 WARNING。现在必须拒绝启动并点名这一项。
    """
    cfg = tmp_path / 'config.yaml'
    cfg.write_text('security:\n  level: off\n', encoding='utf-8')

    with pytest.raises(ConfigValidationError) as exc:
        load_config(str(cfg))
    assert 'security.level' in str(exc.value)


def test_security_level_quoted_off_is_accepted(tmp_path):
    """写成字符串 "off" 才是合法配置，必须正常加载。"""
    cfg = tmp_path / 'config.yaml'
    cfg.write_text('security:\n  level: "off"\n', encoding='utf-8')

    assert load_config(str(cfg))['security']['level'] == 'off'
