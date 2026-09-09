"""
WebUI 管理后台（官方插件）
提供 Web 管理面板和 REST API，支持开关控制
"""
import logging
import os
import sys

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "WebUI 管理后台",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "Web 管理面板 + REST API",
    "priority": 0,
    "official": True,
}

_web_server = None


def register(ctx):
    """注册 WebUI 为官方插件"""
    global _web_server
    fw = ctx._framework

    # 检查配置是否启用
    web_cfg = fw.config.get('web', {})
    if web_cfg.get('enabled') is False:
        ctx.log("WebUI 已禁用 (web.enabled: false)")
        # 注册空服务，防止其他插件调用时报错
        fw.services.register('web_server', None)
        return

    # 延迟导入，只在启用时加载 Flask 等重依赖
    from framework.apis import create_web_app
    from framework.apis import WebServer

    _web_server = WebServer(fw)
    fw.services.register('web_server', _web_server)

    # 启动 Web 服务
    _web_server.start()

    host = web_cfg.get('host', '127.0.0.1')
    port = web_cfg.get('port', 8080)
    ctx.log(f"WebUI 已启动: http://{host}:{port}")


def unregister():
    """卸载时停止"""
    global _web_server
    if _web_server:
        _web_server.stop()
        _web_server = None
