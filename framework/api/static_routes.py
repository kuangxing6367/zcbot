# -*- coding: utf-8 -*-
"""
前端静态文件：css / js / img / HTML 页面 / 根路径与恢复页
"""
import logging
import os

from flask import jsonify, redirect, send_from_directory

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    _project_root = ctx._project_root

    def _web_root_dir():
        """前端根目录（框架默认 web/ 目录）"""
        return os.path.join(_project_root(), 'web')

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
        return send_from_directory(os.path.join(_web_root_dir(), 'css'), filename)

    @app.route('/js/<path:filename>')
    def serve_js(filename):
        return send_from_directory(os.path.join(_web_root_dir(), 'js'), filename)

    @app.route('/img/<path:filename>')
    def serve_img(filename):
        return send_from_directory(os.path.join(_web_root_dir(), 'img'), filename)

    @app.route('/<page>.html')
    def serve_page(page):
        """提供 HTML 页面"""
        web_static = _web_root_dir()
        html_file = os.path.join(web_static, f'{page}.html')
        if os.path.isfile(html_file):
            return send_from_directory(web_static, f'{page}.html')
        return jsonify({'code': 404, 'msg': '页面不存在'}), 404

    @app.route('/')
    def serve_index():
        """根路径：若插件接管了前端则 redirect 到插件入口，否则返回框架默认 index.html"""
        entry = _override_entry_url()
        if entry:
            return redirect(entry, code=302)
        return send_from_directory(_web_root_dir(), 'index.html')

    @app.route('/reset')
    def serve_reset():
        """前端恢复页：若插件接管了前端则返回其 reset.html；
        无接管页时回退框架默认 web/reset.html（不存在则返回默认 index.html）。"""
        web_static = _web_root_dir()
        reset_file = os.path.join(web_static, 'reset.html')
        if os.path.isfile(reset_file):
            return send_from_directory(web_static, 'reset.html')
        return send_from_directory(web_static, 'index.html')
