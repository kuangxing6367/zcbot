"""文件浏览器跨平台回归：Windows 盘符 / 反斜杠 / 跨根目录不能 500。

线上反馈「文件浏览在 Windows 上用不了」，根因是
`os.path.commonpath([root, target])` 在**跨盘符**时抛 `ValueError`：
这个异常被最外层 `except` 接住，于是正常的越界访问变成了 500「服务器内部错误」，
前端只看到一片空白。除此之外：
- 上传文件名的 `os.path.basename` 不认反斜杠，Linux 下会把 `C:\\a\\b.txt` 当文件名；
- 删除根目录的守卫用 `abspath == normpath` 比较，Windows 大小写不敏感，能绕过；
- 断链符号链接让整页 `os.stat` 抛异常，一处坏文件拖垮整个目录列表。

这组用例锁住：越界一律 400（不是 500）、根目录不可删、二进制不预览、
反斜杠文件名被正确截断、列表带 parent/sep 供前端做「上级目录」。
"""
from __future__ import annotations

import io
import os
import shutil
import tempfile

import pytest


@pytest.fixture(scope='module')
def env():
    from framework.api.webapp import create_web_app
    from framework.core import Framework

    tmp = tempfile.mkdtemp(prefix='zcbot_files_')
    old_home = os.environ.get('ZCBOT_HOME')
    os.environ['ZCBOT_HOME'] = tmp          # 让 <home>/data 落在临时目录，可安全读写
    cfg = os.path.join(tmp, 'config.yaml')
    db_path = os.path.join(tmp, 'f.db').replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            f"database:\n  type: sqlite\n  path: {db_path}\n  password: 'x'\n"
            "log:\n  level: ERROR\n"
            "security:\n  silent_auth: false\n  whitelist_ips: []\n"
            "  human_check:\n    enabled: false\n"
            "  honeypot:\n    enabled: false\n"
            "web:\n  host: 127.0.0.1\n  port: 8080\n"
            "core_plugins:\n"
            "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
            "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
            "  telegram: false\n  discord: false\n  session: false\n"
            "  scheduler: false\n  image_renderer: false\n"
        )
    fw = Framework(config_path=cfg, role='standard')
    app = create_web_app(fw)
    client = app.test_client()
    r = client.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    token = (r.get_json() or {}).get('data', {}).get('token')
    assert token, '默认管理员登录失败，后续用例无法验证鉴权行为'
    client.environ_base['HTTP_AUTHORIZATION'] = 'Bearer ' + token
    data_dir = os.path.join(tmp, 'data')
    os.makedirs(data_dir, exist_ok=True)

    yield {'fw': fw, 'client': client, 'tmp': tmp, 'data_dir': data_dir, 'db_path': db_path}

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
    if old_home is None:
        os.environ.pop('ZCBOT_HOME', None)
    else:
        os.environ['ZCBOT_HOME'] = old_home
    shutil.rmtree(tmp, ignore_errors=True)


def test_roots_listed(env):
    r = env['client'].get('/api/files/list?path=')
    assert r.status_code == 200
    body = r.get_json()
    assert body['code'] == 0
    names = [e['name'] for e in body['data']['entries']]
    assert '数据目录' in names


def test_list_has_parent_and_sep(env):
    r = env['client'].get('/api/files/list?path=' + env['data_dir'])
    assert r.status_code == 200
    data = r.get_json()['data']
    assert data['current_path'] == os.path.normpath(env['data_dir'])
    assert data['sep'] == os.sep
    assert 'parent' in data
    assert isinstance(data['entries'], list)


def test_outside_root_is_400_not_500(env):
    """越界路径必须干净拒绝；跨盘符曾抛 ValueError → 500。"""
    outside = 'Z:\\__zcbot_not_here__' if os.name == 'nt' else '/etc'
    r = env['client'].get('/api/files/list?path=' + outside)
    assert r.status_code == 400, f'越界应为 400，实际 {r.status_code}'
    assert r.get_json()['code'] == 400


def test_delete_root_is_refused(env):
    """数据目录本身不许删：删掉它等于清库。"""
    r = env['client'].post('/api/files/delete', json={'path': env['data_dir']})
    assert r.status_code == 400
    assert os.path.isdir(env['data_dir']), '根目录被删了'


def test_binary_preview_refused(env):
    p = os.path.join(env['data_dir'], 'blob.bin')
    with open(p, 'wb') as f:
        f.write(b'ABC\x00\x01\x02DEF')
    r = env['client'].get('/api/files/read?path=' + p.replace('\\', '/'))
    assert r.status_code == 400
    assert '二进制' in r.get_json()['msg']


def test_upload_backslash_filename_is_sanitized(env):
    """浏览器带上来的 `..\\evil.txt` 只能落到目标目录里，且文件名被截断。"""
    r = env['client'].post(
        '/api/files/upload',
        data={'dir': env['data_dir'],
              'files': (io.BytesIO(b'x'), '..\\evil.txt')},
        content_type='multipart/form-data',
    )
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    saved = r.get_json().get('saved') or []
    assert len(saved) == 1, saved
    # 客户端可能已经在 multipart 编码时吃掉反斜杠，所以这里断言的是「结果安全」：
    # 落盘名不含任何路径分隔符，且文件确实在目标目录里（没有 ../ 逃逸）。
    name = saved[0]
    assert '/' not in name and '\\' not in name, f'文件名仍带路径分隔符: {name}'
    assert os.path.isfile(os.path.join(env['data_dir'], name))
    assert not os.path.exists(os.path.join(env['tmp'], 'evil.txt')), '文件逃出了目标目录'


def test_rename_rejects_traversal(env):
    p = os.path.join(env['data_dir'], 'evil.txt').replace('\\', '/')
    r = env['client'].post('/api/files/rename', json={'path': p, 'new_name': '../x'})
    assert r.status_code == 400


def test_upload_rejects_database_extensions(env):
    """上传与写入必须同一套后缀黑名单，不能用上传绕过 .db/.sqlite 限制。"""
    for name in ('evil.db', 'evil.sqlite', 'evil.pYC'):
        r = env['client'].post(
            '/api/files/upload',
            data={'dir': env['data_dir'], 'files': (io.BytesIO(b'x'), name)},
            content_type='multipart/form-data',
        )
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        assert not r.get_json().get('saved'), name
        assert not os.path.exists(os.path.join(env['data_dir'], name)), name


def test_read_rejects_oversized_file(env):
    """在线预览有大小上限，超大文件只能下载，不能整份读进内存。"""
    from framework.api.files import MAX_READ_BYTES

    p = os.path.join(env['data_dir'], 'big.txt')
    with open(p, 'wb') as f:
        f.truncate(MAX_READ_BYTES + 1)
    try:
        r = env['client'].get('/api/files/read?path=' + p.replace('\\', '/'))
        assert r.status_code == 400
        assert '超过' in r.get_json()['msg']
    finally:
        os.remove(p)
