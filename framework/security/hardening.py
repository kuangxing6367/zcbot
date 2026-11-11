# -*- coding: utf-8 -*-
"""HTTP / JSON / 事件流入的共享输入防护。

依据：
  · RFC 9112 §6.3 —— Content-Length 必须是单个合法十进制数，冲突/歧义值必须拒绝
  · RFC 8259 §4 / §6、Python json 文档 —— NaN/Infinity 与重复键不是合规 JSON，
    默认 json.loads 会接受它们（parse_constant / object_pairs_hook 可拒绝）
  · Python hmac 文档 —— 密钥比较应使用 compare_digest，避免短路定时分析
  · CWE-400 / OWASP API4:2023 —— 请求体大小、执行超时必须有上限
  · CWE-117 —— 未受控输入不应携带内部控制字段进入事件流

设计原则：fail-closed（校验失败即 4xx），且所有函数只依赖标准库，
core_plugins 以合成模块名加载时也能 import。
"""
import hmac
import json
import re

__all__ = [
    'InputRejected', 'DEFAULT_MAX_BODY_BYTES', 'DEFAULT_READ_TIMEOUT',
    'DEFAULT_DRAIN_LIMIT_BYTES', 'content_length_value',
    'parse_content_length', 'read_request_body', 'strict_json_object',
    'token_ok', 'sanitize_event', 'sanitize_adapter_event', 'apply_socket_timeout',
    'sanitize_log_text', 'scan_invisible_unicode', 'sanitize_log_detail',
    'coerce_int', 'coerce_text', 'coerce_sql_params',
]

DEFAULT_MAX_BODY_BYTES = 1024 * 1024        # 单请求体 1 MiB
DEFAULT_READ_TIMEOUT = 10.0                 # 慢速请求头/体的 socket 读超时（秒）
DEFAULT_DRAIN_LIMIT_BYTES = 8 * 1024 * 1024  # 超限请求最多排空 8 MiB 再回 413


class InputRejected(ValueError):
    """输入不合规；code 是建议返回给客户端的 HTTP 状态码。"""

    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def content_length_value(headers):
    """严格解析 Content-Length 语法，返回非负整数（不做大小上限判断）。

    旧实现是 `int(self.headers.get('Content-Length', 0))`：
      · 'abc'      -> ValueError 未捕获，连接直接断、零响应
      · '-1'       -> rfile.read(-1) 读到 EOF，挂起直到客户端超时
      · '999...'   -> 同样挂起，且连接关闭后把残留字节当成合法事件派发
      · 重复/逗号值 -> RFC 9112 §6.3 要求拒绝，否则形成请求走私歧义
    """
    try:
        values = headers.get_all('Content-Length', [])
    except AttributeError:
        v = headers.get('Content-Length')
        values = [] if v is None else [v]
    if len(values) > 1:
        raise InputRejected('duplicate Content-Length header')
    if not values:
        # 无 Content-Length：不读 socket（保持旧行为，视为空 body / 空事件）
        return 0
    raw = str(values[0]).strip()
    if not re.fullmatch(r'[0-9]+', raw):
        raise InputRejected('invalid Content-Length')
    return int(raw)


def parse_content_length(headers, max_bytes=DEFAULT_MAX_BODY_BYTES):
    """严格解析并施加大小上限；超限抛 413。"""
    length = content_length_value(headers)
    if length > max_bytes:
        raise InputRejected('request body too large (limit %d bytes)' % max_bytes, 413)
    return length


def read_request_body(handler, max_bytes=DEFAULT_MAX_BODY_BYTES,
                      drain_limit=DEFAULT_DRAIN_LIMIT_BYTES):
    """读取请求体，并在超限时把已发送的 body 排空后再回 413。

    若发现 Content-Length 超限就立刻回 413 并关闭连接，客户端仍在写大 body
    时会被 RST，拿不到响应（实测 2 MiB body 得到 BrokenPipe / 无响应）。
    排空有上界（默认 8 MiB）且受 socket 读超时保护，不会形成新的 Slowloris 面。
    """
    length = content_length_value(handler.headers)
    if length > max_bytes:
        remaining = min(length, drain_limit)
        while remaining > 0:
            try:
                chunk = handler.rfile.read(min(65536, remaining))
            except (OSError, TimeoutError):
                # 声明超大却不发 body 的畸形请求：排空中途断开也要稳定回 413，
                # 不能让 socket 异常冒泡成 400 或让连接静默关闭
                break
            if not chunk:
                break
            remaining -= len(chunk)
        raise InputRejected('request body too large (limit %d bytes)' % max_bytes, 413)
    if length == 0:
        return b''
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise InputRejected('incomplete request body')
    return raw


def _reject_constant(value):
    raise InputRejected('non-standard JSON constant: %s' % value)


def _no_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise InputRejected('duplicate JSON key: %s' % key)
        obj[key] = value
    return obj


def _reject_surrogates(value, depth=0):
    """孤立 UTF-16 代理项不是合法 Unicode 标量值，写日志/DB/JSON 时会崩。"""
    if depth > 32:
        raise InputRejected('JSON nesting too deep')
    if isinstance(value, str):
        for ch in value:
            if 0xD800 <= ord(ch) <= 0xDFFF:
                raise InputRejected('lone surrogate in JSON string')
    elif isinstance(value, dict):
        for k, v in value.items():
            _reject_surrogates(k, depth + 1)
            _reject_surrogates(v, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _reject_surrogates(v, depth + 1)


def strict_json_object(raw, max_bytes=DEFAULT_MAX_BODY_BYTES):
    """把请求体解析成 dict；拒绝 NaN/Infinity、重复键、顶层标量、非法 UTF-8、孤立代理。"""
    if isinstance(raw, str):
        # WebSocket 帧通常是 str；HTTP 请求体是 bytes
        if len(raw.encode('utf-8', 'surrogatepass')) > max_bytes:
            raise InputRejected('payload too large (limit %d bytes)' % max_bytes, 413)
        text = raw
    elif isinstance(raw, (bytes, bytearray)):
        if len(raw) > max_bytes:
            raise InputRejected('request body too large (limit %d bytes)' % max_bytes, 413)
        try:
            text = bytes(raw).decode('utf-8')
        except UnicodeDecodeError:
            raise InputRejected('invalid UTF-8 body')
    else:
        raise InputRejected('body must be bytes or str')
    try:
        data = json.loads(text, parse_constant=_reject_constant,
                          object_pairs_hook=_no_duplicate_keys)
    except InputRejected:
        raise
    except (ValueError, RecursionError) as e:
        raise InputRejected('invalid JSON: %s' % type(e).__name__)
    if not isinstance(data, dict):
        raise InputRejected('JSON body must be an object')
    _reject_surrogates(data)
    return data


def token_ok(received, expected, prefix='Bearer '):
    """常量时间 token 比较；兼容 `Bearer <t>` 与裸 `<t>` 两种写法。

    用 bytes 比较，避免 compare_digest 对含非 ASCII 的 str 抛 TypeError。
    """
    if not expected:
        return True
    if not isinstance(received, str) or not received:
        return False
    candidates = [received]
    if received.startswith(prefix):
        candidates.append(received[len(prefix):])
    target = str(expected).encode('utf-8')
    for cand in candidates:
        try:
            if hmac.compare_digest(cand.encode('utf-8', 'surrogatepass'), target):
                return True
        except (TypeError, ValueError):
            continue
    return False


# ── 结构化入参强类型校验（OWASP ASVS 4.0.3 V5.1.3 / V5.1.4、CWE-20）──
# 正例：允许列表 + 长度/范围上限；失败统一返回 None，由调用方回 400。
# 不用宽泛的 int(value)/str(value)：前者会把 '1_0'、Unicode 数字、空白偷偷
# 转成整数，后者会把 dict/list 变成 "{'a': 1}" 继续流向下游解释器。
_MAX_INT_DIGITS = 18
MAX_TEXT_LEN = 64 * 1024
MAX_SQL_PARAMS = 128


def coerce_int(value, minimum=None, maximum=None):
    """把外部数值收敛为 int；非法 / 越界返回 None。

    只接受 int（非 bool）、纯十进制字符串与整数值 float，拒绝 bool、
    容器、`NaN`、超大数字串与 `1_0` 这类 Python 独有字面量。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    elif isinstance(value, float):
        if not (value == value and value not in (float('inf'), float('-inf'))
                and value.is_integer()):
            return None
        n = int(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s or len(s.lstrip('+-')) > _MAX_INT_DIGITS:
            return None
        if not re.fullmatch(r'[+-]?[0-9]+', s):
            return None
        n = int(s)
    else:
        return None
    if minimum is not None and n < minimum:
        return None
    if maximum is not None and n > maximum:
        return None
    return n


def coerce_text(value, max_len=MAX_TEXT_LEN, allow_empty=False):
    """把外部文本收敛为 str；非字符串、超长、含 NUL 一律返回 None。"""
    if not isinstance(value, str):
        return None
    if not allow_empty and not value:
        return None
    if len(value) > max_len or '\x00' in value:
        return None
    return value


def coerce_sql_params(value):
    """SQL 参数必须是「有界的标量列表」；字符串/字典/NULL 都不能当参数集。

    旧实现 `tuple(params)` 对字符串会逐字符展开成 ('a','b','c')，对 dict
    会取键，属于典型的类型混淆（CWE-20 / ASVS 5.1.4）。
    """
    if not isinstance(value, (list, tuple)):
        return None
    if len(value) > MAX_SQL_PARAMS:
        return None
    for item in value:
        if isinstance(item, (dict, list, tuple, set, bytes, bytearray)):
            return None
        if isinstance(item, str) and len(item) > MAX_TEXT_LEN:
            return None
    return tuple(value)


# 事件流里不允许由外部输入携带的内部 / 凭证类字段（递归剥离）
_DENY_KEYS = frozenset({
    'role', 'roles', 'permission', 'permissions', 'is_super', 'super',
    'admin', 'admin_id', 'super_admin', 'token', 'api_key', 'apikey',
    'access_token', 'secret', 'password', 'passwd', 'pwd', 'session',
    'session_token', 'permanent_token', 'temp_token', 'framework',
    'services', 'ctx', 'db', 'config', '__class__', '__dict__',
    '__globals__', '__reduce__',
    # trace_id 是 EventBus 的 ContextVar 因果域内部生成值（event_bus.py:43），
    # 外部可写的同名键会污染日志关联与排障；第 7 轮 property 探针实测残留。
    'trace_id',
})
_MAX_EVENT_DEPTH = 8
_MAX_LIST_ITEMS = 1000
_MAX_STR_LEN = 64 * 1024


def sanitize_event(data, adapter_name, max_depth=_MAX_EVENT_DEPTH):
    """剥离内部控制字段、限制嵌套/长度，并强制事件归属为当前接入端。

    http_inject 对非 message 事件做 `event = dict(data)` 整包透传，外部可借此
    把 role/permissions/admin 等字段塞进事件流，也能伪造 bot_name 冒充其它接入端。
    """
    def walk(obj, depth):
        if depth > max_depth:
            return None
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if not isinstance(k, str) or k.startswith('_'):
                    continue
                if k.lower() in _DENY_KEYS:
                    continue
                out[k] = walk(v, depth + 1)
            return out
        if isinstance(obj, (list, tuple)):
            return [walk(v, depth + 1) for v in list(obj)[:_MAX_LIST_ITEMS]]
        if isinstance(obj, str) and len(obj) > _MAX_STR_LEN:
            return obj[:_MAX_STR_LEN]
        return obj

    event = walk(data if isinstance(data, dict) else {}, 0)
    if not isinstance(event, dict):
        event = {}
    # 归属字段不可由外部指定：避免跨接入端身份伪造
    event['bot_name'] = adapter_name
    return event


def _coerce_adapter_event_fields(event):
    """接入端事件的字段类型归一化（CWE-20 / OWASP ASVS 5.1.3）。

    实测第 7 轮：外部帧把 `message` 写成 dict、`user_id` 写成 dict、
    `sender` 写成数组时，净化只剥字段不改类型，事件照样派发：
      · `Event.__init__` 走 `_extract_text()` 的 `str(message)` 兜底，
        把整个 dict repr 当成消息文本，攻击者可借此伪造命令文本；
      · `normalize_message()` 把 dict 当单个消息段收下；
      · `Event.sender_role` / `sender_nickname` 直接 `self.sender.get(...)`，
        sender 是数组时抛 AttributeError。
    合法形态必须保留：message / raw_message 可以是 str 或 OneBot 段数组，
    各类 ID 可以是数字或字符串（Matrix 房间号、Discord 雪花 ID 都是字符串），
    sender 是 dict。其余容器/布尔一律收敛成中性值，而不是丢弃整条事件。
    """
    for key in ('message', 'raw_message'):
        if key not in event:
            continue
        value = event[key]
        if not isinstance(value, (str, list)):
            event[key] = ''
    sender = event.get('sender')
    if sender is not None and not isinstance(sender, dict):
        event['sender'] = {}
    for key, fallback in (('user_id', 0), ('group_id', None),
                          ('message_id', 0), ('self_id', 0)):
        if key in event and isinstance(event[key], (dict, list, tuple, bool)):
            event[key] = fallback
    return event


def sanitize_adapter_event(data, adapter_name, max_depth=_MAX_EVENT_DEPTH):
    """协议适配器（OneBot / Telegram / Discord …）专用的事件净化。

    与 `sanitize_event` 的差别：这些协议自身会带合法的 `sender` 字段（含
    `sender.role`），整包剥掉会让 `Event.sender_role` / `is_admin` 在降级路径上
    全部失真。所以先走同一套递归净化剥掉**顶层**伪造的内部控制字段
    （role / permissions / admin / bot_name / _msg_text / …）与所有下划线内部
    字段，再按 OneBot 11 的 sender 字段白名单恢复，role 只允许三种协议取值。

    最后再走 `_coerce_adapter_event_fields()` 收敛 message / sender / ID 的字段类型，
    避免类型混淆把 dict 变成消息文本或直接让 Event 构造抛异常。

    依据：CWE-915 外部控制字段注入、CWE-20 输入类型校验、CWE-117 日志/事件注入、
    RFC 8259 解析边界、OWASP ASVS 5.1.3。
    注意 `sender.role` 是**协议上报值**不是授权值；真正的身份仍应由 Event.role
    查库决定，这里只是不把合法协议字段误杀。
    """
    event = sanitize_event(data, adapter_name, max_depth=max_depth)
    raw = data.get('sender') if isinstance(data, dict) else None
    if isinstance(raw, dict):
        allowed = {}
        for key in ('user_id', 'nickname', 'card', 'sex', 'age', 'area',
                    'level', 'title', 'role'):
            if key not in raw:
                continue
            value = raw[key]
            if key == 'role':
                if value not in ('owner', 'admin', 'member'):
                    continue
            elif isinstance(value, str) and len(value) > _MAX_STR_LEN:
                value = value[:_MAX_STR_LEN]
            allowed[key] = value
        if allowed:
            event['sender'] = allowed
    return _coerce_adapter_event_fields(event)


def apply_socket_timeout(handler, seconds=DEFAULT_READ_TIMEOUT):
    """给 BaseHTTPRequestHandler 的连接加读超时，回收 Slowloris 线程。

    http.server 默认无 socket 超时；ThreadingHTTPServer 每个慢连接永久占一个线程。
    配合 daemon_threads=True 使用后，超时线程会被 socketserver 回收。
    """
    try:
        handler.connection.settimeout(max(0.5, float(seconds)))
    except (TypeError, ValueError, OSError, AttributeError):
        pass


# ── 第 6 轮：日志输出净化（CWE-117 / OWASP Logging Cheat Sheet） ──────────
# 实测修复前：外部消息里的 \r\n 可以伪造整行日志，ESC 序列能操控终端，
# NUL/C0 控制符、bidi override 与零宽字符全部原样进入 log_broker 缓存。
# 依据：CWE-117 Improper Output Neutralization for Logs、CWE-93 CRLF、
# OWASP Logging Cheat Sheet「sanitize all event data」、
# CVE-2021-42574 Trojan Source / UAX #9 / UTS #39。
# 关键取舍：只改「日志输出」，不改事件原文。阿拉伯语、波斯语 ZWNJ、
# emoji ZWJ 序列、CJK、变体选择符全部原样保留，避免误伤正常语言。
_MAX_LOG_VALUE_CHARS = 4096


def scan_invisible_unicode(text):
    """返回日志文本里的隐形/双向控制符类别（只报告，不改原文）。"""
    flags = set()
    for ch in (text or ''):
        cp = ord(ch)
        if 0x202A <= cp <= 0x202E or 0x2066 <= cp <= 0x2069:
            flags.add('bidi_control')
        elif cp in (0x200B, 0x200E, 0x200F, 0x2060, 0xFEFF, 0x180E):
            flags.add('zero_width_or_bidi_mark')
        elif 0xE0000 <= cp <= 0xE007F:
            flags.add('unicode_tag_char')
        elif 0xE0100 <= cp <= 0xE01EF:
            flags.add('variation_selector_supplement')
    return sorted(flags)


def sanitize_log_text(value, max_len=_MAX_LOG_VALUE_CHARS):
    """把不可信文本转成单行、可见、有界的日志字符串。

    · CR/LF/TAB/NUL/C0/DEL/C1/ESC → 可见转义（消除日志伪造与终端操控）
    · bidi / 零宽 / tag 控制符 → ``<U+XXXX>`` 可见标记（不静默删除，
      避免阿拉伯语与 emoji 被破坏，同时让审计者看见「这里有猫腻」）
    · 超长值截断并加省略标记
    """
    if value is None:
        return ''
    if not isinstance(value, str):
        value = str(value)
    out = []
    for ch in value[:max_len]:
        cp = ord(ch)
        if ch == '\r':
            out.append('\\r')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\t':
            out.append('\\t')
        elif cp < 32 or 0x7F <= cp <= 0x9F:
            out.append('\\x%02x' % cp)
        elif (0x202A <= cp <= 0x202E or 0x2066 <= cp <= 0x2069
              or cp in (0x200B, 0x200E, 0x200F, 0x2060, 0xFEFF, 0x180E)
              or 0xE0000 <= cp <= 0xE007F
              or 0xE0100 <= cp <= 0xE01EF):
            out.append('<U+%04X>' % cp)
        else:
            out.append(ch)
    text = ''.join(out)
    if len(value) > max_len:
        text += '…[truncated %d chars]' % (len(value) - max_len)
    return text


def sanitize_log_detail(detail):
    """净化 detail dict 里的字符串值（顶层 + 一层嵌套），并附隐形字符标记。"""
    if not isinstance(detail, dict):
        return {}, []
    out = {}
    flags = set()
    for k, v in detail.items():
        if isinstance(v, str):
            out[k] = sanitize_log_text(v)
            flags.update(scan_invisible_unicode(v))
        elif isinstance(v, dict):
            sub = {}
            for sk, sv in list(v.items())[:50]:
                if isinstance(sv, str):
                    sub[sk] = sanitize_log_text(sv)
                    flags.update(scan_invisible_unicode(sv))
                else:
                    sub[sk] = sv
            out[k] = sub
        else:
            out[k] = v
    if flags:
        out['_unicode_flags'] = sorted(flags)
    return out, sorted(flags)
