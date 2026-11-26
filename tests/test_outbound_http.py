"""出站 HTTP 默认头：别用 Python 默认 UA 去撞 WAF。

背景：curl 通、框架不通，响应是 403 + Cloudflare error 1010——因为默认 UA 是
``Python-urllib/3.x``，被当成脚本拦掉。这里锁住"出站请求带浏览器 UA"这条口径，
避免以后又有人裸写 urllib.request.Request。
"""
import urllib.request

from framework import net


def test_default_headers_has_browser_ua():
    headers = net.default_headers()
    assert headers['User-Agent'] == net.BROWSER_UA
    assert headers['User-Agent'].startswith('Mozilla/5.0')
    assert 'Accept' in headers


def test_caller_headers_win():
    headers = net.default_headers({'User-Agent': 'my-agent/1.0', 'X-Extra': '1'})
    assert headers['User-Agent'] == 'my-agent/1.0'
    assert headers['X-Extra'] == '1'
    # 没覆盖的默认项仍在
    assert 'Accept-Language' in headers


def test_build_request_sets_ua_and_body():
    req = net.build_request('https://example.com/api', data=b'{}', method='POST')
    assert isinstance(req, urllib.request.Request)
    assert req.get_header('User-agent') == net.BROWSER_UA
    assert req.data == b'{}'
    assert req.get_method() == 'POST'


def test_remote_urllib_call_sites_use_default_headers():
    """三处 urllib 出站（aiwriter LLM / 遥测 / 崩溃告警）都走 build_request。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for rel in ('core_plugins/aiwriter/llm.py',
                'framework/telemetry.py',
                'framework/crash_guard.py'):
        text = (root / rel).read_text(encoding='utf-8')
        assert 'net.build_request' in text or '_net.build_request' in text, rel
        assert 'urllib.request.Request(' not in text, f'{rel} 仍在裸构造 Request'
