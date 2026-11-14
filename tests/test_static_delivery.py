"""静态资源交付：按需 gzip / hash 产物长缓存 / 条件请求 / 路径穿越。

背景：首屏 9 个资源合计 1.71 MB，服务端原本既不开压缩也不给 Cache-Control，
每次刷新全量重下。这组用例锁住三件事：

1. 文本产物在客户端声明 gzip 时必须压缩（element-plus 1.03 MB → 313 KB）；
2. 带内容 hash 的文件名可以 immutable 缓存，index.html 必须 no-cache；
3. filename 来自 URL，路径穿越不能落到项目根目录的文件上。
"""
from __future__ import annotations

import gzip
import os
import shutil
import tempfile

import pytest


@pytest.fixture(scope='module')
def web_env():
    from framework.api.webapp import create_web_app
    from framework.core import Framework

    tmp = tempfile.mkdtemp(prefix='zcbot_static_')
    cfg = os.path.join(tmp, 'config.yaml')
    db_path = os.path.join(tmp, 'static.db').replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            f"database:\n  type: sqlite\n  path: {db_path}\n  password: 'db-real-pw'\n"
            "onebot:\n  access_token: 'atk-real-value'\n  listen_port: 6830\n"
            "plugin:\n  heartbeat_interval: 60\n"
            "log:\n  level: ERROR\n"
            "security:\n  silent_auth: false\n  human_check:\n    enabled: false\n"
            "web:\n  host: 127.0.0.1\n  port: 0\n  token_timeout: 3600\n"
            "core_plugins:\n"
            "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
            "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
            "  telegram: false\n  discord: false\n  session: false\n"
            "  scheduler: false\n  image_renderer: false\n"
        )
    fw = Framework(config_path=cfg, role='standard')
    client = create_web_app(fw).test_client()
    yield {'fw': fw, 'client': client, 'tmp': tmp, 'db_path': db_path}
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


def _web_root() -> str:
    from framework.paths import webui_web_dir
    return webui_web_dir()


def _pick_chunk() -> tuple[str, str]:
    """取一个体积够大的 hash 产物（gzip 才看得出效果）。"""
    js_dir = os.path.join(_web_root(), 'js')
    best = None
    for name in os.listdir(js_dir):
        if not name.endswith('.js'):
            continue
        size = os.path.getsize(os.path.join(js_dir, name))
        if '.' not in name.rstrip('.js'):
            continue
        if best is None or size > best[1]:
            best = (name, size)
    assert best, 'WebUI 产物目录里没有 js chunk'
    return best[0], os.path.join(js_dir, best[0])


def test_gzip_used_when_client_accepts_it(web_env):
    """声明 Accept-Encoding: gzip 时必须压缩，且解压后与原文件逐字节一致。"""
    name, path = _pick_chunk()
    with open(path, 'rb') as f:
        raw = f.read()
    assert len(raw) >= 1024, '该产物太小，压缩收益不可测'

    r = web_env['client'].get(f'/js/{name}', headers={'Accept-Encoding': 'gzip'})
    assert r.status_code == 200
    assert r.headers.get('Content-Encoding') == 'gzip', '未启用 gzip，首屏要全量传原文件'
    assert int(r.headers['Content-Length']) == len(r.data)
    assert len(r.data) < len(raw) * 0.6, f'压缩率异常: {len(raw)} -> {len(r.data)}'
    assert gzip.decompress(r.data) == raw, '压缩内容与原文件不一致'
    # 同一 URL 存在两种编码，缺 Vary 会让代理把 gzip 版本喂给不支持的客户端
    assert 'Accept-Encoding' in (r.headers.get('Vary') or '')


def test_plain_body_when_client_does_not_accept_gzip(web_env):
    name, path = _pick_chunk()
    with open(path, 'rb') as f:
        raw = f.read()
    r = web_env['client'].get(f'/js/{name}', headers={'Accept-Encoding': 'identity'})
    assert r.status_code == 200
    assert not r.headers.get('Content-Encoding')
    assert r.data == raw


def test_hashed_chunk_gets_immutable_cache(web_env):
    name, _ = _pick_chunk()
    r = web_env['client'].get(f'/js/{name}')
    cc = r.headers.get('Cache-Control') or ''
    assert 'immutable' in cc, f'hash 产物没拿到 immutable 缓存: {cc!r}'
    assert 'max-age=31536000' in cc


def test_unhashed_script_gets_short_cache(web_env):
    """theme-init.js 没有内容 hash，不能长缓存，否则改了发不出去。"""
    r = web_env['client'].get('/js/theme-init.js')
    assert r.status_code == 200
    cc = r.headers.get('Cache-Control') or ''
    assert 'immutable' not in cc, f'无 hash 的散装脚本被长缓存了: {cc!r}'
    assert 'max-age=300' in cc


def test_index_is_always_revalidated(web_env):
    """入口页必须每次校验：发版后用户不能还在跑旧引用。"""
    r = web_env['client'].get('/')
    assert r.status_code == 200
    assert (r.headers.get('Cache-Control') or '') == 'no-cache'


def test_etag_returns_304(web_env):
    name, _ = _pick_chunk()
    first = web_env['client'].get(f'/js/{name}')
    etag = first.headers.get('ETag')
    assert etag, '静态资源缺少 ETag，条件请求无从谈起'
    second = web_env['client'].get(f'/js/{name}', headers={'If-None-Match': etag})
    assert second.status_code == 304
    assert second.data == b''


def test_path_traversal_is_blocked(web_env):
    """filename 直接来自 URL，必须经过 safe_join，不能读到产物目录外的文件。"""
    c = web_env['client']
    for probe in (
        '/js/../../config.yaml',
        '/js/....//....//config.yaml',
        '/js/%2e%2e/%2e%2e/config.yaml',
        '/css/../../config/local.yaml',
    ):
        r = c.get(probe)
        assert r.status_code in (400, 404), f'路径穿越未被挡住: {probe} -> {r.status_code}'
        body = r.data.decode('utf-8', 'replace')
        assert 'db-real-pw' not in body


def test_local_debug_never_served_by_static_route(web_env):
    """明文诱饵文件绝不能从磁盘直出。

    蜜罐启用时由陷阱接住（返回固定假凭据 200 并拉黑），禁用时必须 404 ——
    两种都行，唯独不能是磁盘上那份真实文件的内容。
    """
    with open(os.path.join(_web_root(), 'js', 'local.debug.js'), encoding='utf-8') as f:
        on_disk = f.read()
    r = web_env['client'].get('/js/local.debug.js')
    assert r.status_code in (200, 404)
    served = r.data.decode('utf-8', 'replace')
    assert served != on_disk, '静态路由把蜜罐文件原文直出了'
    if r.status_code == 200:
        # 命中蜜罐时返回的是内置诱饵（同样含假口令，但形态与磁盘文件不同）
        assert served.lstrip().startswith('window.__ZC'), \
            f'返回内容不是受控诱饵: {served[:60]!r}'
