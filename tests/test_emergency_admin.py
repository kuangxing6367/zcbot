"""WebUI 应急管理员：凭据落盘、自包含 token、DB 故障降级的回归测试。

用临时目录注入 base，既不碰真实 data/，也不依赖损坏的库文件（那类样本
不稳定）。这里测的是我们自己的编排：配置里只有哈希、token 不查库即可校验、
篡改/过期/禁用一概拒绝、审计落到独立日志。
"""
import json
import os
import time

import pytest

from framework.api import emergency_admin as ema


@pytest.fixture()
def base(tmp_path):
    d = tmp_path / 'data'
    d.mkdir()
    return str(d)


def _load_raw(base):
    with open(ema.config_path(base), encoding='utf-8') as f:
        import yaml
        return yaml.safe_load(f)


def test_ensure_config_creates_file_with_hash_only(base, capsys):
    cfg, plaintext = ema.ensure_config(base, announce=True)
    assert cfg['username'].startswith('emerg_')
    assert plaintext
    assert cfg['password_hash'] != plaintext
    assert cfg['token_secret']
    raw = _load_raw(base)
    assert plaintext not in json.dumps(raw, ensure_ascii=False)
    assert raw['password_hash'] == cfg['password_hash']
    out = capsys.readouterr().out
    assert plaintext in out  # 明文只打印一次
    assert cfg['username'] in out


def test_ensure_config_idempotent_no_new_password(base):
    cfg1, p1 = ema.ensure_config(base, announce=False)
    cfg2, p2 = ema.ensure_config(base, announce=False)
    assert p1 is not None       # 首次生成返回明文
    assert p2 is None           # 再次调用不再返明文
    assert cfg1['username'] == cfg2['username']
    assert cfg1['password_hash'] == cfg2['password_hash']


def test_file_permission_tightened(base):
    if os.name == 'nt':
        pytest.skip('Windows 无 POSIX 权限位')
    ema.ensure_config(base, announce=False)
    mode = os.stat(ema.config_path(base)).st_mode & 0o777
    assert mode == 0o600


def test_verify_password_ok_and_wrong(base):
    cfg, plaintext = ema.ensure_config(base, announce=False)
    assert ema.verify_password(cfg['username'], plaintext, base) is True
    assert ema.verify_password(cfg['username'], 'wrong-password', base) is False
    assert ema.verify_password('someone_else', plaintext, base) is False


def test_verify_password_no_config(base):
    assert ema.verify_password('any', 'any', base) is False
    assert ema.is_emergency_user('any', base) is False


def test_disabled_config_rejects_all(base):
    cfg, plaintext = ema.ensure_config(base, announce=False)
    raw = _load_raw(base)
    raw['enabled'] = False
    with open(ema.config_path(base), 'w', encoding='utf-8') as f:
        import yaml
        yaml.safe_dump(raw, f)
    assert ema.verify_password(cfg['username'], plaintext, base) is False
    assert ema.is_emergency_user(cfg['username'], base) is False
    assert ema.issue_token(cfg['username'], base=base) == ''


def test_token_roundtrip_without_db(base, monkeypatch):
    cfg, _ = ema.ensure_config(base, announce=False)

    def boom(*a, **k):
        raise AssertionError('应急 token 校验不允许查库')

    # 模块本身没有 db 依赖；这里用 sqlite3.connect 兜底监测，确保没被意外引入
    import sqlite3
    monkeypatch.setattr(sqlite3, 'connect', boom)
    token = ema.issue_token(cfg['username'], base=base)
    assert token.startswith(ema.TOKEN_PREFIX)
    admin = ema.verify_token(token, base)
    assert admin and admin['emergency'] is True
    assert admin['role'] == 'super'
    assert admin['id'] == 'emergency'
    assert admin['username'] == cfg['username']


def test_token_tamper_rejected(base):
    cfg, _ = ema.ensure_config(base, announce=False)
    token = ema.issue_token(cfg['username'], base=base)
    head, sig = token.rsplit('.', 1)
    bad = head + '.' + ('0' * len(sig) if sig[0] != '0' else '1' * len(sig))
    assert ema.verify_token(bad, base) is None
    assert ema.verify_token('eme1.fake.payload', base) is None
    assert ema.verify_token('', base) is None
    assert ema.verify_token(None, base) is None


def test_token_expired_rejected(base, monkeypatch):
    cfg, _ = ema.ensure_config(base, announce=False)
    token = ema.issue_token(cfg['username'], ttl=-10, base=base)
    assert ema.verify_token(token, base) is None


def test_token_username_must_match_config(base):
    cfg, _ = ema.ensure_config(base, announce=False)
    # 手工签一个用户名为空的 payload
    import base64
    payload = {'u': 'intruder', 'r': 'super', 'exp': int(time.time()) + 60, 'n': 'x'}
    raw = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    body = base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')
    assert ema.verify_token(ema.TOKEN_PREFIX + body + '.deadbeef', base) is None


def test_reset_invalidates_old_token(base):
    cfg, _ = ema.ensure_config(base, announce=False)
    old = ema.issue_token(cfg['username'], base=base)
    cfg2, new_pwd = ema.reset_password(base, announce=False)
    assert ema.verify_token(old, base) is None
    assert ema.verify_password(cfg2['username'], new_pwd, base) is True


def test_audit_writes_independent_log(base):
    ema.write_audit('login', {'username': 'x'}, base=base, ip='1.2.3.4')
    ema.write_audit('recover', base=base, result='failure')
    path = os.path.join(base, 'logs', ema.AUDIT_NAME)
    with open(path, encoding='utf-8') as f:
        lines = [json.loads(x) for x in f if x.strip()]
    assert lines[0]['action'] == 'login'
    assert lines[0]['ip'] == '1.2.3.4'
    assert lines[1]['result'] == 'failure'


def test_no_emoji_in_module():
    with open(ema.__file__, encoding='utf-8') as f:
        text = f.read()
    assert not any(ord(ch) > 0xFFFF for ch in text)
