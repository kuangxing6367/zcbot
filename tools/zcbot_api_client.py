# -*- coding: utf-8 -*-
"""
ZCBOT 管理后台 API 客户端（自用 / 集成脚本）

封装了后台的两道门槛，调用方无需关心细节：
    1. JS 人机验证（工作量证明）—— 自动取题、求解、换票据
    2. 会话 token / API Key       —— 自动附加 Authorization 头

用法
----
    from zcbot_api_client import ZcbotClient

    c = ZcbotClient('http://127.0.0.1:8080', token='<登录返回的 token 或 API Key>')
    print(c.get('/api/me'))
    print(c.get('/api/dashboard'))
    c.post('/api/commands', json={'name': 'hello'})

命令行
------
    python tools/zcbot_api_client.py http://127.0.0.1:8080 <token> /api/me

说明
----
- 票据默认在内存中复用（同一进程只求解一次），可用 refresh_pass() 强制重取。
- 若服务端把 human_check.apply_to 设为 'none' 或关闭人机验证，本客户端照常工作
  （多余的 X-ZC-Pass 头会被忽略）。
- 444 = 服务端「不理会」（票据/认证未通过）。本客户端会自动重取票据重试一次。
"""
import hashlib
import json
import sys
import time

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class ZcbotClientError(Exception):
    pass


class ZcbotClient:
    def __init__(self, base_url, token='', timeout=10, session=None):
        if requests is None:
            raise ZcbotClientError('缺少依赖 requests，请先 pip install requests')
        self.base = base_url.rstrip('/')
        self.token = token.strip()
        self.timeout = timeout
        self.session = session or requests.Session()
        self._pass = ''
        self._pass_expire = 0.0

    # ---------------- 人机验证 ----------------
    def _solve(self, challenge, difficulty):
        prefix = '0' * max(1, min(8, int(difficulty)))
        n = 0
        while True:
            if hashlib.sha256(f'{challenge}:{n}'.encode()).hexdigest().startswith(prefix):
                return str(n)
            n += 1
            if n > 30_000_000:
                raise ZcbotClientError('工作量证明求解超时')

    def refresh_pass(self):
        """重新完成一次人机验证，返回通行票据"""
        r = self.session.get(f'{self.base}/api/challenge', timeout=self.timeout)
        r.raise_for_status()
        d = r.json()['data']
        nonce = self._solve(d['challenge'], d['difficulty'])
        r = self.session.post(f'{self.base}/api/challenge/verify',
                              json={'challenge': d['challenge'], 'nonce': nonce},
                              timeout=self.timeout)
        if r.status_code != 200:
            raise ZcbotClientError(f'人机验证失败: HTTP {r.status_code} {r.text[:120]}')
        self._pass = r.json()['data']['ticket']
        self._pass_expire = time.time() + r.json()['data'].get('ttl', 1800) - 30
        return self._pass

    def _ensure_pass(self):
        if self._pass and time.time() < self._pass_expire:
            return self._pass
        return self.refresh_pass()

    # ---------------- 请求封装 ----------------
    def _headers(self, extra=None):
        h = dict(extra or {})
        if self.token:
            h['Authorization'] = 'Bearer ' + self.token
        try:
            h['X-ZC-Pass'] = self._ensure_pass()
        except Exception:
            pass  # 服务端若关闭人机验证，不带也能通过
        return h

    def request(self, method, path, retry=True, **kw):
        url = path if path.startswith('http') else self.base + path
        r = self.session.request(method, url, headers=self._headers(kw.pop('headers', None)),
                                 timeout=self.timeout, **kw)
        # 444 = 票据失效/认证未通过；重取票据重试一次
        if r.status_code == 444 and retry:
            self.refresh_pass()
            return self.request(method, path, retry=False, **kw)
        return r

    def get(self, path, **kw):
        return self.request('GET', path, **kw)

    def post(self, path, **kw):
        return self.request('POST', path, **kw)

    def put(self, path, **kw):
        return self.request('PUT', path, **kw)

    def delete(self, path, **kw):
        return self.request('DELETE', path, **kw)

    def json(self, method, path, **kw):
        """直接取 JSON，非 2xx 抛异常"""
        r = self.request(method, path, **kw)
        if not (200 <= r.status_code < 300):
            raise ZcbotClientError(f'HTTP {r.status_code}: {r.text[:200]}')
        return r.json()


def _main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 1
    base, token, path = sys.argv[1], sys.argv[2], sys.argv[3]
    c = ZcbotClient(base, token)
    r = c.get(path)
    print(f'HTTP {r.status_code}')
    try:
        print(json.dumps(r.json(), ensure_ascii=False, indent=2)[:2000])
    except Exception:
        print(r.text[:2000])
    return 0


if __name__ == '__main__':
    sys.exit(_main())
