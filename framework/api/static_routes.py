"""
前端静态文件：css / js / img / HTML 页面 / 根路径与恢复页

两个体积相关的约定（v1.22.x 加入，起因是首屏 1.71 MB 全量重下）：

1. **按需 gzip**：JS/CSS/SVG 这类文本产物压缩率能到 70%（element-plus 单个
   1.03 MB → 313 KB），而 Flask 的 send_from_directory 不做任何压缩。压缩结果按
   (路径, mtime, 大小) 缓存在进程内，同一个文件只压一次。
2. **hash 产物长缓存**：`app.D4M7--WT.js` 这种带内容 hash 的文件名，内容一变文件名
   就变，可以放心 `immutable` 缓存一年；`theme-init.js`、`zc-decoy.js` 这类散装脚本
   没有 hash，只能短缓存；`index.html` 必须 `no-cache`，否则发版后用户还在跑旧入口。
"""
from __future__ import annotations

import gzip
import logging
import mimetypes
import os
import re
import threading

from flask import Response, jsonify, redirect, request
from werkzeug.security import safe_join

logger = logging.getLogger('zcbot')

# 带内容 hash 的产物名：app.D4M7--WT.js / element-plus.C4PxGsnR.js / app.DczWWvzl.css
_HASHED_NAME = re.compile(r'^[A-Za-z0-9_.+-]+[.-][A-Za-z0-9_-]{8,}\.(?:js|css)$')

# 值得压的文本类型。png/jpg/woff2 已是压缩格式，再压一遍纯耗 CPU 还可能变大。
_GZIP_EXT = frozenset({'.js', '.css', '.svg', '.json', '.txt', '.html', '.map', '.ico'})
# 低于这个大小不压：gzip 头 + 字典开销会让小文件反而变大
_GZIP_MIN_BYTES = 1024

_TTL_HASHED = 31536000   # 一年（immutable）
_TTL_PLAIN = 300         # 散装脚本：5 分钟，够发版更新
_TTL_HTML = 0            # no-cache：每次都校验

# 压缩结果缓存：(abspath, mtime_ns, size) -> gzip 后的字节
_gzip_cache: dict[tuple[str, int, int], bytes] = {}
_gzip_lock = threading.Lock()
_gzip_cache_bytes = 0
_GZIP_CACHE_LIMIT = 48 * 1024 * 1024
_GZIP_CACHE_ENTRIES = 400


def _accepts_gzip() -> bool:
    return 'gzip' in (request.headers.get('Accept-Encoding') or '').lower()


def _gzip_bytes(path: str, stat: os.stat_result) -> bytes | None:
    """取（或生成）文件的 gzip 内容。超过缓存上限就只压不存，保证内存不失控。"""
    global _gzip_cache_bytes
    key = (path, stat.st_mtime_ns, stat.st_size)
    with _gzip_lock:
        hit = _gzip_cache.get(key)
    if hit is not None:
        return hit

    try:
        with open(path, 'rb') as f:
            raw = f.read()
        packed = gzip.compress(raw, compresslevel=6)
    except OSError as exc:
        logger.debug('[static] 读取待压缩文件失败 %s: %s', path, exc)
        return None

    # 压不小就别压（小文件、已压缩内容），省得白占带宽还多一层解码
    if len(packed) >= len(raw):
        return None

    with _gzip_lock:
        if _gzip_cache_bytes + len(packed) <= _GZIP_CACHE_LIMIT:
            _gzip_cache[key] = packed
            _gzip_cache_bytes += len(packed)
            need_evict = len(_gzip_cache) > _GZIP_CACHE_ENTRIES
        else:
            need_evict = False
    if need_evict:
        _evict_stale_gzip_cache()
    return packed


def _evict_stale_gzip_cache():
    """剔除文件已删除 / 已被新版本替换的缓存键。

    发布新产物时文件名会变（带 hash），旧条目永远命中不了；长跑进程里不清理
    就是纯内存泄漏。调用点在缓存条目超阈值时，不在每个请求上做全量 stat。
    """
    global _gzip_cache_bytes
    with _gzip_lock:
        keys = list(_gzip_cache)
    stale = []
    for key in keys:
        path, mtime_ns, size = key
        try:
            st = os.stat(path)
        except OSError:
            stale.append(key)
            continue
        if st.st_mtime_ns != mtime_ns or st.st_size != size:
            stale.append(key)
    if not stale:
        return
    with _gzip_lock:
        for key in stale:
            packed = _gzip_cache.pop(key, None)
            if packed is not None:
                _gzip_cache_bytes -= len(packed)
        if _gzip_cache_bytes < 0:
            _gzip_cache_bytes = 0


def _serve_file(directory: str, filename: str, *, cache_ttl: int | None = None):
    """安全地返回静态文件，带条件请求、长缓存与按需 gzip。

    走 safe_join 而不是 os.path.join：filename 来自 URL，直接拼接等于把
    `../../config.yaml` 这种路径穿越原样交给 open()。
    """
    path = safe_join(os.path.abspath(directory), filename)
    if not path or not os.path.isfile(path):
        return Response('Not Found', status=404, mimetype='text/plain')

    try:
        st = os.stat(path)
    except OSError:
        return Response('Not Found', status=404, mimetype='text/plain')

    # ETag 用 mtime+size：产物发布后文件名带 hash，同一路径内容变了必须让浏览器知道
    # Werkzeug 的 ETags.contains() 只认不带引号的裸值，响应头里才加引号
    etag_value = f'{st.st_mtime_ns:x}-{st.st_size:x}'
    etag = f'"{etag_value}"'
    if request.if_none_match.contains(etag_value):
        resp = Response(status=304)
        resp.headers['ETag'] = etag
        return resp

    ext = os.path.splitext(path)[1].lower()
    mime = mimetypes.guess_type(path)[0] or 'application/octet-stream'

    if cache_ttl is None:
        cache_ttl = _TTL_HASHED if _HASHED_NAME.match(os.path.basename(path)) else _TTL_PLAIN

    body = None
    encoding = None
    if ext in _GZIP_EXT and st.st_size >= _GZIP_MIN_BYTES and _accepts_gzip():
        body = _gzip_bytes(path, st)
        if body is not None:
            encoding = 'gzip'

    if body is None:
        # 未压缩：交给 Flask 以文件流形式返回，大文件不占内存
        from flask import send_file
        resp = send_file(path, mimetype=mime, etag=False, conditional=False,
                         last_modified=st.st_mtime)
        resp.headers['ETag'] = etag
    else:
        resp = Response(body, mimetype=mime)
        resp.headers['Content-Encoding'] = 'gzip'
        resp.headers['ETag'] = etag

    if cache_ttl > 0:
        resp.headers['Cache-Control'] = (
            f'public, max-age={cache_ttl}, immutable' if cache_ttl == _TTL_HASHED
            else f'public, max-age={cache_ttl}')
    else:
        resp.headers['Cache-Control'] = 'no-cache'
    if ext in _GZIP_EXT:
        # 同一个 URL 两种编码，必须声明 Vary，否则 CDN/代理会拿 gzip 版喂给不支持解压的客户端
        resp.headers['Vary'] = 'Accept-Encoding'
    return resp


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    _project_root = ctx._project_root

    def _web_root_dir():
        """WebUI 构建产物目录：统一走 framework.paths（支持内核内嵌插件布局）"""
        from framework.paths import webui_web_dir
        return webui_web_dir()

    def _override_entry_url() -> str:
        """
        返回接管前端的插件入口 URL。
        插件接管时根路径 / redirect 到该 URL，由插件路由服务其模板网页。
        无接管返回 None。
        """
        name = framework.plugin_loader.get_override_webui()
        if not name:
            return None
        return f'/{name}/'

    @app.route('/css/<path:filename>')
    def serve_css(filename):
        return _serve_file(os.path.join(_web_root_dir(), 'css'), filename)

    @app.route('/js/<path:filename>')
    def serve_js(filename):
        # local.debug.js 是「明文调试凭据」受控诱饵，绝不能经静态路由直出。
        # 蜜罐启用时精确规则 /js/local.debug.js 优先命中陷阱（返回假凭据并拉黑）；
        # 蜜罐禁用时这里必须挡住，否则未登录访客能直接读到磁盘上的文件原文。
        if os.path.basename(filename) == 'local.debug.js':
            return Response('Not Found', status=404, mimetype='text/plain')
        return _serve_file(os.path.join(_web_root_dir(), 'js'), filename)

    @app.route('/img/<path:filename>')
    def serve_img(filename):
        return _serve_file(os.path.join(_web_root_dir(), 'img'), filename)

    @app.route('/favicon.ico')
    def serve_favicon():
        """站点图标（根路径，非 /img 下）"""
        return _serve_file(_web_root_dir(), 'favicon.ico', cache_ttl=_TTL_PLAIN)

    @app.route('/robots.txt')
    def serve_robots():
        """爬虫协议：管理后台明确禁止收录"""
        resp = Response('User-agent: *\nDisallow: /\n', mimetype='text/plain')
        resp.headers['Cache-Control'] = 'public, max-age=86400'
        return resp

    @app.route('/<page>.html')
    def serve_page(page):
        """提供 HTML 页面"""
        web_static = _web_root_dir()
        html_file = os.path.join(web_static, f'{page}.html')
        if os.path.isfile(html_file):
            # HTML 是入口，必须每次校验，否则发版后用户还在跑旧引用
            return _serve_file(web_static, f'{page}.html', cache_ttl=_TTL_HTML)
        return jsonify({'code': 404, 'msg': '页面不存在'}), 404

    @app.route('/')
    def serve_index():
        """根路径：若插件接管了前端则 redirect 到插件入口，否则返回框架默认 index.html

        端口隔离模式（web.api_port 与 web.port 不同）下，真实 API 不在本端口提供，
        需向首页注入 /webui-config.js，让前端知道去哪个端口调 API。
        """
        entry = _override_entry_url()
        if entry:
            return redirect(entry, code=302)

        web_static = _web_root_dir()
        if not getattr(ctx, 'api_split', False):
            return _serve_file(web_static, 'index.html', cache_ttl=_TTL_HTML)

        # 需要注入配置脚本：读模板后在 </head> 前插入同源外部脚本（CSP 允许）
        try:
            with open(os.path.join(web_static, 'index.html'), 'r', encoding='utf-8') as f:
                html = f.read()
        except Exception as _exc:
            logger.debug('[serve_index] 捕获异常后按降级路径继续: %s', _exc)
            return _serve_file(web_static, 'index.html', cache_ttl=_TTL_HTML)
        if '__ZC_API_BASE__' not in html and '<script type="module"' in html:
            html = html.replace('<script type="module"',
                                '<script src="/webui-config.js"></script>\n  <script type="module"', 1)
        resp = Response(html, mimetype='text/html')
        # 注入结果随端口配置变化，不能被缓存复用
        resp.headers['Cache-Control'] = 'no-cache'
        return resp

    @app.route('/reset')
    def serve_reset():
        """前端恢复页：若插件接管了前端则返回其 reset.html；
        无接管页时回退框架默认 core_plugins/webui/web/reset.html（不存在则返回默认 index.html）。"""
        web_static = _web_root_dir()
        reset_file = os.path.join(web_static, 'reset.html')
        if os.path.isfile(reset_file):
            return _serve_file(web_static, 'reset.html', cache_ttl=_TTL_HTML)
        return _serve_file(web_static, 'index.html', cache_ttl=_TTL_HTML)
