# -*- coding: utf-8 -*-
"""后端安全特性冒烟测试

覆盖：
  1. 安全等级预设（off / low / medium / high / paranoid）与单项覆盖
  2. 蜜罐诱饵（basic / full 诱饵集）、假数据真实性
  3. canary 唯一追踪串注入与反查
  4. 路径变形绕过检测
  5. 安全响应头 + 指纹隐藏
  6. 认证失败静默（中性 401）
  7. 静态资源与首页可访问、构建产物已混淆
"""
import sys
from unittest.mock import MagicMock

sys.path.insert(0, '.')

from framework.api.webapp import create_web_app
from framework.api import honeypot
from framework.api import security_levels


class StubFramework:
    def __init__(self, security=None):
        self.config = {
            'web': {'host': '127.0.0.1', 'port': 8080, 'session_timeout': 3600},
            # 测试默认：medium 档位，但关掉人机验证与静默丢弃，便于直连断言业务响应；
            # 人机验证 / 静默丢弃 由第十节单独覆盖。
            'security': security if security is not None else {
                'level': 'medium',
                'silent_auth': False,
                'human_check': {'enabled': False},
                'honeypot': {'tarpit_ms': 0, 'tarpit_auth_ms': 0, 'blacklist': False},
                'whitelist_ips': ['127.0.0.1'],
            },
            'database': {'type': 'sqlite'},
            'github_proxy': '',
        }
        self.config_path = 'config.yaml'

        self.db = MagicMock()
        self.db.query.return_value = []
        self.db.query_one.return_value = None
        self.db.execute.return_value = None

        self.plugin_loader = MagicMock()
        self.plugin_loader.plugins_dir = 'plugins'
        self.plugin_loader.get_loaded_plugins.return_value = {}
        self.plugin_loader.get_override_webui.return_value = None
        self.plugin_loader.get_plugin_webuis.return_value = []
        self.plugin_loader.get_plugin_webui_path.return_value = None

        self.services = MagicMock()
        self.services.protocol_adapters.return_value = {}
        self.services.get.return_value = None

        self.hooks = MagicMock()
        self.hooks.trigger_sync.return_value = []

    def build_ssl_context(self):
        return None


def main():
    results = []

    def check(name, cond, extra=''):
        results.append((name, bool(cond), extra))
        print(('PASS' if cond else 'FAIL'), '|', name, ('| ' + str(extra)) if extra else '')

    # ==================================================================
    # 一、安全等级预设
    # ==================================================================
    print('\n==== 一、安全等级预设 ====')
    for lv in security_levels.LEVELS:
        r = security_levels.resolve({'level': lv})
        check(f'档位 {lv} 可解析', r['level'] == lv and r['level_label'])
    check('非法档位回落 medium', security_levels.resolve({'level': 'xxx'})['level'] == 'medium')
    check('缺省档位为 medium', security_levels.resolve({})['level'] == 'medium')
    check('off 档关闭蜜罐与响应头',
          security_levels.resolve({'level': 'off'})['honeypot']['enabled'] is False
          and security_levels.resolve({'level': 'off'})['headers']['enabled'] is False)
    check('low 档不自动拉黑', security_levels.resolve({'level': 'low'})['honeypot']['blacklist'] is False)
    check('paranoid 档永久封禁',
          security_levels.resolve({'level': 'paranoid'})['honeypot']['blacklist_ttl'] == 0)
    check('单项覆盖优先于预设',
          security_levels.resolve({'level': 'low', 'honeypot': {'tarpit_ms': 0}})
          ['honeypot']['tarpit_ms'] == 0)
    check('档位递减：tarpit 随等级单调增',
          [security_levels.resolve({'level': l})['honeypot']['tarpit_ms']
           for l in ('off', 'low', 'medium', 'high', 'paranoid')] == [0, 200, 600, 1500, 3000])

    # ==================================================================
    # 二、蜜罐诱饵（medium = 全量诱饵集）
    # ==================================================================
    print('\n==== 二、蜜罐诱饵 ====')
    honeypot.reset_stats()
    app = create_web_app(StubFramework())
    c = app.test_client()

    r = c.get('/api/version')
    check('真实路由 /api/version 不受影响', r.status_code == 200, r.status_code)

    r = c.get('/.env')
    body = r.get_data(as_text=True)
    check('诱饵 /.env 含假数据库密码', r.status_code == 200 and 'DB_PASSWORD' in body)

    r = c.get('/.git/config')
    check('诱饵 /.git/config 含 [core]', '[core]' in r.get_data(as_text=True))

    r = c.get('/backup.sql')
    check('诱饵 /backup.sql 含 admin_users', 'admin_users' in r.get_data(as_text=True))

    r = c.get('/admin')
    check('诱饵 /admin 返回 HTML 登录页',
          'text/html' in r.headers.get('Content-Type', '') and '系统管理后台' in r.get_data(as_text=True))

    # 案例驱动新增：Actuator 全套 / Docker API / Spring 配置 / CI
    r = c.get('/actuator/env')
    check('诱饵 /actuator/env 返回 JSON', r.is_json and r.get_json().get('activeProfiles'))

    r = c.get('/actuator/mappings')
    check('诱饵 /actuator/mappings（full 新增）', r.is_json and 'contexts' in r.get_json())

    r = c.get('/actuator/heapdump')
    check('诱饵 /actuator/heapdump 返回假堆转储',
          r.get_data()[:14] == b'JAVA PROFILE 1')

    r = c.get('/version')
    check('诱饵 Docker /version 返回容器版本',
          r.is_json and r.get_json().get('Version') == '20.10.24')

    r = c.get('/containers/json')
    check('诱饵 Docker /containers/json 返回容器列表',
          r.is_json and isinstance(r.get_json(), list) and len(r.get_json()) >= 1)

    r = c.get('/application.yml')
    check('诱饵 /application.yml 含 Spring 配置泄露',
          'management' in r.get_data(as_text=True) and 'password' in r.get_data(as_text=True))

    r = c.get('/.github/workflows/deploy.yml')
    check('诱饵 CI 配置可访问', 'deploy' in r.get_data(as_text=True))

    r = c.get('/server.key')
    check('诱饵 /server.key 返回假私钥', 'BEGIN PRIVATE KEY' in r.get_data(as_text=True))

    r = c.get('/logs/error.log')
    check('诱饵 /logs/error.log 返回假日志', 'ERROR' in r.get_data(as_text=True))

    # 未知路径兜底
    r = c.get('/api/this_does_not_exist_xyz')
    check('未知 /api 路径返回诱饵 200',
          r.status_code == 200 and r.is_json and r.get_json().get('code') == 0)

    r = c.get('/some/random/path')
    b = r.get_data(as_text=True)
    check('未知页面返回伪装 nginx 的假 404',
          r.status_code == 404 and 'Werkzeug' not in b and 'nginx' in b)

    r = c.get('/favicon.ico')
    check('favicon.ico 由静态路由正常提供', r.status_code == 200, r.status_code)

    # ==================================================================
    # 三、canary 唯一追踪串
    # ==================================================================
    print('\n==== 三、canary 追踪串 ====')
    r = c.get('/.env')
    body = r.get_data(as_text=True)
    check('文本诱饵植入 canary（deployed-build 标记）',
          'deployed-build: zcbot-' in body)

    r = c.get('/api/keys')
    check('JSON 诱饵植入 request_id', str(r.get_json().get('request_id', '')).startswith('zcbot-'))

    r = c.get('/admin')
    check('HTML 诱饵植入 build 注释', '<!-- build zcbot-' in r.get_data(as_text=True))

    # 反查
    import re
    m = re.search(r'deployed-build: (zcbot-[0-9a-f]{12})', body)
    if m:
        rec = honeypot.lookup_canary(m.group(1))
        check('canary 可反查到访问记录',
              bool(rec) and rec.get('path') == '/.env' and rec.get('ip'))
    else:
        check('canary 可反查到访问记录', False, '未提取到 canary')

    st = honeypot.get_stats()
    check('统计：canary 计数 > 0', st['canary_issued'] > 0, st['canary_issued'])

    # ==================================================================
    # 四、路径变形绕过检测
    # ==================================================================
    print('\n==== 四、绕过检测 ====')
    cases = [
        ('/.//.env', '重复斜杠'),
        ('/%2eenv', 'URL 编码路径字符'),
        ('/api/uploads/%2e%2e%2f%2e%2e%2f.env', 'URL 编码路径字符'),
        ('/a/../../etc/passwd', '路径穿越'),
        ('/%252e%252e/config', '双重 URL 编码'),
    ]
    for uri, expect in cases:
        with app.test_request_context('/', environ_overrides={'RAW_URI': uri}):
            got = honeypot.detect_evasion()
        check(f'绕过检测 {uri} → {expect}', got == expect, got or '(未识别)')
    with app.test_request_context('/api/version'):
        check('正常路径不误判为绕过', honeypot.detect_evasion() == '')

    # 通过真实请求触发绕过，验证统计与日志链路
    honeypot.reset_stats()
    c.get('/api/uploads/%2e%2e%2f%2e%2e%2f.env')
    check('绕过命中计入 evasion_hits', honeypot.get_stats()['evasion_hits'] >= 1,
          honeypot.get_stats()['evasion_hits'])

    # ==================================================================
    # 五、安全响应头 + 指纹
    # ==================================================================
    print('\n==== 五、安全响应头 ====')
    r = c.get('/api/version')
    h = r.headers
    check('CSP 头存在', 'Content-Security-Policy' in h)
    check('X-Content-Type-Options', h.get('X-Content-Type-Options') == 'nosniff')
    check('X-Frame-Options', h.get('X-Frame-Options') == 'SAMEORIGIN')
    check('Referrer-Policy', h.get('Referrer-Policy') == 'same-origin')
    check('X-Robots-Tag', 'noindex' in (h.get('X-Robots-Tag') or ''))
    check('无 X-Powered-By 指纹', 'X-Powered-By' not in h)
    csp_plugin = c.get('/api/plugin_webui/nonexistent').headers.get('Content-Security-Policy', '')
    check('插件页 CSP 放宽（含 unsafe-inline）', 'unsafe-inline' in csp_plugin)

    # ==================================================================
    # 六、认证静默
    # ==================================================================
    print('\n==== 六、认证静默 ====')
    r = c.get('/api/me')
    check('无 token → 中性 401',
          r.status_code == 401 and r.get_json().get('msg') == 'Unauthorized')
    r = c.get('/api/me', headers={'Authorization': 'Bearer ' + 'x' * 2048})
    check('错误 token → 同一中性文案',
          r.status_code == 401 and r.get_json().get('msg') == 'Unauthorized')

    # ==================================================================
    # 七、off / low 档位行为差异
    # ==================================================================
    print('\n==== 七、档位行为差异 ====')
    app_off = create_web_app(StubFramework({'level': 'off'}))
    c_off = app_off.test_client()
    r = c_off.get('/.env')
    check('off 档：/.env 不再是诱饵', r.status_code != 200 or 'DB_PASSWORD' not in r.get_data(as_text=True),
          r.status_code)
    r = c_off.get('/api/version')
    check('off 档：不注入 CSP', 'Content-Security-Policy' not in r.headers)

    app_low = create_web_app(StubFramework({'level': 'low', 'honeypot': {'tarpit_ms': 0}}))
    c_low = app_low.test_client()
    r = c_low.get('/.env')
    check('low 档：核心诱饵仍生效', 'DB_PASSWORD' in r.get_data(as_text=True))
    r = c_low.get('/actuator/mappings')
    check('low 档：full 专属诱饵不注册（走假 404）',
          r.status_code == 404 and 'nginx' in r.get_data(as_text=True), r.status_code)

    # ==================================================================
    # 八、静态资源与首页
    # ==================================================================
    print('\n==== 八、静态资源与首页 ====')
    r = c.get('/')
    html = r.get_data(as_text=True)
    check('首页 200 且引用 theme-init', r.status_code == 200 and 'js/theme-init.js' in html)
    for path in ('/js/theme-init.js', '/favicon.ico', '/robots.txt', '/img/zcbot-icon-dark.svg'):
        check(f'静态资源 {path}', c.get(path).status_code == 200)
    import re as _re
    mjs = _re.search(r'\./js/(app\.[\w\-]+\.js)', html)
    if mjs:
        code = c.get('/js/' + mjs.group(1)).get_data(as_text=True)
        check('业务 JS chunk 已混淆', code.count('_0x') > 100, code.count('_0x'))

    # ==================================================================
    # 九、拉黑链路（公网 IP 命中诱饵 → 后续全站 403；内网豁免）
    # ==================================================================
    print('\n==== 九、拉黑链路 ====')
    fw_ext = StubFramework({'level': 'medium',
                            'silent_auth': False,
                            'human_check': {'enabled': False},   # 本节只验证拉黑链路
                            'honeypot': {'tarpit_ms': 0, 'tarpit_auth_ms': 0},
                            'trusted_proxies': ['127.0.0.1']})
    app_ext = create_web_app(fw_ext)
    c_ext = app_ext.test_client()
    honeypot.reset_stats()
    EXT = {'X-Forwarded-For': '203.0.113.9'}

    r = c_ext.get('/.env', headers=EXT)
    check('公网 IP 命中诱饵返回假数据', r.status_code == 200 and 'DB_PASSWORD' in r.get_data(as_text=True))
    check('公网 IP 命中后计入已拉黑', honeypot.get_stats()['blacklisted'] == 1,
          honeypot.get_stats()['blacklisted'])
    r = c_ext.get('/api/version', headers=EXT)
    check('被拉黑 IP 后续访问一律 403', r.status_code == 403, r.status_code)
    r = c_ext.get('/.git/config', headers=EXT)
    check('被拉黑 IP 连诱饵也不再服务', r.status_code == 403, r.status_code)

    honeypot.reset_stats()
    r = c_ext.get('/.env')      # 127.0.0.1（内网，豁免）
    check('内网 IP 命中不计入已拉黑', honeypot.get_stats()['blacklisted'] == 0,
          honeypot.get_stats()['blacklisted'])
    r = c_ext.get('/api/version')
    check('内网 IP 不被封禁', r.status_code == 200, r.status_code)

    # ==================================================================
    # 十、JS 人机验证 + 认证静默丢弃
    # ==================================================================
    print('\n==== 十、人机验证与静默丢弃 ====')
    import hashlib

    fw_hc = StubFramework({'level': 'medium'})   # 默认即 human_check on + silent_auth on
    app_hc = create_web_app(fw_hc)
    c_hc = app_hc.test_client()

    def _solve(ch, diff):
        n = 0
        while True:
            if hashlib.sha256(f'{ch}:{n}'.encode()).hexdigest().startswith('0' * diff):
                return str(n)
            n += 1

    # 无票据 / 伪造票据 → 444 空响应
    r = c_hc.get('/api/me')
    check('无票据访问真实 API → 444', r.status_code == 444, r.status_code)
    check('444 响应体为空', r.get_data() == b'', repr(r.get_data()[:30]))
    r = c_hc.get('/api/version', headers={'X-ZC-Pass': 'forged'})
    check('伪造票据 → 444', r.status_code == 444, r.status_code)

    # 静默丢弃：认证不对时不再返回 401 JSON
    r = c_hc.get('/api/me', headers={'X-ZC-Pass': 'forged'})
    check('silent_auth：认证不对不返回 JSON', r.is_json is False and r.status_code == 444)

    # 诱饵与插件页面必须放行（否则欺骗与插件页失效）
    check('诱饵 /.env 不受人机验证拦截', c_hc.get('/.env').status_code == 200)
    check('未知 /api 兜底诱饵不受拦截', c_hc.get('/api/nope').status_code == 200)
    check('挑战接口自身免检', c_hc.get('/api/challenge').status_code == 200)

    # 完整求解流程
    d = c_hc.get('/api/challenge').get_json()['data']
    check('challenge 含 algo/difficulty/expires_in',
          d.get('algo') == 'sha256' and d.get('difficulty') >= 1 and d.get('expires_in'), d)
    nonce = _solve(d['challenge'], d['difficulty'])
    v = c_hc.post('/api/challenge/verify', json={'challenge': d['challenge'], 'nonce': nonce})
    check('正确 nonce → 签发票据',
          v.status_code == 200 and v.get_json()['data'].get('ticket'), v.status_code)
    ticket = v.get_json()['data']['ticket']

    r = c_hc.get('/api/version', headers={'X-ZC-Pass': ticket})
    check('带票据访问真实 API → 200', r.status_code == 200, r.status_code)

    # challenge 一次性 + 错误 nonce
    v2 = c_hc.post('/api/challenge/verify', json={'challenge': d['challenge'], 'nonce': nonce})
    check('重放同一 challenge → 拒绝', v2.status_code == 400, v2.status_code)
    d2 = c_hc.get('/api/challenge').get_json()['data']
    v3 = c_hc.post('/api/challenge/verify', json={'challenge': d2['challenge'], 'nonce': '1'})
    check('错误 nonce → 拒绝', v3.status_code == 400, v3.status_code)

    # 难度不接受客户端自报（防降级绕过）
    d3 = c_hc.get('/api/challenge').get_json()['data']
    v4 = c_hc.post('/api/challenge/verify',
                   json={'challenge': d3['challenge'], 'nonce': _solve(d3['challenge'], 1),
                         'difficulty': 1})
    check('客户端自报低难度不生效', v4.status_code == 400, v4.status_code)

    # 档位开关
    app_off2 = create_web_app(StubFramework({'level': 'off'}))
    check('off 档：无票据也能直连 API',
          app_off2.test_client().get('/api/version').status_code == 200)
    app_low2 = create_web_app(StubFramework({'level': 'low'}))
    check('low 档：白名单 IP 免人机验证',
          app_low2.test_client().get('/api/version').status_code == 200)

    # ==================================================================
    print('\n==== 汇总 ====')
    passed = sum(1 for _, ok, _ in results if ok)
    print(f'{passed}/{len(results)} 通过')
    fails = [n for n, ok, _ in results if not ok]
    if fails:
        print('失败项:')
        for f in fails:
            print('  -', f)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
