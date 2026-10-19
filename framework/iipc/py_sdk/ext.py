#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内核扩展面的插件侧封装：服务注册表、插件间调用、共享 KV、指标、能力声明、依赖注入。

设计取向：
  - **零强制**：不注册这些能力，插件行为与以前完全一致。
  - **懒加载**：HTTP 客户端等重型依赖到真正调用时才 import，不进启动路径。
  - **按名注入**：handler 参数名命中即注入，不要求插件改签名、不要求类型注解。

典型用法::

    @ctx.command("/weather")
    def weather(event, logger, http, kv):
        # logger / http / kv 由内核侧装配，插件只声明要用什么
        data = http.get("https://api.example.com/weather").json()
        kv.set("last_city", data["city"], ttl=600)
        ctx.send_msg(group_id=event.group_id, message=data["desc"])
"""
import inspect
import json
import threading

# 内核回调插件的固定方法名（与 go_kernel/protocol.go 的 MethodPluginInvoke 对齐）
PLUGIN_INVOKE = "plugin.invoke"


# ───────────────────────── 依赖注入 ─────────────────────────

class Depends(object):
    """显式依赖声明：作为 handler 参数的默认值使用。

    例::

        def get_db():
            return my_pool.connect()

        @ctx.command("/q")
        def q(event, db=Depends(get_db)):
            ...
    """

    __slots__ = ("factory", "cached", "name", "_value", "_done")

    def __init__(self, factory, cached=True, name=None):
        self.factory = factory
        self.cached = cached
        self.name = name or getattr(factory, "__name__", "anon")
        self._value = None
        self._done = False

    def resolve(self):
        if self.cached:
            if not self._done:
                self._value = self.factory()
                self._done = True
            return self._value
        return self.factory()


class ConfigProxy(object):
    """配置读取代理：拿到的是当前插件命名空间的配置。"""

    def __init__(self, ctx):
        self._ctx = ctx

    def get(self, key, default=None):
        try:
            v = self._ctx.get_config(key, default)
        except Exception:
            return default
        return default if v is None else v

    def set(self, key, value):
        try:
            return self._ctx.set_config(key, value)
        except Exception:
            return False

    def __getitem__(self, key):
        return self.get(key)


class KVProxy(object):
    """内核托管共享 KV：跨插件共享状态，带 TTL。

    语义是共享缓存而不是持久存储——内核重启即清空，要落库请用 db.*。
    """

    def __init__(self, ctx):
        self._ctx = ctx

    def get(self, key, default=None):
        try:
            r = self._ctx._client.call("kv.get", {"key": key}, timeout=5)
        except Exception:
            return default
        if not r or not r.get("found"):
            return default
        return r.get("value")

    def set(self, key, value, ttl=0):
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        try:
            self._ctx._client.call("kv.set", {"key": key, "value": value, "ttl": int(ttl)}, timeout=5)
            return True
        except Exception:
            return False

    def delete(self, key):
        try:
            self._ctx._client.call("kv.del", {"key": key}, timeout=5)
            return True
        except Exception:
            return False

    def incr(self, key, delta=1):
        try:
            r = self._ctx._client.call("kv.incr", {"key": key, "delta": delta}, timeout=5)
            return (r or {}).get("value")
        except Exception:
            return None

    def keys(self, prefix=""):
        try:
            r = self._ctx._client.call("kv.keys", {"prefix": prefix}, timeout=5)
            return (r or {}).get("keys", [])
        except Exception:
            return []

    # 字典式便利
    def __getitem__(self, key):
        return self.get(key)

    def __setitem__(self, key, value):
        self.set(key, value)


class _LazyHTTP(object):
    """HTTP 客户端：首次调用时才决定用哪个库，不进启动路径。

    优先级 httpx > requests > urllib（stdlib 兜底）。只暴露最常用的几个动词，
    避免把整个客户端 API 面搬进内核约定。
    """

    def __init__(self):
        self._mode = None
        self._client = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._mode:
            return self._mode
        with self._lock:
            if self._mode:
                return self._mode
            try:
                import httpx  # noqa: F401
                self._mode = "httpx"
            except ImportError:
                try:
                    import requests  # noqa: F401
                    self._mode = "requests"
                except ImportError:
                    self._mode = "urllib"
        return self._mode

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def request(self, method, url, **kw):
        mode = self._ensure()
        timeout = kw.pop("timeout", 15)
        if mode == "httpx":
            import httpx
            return httpx.request(method, url, timeout=timeout, **kw)
        if mode == "requests":
            import requests
            return requests.request(method, url, timeout=timeout, **kw)
        from urllib import request as _u
        data = kw.pop("json", None)
        if data is not None:
            data = json.dumps(data).encode("utf-8")
            kw.setdefault("headers", {})["Content-Type"] = "application/json"
        req = _u.Request(url, data=data, method=method,
                         headers=kw.pop("headers", {}))
        return _u.urlopen(req, timeout=timeout)


# 内置可注入名 → 工厂（参数名命中即注入）
def _builtin_injectors(ctx):
    http = _LazyHTTP()
    return {
        "ctx": lambda: ctx,
        "logger": lambda: ctx.logger,
        "log": lambda: ctx.logger,
        "cfg": lambda: ConfigProxy(ctx),
        "config": lambda: ConfigProxy(ctx),
        "kv": lambda: KVProxy(ctx),
        "cache": lambda: KVProxy(ctx),
        "http": lambda: http,
        "db": lambda: ctx,
    }


def bind_args(fn, args, ctx=None):
    """按 handler 签名装配实参。

    关键规则：**依赖参数不占用位置实参**。
    `(event, logger, kv)` 里 logger 是依赖，位置实参只喂给非依赖参数，
    否则 (event, match) 调用会把 match 错塞进 logger。

    兼容性保证：老插件 `(event, match)` 签名行为完全不变——两个参数都不是
    依赖名，顺序填充，与不启用注入时一致。
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return args
    params = list(sig.parameters.values())
    if not params:
        return ()
    has_var = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
    positional = [p for p in params
                  if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    if not positional:
        return args if has_var else ()

    injectors = _builtin_injectors(ctx) if ctx is not None else {}
    is_dep = [isinstance(p.default, Depends) or p.name in injectors
              or (isinstance(p.annotation, str) and p.annotation in injectors)
              for p in positional]

    # 没有任何依赖声明 → 走原路径（含"实参多于形参则截断"的老兼容行为）
    if not any(is_dep):
        values = list(args)
        if not has_var and len(values) > len(positional):
            values = values[:len(positional)]
        return tuple(values)

    values = []
    ai = 0
    for i, p in enumerate(positional):
        if is_dep[i]:
            values.append(_resolve_one(p, injectors))
            continue
        if ai < len(args):
            values.append(args[ai])
            ai += 1
        else:
            values.append(None if p.default is inspect.Parameter.empty else p.default)
    # 多余的实参（如 match）不丢弃会报 TypeError，这里按需截断
    if has_var:
        values.extend(args[ai:])
    return tuple(values)


def _resolve_one(p, injectors):
    # 1) 显式 Depends 默认值优先
    if isinstance(p.default, Depends):
        return p.default.resolve()
    # 2) 参数名命中内置依赖
    if p.name in injectors:
        try:
            return injectors[p.name]()
        except Exception:
            return None
    # 3) 注解名命中（如 def h(event, logger: Logger)）
    ann = p.annotation
    if isinstance(ann, str) and ann in injectors:
        try:
            return injectors[ann]()
        except Exception:
            return None
    # 4) 有默认值就用默认值，否则 None（不让插件因为少个参数就崩）
    return None if p.default is inspect.Parameter.empty else p.default


# ──────────────────── 延迟声明（握手时序问题） ────────────────────
# 插件的 register(ctx) 早于 plugin.register 握手，此刻内核还不认插件 ID，
# service.register / capability.declare 会被拒。这里先排队，握手后重放。


def _queue_decl(ctx, method, payload):
    q = getattr(ctx, "_ext_pending", None)
    if q is None:
        q = []
        ctx._ext_pending = q
    q.append((method, payload))


def flush_pending(ctx):
    """重放排队中的声明（SDK 在 plugin.register 成功后调用）。返回成功条数。"""
    q = list(getattr(ctx, "_ext_pending", None) or [])
    if not q:
        return 0
    ctx._ext_pending = []
    done = 0
    for method, payload in q:
        try:
            ctx._client.call(method, payload, timeout=5)
            done += 1
        except Exception as e:
            try:
                ctx.logger.warning("延迟声明 %s 失败: %s" % (method, e))
            except Exception:
                pass
    return done


# ───────────────────────── 扩展面挂载 ─────────────────────────

def attach(cls):
    """把扩展面方法挂到 PluginContext 上（保持 sdk.py 主体不被撑大）。"""

    def provide(self, name, version="", caps=None, meta=None):
        """声明本插件对外提供的服务。别的插件按名字解析，不必 import 本插件。

        可在 register(ctx) 里直接调用：此刻内核还没完成 plugin.register 握手，
        声明会先排队，握手成功后由 SDK 自动重放。
        """
        payload = {"name": name, "version": version,
                   "caps": list(caps or []), "meta": meta or {}}
        try:
            self._client.call("service.register", payload, timeout=5)
        except Exception:
            _queue_decl(self, "service.register", payload)
        return True

    def resolve_service(self, name=None, cap=None):
        """解析服务：按名（name）或按能力（cap）反查。"""
        try:
            return self._client.call("service.resolve", {"name": name or "", "cap": cap or ""}, timeout=5)
        except Exception as e:
            self.logger.warning("服务解析失败: %s" % e)
            return {"found": False}

    def services(self):
        try:
            r = self._client.call("service.list", {}, timeout=5)
            return (r or {}).get("services", [])
        except Exception:
            return []

    def expose(self, action=None):
        """把函数暴露给别的插件调用（配合 ctx.call_plugin）。

        用法::

            @ctx.expose("translate")
            def _t(params):
                return {"text": do_translate(params["text"])}

            # 别的插件：ctx.call_plugin("my_plugin", "translate", {"text": "hi"})
        """
        def deco(fn):
            name = action or fn.__name__

            def _dispatch(params):
                payload = params or {}
                # 入参既支持 {"action","params"} 信封，也支持裸 params
                inner = payload.get("params", payload)
                return fn(inner)

            table = getattr(self, "_exposed", None)
            if table is None:
                table = {}
                self._exposed = table
                # 内核回调入口只需注册一次
                if not getattr(self, "_invoke_hooked", False):
                    self._client.register(PLUGIN_INVOKE, self._on_invoke)
                    self._invoke_hooked = True
            table[name] = _dispatch
            return fn

        # 允许 @ctx.expose（无参）与 @ctx.expose("name")
        if callable(action):
            fn = action
            action = None
            return deco(fn)
        return deco

    def _on_invoke(self, params):
        table = getattr(self, "_exposed", {}) or {}
        name = (params or {}).get("action", "")
        fn = table.get(name)
        if fn is None:
            raise KeyError("未暴露的动作: %s" % name)
        return fn(params)

    def call_plugin(self, plugin, action, params=None, timeout=10):
        """调用另一个插件暴露的动作；对方不在线会抛异常。"""
        r = self._client.call("plugin.call", {
            "plugin": plugin, "action": action,
            "params": params or {}, "timeout_ms": int(timeout * 1000),
        }, timeout=timeout + 5)
        if not r or not r.get("ok"):
            raise RuntimeError("调用 %s/%s 失败" % (plugin, action))
        return r.get("result")

    def metric(self, name, value=1.0):
        """上报一个观测值（count/sum/min/max/last 聚合）。"""
        try:
            self._client.call("metric.observe", {"name": str(name), "value": float(value)}, timeout=5)
            return True
        except Exception:
            return False

    def metrics(self):
        try:
            r = self._client.call("metric.snapshot", {}, timeout=5)
            return (r or {}).get("metrics", {})
        except Exception:
            return {}

    def declare_caps(self, caps):
        """能力自述：告诉内核与其他插件"我能做什么"。同样支持 register 期声明。"""
        payload = {"caps": list(caps)}
        try:
            self._client.call("capability.declare", payload, timeout=5)
        except Exception:
            _queue_decl(self, "capability.declare", payload)
        return True

    def who_has(self, cap):
        """反查具备某能力的插件列表。"""
        try:
            r = self._client.call("capability.query", {"cap": cap}, timeout=5)
            return (r or {}).get("plugins", [])
        except Exception:
            return []

    def plugins(self):
        """当前在线插件清单。"""
        try:
            r = self._client.call("plugin.list", {}, timeout=5)
            return (r or {}).get("plugins", [])
        except Exception:
            return []

    def transform(self, priority=50, action="transform", handler=None):
        """注册一个全局变换节点（节点间数据变换的等价物）。

        内核在把任意事件扇出给所有订阅者之前，按 priority 升序调用各变换节点，
        把插件返回的 event.data 作为新的事件数据往下走；返回 None 则全局丢弃该事件。
        所有订阅者都看到变换后的事件，而无需每个插件各自实现一遍——脱敏、翻译、
        字段补全、协议归一都适合落在这里。

        用法::

            @ctx.transform(priority=10)
            def normalize(params):
                # params 即 event.data（dict）；返回改写后的 dict，或 None 丢弃
                params = dict(params or {})
                params["message"] = (params.get("message") or "").strip()
                return params

        等价于在内核级插入一个"变换节点"：注册阶段声明，握手后由内核自动重放，
        插件断开自动回收。
        """
        if handler is None:
            def deco(fn):
                self.transform(priority=priority, action=action, handler=fn)
                return fn
            return deco
        # 暴露动作（内核经 plugin.invoke 回调本插件取变换结果）
        self.expose(action)(handler)
        payload = {"plugin": self.plugin_id, "action": action, "priority": int(priority)}
        try:
            self._client.call("pipeline.transform", payload, timeout=5)
        except Exception:
            _queue_decl(self, "pipeline.transform", payload)
        return handler

    # 显式挂载：不靠遍历 locals()，避免把辅助函数误挂成 API
    cls.provide = provide
    cls.resolve_service = resolve_service
    cls.services = services
    cls.expose = expose
    cls._on_invoke = _on_invoke
    cls.call_plugin = call_plugin
    cls.metric = metric
    cls.metrics = metrics
    cls.declare_caps = declare_caps
    cls.who_has = who_has
    cls.plugins = plugins
    cls.transform = transform
    cls.kv = property(_kv_of)
    cls.config = property(_cfg_of)
    cls.http = property(_http_of)
    return cls


def _kv_of(self):
    v = getattr(self, "_kv_proxy", None)
    if v is None:
        v = KVProxy(self)
        self._kv_proxy = v
    return v


def _cfg_of(self):
    v = getattr(self, "_cfg_proxy", None)
    if v is None:
        v = ConfigProxy(self)
        self._cfg_proxy = v
    return v


def _http_of(self):
    v = getattr(self, "_http_client", None)
    if v is None:
        v = _LazyHTTP()
        self._http_client = v
    return v
