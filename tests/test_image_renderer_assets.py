# -*- coding: utf-8 -*-
"""image_renderer 运行时资源层（assets.py）的回归测试。

覆盖的是几条「坏了会静默出事」的路径：

  1. 资源定位顺序：插件目录 → data 缓存。搞反了会让下载来的旧包盖掉开发机上的新文件。
  2. 完整性校验：manifest 里的 sha256 必须真的被比对。少这一步，半截文件会进正式目录，
     等到用户切 backend=rust 才炸，而且报错跟真实原因（下载损坏）完全无关。
  3. 路径穿越：zip 里带 `../` 必须拒绝，否则解压能写到任意位置。
  4. 失败不污染：校验失败时不能留下 `.part` 目录或半套文件。
  5. 不静默降级：未配置 asset_url 时返回明确错误，而不是假装成功。

测试全部走 file:// URL，不联网——CI 与本地行为一致。
"""
import hashlib
import importlib.util
import io
import json
import os
import sys
import zipfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ASSETS_PY = os.path.join(os.path.dirname(_HERE), 'core_plugins',
                          'image_renderer', 'assets.py')


def _load_assets():
    spec = importlib.util.spec_from_file_location('zcbot_assets_under_test', _ASSETS_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


assets = _load_assets()


# ---------------------------------------------------------------- 构造资源包

def _font_payloads():
    return {
        'fonts/' + f: ('payload:' + f).encode('utf-8')
        for f in assets.FONT_FILES
    }


def build_zip(path, files=None, with_manifest=True, mutate_manifest=None):
    """按 assets.zip 的约定打一个小包，返回 (zip 路径, manifest)。

    :param mutate_manifest: 接受 manifest dict 并原地修改的钩子，用来制造不一致
    """
    files = dict(files if files is not None else _font_payloads())
    plat = assets.platform_key() or 'linux-x86_64'
    files['native/{}/{}'.format(plat, assets.native_filename())] = b'native-binary'

    manifest = {'version': '9.9.9', 'files': {}}
    for arc, data in files.items():
        manifest['files'][arc] = {
            'sha256': hashlib.sha256(data).hexdigest(),
            'size': len(data),
        }
    if mutate_manifest:
        mutate_manifest(manifest)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        if with_manifest:
            zf.writestr('manifest.json', json.dumps(manifest).encode('utf-8'))
        for arc, data in files.items():
            zf.writestr(arc, data)
    with open(path, 'wb') as f:
        f.write(buf.getvalue())
    return path, manifest


def file_url(path):
    # Windows 下必须是 file:///E:/... 三段斜杠；pathlib 的 as_uri 已处理跨平台差异
    import pathlib
    return pathlib.Path(path).as_uri()


@pytest.fixture
def mgr(tmp_path):
    plugin_dir = tmp_path / 'plugin'
    plugin_dir.mkdir()
    data_dir = tmp_path / 'data'
    m = assets.AssetManager(str(plugin_dir), str(data_dir))
    return m


# ---------------------------------------------------------------- 平台判定

def test_platform_key_supported():
    key = assets.platform_key()
    assert key is not None, '当前平台应被识别，否则下载逻辑整条走不通'
    assert key in ('win32', 'win64', 'linux-x86_64', 'linux-aarch64',
                   'linux-i686', 'linux-armv7', 'linux-loongarch64')


def test_native_filename_matches_platform():
    if sys.platform.startswith('win'):
        assert assets.native_filename() == 'zcbot_render.pyd'
    else:
        assert assets.native_filename() == 'zcbot_render.so'


# ---------------------------------------------------------------- 资源定位

def test_refresh_state_empty_when_nothing_present(mgr):
    st = mgr.refresh_state()
    assert st.ok is False
    assert st.fonts_ok is False and st.native_ok is False
    assert st.source == 'none'


def test_plugin_dir_takes_precedence_over_cache(tmp_path):
    """开发机上插件目录里的文件更新，不能被 data 里下载来的旧包盖掉。"""
    plugin_dir = tmp_path / 'plugin'
    data_dir = tmp_path / 'data'
    cache_fonts = data_dir / 'image_renderer' / 'assets' / 'fonts'
    cache_fonts.mkdir(parents=True)
    plugin_dir.mkdir()
    for f in assets.FONT_FILES:
        (cache_fonts / f).write_bytes(b'cached')
        (plugin_dir / f).write_bytes(b'plugin')

    m = assets.AssetManager(str(plugin_dir), str(data_dir))
    st = m.refresh_state()
    assert st.fonts_ok is True
    assert st.fonts_dir == str(plugin_dir), '插件目录必须优先于下载缓存'


def test_state_as_dict_has_ui_fields(mgr):
    d = mgr.refresh_state().as_dict()
    for k in ('ok', 'source', 'fonts_ok', 'native_ok', 'pending_consent',
              'download_size_mb', 'manual_hint', 'error'):
        assert k in d, 'WebUI/日志依赖这些键，缺了会 KeyError'


# ---------------------------------------------------------------- 下载与校验

def test_download_and_extract_ok(mgr, tmp_path):
    zip_path, manifest = build_zip(tmp_path / 'assets.zip')
    mgr.asset_url = file_url(zip_path)

    st = mgr.download()
    assert st.ok is True, '下载失败: {}'.format(st.error)
    assert st.version == '9.9.9'
    assert st.source == 'download'
    assert os.path.isdir(os.path.join(mgr.version_dir, 'fonts'))
    assert os.path.isfile(st.native_path)
    # 归档必须留下，便于事后核对
    assert os.path.isfile(os.path.join(mgr.data_dir, 'image_renderer', 'assets.zip'))


def test_download_skips_when_already_ok(mgr, tmp_path):
    """资源齐了就不该再产生任何网络请求。"""
    for f in assets.FONT_FILES:
        (tmp_path / 'plugin' / f).write_bytes(b'x')
    plat = assets.platform_key()
    native = tmp_path / 'plugin' / 'native' / 'bin' / plat / assets.native_filename()
    native.parent.mkdir(parents=True)
    native.write_bytes(b'bin')

    mgr.asset_url = 'http://127.0.0.1:1/should-never-be-fetched'
    st = mgr.download()
    assert st.ok is True
    assert st.source == 'plugin'


def test_corrupted_payload_rejected(mgr, tmp_path):
    """内容被改但 manifest 没改 → 必须拒绝，且不能留下半套文件。"""
    def corrupt(m):
        # 故意写一个错误的 sha256，模拟传输损坏/代理返回旧包
        first = sorted(m['files'])[0]
        m['files'][first]['sha256'] = '0' * 64

    zip_path, _ = build_zip(tmp_path / 'assets.zip', mutate_manifest=corrupt)
    mgr.asset_url = file_url(zip_path)

    st = mgr.download()
    assert st.ok is False
    assert st.error and '校验失败' in st.error, st.error
    assert not os.path.isdir(mgr.version_dir), '校验失败不能把半套资源留在正式目录'
    assert not os.path.isdir(mgr.version_dir + '.part'), '.part 临时目录必须清理'


def test_size_mismatch_rejected(mgr, tmp_path):
    def wrong_size(m):
        first = sorted(m['files'])[0]
        m['files'][first]['size'] = 999999

    zip_path, _ = build_zip(tmp_path / 'assets.zip', mutate_manifest=wrong_size)
    mgr.asset_url = file_url(zip_path)
    st = mgr.download()
    assert st.ok is False
    assert '大小不符' in (st.error or '')


def test_missing_file_in_package_rejected(mgr, tmp_path):
    """manifest 声明了但包里没有 → 拒绝（发布时漏打文件的典型症状）。"""
    def ghost(m):
        m['files']['fonts/ghost.ttf'] = {'sha256': '0' * 64, 'size': 1}

    zip_path, _ = build_zip(tmp_path / 'assets.zip', mutate_manifest=ghost)
    mgr.asset_url = file_url(zip_path)
    st = mgr.download()
    assert st.ok is False
    assert '缺文件' in (st.error or '')


def test_manifest_without_files_is_tolerated_but_logged(mgr, tmp_path):
    """老的手工包没有 files 段：仍可用，只少一层保障，不能因此判死。"""
    def strip(m):
        m.pop('files', None)

    zip_path, _ = build_zip(tmp_path / 'assets.zip', mutate_manifest=strip)
    mgr.asset_url = file_url(zip_path)
    st = mgr.download()
    assert st.ok is True, st.error


def test_path_traversal_rejected(mgr, tmp_path):
    """zip 条目带 ../ 时必须拒绝，否则可写到目标目录之外。"""
    zip_path = tmp_path / 'evil.zip'
    with zipfile.ZipFile(zip_path, 'w') as zf:
        zf.writestr('manifest.json', json.dumps({'version': '1', 'files': {}}))
        zf.writestr('../../evil.txt', b'pwned')

    mgr.asset_url = file_url(zip_path)
    st = mgr.download()
    assert st.ok is False
    assert '非法路径' in (st.error or '')
    escaped = tmp_path.parent / 'evil.txt'
    assert not escaped.exists(), '路径穿越防护失效'


def test_download_without_url_fails_loudly(mgr):
    st = mgr.download()
    assert st.ok is False
    assert 'asset_url' in (st.error or '')


def test_partial_download_is_cleaned(mgr, tmp_path):
    """URL 打不开时不能留下 .part 文件占着磁盘。"""
    mgr.asset_url = 'http://127.0.0.1:1/assets.zip'
    mgr.timeout = 2
    st = mgr.download()
    assert st.ok is False
    part = os.path.join(mgr.data_dir, 'image_renderer', 'assets.zip.part')
    assert not os.path.exists(part)


def test_platform_missing_in_package_is_reported(mgr, tmp_path):
    """包里没有当前平台的产物 → 下载成功但 ok=False，且错误里点名平台。"""
    files = {k: v for k, v in _font_payloads().items()}
    buf = io.BytesIO()
    manifest = {'version': '1', 'files': {}}
    for arc, data in files.items():
        manifest['files'][arc] = {'sha256': hashlib.sha256(data).hexdigest(),
                                  'size': len(data)}
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('manifest.json', json.dumps(manifest).encode('utf-8'))
        for arc, data in files.items():
            zf.writestr(arc, data)
    zip_path = tmp_path / 'fonts-only.zip'
    zip_path.write_bytes(buf.getvalue())

    mgr.asset_url = file_url(zip_path)
    st = mgr.download()
    assert st.ok is False
    assert 'native' in (st.error or '') or '不完整' in (st.error or '')


# ---------------------------------------------------------------- 工具函数

def test_sha256_of_detects_change(tmp_path):
    p = tmp_path / 'f.bin'
    p.write_bytes(b'abc')
    h1 = assets.sha256_of(str(p))
    p.write_bytes(b'abd')
    assert assets.sha256_of(str(p)) != h1


# ---------------------------------------------------------------- 落盘失败
#
# 下面这组是「目录不可用」的正面回答。共同前提只有一条：
# **资源层的任何失败都必须转成 st.error，绝不能抛异常**。
# 资源是可选增强，插件不该因为写不了盘就整块失效——PIL 后端本来能跑。

def _seed_cache(mgr, version='1.0.0'):
    """在缓存目录里放一套完整可用的旧资源，返回字体目录。"""
    d = os.path.join(mgr.version_dir, 'fonts')
    os.makedirs(d, exist_ok=True)
    for f in assets.FONT_FILES:
        with open(os.path.join(d, f), 'wb') as fh:
            fh.write(b'old')
    plat = assets.platform_key()
    np = os.path.join(mgr.version_dir, 'native', plat, assets.native_filename())
    os.makedirs(os.path.dirname(np), exist_ok=True)
    with open(np, 'wb') as fh:
        fh.write(b'old-native')
    # manifest 是版本号的唯一来源，必须一起种下
    with open(os.path.join(mgr.version_dir, 'manifest.json'), 'w', encoding='utf-8') as fh:
        json.dump({'version': version, 'files': {}}, fh)
    return d


def test_download_never_raises_when_dir_cannot_be_created(tmp_path):
    """data 目录的父路径被一个普通文件占住 → makedirs 必失败。

    旧实现里 makedirs 在 try 之外，这个 PermissionError/NotADirectoryError
    会一路冒到插件初始化，把本来正常的 PIL 后端一起带走。
    """
    blocker = tmp_path / 'blocked'
    blocker.write_bytes(b'i am a file, not a dir')
    m = assets.AssetManager(str(tmp_path / 'plugin'),
                            str(blocker / 'data'))
    m.asset_url = 'http://127.0.0.1:1/should-not-be-fetched'

    st = m.download()          # 这里不许抛
    assert st.ok is False
    assert st.writable is False
    assert '不可创建' in (st.error or '') or '不可写' in (st.error or '')
    assert st.manual_hint, '失败时必须给出可照做的手动放置路径'


def test_probe_writable_reports_reason(tmp_path):
    blocker = tmp_path / 'blocked2'
    blocker.write_bytes(b'x')
    m = assets.AssetManager(str(tmp_path / 'plugin'), str(blocker / 'data'))
    ok, reason, free = m.probe_writable()
    assert ok is False
    assert reason and len(reason) > 10
    assert free is None or isinstance(free, float)


def test_disk_full_detected_before_network(mgr, tmp_path, monkeypatch):
    """空间不足要在下载前判掉，而不是下到一半 ENOSPC 留下半截文件。"""
    import collections
    Usage = collections.namedtuple('usage', 'total used free')
    monkeypatch.setattr(assets.shutil, 'disk_usage',
                        lambda p: Usage(100, 99, 5 * 1048576))
    mgr.asset_url = 'http://127.0.0.1:1/should-not-be-fetched'

    st = mgr.download()
    assert st.ok is False
    assert '磁盘空间不足' in (st.error or ''), st.error
    assert st.writable is True, '空间不足不等于目录不可写，两个状态必须分开'


def test_readonly_target_degrades_to_inplace_overwrite(mgr, tmp_path, monkeypatch):
    """正式目录删不掉（只读挂载/被占用）时，不能报「下载失败」了事。

    旧实现：rmtree(ignore_errors=True) 静默吞掉删除失败 → os.replace 必然抛错
    → 用户看到「下载失败」，实际网络和包都是好的，且旧资源明明还在。
    """
    _seed_cache(mgr)
    zip_path, _ = build_zip(tmp_path / 'assets.zip')

    real_rmtree = assets.shutil.rmtree

    def fake_rmtree(path, *a, **kw):
        # 只拦「删除正式资源目录」这一下，模拟只读挂载
        if os.path.abspath(str(path)) == os.path.abspath(mgr.version_dir):
            if kw.get('ignore_errors'):
                return
            raise PermissionError('read-only file system')
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(assets.shutil, 'rmtree', fake_rmtree)
    mgr.asset_url = file_url(zip_path)

    st = mgr.download(force=True)
    assert st.ok is True, '删不掉旧目录应退化为就地覆盖，而不是整体失败: {}'.format(st.error)
    assert st.stale is False


def test_readonly_target_and_unwritable_files_keeps_old_assets(mgr, tmp_path, monkeypatch):
    """覆盖也写不进去时：必须报清楚，并且**保住**已有旧资源。

    这条是「目录突然变只读」的最坏情况。用户要的结论是两句：
      1) 这次更新失败了，原因是目录只读；
      2) 但旧的还能用，渲染没挂。
    """
    _seed_cache(mgr)
    zip_path, _ = build_zip(tmp_path / 'assets.zip')

    def deny_rmtree(path, *a, **kw):
        if os.path.abspath(str(path)) == os.path.abspath(mgr.version_dir) and not kw.get('ignore_errors'):
            raise PermissionError('read-only file system')
        return None

    def deny_copy(src, dst, *a, **kw):
        raise PermissionError('read-only file system')

    monkeypatch.setattr(assets.shutil, 'rmtree', deny_rmtree)
    monkeypatch.setattr(assets.shutil, 'copyfile', deny_copy)
    mgr.asset_url = file_url(zip_path)

    st = mgr.download(force=True)
    assert st.ok is True, '旧资源应继续可用'
    assert st.stale is True, '必须标出「在用旧版」，否则用户以为更新成功'
    assert st.error and ('只读' in st.error or '无法写入' in st.error), st.error
    # 旧文件必须原样还在
    for f in assets.FONT_FILES:
        assert os.path.isfile(os.path.join(st.fonts_dir, f))
    # 版本号必须仍是旧的：只要有文件没写进去，manifest 就不该被更新，
    # 否则会出现「显示 2.0、字体还是 1.0」的混合态。
    assert st.version == '1.0.0', '部分覆盖时版本号不能提前跳版: %r' % st.version


def test_update_failure_with_existing_assets_marks_stale(mgr, tmp_path):
    """包坏了但本地已有可用资源 → ok=True + stale=True，不是 ok=False。"""
    _seed_cache(mgr)

    def corrupt(m):
        first = sorted(m['files'])[0]
        m['files'][first]['sha256'] = '0' * 64

    zip_path, _ = build_zip(tmp_path / 'assets.zip', mutate_manifest=corrupt)
    mgr.asset_url = file_url(zip_path)

    st = mgr.download(force=True)
    assert st.ok is True
    assert st.stale is True
    assert '校验失败' in (st.error or '')
    assert not os.path.isdir(mgr.version_dir + '.part'), '.part 必须清理'


def test_no_writable_dir_does_not_block_pil_backend(mgr, tmp_path):
    """资源完全拿不到时，状态里必须能区分「没资源」和「写不了盘」。

    面板要靠 writable 决定提示语：是让用户改网络源，还是让用户改目录权限。
    """
    m = assets.AssetManager(str(tmp_path / 'p'), str(tmp_path / 'a-file' / 'd'))
    (tmp_path / 'a-file').write_bytes(b'x')
    st = m.download()
    d = st.as_dict()
    assert d['ok'] is False
    assert d['writable'] is False
    assert d['free_mb'] is None
    assert d['stale'] is False


def test_space_constant_is_realistic():
    """预留空间必须大于包本身，否则等于没检查。"""
    assert assets.SPACE_REQUIRED_MB >= 24



# ---------------------------------------------------------------- 插件层兜底
#
# 资源层的失败最终要在 _resolve_backend 里被吃掉。这里验证的是最坏情况：
# data 目录写不了 + asset_consent=auto（唯一会真去下载的组合），
# 插件初始化必须照常完成——否则一个只读挂载点就能让整个渲染插件消失。

_MAIN_PY = os.path.join(os.path.dirname(_HERE), 'core_plugins',
                        'image_renderer', 'main.py')


class _FakeCtx:
    def __init__(self, cfg, data_dir):
        self._cfg = cfg
        self._data_dir = data_dir

    def get_config(self, key, default=None):
        return self._cfg.get(key, default)

    def get_data_dir(self):
        return self._data_dir


def _load_main():
    spec = importlib.util.spec_from_file_location('zcbot_img_main_under_test', _MAIN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_backend_survives_unwritable_data_dir(tmp_path):
    main = _load_main()
    blocker = tmp_path / 'not-a-dir'
    blocker.write_bytes(b'x')
    empty_plugin = tmp_path / 'empty_plugin'
    empty_plugin.mkdir()

    main._CTX = _FakeCtx({'backend': 'pil', 'asset_consent': 'auto',
                          'asset_url': 'http://127.0.0.1:1/never',
                          'asset_timeout': 2},
                         str(blocker / 'data'))
    # 必须注入一个「插件目录里也没有资源」的实例：开发机上插件目录是有字体的，
    # 不这么做 st.ok 直接为 True，整条探测分支都不会走，测的就不是目标路径了。
    main._ASSETS = assets.AssetManager(
        str(empty_plugin), str(blocker / 'data'),
        asset_url='http://127.0.0.1:1/never', timeout=2)
    main._BACKEND_READY = False

    main._resolve_backend()      # 不许抛

    st = main.backend_status()
    assert st['assets']['writable'] is False
    assert st['assets']['ok'] is False
    assert st['backend'] == 'pil', '拿不到资源时 PIL 后端必须仍然可用'


def test_resolve_backend_handles_data_dir_exception(tmp_path):
    """get_data_dir() 自己抛异常（宿主实现有问题）时也不能连累插件。"""
    main = _load_main()

    class Boom(_FakeCtx):
        def get_data_dir(self):
            raise RuntimeError('host exploded')

    main._CTX = Boom({'backend': 'pil', 'asset_consent': 'ask'}, None)
    main._ASSETS = None
    main._BACKEND_READY = False

    main._resolve_backend()
    assert main.backend_status()['backend'] == 'pil'


def test_backend_status_keys_match_state_dict(tmp_path):
    """fallback 的键集合必须与真实状态一致，否则面板取字段会 KeyError。"""
    main = _load_main()
    real = assets.AssetState().as_dict()
    main._ASSETS = None
    fallback = main.backend_status()['assets']
    assert set(fallback) == set(real), set(real) ^ set(fallback)


# ---------------------------------------------------------------- 配置读取
#
# 核心插件的配置在 core_plugins.yaml 的 image_renderer 块，启动时被合并进
# 主配置的同名段。早期实现用 ctx.get_config() 读——那个接口查的是
# plugin_configs 表（userspace 插件专用），核心插件从没往表里写过值，
# 于是任何配置都读成 default：配置写了等于没写，而且不报错。

def test_read_cfg_uses_framework_config(monkeypatch):
    import framework.config as fcfg
    main = _load_main()
    monkeypatch.setattr(fcfg, 'get_config',
                        lambda: {'image_renderer': {'backend': 'rust',
                                                    'asset_consent': 'auto'}})
    assert main._read_cfg('backend') == 'rust'
    assert main._read_cfg('asset_consent') == 'auto'


def test_read_cfg_falls_back_to_default(monkeypatch):
    import framework.config as fcfg
    main = _load_main()
    monkeypatch.setattr(fcfg, 'get_config', lambda: {})
    assert main._read_cfg('backend', 'pil') == 'pil'
    monkeypatch.setattr(fcfg, 'get_config', lambda: {'image_renderer': {'backend': None}})
    assert main._read_cfg('backend', 'pil') == 'pil', 'None 必须回落，不能把 None 当配置值'


def test_read_cfg_ignores_ctx_database(monkeypatch):
    """ctx 存在也不该改变读取来源：DB 里放个值，配置仍以主配置为准。"""
    import framework.config as fcfg
    main = _load_main()

    class CtxWithDbValue:
        def get_config(self, key, default=None):
            return 'db-value'

    main._CTX = CtxWithDbValue()
    monkeypatch.setattr(fcfg, 'get_config',
                        lambda: {'image_renderer': {'backend': 'pil'}})
    assert main._read_cfg('backend') == 'pil'


# ---------------------------------------------------------------- 状态接口
#
# WebUI 打开时靠这个接口决定要不要显示「资源缺失」横幅。它必须是纯读的：
# 打开后台就触发 9MB 下载，是按流量计费环境下的严重事故。

def test_status_route_serves_json():
    from flask import Flask
    from framework.api import registry
    main = _load_main()
    app = Flask(__name__)
    old_app = registry._web_app
    try:
        registry._web_app = app
        registry.register_route('/api/plugins/image_renderer/status', ['GET'],
                                main._status_view, auth=False,
                                plugin='image_renderer_probe')
        r = app.test_client().get('/api/plugins/image_renderer/status')
        assert r.status_code == 200
        body = r.get_json()
        assert body['code'] == 0
        assert set(body['data']) >= {'backend', 'assets', 'asset_consent'}
        assert 'ok' in body['data']['assets']
    finally:
        registry.unregister_plugin('image_renderer_probe')
        registry._web_app = old_app


def test_status_view_does_not_build_assets_or_download(monkeypatch):
    """状态查询必须是纯读：既不建资源管理器，也不走下载分支。

    WebUI 打开时就会调它，若顺手触发下载，按流量计费的环境会当成事故。
    """
    main = _load_main()
    main._ASSETS = None
    main._BACKEND_READY = False

    called = []

    def _boom(*a, **k):
        called.append('download')
        raise AssertionError('状态查询不该下载资源')

    monkeypatch.setattr(main, '_resolve_backend', _boom)
    st = main.backend_status()
    assert called == [], '状态查询触发了后端解析（可能连带下载）'
    assert main._ASSETS is None, '状态查询不得创建资源管理器'
    assert st['assets']['ok'] is False
