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
import secrets
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from types import SimpleNamespace

from flask import Flask, Response, jsonify, request

from framework.dual_auth import DualRequestAuthSystem
from framework.log_broker import log_broker
from framework.api.honeypot import tarpit as _tarpit_sleep

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

    # ---- 解析安全等级：一个 security.level 档位决定蜜罐 / 响应头 / 拉黑等全部默认值 ----
    from framework.api.security_levels import resolve as _resolve_security
    _sec = _resolve_security(framework.config.get('security'))
    _hdr_cfg = _sec['headers']
    _hp_cfg = _sec['honeypot']
    _hc_cfg = _sec['human_check']
    _SILENT_AUTH = bool(_sec.get('silent_auth', True))
    logger.info(f"[安全] 等级={_sec['level']}({_sec['level_label']}) "
                f"响应头={_hdr_cfg.get('enabled')} 蜜罐={_hp_cfg.get('enabled')} "
                f"诱饵集={_hp_cfg.get('decoy_set')} 人机验证={_hc_cfg.get('enabled')} "
                f"认证静默={_SILENT_AUTH}")

    # ---- 端口隔离：WebUI 与 API 可各自独立监听 ----
    # web.api_port 非空且 != web.port 时启用：
    #   WebUI 端口 → 只提供前端静态文件 + 蜜罐诱饵，不暴露任何真实 API
    #   API   端口 → 只提供 /api/*
    # 两者在同一进程内，共享黑名单 / 票据 / 会话状态（天然同步）。
    _web_port = str(int(web_cfg.get('port', 8080) or 8080))
    _raw_api_port = int(web_cfg.get('api_port', 0) or 0)
    _api_port = str(_raw_api_port) if _raw_api_port else _web_port
    SPLIT_PORTS = _api_port != _web_port
    if SPLIT_PORTS:
        logger.info(f"[端口隔离] 已启用：WebUI={_web_port}（无真实 API）  API={_api_port}")

    # ---- 纯 API 模式：ui_enabled=false 时只起 API 监听，不提供 WebUI 前端 ----
    _ui_raw = web_cfg.get('ui_enabled', True)
    if isinstance(_ui_raw, bool):
        _UI_ENABLED = _ui_raw
    elif isinstance(_ui_raw, str):
        _UI_ENABLED = str(_ui_raw).strip().lower() in ('1', 'true', 'yes', 'y', 'on')
    else:
        _UI_ENABLED = bool(_ui_raw)
    _API_ONLY = not _UI_ENABLED
    if _API_ONLY:
        logger.info(f"[纯API模式] ui_enabled=false：本进程仅 API 端口={_api_port} 提供真实 /api/*"
                    f"（不启动 WebUI 前端，非 /api/* 一律 404）")

    @app.route('/webui-config.js')
    def _webui_config():
        """前端配置：告诉页面真实 API 在哪（端口隔离时用，同源外部脚本，CSP 允许）"""
        if not SPLIT_PORTS:
            base = ''
        else:
            host = (request.host or '').split(':')[0]
            scheme = 'https' if request.is_secure else 'http'
            base = f'{scheme}://{host}:{_api_port}'
        resp = Response(f'window.__ZC_API_BASE__ = "{base}";\n',
                        mimetype='application/javascript')
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    @app.before_request
    def _port_split_guard():
        """按监听端口隔离职责：WebUI 端口无真实 API，API 端口无静态页面；
        纯 API 模式下本进程只认 /api/*，其余一律 404（不提供前端、也不挂诱饵）。"""
        path = request.path or ''
        # 纯 API 模式：本进程只监听 API，非 /api/* 一律 404
        if _API_ONLY:
            if not path.startswith('/api/'):
                return jsonify({'code': 404, 'msg': 'Not Found'}), 404
            return None
        if not SPLIT_PORTS:
            return None
        srv = str(request.environ.get('SERVER_PORT') or '')

        # API 端口：只服务 /api/*
        if srv == _api_port:
            if not path.startswith('/api/'):
                return jsonify({'code': 404, 'msg': 'Not Found'}), 404
            return None

        # WebUI 端口：不暴露真实 API，一律交给诱饵
        if srv == _web_port and path.startswith('/api/'):
            endpoint = getattr(request.url_rule, 'endpoint', '') or ''
            if not endpoint.startswith('_honeypot'):
                from framework.api import honeypot as _hpm
                _hpm.record_hit(path, 'WebUI 端口探测真实 API')
                return _hpm.body_generic_decoy()
        return None

    # ---- 跨域支持（自定义网页 / 第三方面板跨源调用 API 用）----
    _cors_cfg = (
        framework.config.get('security', {}).get('cors_allowed_origins')
        or framework.config.get('web', {}).get('cors_allowed_origins')
    )

    def _resolve_cors_origin():
        origin = request.headers.get('Origin')
        if not origin:
            return None
        # 端口隔离：同一主机的 WebUI 端口始终允许调用 API 端口
        if SPLIT_PORTS:
            try:
                scheme = 'https' if request.is_secure else 'http'
                if origin.startswith(scheme + '://'):
                    ohost = origin[len(scheme) + 3:].split(':')[0]
                    if ohost == (request.host or '').split(':')[0]:
                        return origin
            except Exception:
                pass
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

    # ---- 安全响应头 + 技术栈指纹隐藏 ----
    # 生效值来自 security.level 档位解析（见上方 _hdr_cfg）
    _HEADERS_ENABLED = bool(_hdr_cfg.get('enabled', True))
    _SERVER_IDENT = str(_hdr_cfg.get('server', '') or '')

    def _build_csp(relaxed: bool = False) -> str:
        """构建 CSP。relaxed=True 用于插件自建页面（含内联脚本），放宽 script-src。"""
        custom = _hdr_cfg.get('csp')
        if isinstance(custom, str) and custom.strip():
            return custom.strip()
        script_src = "'self' 'unsafe-inline'" if relaxed else "'self'"
        return (
            "default-src 'self'; "
            f"script-src {script_src}; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "frame-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'self'"
        )

    @app.after_request
    def _security_headers(resp):
        if not _HEADERS_ENABLED:
            return resp
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        resp.headers.setdefault('Referrer-Policy', 'same-origin')
        resp.headers.setdefault(
            'Permissions-Policy',
            'geolocation=(), microphone=(), camera=(), payment=(), usb=()')
        resp.headers.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
        resp.headers.setdefault('X-Permitted-Cross-Domain-Policies', 'none')
        resp.headers.setdefault('X-Robots-Tag', 'noindex, nofollow')
        # 插件自建页面允许内联脚本，其余走严格 CSP
        relaxed = (request.path or '').startswith('/api/plugin_webui/')
        resp.headers.setdefault('Content-Security-Policy', _build_csp(relaxed))
        # 指纹隐藏：删除/覆盖 Server 与 X-Powered-By
        if _SERVER_IDENT:
            resp.headers['Server'] = _SERVER_IDENT
        else:
            resp.headers.pop('Server', None)
        resp.headers.pop('X-Powered-By', None)
        return resp

    # ---- 统一错误页（不泄露框架指纹与堆栈）----
    @app.errorhandler(404)
    def _err_404(e):
        return jsonify({'code': 404, 'msg': 'Not Found'}), 404

    @app.errorhandler(405)
    def _err_405(e):
        return jsonify({'code': 405, 'msg': 'Method Not Allowed'}), 405

    @app.errorhandler(500)
    def _err_500(e):
        logger.exception("Web 请求内部错误")
        return jsonify({'code': 500, 'msg': 'Internal Server Error'}), 500

    @app.errorhandler(Exception)
    def _err_any(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e
        logger.exception("Web 未捕获异常")
        return jsonify({'code': 500, 'msg': 'Internal Server Error'}), 500

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

    # ---- JS 人机验证（工作量证明）----
    # 客户端必须先执行 JS 求解 challenge，换取通行票据（请求头 X-ZC-Pass），
    # 之后所有 API 请求携带该票据。纯脚本不执行 JS → 拿不到票据 → 被静默丢弃。
    from framework.api import human_check as _hc
    _HC_ENABLED = bool(_hc_cfg.get('enabled', True))
    _HC_APPLY_TO = str(_hc_cfg.get('apply_to', 'all') or 'all').lower()
    _HC_BYPASS_WL = bool(_hc_cfg.get('bypass_whitelist', False))
    _HC_DIFFICULTY = int(_hc_cfg.get('difficulty', 4) or 4)
    _HC_CHALLENGE_TTL = int(_hc_cfg.get('challenge_ttl', 120) or 120)
    _HC_TICKET_TTL = int(_hc_cfg.get('ticket_ttl', 1800) or 1800)

    @app.route('/api/challenge', methods=['GET'])
    def _issue_challenge():
        """下发人机验证 challenge（无需票据）"""
        return jsonify({'code': 0,
                        'data': _hc.issue_challenge(_HC_DIFFICULTY, _HC_CHALLENGE_TTL)})

    @app.route('/api/challenge/verify', methods=['POST'])
    def _verify_challenge():
        """复核工作量证明并签发通行票据"""
        data = request.get_json(silent=True) or {}
        ticket = _hc.verify(data.get('challenge'), data.get('nonce'), _HC_TICKET_TTL)
        if not ticket:
            return jsonify({'code': 400, 'msg': '验证失败'}), 400
        return jsonify({'code': 0, 'data': {'ticket': ticket, 'ttl': _HC_TICKET_TTL}})

    @app.before_request
    def _human_check_guard():
        """未携带有效通行票据的 API 请求 → 静默丢弃（诱饵端点与插件路由除外）"""
        if not _HC_ENABLED or _HC_APPLY_TO == 'none':
            return None
        path = request.path or ''
        if not path.startswith('/api/'):
            return None
        # 挑战接口本身、以及插件自建页面（iframe 无法携带票据）免检
        if path.startswith('/api/challenge') or path.startswith('/api/plugin_webui'):
            return None
        # 诱饵端点与插件自注册路由不受拦截：诱饵要让扫描器咬到才有价值，
        # 插件页面（iframe）也无法携带票据。
        endpoint = getattr(request.url_rule, 'endpoint', '') or ''
        if endpoint.startswith('_honeypot') or endpoint.startswith('_zcbot_api_'):
            return None
        if _HC_APPLY_TO == 'login' and path != '/api/login':
            return None
        ip = get_client_ip()
        if _HC_BYPASS_WL and (dual_auth.is_whitelisted(ip) or dual_auth.is_internal(ip)):
            return None
        if _hc.is_valid_ticket(request.headers.get('X-ZC-Pass')):
            return None
        logger.info(f"[人机验证] 未通过，静默丢弃: {request.method} {path} ← {ip}")
        return _silent_drop()

    # ---- 认证失败防护：tarpit 延迟 + 中性响应 + 阈值自动拉黑 ----
    # 目标：无效/缺失 token 不再返回「令牌无效或已过期」这类可区分信息，
    #      改为统一的 Unauthorized + 随机延迟 + 记风险；同一 IP 反复失败则拉黑。
    # 生效值来自 security.level 档位解析（见上方 _hp_cfg）
    _AUTH_FAIL_LIMIT = int(_hp_cfg.get('auth_fail_blacklist', 20) or 0)
    _AUTH_FAIL_WINDOW = int(_hp_cfg.get('auth_fail_window', 300) or 300)
    _AUTH_TARPIT_MS = int(_hp_cfg.get('tarpit_auth_ms', 800) or 0)
    _AUTH_DECOY = bool(_hp_cfg.get('invalid_token_decoy', False))
    _AUTH_BLACKLIST_TTL = int(_hp_cfg.get('blacklist_ttl', 3600) or 0)
    _auth_failures = {}      # ip -> list[timestamp]
    _auth_fail_lock = threading.Lock()

    def _record_auth_failure(ip: str):
        """记录认证失败；达到阈值自动拉黑（内网/白名单由 dual_auth 豁免）"""
        if not ip or ip == 'unknown':
            return
        now = time.time()
        hit = False
        with _auth_fail_lock:
            lst = [t for t in _auth_failures.get(ip, []) if now - t < _AUTH_FAIL_WINDOW]
            lst.append(now)
            _auth_failures[ip] = lst
            if len(_auth_failures) > 4096:
                stale = [k for k, v in _auth_failures.items()
                         if not v or now - v[-1] >= _AUTH_FAIL_WINDOW]
                for k in stale:
                    _auth_failures.pop(k, None)
            if _AUTH_FAIL_LIMIT > 0 and len(lst) >= _AUTH_FAIL_LIMIT:
                hit = True
        if hit:
            try:
                expires_at = (time.time() + _AUTH_BLACKLIST_TTL) if _AUTH_BLACKLIST_TTL > 0 else None
                dual_auth.blacklist(ip, reason=f'认证连续失败 {len(lst)} 次',
                                    source='honeypot', expires_at=expires_at)
                logger.warning(f"[认证防护] IP 因连续认证失败被拉黑: {ip}")
            except Exception as e:
                logger.warning(f"[认证防护] 拉黑失败 {ip}: {e}")

    def _deny_auth():
        """统一的认证拒绝：延迟 + （按配置）静默丢弃 / 中性文案"""
        _record_auth_failure(get_client_ip())
        _tarpit_sleep(_AUTH_TARPIT_MS)
        if _AUTH_DECOY:
            # 诱饵模式：返回看似成功的假数据（会让前端无法感知过期，谨慎开启）
            return jsonify({'code': 0, 'msg': 'ok',
                            'data': {'server': 'node-01', 'token': secrets.token_hex(16)}})
        if _SILENT_AUTH:
            # 「不对就不理」：不返回任何有意义内容，仅关闭连接
            return _silent_drop()
        return jsonify({'code': 401, 'msg': 'Unauthorized'}), 401

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

    def _silent_drop():
        """
        静默丢弃：不返回任何有意义的内容，仅关闭连接。
        语义等同 nginx 的 444（"不理会"），用于「认证不对就不理」的场景：
        扫描器/脚本拿不到 401 JSON，也就无法据此判断端点是否存在。

        注意：不能设置 Connection: close —— 按 PEP 3333，hop-by-hop 头
        不允许由 WSGI 应用设置，waitress 会直接抛 AssertionError。
        """
        return Response(b'', status=444, mimetype='text/plain')

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
            admin = _verify_token(token) if token else None
            if not admin:
                # 统一中性拒绝：不区分「未提供」「无效」「过期」，并计入风险
                return _deny_auth()
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
        # 端口隔离（True 时首页需注入 /webui-config.js）
        api_split=SPLIT_PORTS,
        # 通用工具 / yaml / 插件市场 / GitHub（app_helpers 工厂）
        **helpers,
        # 日志经纪
        log_broker=log_broker,
    )

    # ---- 按功能域注册路由（各模块 import 后调用 register(ctx)）----
    from framework.api import (
        auth, admins, apikeys, dashboard, plugins, commands, users_groups, tasks,
        logs, config, db_gateway, framework_ops, webui, files, stats, perm_api,
        static_routes, honeypot,
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
    # 蜜罐最后注册：其兜底规则权重最低，仅在无真实路由匹配时生效
    honeypot.register(ctx)

    # ---- 插件自定义 API 路由挂载（framework.api.registry）----
    from framework.api import registry as _api_registry
    _api_registry.set_web_context(app, require_auth)
    _api_registry.mount_all(app, require_auth)

    return app


# 向后兼容 re-export（WebServer 本体已拆至 webserver.py）
from framework.api.webserver import WebServer  # noqa: F401,E402
