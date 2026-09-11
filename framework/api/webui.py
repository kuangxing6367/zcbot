# -*- coding: utf-8 -*-
"""
插件 WebUI 内嵌：列表 + 入口页 + 静态资源 + 侧边栏菜单
"""
import logging
import os

from flask import jsonify, send_from_directory
import yaml

logger = logging.getLogger('zcbot')


def _read_web_section(framework, project_root) -> dict:
    """直读 config.yaml 的 web 段（保存后即时生效，无需重启）"""
    path = getattr(framework, 'config_path', None) or os.path.join(project_root(), 'config.yaml')
    try:
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                doc = yaml.safe_load(f) or {}
            web = doc.get('web')
            return web if isinstance(web, dict) else {}
    except Exception:
        pass
    return {}


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    require_auth = ctx.require_auth
    project_root = ctx._project_root

    @app.route('/api/menu', methods=['GET'])
    @require_auth
    def get_menu():
        """前端侧边栏菜单：官方侧边栏开关 / 自定义（顺序+隐藏）+ 插件注册项"""
        web = _read_web_section(framework, project_root)
        sidebar = web.get('sidebar')
        if not isinstance(sidebar, dict):
            sidebar = {}
        return jsonify({'code': 0, 'data': {
            'official_sidebar': bool(web.get('official_sidebar', True)),
            'sidebar': {
                'order': sidebar.get('order') or [],
                'hidden': sidebar.get('hidden') or [],
            },
            'plugins': framework.plugin_loader.get_plugin_webuis(),
        }})

    @app.route('/api/plugin_webuis', methods=['GET'])
    @require_auth
    def list_plugin_webuis():
        """获取所有已注册的插件 WebUI 列表"""
        webuis = framework.plugin_loader.get_plugin_webuis()
        return jsonify({'code': 0, 'data': webuis})

    @app.route('/api/plugin_webui/<plugin_name>', methods=['GET'])
    @require_auth
    def serve_plugin_webui(plugin_name):
        """
        获取插件 WebUI 入口页面
        查询参数 ?entry=xxx.html 可指定入口文件（默认 index.html）
        """
        from flask import request as flask_request
        entry = flask_request.args.get('entry', 'index.html')
        web_dir = framework.plugin_loader.get_plugin_webui_path(plugin_name)
        if not web_dir:
            return jsonify({'code': 404, 'msg': f'插件 {plugin_name} 未提供 WebUI'}), 404
        entry_path = os.path.join(web_dir, entry)
        if not os.path.isfile(entry_path):
            return jsonify({'code': 404, 'msg': f'入口文件 {entry} 不存在'}), 404
        return send_from_directory(web_dir, entry)

    @app.route('/api/plugin_webui/<plugin_name>/assets/<path:filename>', methods=['GET'])
    @require_auth
    def serve_plugin_webui_assets(plugin_name, filename):
        """提供插件 WebUI 的静态资源文件（JS/CSS/图片等）

        路由中 /assets/ 为字面段，故 filename 仅含 assets 之后的部分
        （如 mf.js），需拼到 web_dir/assets 下。
        """
        web_dir = framework.plugin_loader.get_plugin_webui_path(plugin_name)
        if not web_dir:
            return jsonify({'code': 404, 'msg': 'WebUI 目录不存在'}), 404
        assets_dir = os.path.join(web_dir, 'assets')
        file_path = os.path.join(assets_dir, filename)
        if not os.path.isfile(file_path):
            return jsonify({'code': 404, 'msg': '文件不存在'}), 404
        return send_from_directory(assets_dir, filename)
