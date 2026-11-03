# -*- coding: utf-8 -*-
"""
健康探针与指标端点（可观测性）
================================
在此之前框架没有任何真实健康端点：`/healthz`、`/readyz`、`/metrics` 这三个
路径只是蜜罐的诱饵（返回伪 nginx 404 / 假 actuator 环境变量），任何编排系统
（Docker healthcheck、Kubernetes probe、systemd、负载均衡）都无从判断
「进程活着但数据库挂了」「还在加载插件」「插件全挂了」这些真实状态。

本模块补齐三个只读端点，均为纯新增、不触碰任何既有契约：

    /healthz   存活探针：进程能响应即 200，绝不查数据库（避免探针把慢库拖成死锁）
    /readyz    就绪探针：进程运行中 + 启动完成 + 数据库可读，任一不满足返回 503
               响应体带逐项检查明细，排障时直接定位是哪一环没起来
    /metrics   Prometheus 文本格式（exposition v0.0.4）指标

设计约束
--------
- 零新依赖：指标文本手写，不引入 prometheus_client；psutil 是框架已有可选依赖，
  缺失时只是少几条进程指标，不影响端点可用性。
- 探针必须廉价：`/healthz` 只返回常量与运行时长；`/readyz` 只做一次 `SELECT 1`。
- 探针在端口隔离 / 纯 API 模式下同样可达（由 webapp 的 `_port_split_guard` 放行），
  因为编排系统不感知「WebUI 端口 / API 端口」这层区分。
- `/metrics` 只在 API 端口暴露（与 `/api/*` 同侧），避免公网 WebUI 端口泄露运行数据；
  需要跨机抓取时在 `observability.metrics_token` 设令牌，走 Bearer 鉴权。
- 三个端点均免登录、免限速、免人机验证，且不写入审计日志（否则探针会刷满审计表）。
"""
import logging
import os
import secrets
import sys
import threading
import time
from collections import Counter

from flask import Response, jsonify, request

logger = logging.getLogger('zcbot')

# 与 webapp._PROBE_PATHS 保持一致：healthz/readyz 任何端口都放行，metrics 只在 API 侧
PROBE_PATHS = ('/healthz', '/readyz')
METRICS_PATH = '/metrics'

_CONTENT_TYPE = 'text/plain; version=0.0.4; charset=utf-8'


def _truthy(value, default=True) -> bool:
    """配置值宽松解析（YAML 里可能写成 true / "true" / 1 / yes）"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ('1', 'true', 'yes', 'y', 'on')


def _escape_label(value) -> str:
    """Prometheus 标签值转义：反斜杠 / 双引号 / 换行"""
    return (str(value).replace('\\', '\\\\')
            .replace('"', '\\"').replace('\n', '\\n'))


def _fmt_num(value) -> str:
    """数值格式化：整数原样，浮点保留 3 位（NaN/Inf 按 Prometheus 约定输出）"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return '0'
    if f != f:
        return 'NaN'
    if f == float('inf'):
        return '+Inf'
    if f == float('-inf'):
        return '-Inf'
    if abs(f - int(f)) < 1e-9:
        return str(int(f))
    return f'{f:.3f}'


# ── 请求计数（模块级：探针与 WebUI 运行状态页共用同一份累计）──────
_REQ_LOCK = threading.Lock()
_REQ_COUNTS = Counter()   # (method, status) -> 累计次数


def version_of(ctx=None) -> str:
    """框架版本号（优先用 Web 上下文提供的 getter，其次读 VERSION 文件）"""
    getter = getattr(ctx, '_get_framework_local_version', None) if ctx is not None else None
    if callable(getter):
        try:
            return str(getter() or '')
        except Exception:  # noqa: BLE001
            pass
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), 'VERSION')
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:  # noqa: BLE001
        return ''


def uptime_of(framework) -> float:
    start = getattr(framework, '_start_time', None)
    try:
        return max(0.0, time.time() - float(start)) if start else 0.0
    except (TypeError, ValueError):
        return 0.0


def db_probe(framework):
    """探针级数据库检查：一次 SELECT 1，返回 (ok, detail)"""
    db = getattr(framework, 'db', None)
    if db is None:
        return False, '数据库实例不存在'
    t0 = time.perf_counter()
    try:
        try:
            db.scalar('SELECT 1')
        except AttributeError:
            db.query_one('SELECT 1')
    except Exception as e:  # noqa: BLE001
        return False, f'{type(e).__name__}: {e}'
    ms = (time.perf_counter() - t0) * 1000
    if getattr(framework, 'db_degraded', False):
        return True, f'{ms:.1f}ms（降级写缓冲中）'
    return True, f'{ms:.1f}ms'


def plugin_counts(framework):
    """(已加载数, 加载失败数)"""
    loader = getattr(framework, 'plugin_loader', None)
    loaded = failed = 0
    if loader is not None:
        try:
            loaded = len(loader.get_loaded_plugins() or {})
        except Exception:  # noqa: BLE001
            loaded = 0
        try:
            failed = len(getattr(loader, '_failed_mtimes', {}) or {})
        except Exception:  # noqa: BLE001
            failed = 0
    return loaded, failed


def collect_checks(framework) -> dict:
    """逐项健康检查（/readyz 与 WebUI 运行状态页共用）"""
    checks = {}
    running = bool(getattr(framework, '_running', False))
    checks['process'] = {'ok': running, 'detail': '运行中' if running else '进程未运行'}
    ready = bool(getattr(framework, '_ready', False))
    checks['startup'] = {'ok': ready, 'detail': '插件加载完成' if ready else '插件加载中'}
    db_ok, db_detail = db_probe(framework)
    checks['database'] = {'ok': db_ok, 'detail': db_detail}
    loaded, failed = plugin_counts(framework)
    # 插件加载失败不阻止就绪（框架本身可用），但如实暴露，便于排障
    checks['plugins'] = {'ok': True, 'required': False,
                         'detail': f'已加载 {loaded}，失败 {failed}',
                         'loaded': loaded, 'failed': failed}
    return checks


def snapshot(framework, ctx=None) -> dict:
    """就绪快照（与 GET /readyz 响应体同构），供 WebUI 复用，避免重复实现"""
    checks = collect_checks(framework)
    ok = all(c.get('ok') for c in checks.values() if c.get('required', True))
    return {'status': 'ready' if ok else 'not_ready',
            'ready': ok,
            'uptime_seconds': round(uptime_of(framework), 3),
            'version': version_of(ctx),
            'checks': checks}


def request_counts(limit: int = 12) -> list:
    """HTTP 请求计数快照（按次数倒序，仅取前 N 项供面板展示）"""
    with _REQ_LOCK:
        items = sorted(_REQ_COUNTS.items(), key=lambda kv: kv[1], reverse=True)
    return [{'method': m, 'status': s, 'count': n} for (m, s), n in items[:limit]]


def register(ctx):
    """注册健康探针与指标端点（由 create_web_app 调用）"""
    app = ctx.app
    framework = ctx.framework

    cfg = {}
    try:
        cfg = framework.config.get('observability') or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}

    health_enabled = _truthy(cfg.get('health', True))
    metrics_enabled = _truthy(cfg.get('metrics', True))
    metrics_token = str(cfg.get('metrics_token') or '')

    if not health_enabled and not metrics_enabled:
        logger.info("[可观测性] 已禁用（observability.health / metrics 均为 false）")
        return

    # 薄包装：复用模块级实现，保证 /readyz 与 WebUI 运行状态页口径一致
    def _version() -> str:
        return version_of(ctx)

    def _uptime() -> float:
        return uptime_of(framework)

    def _db_probe():
        return db_probe(framework)

    def _plugin_counts():
        return plugin_counts(framework)

    def _collect_checks() -> dict:
        return collect_checks(framework)

    # ── 存活探针 ─────────────────────────────────────────────
    if health_enabled:
        @app.route('/healthz', methods=['GET'])
        def _zcbot_healthz():
            """存活探针：能返回即代表进程未死，故意不查数据库"""
            return jsonify({'status': 'ok',
                            'uptime_seconds': round(_uptime(), 3),
                            'version': _version()}), 200

        @app.route('/readyz', methods=['GET'])
        def _zcbot_readyz():
            """就绪探针：进程运行中 + 启动完成 + 数据库可读，否则 503"""
            checks = _collect_checks()
            ok = all(c.get('ok') for c in checks.values() if c.get('required', True))
            body = {'status': 'ready' if ok else 'not_ready',
                    'uptime_seconds': round(_uptime(), 3),
                    'version': _version(),
                    'checks': checks}
            return jsonify(body), (200 if ok else 503)

        logger.info("[可观测性] 健康探针已注册：GET /healthz（存活） /readyz（就绪）")

    # ── 指标端点 ─────────────────────────────────────────────
    if metrics_enabled:
        # 指向模块级累计：/metrics 与 WebUI 运行状态页读同一份计数
        _req_lock = _REQ_LOCK
        _req_counts = _REQ_COUNTS

        @app.after_request
        def _zcbot_count_requests(resp):
            """累计请求数（不含探针自身，避免抓取流量淹没业务量）"""
            try:
                path = request.path or ''
                if path not in PROBE_PATHS and path != METRICS_PATH:
                    with _req_lock:
                        _req_counts[(request.method, int(resp.status_code))] += 1
            except Exception:  # noqa: BLE001
                pass
            return resp

        def _buffer_stats():
            buf = getattr(framework, '_event_buffer', None)
            if buf is None or not hasattr(buf, 'stats'):
                return {}
            try:
                return buf.stats() or {}
            except Exception:  # noqa: BLE001
                return {}

        def _ws_connections():
            try:
                primary = framework.services.primary_adapter() \
                    if hasattr(framework.services, 'primary_adapter') else None
                if primary is None:
                    primary = framework.services.get('protocol_adapter')
                if primary is not None and hasattr(primary, 'get_connected_bots'):
                    return len(primary.get_connected_bots() or [])
            except Exception:  # noqa: BLE001
                pass
            return 0

        def _process_gauges():
            """进程级指标（psutil 可选：缺失时返回空，端点照常可用）"""
            out = {}
            try:
                import psutil  # 延迟导入
                proc = psutil.Process(os.getpid())
                out['rss'] = proc.memory_info().rss
                out['threads'] = proc.num_threads()
            except Exception:  # noqa: BLE001
                out['threads'] = threading.active_count()
            return out

        def _authorized() -> bool:
            if not metrics_token:
                return True
            hdr = request.headers.get('Authorization') or ''
            if hdr.startswith('Bearer '):
                return secrets.compare_digest(hdr[7:].strip(), metrics_token)
            return secrets.compare_digest(str(request.args.get('token') or ''),
                                          metrics_token)

        def _render_metrics() -> str:
            loaded, failed = _plugin_counts()
            db_ok, _ = _db_probe()
            buf = _buffer_stats()
            proc = _process_gauges()
            version = _version()
            storage = str(getattr(framework, 'storage_mode', '') or
                          framework.config.get('database', {}).get('type', 'unknown'))

            lines = []

            def emit(name, mtype, help_text, samples):
                """samples: [(labels_dict_or_None, value)]"""
                lines.append(f'# HELP {name} {help_text}')
                lines.append(f'# TYPE {name} {mtype}')
                for labels, value in samples:
                    if labels:
                        lb = ','.join(f'{k}="{_escape_label(v)}"'
                                      for k, v in labels.items())
                        lines.append(f'{name}{{{lb}}} {_fmt_num(value)}')
                    else:
                        lines.append(f'{name} {_fmt_num(value)}')

            emit('zcbot_up', 'gauge', '进程存活标记（1=活着）', [(None, 1)])
            emit('zcbot_build_info', 'gauge', '构建信息（值恒为 1）',
                 [({'version': version, 'python': sys.version.split()[0],
                    'storage': storage}, 1)])
            emit('zcbot_uptime_seconds', 'gauge', '进程运行时长（秒）',
                 [(None, _uptime())])
            emit('zcbot_ready', 'gauge', '就绪标记（1=已就绪）',
                 [(None, 1 if all(c.get('ok') for c in _collect_checks().values()
                                  if c.get('required', True)) else 0)])
            emit('zcbot_plugins_loaded', 'gauge', '已加载插件数', [(None, loaded)])
            emit('zcbot_plugins_failed', 'gauge', '加载失败插件数', [(None, failed)])
            emit('zcbot_db_up', 'gauge', '数据库可读标记（1=可用）',
                 [(None, 1 if db_ok else 0)])

            if buf:
                emit('zcbot_event_buffer_items', 'gauge', '事件缓冲各层待处理条目数',
                     [({'layer': k[:-6]}, buf.get(k, 0)) for k in
                      ('l1_items', 'l2_pending', 'l3_items', 'l4_items')])
                emit('zcbot_event_buffer_bytes', 'gauge', '事件缓冲各层占用字节数',
                     [({'layer': k[:-6]}, buf.get(k, 0)) for k in
                      ('l1_bytes', 'l3_bytes', 'l4_bytes')])
                emit('zcbot_event_buffer_dropped_total', 'counter',
                     '事件缓冲累计丢弃数', [(None, buf.get('dropped', 0))])

            emit('zcbot_ws_connections', 'gauge', '已连接接入端数量',
                 [(None, _ws_connections())])

            if 'rss' in proc:
                emit('zcbot_process_resident_memory_bytes', 'gauge',
                     '进程常驻内存（字节）', [(None, proc['rss'])])
            emit('zcbot_process_threads', 'gauge', '进程线程数',
                 [(None, proc.get('threads', 0))])

            with _req_lock:
                snapshot = sorted(_req_counts.items())
            # 即使还没有业务请求也输出 HELP/TYPE，便于抓取端稳定发现该指标族
            emit('zcbot_http_requests_total', 'counter', 'HTTP 请求累计数（不含探针）',
                 [({'method': m, 'status': s}, n) for (m, s), n in snapshot])

            return '\n'.join(lines) + '\n'

        @app.route(METRICS_PATH, methods=['GET'])
        def _zcbot_metrics():
            """Prometheus 抓取端点（文本格式，无第三方依赖）"""
            if not _authorized():
                resp = Response('unauthorized\n', status=401, mimetype='text/plain')
                resp.headers['WWW-Authenticate'] = 'Bearer'
                return resp
            try:
                body = _render_metrics()
            except Exception as e:  # noqa: BLE001
                logger.error(f"[可观测性] 指标渲染失败: {e}")
                return Response('# metrics render failed\n', status=500,
                                mimetype='text/plain')
            resp = Response(body, status=200, content_type=_CONTENT_TYPE)
            resp.headers['Cache-Control'] = 'no-store'
            return resp

        logger.info("[可观测性] 指标端点已注册：GET %s（%s）", METRICS_PATH,
                    '需 Bearer 令牌' if metrics_token else '未设令牌')
