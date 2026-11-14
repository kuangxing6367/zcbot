"""会话令牌安全加固回归测试（真实 Framework + Flask test_client 发真 HTTP）。

覆盖本轮三个安全点及配套取舍：

  · 登录后库里只存 sha256 哈希、2048 字符明文不落任何列（DB 泄露不能冒用）
  · 正确 token 通过 / 错误 token、伪造 2048 字符串、无 token 一律拒绝
  · 改密 / 退出后旧 token 立即失效
  · 老库升级：追加 token_hash 列并清空历史明文（强制全部重新登录）
  · /metrics 未配 metrics_token 时非本机来源不泄露任何指标（健康端点仍可用）
  · 配了 metrics_token 后跨机抓取仍可用，健康检查不受影响

测试铁律：所有路径由 __file__ / pytest 的 tmp_path 推导，用临时绝对路径建库；
不依赖 cwd、不写死时区、不污染真实项目目录。

运行：pytest tests/test_token_security_regressions.py
"""
import hashlib
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.api.webapp import create_web_app
from framework.core import Framework

# 模拟「外部抓取」用的非回环地址（TEST-NET-2 文档地址，不会是真实主机）
_REMOTE_ADDR = '198.51.100.23'


def _write_config(tmp_path, extra_yaml=''):
    cfg = tmp_path / 'config.yaml'
    db_path = (tmp_path / 'token_sec.db').as_posix()
    text = (
        f"database:\n  type: sqlite\n  path: {db_path}\n"
        "plugin:\n  heartbeat_interval: 60\n"
        "log:\n  level: ERROR\n"
        # 关闭主动防御，稳定断言应用层 401（444 静默丢弃另由 smoke 测试覆盖）
        "security:\n  silent_auth: false\n  human_check:\n    enabled: false\n"
        "web:\n  host: 127.0.0.1\n  port: 0\n"
        "core_plugins:\n"
        "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
        "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
        "  telegram: false\n  discord: false\n  session: false\n"
        "  scheduler: false\n  image_renderer: false\n"
    )
    if extra_yaml:
        text += extra_yaml
    cfg.write_text(text, encoding='utf-8')
    return cfg, db_path


def _build_env(tmp_path, extra_yaml=''):
    cfg, db_path = _write_config(tmp_path, extra_yaml)
    fw = Framework(config_path=str(cfg), role='standard')  # 只初始化，不 start()
    return {'fw': fw, 'client': create_web_app(fw).test_client(),
            'db_path': db_path, 'tmp': str(tmp_path)}


def _teardown(env):
    try:
        env['fw']._db_executor.shutdown(wait=False)
    except Exception:
        pass
    for p in (env['db_path'], env['db_path'] + '-wal', env['db_path'] + '-shm'):
        try:
            os.remove(p)
        except OSError:
            pass
    shutil.rmtree(env['tmp'], ignore_errors=True)


@pytest.fixture
def env(tmp_path):
    e = _build_env(tmp_path)
    yield e
    _teardown(e)


def _login(client, username='admin', password='admin123'):
    r = client.post('/api/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.get_json()
    return r.get_json()['data']['token']


def _auth(token):
    return {'Authorization': f'Bearer {token}'}


# ── 1. 库里不含明文 token ─────────────────────────────────────
def test_db_has_no_plaintext_token(env):
    """登录后 admin_users 只存 sha256(token)，2048 字符明文不落任何列。"""
    token = _login(env['client'])
    assert len(token) == 2048  # 客户端仍拿到 2048 字符明文（兼容既有契约）

    row = env['fw'].db.query_one(
        "SELECT * FROM admin_users WHERE username = %s", ('admin',))
    assert row is not None
    assert row['token'] in (None, ''), '历史明文 token 列必须恒为空'
    assert row['token_hash'] == hashlib.sha256(token.encode('utf-8')).hexdigest()
    assert token not in str(list(row.values())), '明文 token 不得出现在任何列值里'


# ── 2. 正确 / 错误 token 校验 ─────────────────────────────────
def test_correct_and_wrong_token(env):
    c = env['client']
    # 登录会种 HttpOnly Cookie（test_client 自动回传），故「无凭据」必须在登录前测
    assert c.get('/api/me').status_code == 401

    token = _login(c)
    assert c.get('/api/me', headers=_auth(token)).status_code == 200

    wrong = token[:-1] + ('0' if token[-1] != '0' else '1')
    assert len(wrong) == 2048
    assert c.get('/api/me', headers=_auth(wrong)).status_code == 401
    assert c.get('/api/me', headers=_auth('f' * 2048)).status_code == 401


# ── 3. 改密 / 退出后旧会话失效 ─────────────────────────────────
def test_change_password_invalidates_old_token(env):
    c = env['client']
    token = _login(c)

    r = c.post('/api/change_password',
               json={'old_password': 'admin123', 'new_password': 'newpass123'},
               headers=_auth(token))
    assert r.status_code == 200, r.get_json()

    # 旧 token 立即失效，且库里 token_hash 被清空（不是只改密码哈希）
    assert c.get('/api/me', headers=_auth(token)).status_code == 401
    row = env['fw'].db.query_one(
        "SELECT token_hash FROM admin_users WHERE username = %s", ('admin',))
    assert row['token_hash'] in (None, '')

    # 用新密码可以重新登录（功能没被改坏）
    r2 = c.post('/api/login', json={'username': 'admin', 'password': 'newpass123'})
    assert r2.status_code == 200, r2.get_json()


def test_logout_invalidates_token(env):
    c = env['client']
    token = _login(c)
    assert c.post('/api/logout', headers=_auth(token)).status_code == 200
    assert c.get('/api/me', headers=_auth(token)).status_code == 401


# ── 4. /metrics 未配 token 时不泄露，健康端点仍可用 ──────────────
def test_metrics_without_token_does_not_leak(env):
    """未配 metrics_token：非本机来源 404 且不含任何指标；本机回环仍可抓取。"""
    c = env['client']
    r = c.get('/metrics', environ_base={'REMOTE_ADDR': _REMOTE_ADDR})
    assert r.status_code == 404, r.status_code
    body = r.get_data(as_text=True)
    assert 'zcbot_' not in body
    assert 'zcbot_up' not in body

    # 本机回环（test_client 默认 127.0.0.1）保留同机抓取能力，体现保守默认而非一刀切
    r_local = c.get('/metrics')
    assert r_local.status_code == 200
    assert 'zcbot_up' in r_local.get_data(as_text=True)


def test_health_endpoints_still_available(env):
    """健康探针与 /metrics 放行语义区分：非本机来源仍然无条件可达。"""
    c = env['client']
    r = c.get('/healthz', environ_base={'REMOTE_ADDR': _REMOTE_ADDR})
    assert r.status_code == 200
    assert (r.get_json() or {}).get('status') == 'ok'

    r2 = c.get('/readyz', environ_base={'REMOTE_ADDR': _REMOTE_ADDR})
    # 未 start() 时是 503(not_ready)，但端点必须可达（不是 404）且带逐项检查
    assert r2.status_code in (200, 503)
    assert (r2.get_json() or {}).get('checks')


def test_metrics_token_allows_remote(tmp_path):
    """配了 metrics_token：非本机来源凭 Bearer 可抓取，无令牌仍 401。"""
    env = _build_env(tmp_path, 'observability:\n  metrics_token: "s3cret"\n')
    try:
        c = env['client']
        r = c.get('/metrics', environ_base={'REMOTE_ADDR': _REMOTE_ADDR})
        assert r.status_code == 401
        r2 = c.get('/metrics', headers={'Authorization': 'Bearer s3cret'},
                   environ_base={'REMOTE_ADDR': _REMOTE_ADDR})
        assert r2.status_code == 200
        assert 'zcbot_up' in r2.get_data(as_text=True)
    finally:
        _teardown(env)


# ── 5. 老库平滑升级：加列 + 清空历史明文（强制重新登录）──────────
def test_migration_adds_hash_column_and_purges_plaintext(tmp_path):
    """模拟老库（只有明文 token 列且已有会话）：跑 MIGRATIONS 后
    token_hash 列存在、明文被清空、且不回填哈希 —— 老会话全部作废。"""
    from framework.database import migrations as M
    from framework.database.db import Database
    from framework.database.schema import MIGRATIONS

    # 注册表形状：v1–v10 不可变，末尾追加 v11（api_tokens 摘要化）
    versions = [m.version for m in MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert M.latest_version(MIGRATIONS) == 11
    assert MIGRATIONS[-2].version == 10, 'v10 仍在原位，未被重排'
    assert M.checksum_of(MIGRATIONS[-1]), 'v11 必须能算出源码校验和'

    db = Database({'type': 'sqlite',
                   'path': (tmp_path / 'legacy.db').as_posix()})
    db.execute(
        "CREATE TABLE admin_users ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, username VARCHAR(50), "
        "token VARCHAR(2048), token_created_at TEXT)")
    legacy_token = 'a' * 2048
    db.execute(
        "INSERT INTO admin_users (username, token) VALUES (%s, %s)",
        ('admin', legacy_token))

    report = M.run_pending(db, MIGRATIONS, M.MigrationContext(source='test'))
    assert report['ok'] is True, report
    assert report['current'] == 11, report
    assert db.table_has_column('admin_users', 'token_hash')

    row = db.query_one("SELECT * FROM admin_users WHERE username = %s", ('admin',))
    assert row['token'] is None, '历史明文 token 必须被物理清空'
    assert legacy_token not in str(list(row.values()))
    assert row['token_hash'] in (None, ''), '不回填哈希：老会话作废、需重新登录'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-o', 'asyncio_mode=auto']))
