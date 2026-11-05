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
import ipaddress
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


def _is_loopback(addr) -> bool:
    """请求是否来自本机回环（127.0.0.0/8、::1）。

    地址缺失 / 非法一律按「非本机」处理（拒绝），避免解析失败被当成放行。
    只看 request.remote_addr（TCP 对端），不采信 X-Forwarded-For —— 后者可被
    客户端伪造，会让「仅本机」这道边界形同虚设。
    """
    try:
        return ipaddress.ip_address((addr or '').strip()).is_loopback
    except (ValueError, TypeError, AttributeError):
        return False


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


def outbound_stats() -> dict:
    """出站闸门计数快照（发送/失败/限流/重试/累计等待），与 /metrics 同源"""
    try:
        from framework.messaging.outbound_guard import gate
        return gate.stats()
    except Exception:  # noqa: BLE001
        return {}


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
            """配了令牌：Bearer / ?token= 常量时间比对（任意来源）；
            未配令牌：保守默认，仅放行本机回环，非本机一律拒绝读取指标。

            与 /healthz /readyz 区分：那两个探针只暴露「进程活着 / 就绪」这类
            无敏感信息的状态，故无条件免鉴权；/metrics 含运行时数据，未配令牌
            时不再对任意来源 fail-open。
            """
            if not metrics_token:
                return _is_loopback(request.remote_addr)
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

            ob = outbound_stats()
            if ob:
                emit('zcbot_outbound_sent_total', 'counter', '出站发送成功累计数',
                     [(None, ob.get('sent', 0))])
                emit('zcbot_outbound_failed_total', 'counter', '出站发送失败累计数',
                     [(None, ob.get('failed', 0))])
                emit('zcbot_outbound_throttled_total', 'counter',
                     '出站因限流被拒累计数', [(None, ob.get('throttled', 0))])
                emit('zcbot_outbound_retried_total', 'counter', '出站重试累计次数',
                     [(None, ob.get('retried', 0))])
                emit('zcbot_outbound_waited_seconds_total', 'counter',
                     '出站因限流累计等待秒数',
                     [(None, (ob.get('waited_ms', 0) or 0) / 1000.0)])

            # 事件循环卡顿：偶发一次性抖动和持续性阻塞的处置完全不同，
            # 所以次数、累计时长、峰值三个维度都要有，不能只看一个 current 值。
            try:
                from framework.core.loop_watchdog import watchdog as _wd
                ws = _wd.stats()
            except Exception:
                ws = None
            if ws:
                emit('zcbot_event_loop_lag_seconds', 'gauge',
                     '最近一次采样的事件循环延迟（秒）',
                     [(None, (ws.get('last_lag_ms', 0) or 0) / 1000.0)])
                emit('zcbot_event_loop_lag_max_seconds', 'gauge',
                     '启动以来最大事件循环延迟（秒）',
                     [(None, (ws.get('max_lag_ms', 0) or 0) / 1000.0)])
                emit('zcbot_event_loop_blocked', 'gauge',
                     '事件循环当前是否处于阻塞（1=阻塞）',
                     [(None, 1 if ws.get('blocked') else 0)])
                emit('zcbot_event_loop_stalls_total', 'counter',
                     '事件循环阻塞次数（超阈值）', [(None, ws.get('stalls', 0))])
                emit('zcbot_event_loop_stall_seconds_total', 'counter',
                     '事件循环阻塞累计时长（秒）',
                     [(None, (ws.get('total_stall_ms', 0) or 0) / 1000.0)])

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
                # 未配令牌且非回环：按保守默认当作端点不存在，404 且不渲染任何
                # 指标（与蜜罐诱饵语义一致：不暴露端点、不泄露运行数据）。
                if not metrics_token:
                    return Response('not found\n', status=404, mimetype='text/plain')
                # 配了令牌但校验失败：沿用 401 + Bearer 挑战
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

        if metrics_token:
            logger.info("[可观测性] 指标端点已注册：GET %s（需 Bearer 令牌）",
                        METRICS_PATH)
        else:
            logger.warning(
                "[可观测性] 指标端点已注册：GET %s，但未配置 "
                "observability.metrics_token —— 按保守默认仅允许本机回环访问，"
                "非本机请求返回 404（不泄露指标）。如需跨机抓取，请在 config.yaml "
                "设置 observability.metrics_token 并携带 Authorization: Bearer <token>；"
                "如完全不需要指标，可设 observability.metrics: false。",
                METRICS_PATH)
