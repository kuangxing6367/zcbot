# -*- coding: utf-8 -*-
"""
插件 WebUI 内嵌：列表 + 入口页 + 静态资源 + 侧边栏菜单
"""
import logging
import os

from flask import jsonify, send_from_directory
import yaml

logger = logging.getLogger('zcbot')


def _read_web_official_sidebar(framework, project_root) -> bool:
    """读取 config.yaml 中 web.official_sidebar（直读文件，保存后即时生效，无需重启）"""
    path = getattr(framework, 'config_path', None) or os.path.join(project_root(), 'config.yaml')
    try:
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                doc = yaml.safe_load(f) or {}
            return bool((doc.get('web') or {}).get('official_sidebar', True))
    except Exception:
        pass
    return True


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    require_auth = ctx.require_auth
    project_root = ctx._project_root

    @app.route('/api/menu', methods=['GET'])
    @require_auth
    def get_menu():
        """前端侧边栏菜单：官方侧边栏开关 + 插件注册的侧边栏项/聚合项"""
        official = _read_web_official_sidebar(framework, project_root)
        plugins = framework.plugin_loader.get_plugin_webuis()
        return jsonify({'code': 0, 'data': {'official_sidebar': official, 'plugins': plugins}})

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
        """提供插件 WebUI 的静态资源文件（JS/CSS/图片等）"""
        web_dir = framework.plugin_loader.get_plugin_webui_path(plugin_name)
        if not web_dir:
            return jsonify({'code': 404, 'msg': 'WebUI 目录不存在'}), 404
        file_path = os.path.join(web_dir, filename)
        if not os.path.isfile(file_path):
            return jsonify({'code': 404, 'msg': '文件不存在'}), 404
        return send_from_directory(web_dir, filename)
