# -*- coding: utf-8 -*-
"""框架可观测性端点（/healthz /readyz /metrics）真实 HTTP 行为回归测试。

验证的是「运行期真的能用」，不是源码里写了字符串：

  · /healthz 免鉴权、可 JSON 解析、含运行时长与版本
  · /readyz 在未启动 / 未就绪 / 数据库不可用时返回 503，就绪后 200 且逐项检查通过
  · /metrics 输出 Prometheus 文本格式，关键指标齐全，且不把探针自身计入请求数
  · metrics_token 设置后强制 Bearer（缺失 401，正确 200，query token 也认）
  · observability.health=false 时不注册真实路由（回落到蜜罐诱饵 404）
  · 端口隔离时 /healthz 在两个端口都可达，/metrics 只在 API 端口给出真实指标

实现方式与 tests/test_api_security.py 一致：真实 Framework（临时 SQLite，core_plugins
全关，不 start()）+ create_web_app() + Flask test_client，CI 友好无网络。

运行：python tests/test_observability.py   或   pytest tests/test_observability.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.core import Framework
from framework.api.webapp import create_web_app

_FAILED = []


def check(cond, msg):
    if cond:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}")
        _FAILED.append(msg)


_CONFIG_TAIL = (
    "core_plugins:\n"
    "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
    "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
    "  telegram: false\n  discord: false\n  session: false\n"
    "  scheduler: false\n  image_renderer: false\n"
)


def _build(extra_yaml='', db_name='obs.db', started=False):
    """构造 (framework, flask_app)；started=True 时伪造「已启动 + 已就绪」"""
    tmp = tempfile.mkdtemp(prefix='zcbot_obs_')
    cfg = os.path.join(tmp, 'config.yaml')
    db = os.path.join(tmp, db_name).replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            "database:\n  type: sqlite\n  path: {db}\n"
            "plugin:\n  heartbeat_interval: 60\n"
            "log:\n  level: ERROR\n"
            # 关闭主动防御，让断言稳定观察 HTTP 语义（444 静默丢弃另由 smoke 测试覆盖）
            "security:\n  silent_auth: false\n  human_check:\n    enabled: false\n"
            "web:\n  host: 127.0.0.1\n  port: 8080\n"
            "{extra}"
            "{tail}".format(db=db, extra=extra_yaml, tail=_CONFIG_TAIL)
        )
    fw = Framework(config_path=cfg, role='standard')   # 只初始化，不 start()
    if started:
        fw._running = True
        fw._ready = True
    return fw, create_web_app(fw)


# ── 1. 存活探针 ─────────────────────────────────────────────
def test_healthz_open():
    print("[1] /healthz 存活探针")
    fw, app = _build()
    r = app.test_client().get('/healthz')
    check(r.status_code == 200, f"免鉴权返回 200（实际 {r.status_code}）")
    body = r.get_json() or {}
    check(body.get('status') == 'ok', "status=ok")
    check(isinstance(body.get('uptime_seconds'), (int, float)), "含 uptime_seconds")
    check(isinstance(body.get('version'), str) and body['version'] != '',
          f"含版本号（{body.get('version')}）")


# ── 2. 就绪探针：未启动 / 已就绪 ─────────────────────────────
def test_readyz_not_started():
    print("[2] /readyz 未启动时应 503 并指出是哪一项")
    fw, app = _build()
    r = app.test_client().get('/readyz')
    check(r.status_code == 503, f"未启动返回 503（实际 {r.status_code}）")
    body = r.get_json() or {}
    check(body.get('status') == 'not_ready', "status=not_ready")
    checks = body.get('checks') or {}
    check(checks.get('process', {}).get('ok') is False, "process 检查为 False")
    check(checks.get('startup', {}).get('ok') is False, "startup 检查为 False")


def test_readyz_ready():
    print("[3] /readyz 就绪后应 200 且数据库检查通过")
    fw, app = _build(started=True)
    r = app.test_client().get('/readyz')
    check(r.status_code == 200, f"已就绪返回 200（实际 {r.status_code}）")
    body = r.get_json() or {}
    check(body.get('status') == 'ready', "status=ready")
    checks = body.get('checks') or {}
    check(checks.get('database', {}).get('ok') is True,
          f"database 检查通过（{checks.get('database', {}).get('detail')}）")
    check('loaded' in (checks.get('plugins') or {}), "plugins 检查带 loaded/failed 计数")


# ── 3. 指标端点 ─────────────────────────────────────────────
def test_metrics_content():
    print("[4] /metrics 文本格式与关键指标")
    fw, app = _build(started=True)
    r = app.test_client().get('/metrics')
    check(r.status_code == 200, f"返回 200（实际 {r.status_code}）")
    ctype = r.headers.get('Content-Type', '')
    check('text/plain' in ctype, f"Content-Type 为纯文本（{ctype}）")
    text = r.get_data(as_text=True)
    for name in ('zcbot_up 1', 'zcbot_uptime_seconds', 'zcbot_ready 1',
                 'zcbot_db_up 1', 'zcbot_plugins_loaded', 'zcbot_build_info',
                 'zcbot_event_buffer_items', 'zcbot_http_requests_total'):
        check(name in text, f"包含指标 {name}")
    check('# TYPE zcbot_up gauge' in text, "带 Prometheus TYPE 声明")
    check('zcbot_plugins_failed' in text, "暴露插件失败数")
    for name in ('zcbot_outbound_sent_total', 'zcbot_outbound_throttled_total',
                 'zcbot_outbound_retried_total'):
        check(name in text, f"暴露出站闸门指标 {name}")
    # 探针自身不计入请求数
    check('status="200"' not in text, "探针请求未被计入 zcbot_http_requests_total")


def test_metrics_counts_api_requests():
    print("[5] 业务请求被计入 zcbot_http_requests_total")
    fw, app = _build(started=True)
    c = app.test_client()
    c.get('/api/me')            # 未带 token → 401（应用层拒绝）
    text = c.get('/metrics').get_data(as_text=True)
    check('zcbot_http_requests_total{method="GET",status="401"}' in text,
          "出现 method=GET status=401 计数")


def test_metrics_token():
    print("[6] metrics_token 启用后强制 Bearer")
    fw, app = _build(extra_yaml='observability:\n  metrics_token: "s3cret"\n', started=True)
    c = app.test_client()
    r = c.get('/metrics')
    check(r.status_code == 401, f"无令牌 401（实际 {r.status_code}）")
    check(r.headers.get('WWW-Authenticate') == 'Bearer', "返回 WWW-Authenticate: Bearer")
    r = c.get('/metrics', headers={'Authorization': 'Bearer s3cret'})
    check(r.status_code == 200, "Bearer 正确 200")
    r = c.get('/metrics', headers={'Authorization': 'Bearer wrong'})
    check(r.status_code == 401, "Bearer 错误 401")
    r = c.get('/metrics?token=s3cret')
    check(r.status_code == 200, "query token 也认（便于临时排查）")
    # 探针不受令牌影响
    r = c.get('/healthz')
    check(r.status_code == 200, "/healthz 不受 metrics_token 影响")


# ── 4. 关闭开关 ─────────────────────────────────────────────
def test_disabled():
    print("[7] observability.health=false 时不注册真实路由")
    fw, app = _build(extra_yaml='observability:\n  health: false\n  metrics: false\n')
    r = app.test_client().get('/healthz')
    check(r.status_code == 404, f"回落到诱饵/404（实际 {r.status_code}）")


# ── 5. 端口隔离 ─────────────────────────────────────────────
def test_port_split():
    print("[8] 端口隔离：探针两端口可达，指标只在 API 端口")
    fw, app = _build(extra_yaml='  api_port: 8081\n', started=True)
    c = app.test_client()
    r_web = c.get('/healthz', environ_overrides={'SERVER_PORT': '8080'})
    r_api = c.get('/healthz', environ_overrides={'SERVER_PORT': '8081'})
    check(r_web.status_code == 200, f"WebUI 端口 /healthz 200（实际 {r_web.status_code}）")
    check(r_api.status_code == 200, f"API 端口 /healthz 200（实际 {r_api.status_code}）")

    m_api = c.get('/metrics', environ_overrides={'SERVER_PORT': '8081'})
    check(m_api.status_code == 200 and 'zcbot_up' in m_api.get_data(as_text=True),
          "API 端口给出真实指标")
    m_web = c.get('/metrics', environ_overrides={'SERVER_PORT': '8080'})
    check('zcbot_up' not in m_web.get_data(as_text=True),
          f"WebUI 端口不暴露真实指标（status={m_web.status_code}）")


def test_runtime_stats_shares_health():
    """WebUI 运行状态页与 /readyz 同源：/api/runtime/stats 必须带健康快照"""
    fw, app = _build(started=True)
    c = app.test_client()
    # 该端点需要管理员登录（默认账号由框架初始化写入）
    login = c.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    token = ((login.get_json() or {}).get('data') or {}).get('token')
    check(bool(token), f"管理员登录成功（{login.status_code}）")
    hdr = {'Authorization': f'Bearer {token}'}

    # 先产生一次业务请求，让请求计数非空
    c.get('/healthz')
    c.get('/api/runtime/stats', headers=hdr)
    r = c.get('/api/runtime/stats', headers=hdr)
    check(r.status_code == 200, f"/api/runtime/stats 可访问（{r.status_code}）")
    data = (r.get_json() or {}).get('data') or {}

    health = data.get('health') or {}
    check(health.get('status') == 'ready', f"健康状态与 /readyz 一致（{health.get('status')}）")
    check(health.get('ready') is True, "ready 标记为真")
    checks = health.get('checks') or {}
    check(set(checks) == {'process', 'startup', 'database', 'plugins'},
          f"逐项检查齐全（{sorted(checks)}）")
    check(all(c.get('ok') for c in checks.values() if c.get('required', True)),
          "必需检查项均通过")

    readyz = c.get('/readyz').get_json() or {}

    # 与 /readyz 返回同构，避免两处口径漂移。
    # 注意**不能要求 detail 全等**：database 的 detail 是 f'{耗时:.1f}ms'，
    # 两次请求各自实时探测，耗时必然不同（本地偶然通过只是两次恰好落在同一个
    # 0.1ms 档）。CI 在 Python 3.14 上因此偶发失败过一次，属于断言写错而不是
    # 产品缺陷。这里比较键集合 + ok + required，语义等价且稳定。
    def _stable(c):
        out = {}
        for k, v in (c or {}).items():
            v = dict(v or {})
            v.pop('detail', None)
            out[k] = v
        return out

    check(_stable(readyz.get('checks')) == _stable(checks),
          "与 /readyz 的 checks 同构（键/ok/required 一致，忽略 detail 耗时）")

    check(isinstance(data.get('event_buffer'), dict), "带事件缓冲统计字段")
    reqs = data.get('requests')
    check(isinstance(reqs, list) and len(reqs) >= 1,
          f"带 HTTP 请求分布（{len(reqs) if isinstance(reqs, list) else reqs} 项）")
    check('db_degraded' in data, "带降级标记")


def main():
    for fn in (test_healthz_open, test_readyz_not_started, test_readyz_ready,
               test_metrics_content, test_metrics_counts_api_requests,
               test_metrics_token, test_disabled, test_port_split,
               test_runtime_stats_shares_health):
        fn()
    print()
    if _FAILED:
        print(f"FAILED {len(_FAILED)} 项：")
        for m in _FAILED:
            print("  -", m)
        return 1
    print("ALL PASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
