# -*- coding: utf-8 -*-
"""
Web UI 服务器（自 framework/api/webapp.py 剥离）
在独立线程中运行 Flask 应用：waitress（HTTP）/ werkzeug（HTTPS）。
"""
import logging
import threading

logger = logging.getLogger('zcbot')


class WebServer:
    """Web UI 服务器，在独立线程中运行"""

    def __init__(self, framework):
        from framework.api.webapp import create_web_app
        self.framework = framework
        self.app = create_web_app(framework)
        web_cfg = framework.config.get('web', {})
        self.host = web_cfg.get('host', '0.0.0.0')
        self.port = web_cfg.get('port', 8080)
        # SSL：与 OneBot WSS 共用 config['ssl']；证书路径支持相对/绝对。
        # waitress 不支持 TLS，故启用 SSL 时改用 werkzeug（支持 ssl_context）。
        self.ssl_context = None
        try:
            self.ssl_context = framework.build_ssl_context()
        except Exception as e:
            logger.error(f"SSL 配置无效，Web 后台回退为 http: {e}")
        self.scheme = 'https' if self.ssl_context else 'http'
        self._thread = None
        self._server = None
        self._running = False

    def start(self):
        """启动 Web 服务器"""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="web-server")
        self._thread.start()
        logger.info(f"Web UI 已启动: {self.scheme}://{self.host}:{self.port}")

    def _run(self):
        """运行 Web 服务器（保存 server 句柄，供 stop() 真正停止并释放端口）"""
        try:
            if self.ssl_context is not None:
                # HTTPS：waitress 不支持 TLS，使用 werkzeug 的 ssl_context
                from werkzeug.serving import make_server
                self._server = make_server(self.host, self.port, self.app,
                                           threaded=True, ssl_context=self.ssl_context)
                self._server.serve_forever()
                return
            try:
                from waitress.server import create_server as waitress_create_server
                self._server = waitress_create_server(
                    self.app, host=self.host, port=self.port, threads=8)
                self._server.run()
            except ImportError:
                from werkzeug.serving import make_server
                self._server = make_server(self.host, self.port, self.app)
                self._server.serve_forever()
        except Exception as e:
            self._server = None
            if getattr(e, 'errno', None) == 98 or 'Address already in use' in str(e):
                logger.error(
                    f"Web UI 启动失败: 端口 {self.port} 已被占用。"
                    f"可能残留了旧实例，请先停止旧进程（如: ss -tlnp | grep {self.port}）")
            else:
                logger.error(f"Web UI 异常: {e}")

    def stop(self):
        """停止 Web 服务器（真正关闭监听，避免优雅停机后端口残留）"""
        self._running = False
        srv = self._server
        self._server = None
        if srv is not None:
            try:
                if hasattr(srv, 'close'):
                    srv.close()      # waitress WSGIServer
                elif hasattr(srv, 'shutdown'):
                    srv.shutdown()   # werkzeug
            except Exception as e:
                logger.warning(f"Web 服务器关闭异常: {e}")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        logger.info("Web UI 已停止")
