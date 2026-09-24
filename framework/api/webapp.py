# -*- coding: utf-8 -*-
"""
Web 应用核心：Flask app 构建 + 共享基础设施（鉴权/CORS/限速/审计）

原 framework/apis.py 的 create_web_app 是一个 4600 行的上帝函数，把所有
鉴权/CORS/限速/辅助函数/约 180 个端点全塞在一个闭包里。当前拆分结果：

    webapp.py       本文件：Flask app + 鉴权装饰器 + CORS/限速/黑名单 + ctx 分发
                    （约 400 行）+ WebServer re-export
    webserver.py    WebServer：waitress/werkzeug 独立线程运行
    app_helpers.py  make_app_helpers() 工厂：版本 / yaml 配置 / 插件市场 / GitHub 下载
    auth.py         登录 / 登出 / 当前用户 / 改密 / 双请求认证 / 安全黑名单
    admins.py       管理员增删查
    apikeys.py      接口令牌（API Key）管理
    dashboard.py    仪表盘 + WebUI 扩展 + 群级插件开关
    plugins.py      插件生命周期 + 依赖/venv + 编排入口
    plugin_market.py  GitHub 更新 / 插件市场源 / 安装
    plugin_meta.py  README / 配置 / config_schema / 命令 / 依赖图
    commands.py     静态/动态命令 + 关键词自动回复
    users_groups.py 用户 / 群管理
    tasks.py        定时任务
    logs.py         审计日志 + 运行日志（SSE）
    config.py       系统配置 / 接入端连接 / 运行状态 / config.yaml
    db_gateway.py   内嵌数据库管理
    framework_ops.py version / restart / terminal_exec + 编排 framework_update
    framework_update.py 检查更新 / 源码更新（自 framework_ops 剥离）
    webui.py        插件 WebUI 内嵌
    files.py        文件浏览器
    stats.py        统计图表 + 环境信息
    perm_api.py     权限系统管理接口
    static_routes.py 前端静态文件

向后兼容：framework/apis.py 仍导出 create_web_app / WebServer。
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from types import SimpleNamespace

from flask import Flask, Response, jsonify, request

from framework.dual_auth import DualRequestAuthSystem
from framework.log_broker import log_broker

logger = logging.getLogger('zcbot')

# 项目根目录（framework/api/webapp.py 向上三级：api → framework → 根目录）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def create_web_app(framework) -> Flask:
    """
    创建 Flask 应用并注册所有路由
    :param framework: Framework 实例
    """
    app = Flask(__name__, static_folder=None)
    app._framework = framework
    web_cfg = framework.config.get('web', {})

    db = framework.db
    plugins_dir = framework.plugin_loader.plugins_dir

    # ---- 跨域支持（自定义网页 / 第三方面板跨源调用 API 用）----
    _cors_cfg = (
        framework.config.get('security', {}).get('cors_allowed_origins')
        or framework.config.get('web', {}).get('cors_allowed_origins')
    )

    def _resolve_cors_origin():
        origin = request.headers.get('Origin')
        if not origin:
            return None
        if _cors_cfg:
            if isinstance(_cors_cfg, str):
                allowed = [o.strip() for o in _cors_cfg.split(',') if o.strip()]
            else:
                allowed = [str(o).strip() for o in _cors_cfg]
            return origin if origin in allowed else None
        return origin

    @app.after_request
    def _cors_headers(resp):
        origin = _resolve_cors_origin()
        if not origin:
            return resp
        resp.headers['Access-Control-Allow-Origin'] = origin
        resp.headers['Access-Control-Allow-Credentials'] = 'true'
        resp.headers['Access-Control-Allow-Headers'] = \
            'Content-Type, Authorization, X-Requested-With'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        resp.headers['Vary'] = 'Origin'
        return resp

    @app.before_request
    def _cors_preflight():
        if request.method == 'OPTIONS':
            return ('', 204)

    # ---- 登录防爆破（内存限速：同一 IP 10 分钟内最多失败 5 次）----
    _login_failures = {}  # ip -> list[timestamp]
    _login_lock = threading.Lock()

    def _check_login_rate(ip: str) -> bool:
        now = time.time()
        with _login_lock:
            ts_list = [t for t in _login_failures.get(ip, []) if now - t < 600]
            return len(ts_list) < 5

    def _record_login_failure(ip: str):
        now = time.time()
        with _login_lock:
            _login_failures.setdefault(ip, []).append(now)
            _login_failures[ip] = [t for t in _login_failures[ip] if now - t < 600]

    def _clear_login_failures(ip: str):
        with _login_lock:
            _login_failures.pop(ip, None)

    # ---- 公开接口限速（/api/version 等无需认证端点防刷）----
    _pub_rate = {}            # ip -> list[timestamp]
    _pub_rate_lock = threading.Lock()
    _PUB_RATE_MAX = 30        # 每窗口最多请求数
    _PUB_RATE_WINDOW = 60     # 窗口秒数

    def _check_public_rate(ip: str) -> bool:
        """滑动窗口限速：同一 IP 每 60 秒最多 _PUB_RATE_MAX 次"""
        now = time.time()
        with _pub_rate_lock:
            ts_list = [t for t in _pub_rate.get(ip, []) if now - t < _PUB_RATE_WINDOW]
            if len(ts_list) >= _PUB_RATE_MAX:
                return False
            ts_list.append(now)
            _pub_rate[ip] = ts_list
            return True

    # ---- 双请求防破解认证系统 ----
    dual_auth = DualRequestAuthSystem(framework.config.get('security', {}), db=db)

    @app.before_request
    def _global_blacklist_guard():
        """全局黑名单拦截：被封禁的 IP 无法访问任何路由（白名单除外）"""
        ip = get_client_ip()
        if dual_auth.is_whitelisted(ip) or not dual_auth.is_blacklisted(ip):
            return None
        return jsonify({'code': 403, 'msg': '访问被拒绝'}), 403

    # ---- 刷新过快检测（同一 IP 5 秒内刷新页面 ≥2 次 → 引导到 /reset）----
    _refresh_windows = {}          # ip -> list[timestamp]（仅记录页面导航请求）
    _refresh_lock = threading.Lock()
    _REFRESH_WINDOW = 5            # 秒
    _REFRESH_LIMIT = 5             # 窗口内触发阈值

    @app.before_request
    def _detect_rapid_reload():
        """同一 IP 在 5 秒内刷新页面超过阈值时，重定向到 /reset 恢复页。"""
        if request.method != 'GET':
            return None
        path = request.path or '/'
        is_page = (path == '/' or path.endswith('.html') or path == '/reset')
        if not is_page or path.startswith('/api/'):
            return None
        ip = get_client_ip()
        now = time.time()
        with _refresh_lock:
            ts = [t for t in _refresh_windows.get(ip, []) if now - t < _REFRESH_WINDOW]
            ts.append(now)
            _refresh_windows[ip] = ts
            count = len(ts)
            if len(_refresh_windows) > 2048:
                stale = [k for k, v in _refresh_windows.items() if not v or now - v[-1] >= 600]
                for k in stale:
                    _refresh_windows.pop(k, None)
        if count >= _REFRESH_LIMIT and path != '/reset':
            from flask import redirect
            return redirect('/reset', code=302)
        return None

    # ---- 扩展点：Web 请求前后（插件可在此做鉴权增强/审计/改写响应）----
    @app.before_request
    def _hook_before_request():
        """请求前扩展点：handler 可返回 Flask Response 以短路请求。"""
        fw = getattr(app, '_framework', None)
        hooks = getattr(fw, 'hooks', None) if fw else None
        if hooks is None:
            return None
        try:
            results = hooks.trigger_sync('http.before_request', request)
            for r in (results or []):
                if hasattr(r, 'status_code'):   # 形如 Flask Response
                    return r
        except Exception as e:
            logger.warning(f"http.before_request 扩展点异常: {e}")
        return None

    @app.after_request
    def _hook_after_request(resp):
        """请求后扩展点：可读取/改写响应，必须返回响应对象。"""
        fw = getattr(app, '_framework', None)
        hooks = getattr(fw, 'hooks', None) if fw else None
        if hooks is not None:
            try:
                hooks.trigger_sync('http.after_request', request, resp)
            except Exception as e:
                logger.warning(f"http.after_request 扩展点异常: {e}")
        return resp

    # ---- 共享工具函数（版本 / yaml / 插件市场 / GitHub 下载）----
    from framework.api.app_helpers import make_app_helpers
    helpers = make_app_helpers(framework, db, plugins_dir)

    _trusted_proxies = set(framework.config.get('security', {}).get('trusted_proxies', []))

    def get_client_ip():
        remote = request.remote_addr or 'unknown'
        if _trusted_proxies and remote in _trusted_proxies:
            xff = request.headers.get('X-Forwarded-For', '')
            if xff:
                parts = [p.strip() for p in xff.split(',') if p.strip()]
                if parts:
                    return parts[0]
        return remote

    def audit_log(admin_id, admin_name, action, target_type=None, target_name=None,
                  detail=None, result='success', error_message=None):
        """记录审计日志"""
        try:
            db.execute(
                "INSERT INTO audit_logs (admin_id, admin_name, action, target_type, target_name, "
                "detail, ip_address, result, error_message) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (admin_id, admin_name, action, target_type, target_name,
                 json.dumps(detail, ensure_ascii=False) if detail else None,
                 get_client_ip(), result, error_message)
            )
        except Exception as e:
            logger.error(f"审计日志写入失败: {e}")

    def _extract_token(req):
        """从 Authorization: Bearer xxx 头或 Cookie 提取 token"""
        auth = req.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            return auth[7:]
        return req.cookies.get('zcbot_token')

    def _verify_token(token):
        """验证 token，返回 admin 字典或 None（支持用户会话 token 与接口令牌 API Key）"""
        if not token:
            return None
        if len(token) == 2048:
            row = db.query_one(
                "SELECT id, username, role, is_active, token_created_at FROM admin_users WHERE token = %s",
                (token,)
            )
            if row and row['is_active']:
                timeout = web_cfg.get('token_timeout') or web_cfg.get('session_timeout', 86400)
                if row['token_created_at']:
                    created = row['token_created_at']
                    if isinstance(created, str):
                        try:
                            created = datetime.strptime(created, '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            return None
                    expiry = created + timedelta(seconds=timeout)
                    if datetime.now() > expiry:
                        return None
                return {'id': row['id'], 'username': row['username'], 'role': row['role']}
        if len(token) >= 40:
            row = db.query_one(
                "SELECT id, name, role, is_active, expires_at, last_used_at FROM api_tokens WHERE token = %s",
                (token,)
            )
            if row and row['is_active']:
                if row['expires_at']:
                    try:
                        if time.time() > float(row['expires_at']):
                            return None
                    except (ValueError, TypeError):
                        return None
                try:
                    db.execute(
                        "UPDATE api_tokens SET last_used_at = %s WHERE id = %s",
                        (str(int(time.time())), row['id'])
                    )
                except Exception:
                    pass
                return {'id': 'api:' + str(row['id']), 'username': 'api:' + row['name'], 'role': row['role']}
        return None

    def _sync_token_cookie(resp, token: str):
        """将登录 token 同步到 HttpOnly Cookie（SameSite=Lax），供 iframe 场景兜底鉴权"""
        try:
            timeout = web_cfg.get('token_timeout') or web_cfg.get('session_timeout', 86400)
        except Exception:
            timeout = 86400
        resp.set_cookie(
            'zcbot_token', token or '',
            max_age=timeout, path='/',
            httponly=True, samesite='Lax',
            secure=bool(request.is_secure),
        )

    def _auth_wrap(fn, super_only=False):
        """鉴权装饰器工厂：校验通过后把 token 同步种到 Cookie（iframe 场景兜底）"""
        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = _extract_token(request)
            if not token:
                return jsonify({'code': 401, 'msg': '未提供认证令牌'}), 401
            admin = _verify_token(token)
            if not admin:
                return jsonify({'code': 401, 'msg': '令牌无效或已过期'}), 401
            if super_only and admin.get('role') != 'super':
                return jsonify({'code': 403, 'msg': '权限不足，需要超级管理员'}), 403
            request.admin = admin
            result = fn(*args, **kwargs)
            if isinstance(result, tuple):
                resp, status = result[0], (result[1] if len(result) > 1 else None)
            else:
                resp, status = result, None
            if isinstance(resp, Response):
                _sync_token_cookie(resp, token)
            return (resp, status) if status else resp
        return wrapper

    def require_auth(fn):
        """登录验证装饰器（基于 token）"""
        return _auth_wrap(fn, super_only=False)

    def require_super(fn):
        """超级管理员验证装饰器（基于 token）"""
        return _auth_wrap(fn, super_only=True)

    # ---- 构建共享上下文，分发给各功能域模块 ----
    ctx = SimpleNamespace(
        app=app,
        framework=framework,
        db=db,
        plugins_dir=plugins_dir,
        web_cfg=web_cfg,
        dual_auth=dual_auth,
        require_auth=require_auth,
        require_super=require_super,
        get_client_ip=get_client_ip,
        audit_log=audit_log,
        # token 相关
        _extract_token=_extract_token,
        _verify_token=_verify_token,
        _sync_token_cookie=_sync_token_cookie,
        _auth_wrap=_auth_wrap,
        # 限速 / 登录防爆破
        _check_login_rate=_check_login_rate,
        _record_login_failure=_record_login_failure,
        _clear_login_failures=_clear_login_failures,
        _check_public_rate=_check_public_rate,
        # 通用工具 / yaml / 插件市场 / GitHub（app_helpers 工厂）
        **helpers,
        # 日志经纪
        log_broker=log_broker,
    )

    # ---- 按功能域注册路由（各模块 import 后调用 register(ctx)）----
    from framework.api import (
        auth, admins, apikeys, dashboard, plugins, commands, users_groups, tasks,
        logs, config, db_gateway, framework_ops, webui, files, stats, perm_api,
        static_routes,
    )
    auth.register(ctx)
    admins.register(ctx)
    apikeys.register(ctx)
    dashboard.register(ctx)
    plugins.register(ctx)
    commands.register(ctx)
    users_groups.register(ctx)
    tasks.register(ctx)
    logs.register(ctx)
    config.register(ctx)
    db_gateway.register(ctx)
    framework_ops.register(ctx)
    webui.register(ctx)
    files.register(ctx)
    stats.register(ctx)
    perm_api.register(ctx)
    static_routes.register(ctx)

    # ---- 插件自定义 API 路由挂载（framework.api.registry）----
    from framework.api import registry as _api_registry
    _api_registry.set_web_context(app, require_auth)
    _api_registry.mount_all(app, require_auth)

    return app


# 向后兼容 re-export（WebServer 本体已拆至 webserver.py）
from framework.api.webserver import WebServer  # noqa: F401,E402
