"""出站 HTTP 的统一口径：浏览器 UA 与默认请求头。

存在的理由
==========
Python 的默认 UA 是 ``Python-urllib/3.x`` / ``python-requests/2.x``。Cloudflare、
Akamai 这类 WAF 会把它当脚本直接拦掉——实测表现为 **HTTP 403 + error code 1010**
（"The owner of this website has banned your browser's signature"）。curl 能通、
框架里跑不通，就是这条。

单点修在某个插件里（例如 aiwriter 请求 LLM 端点）只能救那一条链路；同类问题会在
遥测上报、崩溃告警 webhook、插件市场下载、框架自更新等处各踩一次。所以这里提供
一份默认头，出站请求统一从这里取。

用法::

    from framework import net

    # urllib
    req = net.build_request(url, data=body, headers={'Content-Type': 'application/json'})
    urllib.request.urlopen(req, timeout=15)

    # requests
    requests.get(url, headers=net.default_headers({'Accept': 'application/json'}), timeout=15)

不需要伪装成浏览器的内部调用（本机控制通道、内网接口）可以显式传自己的 UA；
``default_headers`` 只提供默认值，调用方传的键永远优先。
"""
import urllib.request

# 取一个长期稳定的桌面 Chrome UA：太新的版本号反而更容易被指纹规则盯上，
# 这里固定主版本号，避免"版本号每年变一次导致行为漂移"。
BROWSER_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)

# 多数 JSON 接口与静态资源都能接受的一组默认 Accept / Accept-Language，
# 缺了它们，部分 WAF 会把"只有 UA 没有 Accept"的请求判成脚本。
DEFAULT_HEADERS = {
    'User-Agent': BROWSER_UA,
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}


def default_headers(extra=None) -> dict:
    """默认请求头 + 调用方覆盖项（调用方的键优先）。"""
    headers = dict(DEFAULT_HEADERS)
    for key, value in (extra or {}).items():
        if value is None:
            continue
        headers[str(key)] = str(value)
    return headers


def build_request(url, *, data=None, method=None, headers=None) -> urllib.request.Request:
    """构造带默认头的 urllib 请求（替代裸 ``urllib.request.Request``）。"""
    return urllib.request.Request(
        url, data=data, method=method, headers=default_headers(headers))
