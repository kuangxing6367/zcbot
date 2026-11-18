"""用户 / 群组管理 CRUD 回归。

此前后台只能「看列表 + 拉黑 + 设超管」，新增与编辑只能去改数据库。这组用例锁住
新增 / 编辑 / 删除三条链路的真实落库：
- 新增重复 ID 必须 400（依赖 UNIQUE 约束，不能靠前端挡）；
- 编辑只改昵称/备注，不改 ID；
- 删除后列表里确实没有这一行；
- 这些写操作都必须 `@require_super`，普通管理员拿不到。
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

    tmp = tempfile.mkdtemp(prefix='zcbot_ug_')
    cfg = os.path.join(tmp, 'config.yaml')
    db_path = os.path.join(tmp, 'ug.db').replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            f"database:\n  type: sqlite\n  path: {db_path}\n  password: 'x'\n"
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
        fw.db.execute(
            "INSERT INTO admin_users (username, password_hash, role, is_active) "
            "VALUES ('opsadmin', %s, 'admin', 1)", (pw,))
    except Exception:
        fw.db.execute("UPDATE admin_users SET password_hash=%s WHERE username='opsadmin'", (pw,))
    app = create_web_app(fw)

    sup = app.test_client()
    r = sup.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    assert r.status_code == 200
    sup.environ_base['HTTP_AUTHORIZATION'] = 'Bearer ' + r.get_json()['data']['token']

    ops = app.test_client()
    r2 = ops.post('/api/login', json={'username': 'opsadmin', 'password': 'admin123'})
    assert r2.status_code == 200
    ops.environ_base['HTTP_AUTHORIZATION'] = 'Bearer ' + r2.get_json()['data']['token']

    yield {'fw': fw, 'sup': sup, 'ops': ops, 'db_path': db_path}

    try:
        fw._db_executor.shutdown(wait=False)
    except Exception:
        pass
    try:
        fw.db.close()
    except Exception:
        pass
    for p in (db_path, db_path + '-wal', db_path + '-shm'):
        try:
            os.remove(p)
        except OSError:
            pass
    shutil.rmtree(tmp, ignore_errors=True)


def test_user_crud(env):
    c = env['sup']
    uid = '900001'
    r = c.post('/api/users', json={'user_id': uid, 'nickname': '小明', 'remark': '测试'})
    assert r.status_code == 200 and r.get_json()['code'] == 0, r.get_data(as_text=True)[:200]

    # 重复新增：靠 UNIQUE 约束挡住
    dup = c.post('/api/users', json={'user_id': uid})
    assert dup.status_code == 400

    rows = c.get('/api/users?keyword=' + uid).get_json()['data']
    assert len(rows) == 1 and rows[0]['nickname'] == '小明'

    r = c.put(f'/api/users/{uid}', json={'nickname': '小明2', 'remark': '改过'})
    assert r.status_code == 200
    rows = c.get('/api/users?keyword=' + uid).get_json()['data']
    assert rows[0]['nickname'] == '小明2' and rows[0]['remark'] == '改过'

    r = c.delete(f'/api/users/{uid}')
    assert r.status_code == 200
    assert c.get('/api/users?keyword=' + uid).get_json()['data'] == []


def test_user_update_missing_is_404(env):
    r = env['sup'].put('/api/users/999999', json={'nickname': 'x'})
    assert r.status_code == 404


def test_group_crud(env):
    c = env['sup']
    gid = '700001'
    r = c.post('/api/groups', json={'group_id': gid, 'group_name': '测试群'})
    assert r.status_code == 200 and r.get_json()['code'] == 0, r.get_data(as_text=True)[:200]
    assert c.post('/api/groups', json={'group_id': gid}).status_code == 400

    groups = c.get('/api/groups').get_json()['data']
    assert any(g['group_id'] == gid and g['group_name'] == '测试群' for g in groups)

    assert c.put(f'/api/groups/{gid}', json={'group_name': '改名了'}).status_code == 200
    groups = c.get('/api/groups').get_json()['data']
    assert any(g['group_name'] == '改名了' for g in groups if g['group_id'] == gid)

    assert c.delete(f'/api/groups/{gid}').status_code == 200
    assert not any(g['group_id'] == gid for g in c.get('/api/groups').get_json()['data'])


def test_writes_require_super(env):
    """普通管理员只能看，不能增删改。"""
    ops = env['ops']
    assert ops.get('/api/users').status_code == 200
    r = ops.post('/api/users', json={'user_id': '900900'})
    assert r.status_code == 403, f'普通管理员不该能新增用户，实际 {r.status_code}'
    r = ops.delete('/api/groups/1')
    assert r.status_code == 403
