# -*- coding: utf-8 -*-
"""framework.plugin 的 IIPC 兼容实现：模块级装饰器一等公民 API。

与框架原版 framework/plugin.py 的差异仅在「登记去向」：原版记入框架 _PENDING、
由框架在 register(ctx) 时 flush；IIPC 版直接记入 SDK 注册表，由 run_plugin 在
调用完 register(ctx) 后 flush——对插件代码完全透明，写法一字不改。

支持（与原版签名逐一对应）：
    command / on / on_message / on_raw_message / hook / task / api /
    dashboard_card / webui / override_webui / group_extension / user_extension
"""
import inspect
import threading

_lock = threading.Lock()
# module_name -> [(kind, kwargs, handler), ...]（与原版 _PENDING 同构）
_PENDING = {}


def _record(kind, handler, **kwargs):
    if handler is not None:
        mod = getattr(handler, '__module__', None)
    else:
        frame = inspect.currentframe().f_back.f_back
        mod = frame.f_globals.get('__name__') if frame else None
    if not mod:
        mod = '__unknown__'
    with _lock:
        _PENDING.setdefault(mod, []).append((kind, kwargs, handler))


def flush(module_name, ctx):
    """把某插件模块的待应用注册项落到 ctx（run_plugin 在 register(ctx) 后调用）。

    匹配模块本身及其子模块；重载时重新导入会再次登记、再次应用，不重复累积。
    """
    with _lock:
        matched = {k: v for k, v in _PENDING.items()
                   if k == module_name or k.startswith(module_name + '.')}
        for k in matched:
            del _PENDING[k]
    for items in matched.values():
        for kind, kwargs, handler in items:
            _apply(kind, kwargs, handler, ctx)


def clear_module(module_name):
    with _lock:
        for k in list(_PENDING.keys()):
            if k == module_name or k.startswith(module_name + '.'):
                _PENDING.pop(k, None)


def _apply(kind, kwargs, handler, ctx):
    try:
        if kind == 'command':
            ctx.command(handler=handler, **kwargs)
        elif kind == 'on':
            ctx.on(handler=handler, **kwargs)
        elif kind == 'on_raw_message':
            ctx.on_raw_message(handler)
        elif kind == 'hook':
            ctx.hook(handler=handler, **kwargs)
        elif kind == 'task':
            ctx.task(executor=handler, **kwargs)
        elif kind == 'api':
            ctx.register_api(handler=handler, **kwargs)
        elif kind == 'dashboard_card':
            ctx.dashboard_card(handler=handler, **kwargs)
        elif kind == 'webui':
            ctx.webui(**kwargs)
        elif kind == 'override_webui':
            ctx.override_webui()
        elif kind == 'group_extension':
            ctx.register_group_extension(handler=handler, **kwargs)
        elif kind == 'user_extension':
            ctx.register_user_extension(handler=handler, **kwargs)
    except Exception:
        # 单个注册项失败不影响其余项（与原版 _apply 的容错一致）
        pass


# ── 装饰器 API（签名与原版一致）─────────────────────────────────

def command(pattern, priority=50, dynamic=False, alias=None, description=None,
            require_admin=False, require_superuser=False, require_perm=None):
    def deco(func):
        _record('command', func, pattern=pattern, priority=priority, dynamic=dynamic,
                alias=alias, description=description, require_admin=require_admin,
                require_superuser=require_superuser, require_perm=require_perm)
        return func
    return deco


def on(event_name):
    def deco(func):
        _record('on', func, event_name=event_name)
        return func
    return deco


def on_message(func):
    _record('on', func, event_name='message')
    return func


def on_raw_message(func):
    _record('on_raw_message', func)
    return func


def hook(point, priority=50):
    def deco(func):
        _record('hook', func, point=point, priority=priority)
        return func
    return deco


def task(cron_expr, description=None):
    def deco(func):
        _record('task', func, cron_expr=cron_expr, description=description)
        return func
    return deco


def api(path, methods=None, auth=True, description=None):
    def deco(func):
        _record('api', func, path=path, methods=methods, auth=auth, description=description)
        return func
    return deco


def dashboard_card(title, icon=None, priority=50):
    def deco(func):
        _record('dashboard_card', func, title=title, icon=icon, priority=priority)
        return func
    return deco


def webui(title, entry='index.html', icon=None, order=50, sidebar=False):
    _record('webui', None, title=title, entry=entry, icon=icon, order=order, sidebar=sidebar)


def override_webui():
    _record('override_webui', None)


def group_extension(key, title, ext_type='column'):
    def deco(func):
        _record('group_extension', func, key=key, title=title, ext_type=ext_type)
        return func
    return deco


def user_extension(key, title, ext_type='column'):
    def deco(func):
        _record('user_extension', func, key=key, title=title, ext_type=ext_type)
        return func
    return deco
