# -*- coding: utf-8 -*-
"""framework/api 安全面真实 HTTP 行为回归测试。

与 tests/test_security_hardening.py（源码字符串匹配）互补：本文件验证
「运行期防护真的生效」，而不是「源码里写了防护字符串」。

覆盖 v1.7.3 七项安全加固中最关键的运行时行为：
  · 认证绕过防护：无 token 一律 401
  · /api/db 敏感表（admin_users/api_tokens）对普通管理员隐藏 + schema/rows 403
  · 高危操作（/api/security/*、/api/plugins/upload 等）收紧为 super
  · 登录失败文案统一（防用户名枚举）
  · 数据库文件下载对普通管理员 403

实现说明：用真实 Framework（SQLite，全新临时库，core_plugins 全关）初始化，
仅 Framework() 不 start()，避免拉起网络/等待线程（CI 友好）；init_db 在建表时
会插入默认 admin/super 账号（admin/admin123），本测试再插入一个普通管理员与一个
被禁用管理员用于对比。create_web_app(fw) 构建完整 Flask 路由后用 test_client 发请求。

运行：pytest tests/test_api_security.py  （或 python tests/test_api_security.py）
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bcrypt
import pytest

from framework.core import Framework
from framework.api.webapp import create_web_app


def _write_config(tmp):
    cfg = os.path.join(tmp, 'config.yaml')
    db = os.path.join(tmp, 'sec_test.db').replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            "database:\n  type: sqlite\n  path: {db}\n"
            "plugin:\n  heartbeat_interval: 60\n"
            "log:\n  level: ERROR\n"
            "web:\n  host: 127.0.0.1\n  port: 0\n"
            "core_plugins:\n"
            "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
            "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
            "  telegram: false\n  discord: false\n  session: false\n"
            "  scheduler: false\n  image_renderer: false\n".format(db=db)
        )
    return cfg, db


@pytest.fixture(scope='module')
def env():
    tmp = tempfile.mkdtemp(prefix='zcbot_api_sec_')
    cfg, db_path = _write_config(tmp)
    fw = Framework(config_path=cfg, role='standard')
    # 仅初始化，不 start()，避免拉起网络/等待线程（CI 友好）

    pw = bcrypt.hashpw(b'admin123', bcrypt.gensalt(12)).decode('utf-8')
    for uname, active in (('normaladmin', 1), ('disabledadmin', 0)):
        try:
            fw.db.execute(
                "INSERT INTO admin_users (username, password_hash, role, is_active) "
                "VALUES (%s, %s, 'admin', %s)",
                (uname, pw, active),
            )
        except Exception:
            # 全新库理论上不会已存在，忽略即可
            pass

    app = create_web_app(fw)
    client = app.test_client()

    data = {'fw': fw, 'client': client, 'db_path': db_path, 'tmp': tmp}
    yield data

    # teardown：释放线程池 + 清理临时库与目录
    try:
        fw._db_executor.shutdown(wait=False)
    except Exception:
        pass
    for p in (db_path, db_path + '-wal', db_path + '-shm'):
        try:
            os.remove(p)
        except OSError:
            pass
    shutil.rmtree(tmp, ignore_errors=True)


def _login(client, username, password):
    r = client.post('/api/login', json={'username': username, 'password': password})
    if r.status_code == 200:
        return r.get_json().get('data', {}).get('token')
    return None


def test_auth_required_without_token(env):
    c = env['client']
    assert c.get('/api/db/tables').status_code == 401
    assert c.get('/api/me').status_code == 401


def test_sensitive_tables_hidden_for_normal_admin(env):
    c = env['client']
    super_token = _login(c, 'admin', 'admin123')
    normal_token = _login(c, 'normaladmin', 'admin123')
    assert super_token and normal_token

    r_super = c.get('/api/db/tables', headers={'Authorization': f'Bearer {super_token}'})
    r_normal = c.get('/api/db/tables', headers={'Authorization': f'Bearer {normal_token}'})
    assert r_super.status_code == 200
    assert r_normal.status_code == 200

    names_super = {t['name'] for t in r_super.get_json()['data']}
    names_normal = {t['name'] for t in r_normal.get_json()['data']}
    assert 'admin_users' in names_super and 'api_tokens' in names_super
    assert 'admin_users' not in names_normal and 'api_tokens' not in names_normal


def test_sensitive_table_schema_403_for_normal_admin(env):
    c = env['client']
    super_token = _login(c, 'admin', 'admin123')
    normal_token = _login(c, 'normaladmin', 'admin123')

    r = c.get('/api/db/tables/admin_users/schema',
              headers={'Authorization': f'Bearer {normal_token}'})
    assert r.status_code == 403
    r = c.get('/api/db/tables/admin_users/schema',
              headers={'Authorization': f'Bearer {super_token}'})
    assert r.status_code == 200


def test_sensitive_table_rows_403_for_normal_admin(env):
    c = env['client']
    super_token = _login(c, 'admin', 'admin123')
    normal_token = _login(c, 'normaladmin', 'admin123')

    r = c.get('/api/db/tables/api_tokens/rows',
              headers={'Authorization': f'Bearer {normal_token}'})
    assert r.status_code == 403
    r = c.get('/api/db/tables/api_tokens/rows',
              headers={'Authorization': f'Bearer {super_token}'})
    assert r.status_code == 200


def test_login_message_uniform(env):
    c = env['client']
    # 不存在用户
    r = c.post('/api/login', json={'username': 'ghost', 'password': 'whatever'})
    assert r.status_code == 401
    assert r.get_json()['msg'] == '用户名或密码错误'
    # 禁用账号（文案一致，防枚举）
    r = c.post('/api/login', json={'username': 'disabledadmin', 'password': 'admin123'})
    assert r.status_code == 401
    assert r.get_json()['msg'] == '用户名或密码错误'
    # 正确凭证
    r = c.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    assert r.status_code == 200
    assert len(r.get_json()['data']['token']) == 2048


def test_super_only_endpoints(env):
    c = env['client']
    super_token = _login(c, 'admin', 'admin123')
    normal_token = _login(c, 'normaladmin', 'admin123')

    # 高危只读接口：普通管理员 403，super 200
    r = c.get('/api/security/blacklist',
              headers={'Authorization': f'Bearer {normal_token}'})
    assert r.status_code == 403
    r = c.get('/api/security/blacklist',
              headers={'Authorization': f'Bearer {super_token}'})
    assert r.status_code == 200

    # 高危写操作：插件上传收紧为 super（普通管理员被装饰器拦截 403）
    r = c.post('/api/plugins/upload',
               headers={'Authorization': f'Bearer {normal_token}'})
    assert r.status_code == 403
    r = c.post('/api/plugins/upload',
               headers={'Authorization': f'Bearer {super_token}'})
    # super 进入函数体（无文件 → 400 缺文件），核心是被装饰器放行 ≠ 403
    assert r.status_code != 403


def test_db_file_download_blocked_for_normal_admin(env):
    c = env['client']
    super_token = _login(c, 'admin', 'admin123')
    normal_token = _login(c, 'normaladmin', 'admin123')

    target = os.path.join(ROOT, '_sec_dl_tmp.db')
    try:
        with open(target, 'wb') as f:
            f.write(b'SQLite format 3\x00')
        path = os.path.abspath(target)

        r = c.get(f'/api/files/download?path={path}',
                  headers={'Authorization': f'Bearer {normal_token}'})
        assert r.status_code == 403
        r = c.get(f'/api/files/download?path={path}',
                  headers={'Authorization': f'Bearer {super_token}'})
        assert r.status_code == 200
    finally:
        try:
            os.remove(target)
        except OSError:
            pass


if __name__ == '__main__':
    import pytest as _pytest
    _pytest.main([__file__, '-v', '-o', 'asyncio_mode=auto'])
