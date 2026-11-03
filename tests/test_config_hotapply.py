# -*- coding: utf-8 -*-
"""配置热应用（framework/hotapply.py）与 /api/v1 别名回归测试。

覆盖：
  1. 日志级别保存即生效（root logger 级别真的变了）
  2. 黑匣子 enabled/max_bytes 保存即生效
  3. 分段判定：database/web 等需重启；log 不需重启；未知段保守判定需重启
  4. 部分生效段：plugin 改 hot_reload 即时生效、改 heartbeat_interval 仍需重启
  5. 合并语义：只提交部分键时，未提交的旧键不丢
  6. 自定义 applier 注册/注销
  7. /api/v1/* 别名与旧路径行为一致，且不会把未知路径变成 200

运行：python tests/test_config_hotapply.py   或   pytest tests/test_config_hotapply.py
"""
import logging
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework import hotapply
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


def _build(extra_yaml=''):
    tmp = tempfile.mkdtemp(prefix='zcbot_hot_')
    cfg = os.path.join(tmp, 'config.yaml')
    db = os.path.join(tmp, 'hot.db').replace('\\', '/')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            "database:\n  type: sqlite\n  path: {db}\n"
            "log:\n  level: INFO\n"
            "security:\n  silent_auth: false\n  human_check:\n    enabled: false\n"
            "web:\n  host: 127.0.0.1\n  port: 8080\n"
            "{extra}{tail}".format(db=db, extra=extra_yaml, tail=_CONFIG_TAIL)
        )
    fw = Framework(config_path=cfg, role='standard')
    return fw, create_web_app(fw)


# ── 1. 日志级别 ─────────────────────────────────────────────
def test_log_level():
    print("[1] 日志级别即时生效")
    fw, _app = _build()
    root = logging.getLogger()
    old = root.level
    try:
        res = hotapply.apply_section(fw, 'log', {'level': 'DEBUG'})
        check(res['live'] is True, "标记为即时生效")
        check(root.level == logging.DEBUG, f"root logger 级别已变（{logging.getLevelName(root.level)}）")
        check(res['restart_required'] is False, "日志段不需要重启")
        check(fw.config['log']['level'] == 'DEBUG', "内存配置同步")
        # 只提交 level 时，其它 log 配置不丢
        fw.config['log']['file'] = 'data/logs/x.log'
        hotapply.apply_section(fw, 'log', {'level': 'WARNING'})
        check(fw.config['log'].get('file') == 'data/logs/x.log', "部分提交不丢其余键")
        check('未知日志级别' in ' '.join(
            hotapply.apply_section(fw, 'log', {'level': 'NOPE'})['notes']),
            "非法级别给出明确提示")

        # 输出格式同样支持热切换（text <-> json）
        from framework import jsonlog
        res_fmt = hotapply.apply_section(fw, 'log', {'format': 'json'})
        check(res_fmt['live'] is True, "格式切换标记为即时生效")
        check(jsonlog.current_format() == 'json', "根 logger 已切到 JSON")
        hotapply.apply_section(fw, 'log', {'format': 'text'})
        check(jsonlog.current_format() == 'text', "切回 text")
        check('未知日志格式' in ' '.join(
            hotapply.apply_section(fw, 'log', {'format': 'yaml'})['notes']),
            "非法格式给出明确提示")
    finally:
        root.setLevel(old)
        from framework import jsonlog as _jl
        _jl.apply_format('text')


# ── 2. 黑匣子 ───────────────────────────────────────────────
def test_blackbox():
    print("[2] 黑匣子配置即时生效")
    fw, _app = _build()
    from framework import blackbox
    res = hotapply.apply_section(fw, 'framework', {'blackbox': {'enabled': False, 'max_bytes': 32768}})
    st = blackbox.stats()
    check(res['live'] is True, "标记为即时生效")
    check(st.get('enabled') is False, f"enabled 已关（{st.get('enabled')}）")
    check(st.get('max_bytes') == 32768, f"max_bytes 已生效（{st.get('max_bytes')}）")
    # 过小值按 16KB 下限兜底（黑匣子的既有约定，热应用不应绕过它）
    hotapply.apply_section(fw, 'framework', {'blackbox': {'max_bytes': 1}})
    check(blackbox.stats().get('max_bytes') == 16384, "过小值按 16KB 下限兜底")
    check(res['restart_required'] is False, "framework 段（黑匣子）不需重启")
    blackbox.configure(enabled=True, max_bytes=262144)


# ── 3. 分段判定 ─────────────────────────────────────────────
def test_restart_classification():
    print("[3] 需重启 / 即时生效 的判定")
    fw, _app = _build()
    for section in ('database', 'web', 'onebot', 'ssl', 'event_queue', 'observability'):
        res = hotapply.apply_section(fw, section, {})
        check(res['restart_required'] is True, f"{section} 判定需重启")
        check(bool(res['restart_reason']), f"{section} 给出重启原因")
    res = hotapply.apply_section(fw, 'no_such_section', {'a': 1})
    check(res['restart_required'] is True, "未知段保守判定需重启")
    check(res['live'] is False, "未知段没有即时效果")


def test_partial_sections():
    print("[4] 部分生效段")
    fw, _app = _build()
    res = hotapply.apply_section(fw, 'plugin', {'hot_reload': True})
    check(res['restart_required'] is False, "plugin.hot_reload 即时生效")
    res = hotapply.apply_section(fw, 'plugin', {'heartbeat_interval': 30})
    check(res['restart_required'] is True, "plugin.heartbeat_interval 需重启")
    check('重启' in res['restart_reason'], f"原因说明（{res['restart_reason']}）")


def test_custom_applier():
    print("[5] 自定义 applier 注册/注销")
    fw, _app = _build()
    seen = {}

    def _fn(_fw, cfg):
        seen.update(cfg)
        return ['自定义段即时生效']

    hotapply.register('custom_section', _fn)
    try:
        res = hotapply.apply_section(fw, 'custom_section', {'x': 1})
        check(seen.get('x') == 1, "applier 收到合并后的配置")
        check(res['live'] is True and res['restart_required'] is False,
              "注册后判定为即时生效")
    finally:
        hotapply.unregister('custom_section')
    res = hotapply.apply_section(fw, 'custom_section', {'x': 2})
    check(res['restart_required'] is True, "注销后回到需重启")


# ── 6. /api/v1 别名 ─────────────────────────────────────────
def test_api_v1_alias():
    print("[6] /api/v1 别名")
    fw, app = _build()
    c = app.test_client()

    old = c.get('/api/me')
    new = c.get('/api/v1/me')
    check(new.status_code == old.status_code == 401,
          f"鉴权语义一致（旧 {old.status_code} / 新 {new.status_code}）")

    old2 = c.get('/api/plugins')
    new2 = c.get('/api/v1/plugins')
    check(new2.status_code == old2.status_code,
          f"普通端点一致（旧 {old2.status_code} / 新 {new2.status_code}）")

    # 蜜罐兜底不应被复制到 v1：v1 规则里不能出现 _honeypot 端点
    aliae = [r for r in app.url_map.iter_rules() if r.rule.startswith('/api/v1/')]
    check(len(aliae) > 50, f"已生成 v1 别名（{len(aliae)} 条）")
    check(all(not r.endpoint.startswith('_honeypot') for r in aliae),
          "v1 别名不含蜜罐兜底端点")
    old_rules = [r.rule for r in app.url_map.iter_rules()
                 if r.rule.startswith('/api/') and not r.rule.startswith('/api/v1/')
                 and not r.endpoint.startswith('_honeypot')]
    alias_paths = {r.rule for r in aliae}
    missing = [p for p in old_rules
               if '/api/v1' + p[len('/api'):] not in alias_paths]
    check(missing == [], f"每条真实 /api 规则都有 v1 对应（缺失 {missing[:3]}）")


def main():
    for fn in (test_log_level, test_blackbox, test_restart_classification,
               test_partial_sections, test_custom_applier, test_api_v1_alias):
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
