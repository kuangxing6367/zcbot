"""数据库备份接口回归。

`/api/db-backup*` 只有一层 HTTP 适配，逻辑全在 `framework/database/backup.py`。
这组用例锁接口层的三件事：

- 状态查询要能看见「上次跑成没成 + 现有几份 + 生效配置」——不然「超过 100MB
  暂停备份」这种提示没有任何地方能看见；
- 手动触发要超管 + 审计，普通管理员不能凭空改备份策略；
- 改阈值要**写回 config.yaml**，不能只在内存生效（重启就变回去等于没改）。
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest


@pytest.fixture(scope='module')
def env():
    import bcrypt

    from framework.api.webapp import create_web_app
    from framework.core import Framework

    tmp = tempfile.mkdtemp(prefix='zcbot_bk_api_')
    cfg = os.path.join(tmp, 'config.yaml')
    db_path = os.path.join(tmp, 'c.db').replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            f"database:\n  type: sqlite\n  path: {db_path}\n  password: 'x'\n"
            "  backup:\n    keep: 2\n    interval_hours: 6\n"
            "log:\n  level: ERROR\n"
            "security:\n  silent_auth: false\n  human_check:\n    enabled: false\n"
            "  honeypot:\n    enabled: false\n"
            "web:\n  host: 127.0.0.1\n  port: 8080\n"
            "core_plugins:\n"
            "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
            "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
            "  telegram: false\n  discord: false\n  session: false\n"
            "  scheduler: false\n  image_renderer: false\n"
        )
    fw = Framework(config_path=cfg, role='standard')
    pw = bcrypt.hashpw(b'admin123', bcrypt.gensalt(12)).decode('utf-8')
    try:
        fw.db.execute("INSERT INTO admin_users (username, password_hash, role, is_active) "
                      "VALUES ('ops2', %s, 'admin', 1)", (pw,))
    except Exception:
        fw.db.execute("UPDATE admin_users SET password_hash=%s WHERE username='ops2'", (pw,))
    app = create_web_app(fw)

    sup = app.test_client()
    r = sup.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    assert r.status_code == 200
    sup.environ_base['HTTP_AUTHORIZATION'] = 'Bearer ' + r.get_json()['data']['token']

    ops = app.test_client()
    r2 = ops.post('/api/login', json={'username': 'ops2', 'password': 'admin123'})
    ops.environ_base['HTTP_AUTHORIZATION'] = 'Bearer ' + r2.get_json()['data']['token']

    yield {'fw': fw, 'sup': sup, 'ops': ops, 'cfg': cfg, 'tmp': tmp,
           'db_path': db_path}

    try:
        fw.backup_service.stop(timeout=2)
    except Exception:
        pass
    try:
        fw._db_executor.shutdown(wait=False)
    except Exception:
        pass
    try:
        fw.db.shutdown()
    except Exception:
        pass
    for p in (db_path, db_path + '-wal', db_path + '-shm'):
        try:
            os.remove(p)
        except OSError:
            pass
    shutil.rmtree(tmp, ignore_errors=True)


def test_status_reports_config_and_files(env):
    r = env['sup'].get('/api/db-backup')
    assert r.status_code == 200
    data = r.get_json()['data']
    assert data['keep'] == 2
    assert data['interval_hours'] == 6
    assert data['dir'].endswith('backups')
    assert isinstance(data['backups'], list)
    # 默认值也要一起给前端，改配置时才知道范围
    assert data['defaults']['refuse_above_mb'] == 100
    assert data['defaults']['min_free_after_mb'] == 0.5


def test_manual_run_creates_backup(env):
    r = env['sup'].post('/api/db-backup/run')
    assert r.status_code == 200
    data = r.get_json()['data']
    assert data['status'] == 'ok', data.get('note')
    assert os.path.isfile(data['path'])
    # 跑完 status 里应该能看到这一份
    listing = env['sup'].get('/api/db-backup').get_json()['data']['backups']
    assert any(b['name'] == os.path.basename(data['path']) for b in listing)


def test_config_update_persists_to_yaml(env):
    r = env['sup'].post('/api/db-backup/config',
                        json={'keep': 3, 'interval_hours': 1.5,
                              'refuse_above_mb': 200})
    assert r.status_code == 200
    data = r.get_json()['data']
    assert data['config']['keep'] == 3
    assert data['config']['interval_hours'] == 1.5

    with open(env['cfg'], encoding='utf-8') as f:
        text = f.read()
    assert 'keep: 3' in text
    assert 'refuse_above_mb: 200' in text
    # 写回不能把同一段里别的键冲掉
    assert 'path:' in text and 'interval_hours' in text


def test_config_rejects_bad_value(env):
    r = env['sup'].post('/api/db-backup/config', json={'keep': '很多'})
    assert r.status_code == 400
    assert 'keep' in r.get_json()['msg']


def test_config_clamps_out_of_range(env):
    """超出范围的数字要按钳制值写入，而不是原样落盘。"""
    r = env['sup'].post('/api/db-backup/config', json={'compress_level': 99})
    assert r.status_code == 200
    assert r.get_json()['data']['config']['compress_level'] == 9


def test_non_super_cannot_run_or_tune(env):
    assert env['ops'].post('/api/db-backup/run').status_code == 403
    assert env['ops'].post('/api/db-backup/config', json={'keep': 1}).status_code == 403
    # 看状态是可以的：普通管理员也该知道备份有没有在跑
    assert env['ops'].get('/api/db-backup').status_code == 200
