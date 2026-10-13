# -*- coding: utf-8 -*-
"""
蜜罐探针（本地调陷阱逻辑用）
============================
模拟两类「自动化脚本」，观察蜜罐的反应，方便反复调试：

  direct  —— 直连脚本：GET /admin 后直接 POST（不带令牌）→ 应被立即封 IP
  follow  —— 跟提示的脚本：GET /admin 解析出下发令牌，提交 1024 位（256 hex）→ 应被立即封
  scan    —— 扫描器：GET 一批噪声节点，观察状态码分布（应多数非 200）

用法：
  python tools/probe_honeypot.py --url http://127.0.0.1:8080 --mode all

注意：陷阱命中会封来源 IP（内网/白名单豁免）。建议对测试实例运行，或先把自己
      的 IP 加进白名单，避免把自己封掉。
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request

_NOISE_SAMPLE = [
    '/wp-admin/admin-ajax.php', '/health', '/api/v1/exec', '/.env.dev',
    '/dashboard/', '/api/internal/status', '/api/v1/callback', '/nginx_status',
    '/.git/index', '/jenkins/script',
]


def _req(url, method='GET', data=None):
    body = None
    headers = {'User-Agent': 'zcbot-probe/1.0'}
    if data is not None:
        body = json.dumps(data).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def mode_direct(base):
    print("\n[direct] 直连脚本：GET /admin → POST /admin（无令牌）")
    st, _ = _req(base + '/admin', 'GET')
    print(f"  GET  /admin        -> {st}")
    st, _ = _req(base + '/admin', 'POST', {})
    print(f"  POST /admin (空)   -> {st}   （预期：403 / 已被封）")


def mode_follow(base):
    print("\n[follow] 跟提示脚本：GET /admin → 解析令牌 → POST 1024 位（256 hex）")
    st, body = _req(base + '/admin', 'GET')
    text = body.decode('utf-8', 'replace')
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', text)
    if not m:
        print(f"  GET  /admin -> {st}，未解析到下发令牌（可能已被封 / 页面结构变了）")
        return
    token = m.group(1)
    print(f"  GET  /admin        -> {st}，下发令牌 {len(token)} hex")
    # 跟提示：按「一半长度」构造一个令牌提交（服务端不透露长度，这里只是模拟脚本行为）
    st, _ = _req(base + '/admin/login', 'POST', {'token': token[: len(token) // 2]})
    print(f"  POST 半长令牌      -> {st}   （预期：403 / 已被封）")


def mode_scan(base):
    print("\n[scan] 扫描器：GET 一批噪声节点，观察状态码")
    from collections import Counter
    dist = Counter()
    for p in _NOISE_SAMPLE:
        st, _ = _req(base + p, 'GET')
        dist[st] += 1
        print(f"  {p:34s} -> {st}")
    print(f"  状态码分布: {dict(sorted(dist.items()))}   （预期：几乎不含 200）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default='http://127.0.0.1:8080', help='ZCBOT 服务地址')
    ap.add_argument('--mode', default='all',
                    choices=['direct', 'follow', 'scan', 'all'])
    args = ap.parse_args()
    base = args.url.rstrip('/')

    if args.mode in ('direct', 'all'):
        mode_direct(base)
    if args.mode in ('follow', 'all'):
        mode_follow(base)
    if args.mode in ('scan', 'all'):
        mode_scan(base)
    print("\n提示：命中陷阱会封来源 IP（内网/白名单豁免）。")


if __name__ == '__main__':
    sys.exit(main())
