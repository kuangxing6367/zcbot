# -*- coding: utf-8 -*-
"""插件声明式注册 API（模块级装饰器）。

原生一等公民写法，与 ``ctx.command(...)`` 等实例方法等价，只是把「收集」提前到模块
顶层、由框架在 ``register(ctx)`` 时统一落库，省去在 ``register`` 函数体里反复调用
``ctx.xxx(handler)`` 的样板。

用法（插件 main.py 顶层）：

    from framework.plugin import command, on, on_message, on_raw_message, hook, task, api, dashboard_card

    @command("/help", description="查看帮助")
    def help_handler(event, match):
        ...

    @on("member.join")
    def on_join(event):
        ...

    @hook("command.before")
    def before_command(event, ctx):
        ...

    @task("0 0 * * *")
    def daily_job():
        ...

所有装饰器在导入期只做「登记」，不触碰框架；框架加载插件、调用 ``register(ctx)`` 时
通过 :func:`flush` 把登记项应用到当前插件的 ``ctx`` 上（与直接在 ``register`` 里调用
``ctx.command(...)`` 完全等价）。未使用本 API 的旧插件不受影响（``flush`` 对该模块为空操作）。
"""
import inspect
import logging
import threading

logger = logging.getLogger('zcbot')

# module_name -> [(kind, kwargs, handler_or_None), ...]
_PENDING = {}
_PENDING_LOCK = threading.Lock()


def _record(kind, handler, **kwargs):
    """把一条注册项登记到所属模块的待应用列表。

    handler 非空时用其 ``__module__`` 定位模块；无 handler 的直接调用（如 ``webui``）
    用调用者帧的 ``__name__`` 定位。二者都指向「定义/调用所在的插件模块」。
    """
    if handler is not None:
        mod = getattr(handler, '__module__', None)
    else:
        # 直接调用型（webui / override_webui 无 handler）：需上溯两层帧——
        # 本帧(_record) → 装饰器函数帧(webui) → 调用者（插件模块）帧
        frame = inspect.currentframe().f_back.f_back
        mod = frame.f_globals.get('__name__') if frame else None
    if not mod:
        mod = '__unknown__'
    with _PENDING_LOCK:
        _PENDING.setdefault(mod, []).append((kind, kwargs, handler))


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
        else:
            logger.warning(f"未知插件注册类型: {kind}")
    except Exception as e:
        logger.warning(f"插件装饰器注册失败（{kind}）: {e}")


def flush(module_name, ctx):
    """把某插件模块的待应用注册项落到 ctx（框架在 ``register(ctx)`` 时调用，内部 API）。

    匹配 module_name 本身及其子模块（如 ``plugin_foo.sub``），避免多文件插件漏注册；
    已应用的项会从待注册表移除，重载时重新导入会再次登记、再次应用，不会重复累积。
    """
    with _PENDING_LOCK:
        matched = {k: v for k, v in _PENDING.items()
                   if k == module_name or k.startswith(module_name + '.')}
        for k in matched:
            del _PENDING[k]
    for items in matched.values():
        for kind, kwargs, handler in items:
            _apply(kind, kwargs, handler, ctx)


def clear_module(module_name):
    """清除某模块的待应用注册项（插件卸载/重载清理用，内部 API）"""
    with _PENDING_LOCK:
        for k in list(_PENDING.keys()):
            if k == module_name or k.startswith(module_name + '.'):
                _PENDING.pop(k, None)


# ── 装饰器 / 直接调用 API ──────────────────────────────────────────

def command(pattern, priority=50, dynamic=False, alias=None, description=None,
            require_admin=False, require_superuser=False, require_perm=None):
    """注册命令（等价于 ``ctx.command``）。handler 签名 ``(event, match)``。"""
    def deco(func):
        _record('command', func, pattern=pattern, priority=priority, dynamic=dynamic,
                alias=alias, description=description, require_admin=require_admin,
                require_superuser=require_superuser, require_perm=require_perm)
        return func
    return deco


def on(event_name):
    """订阅系统事件（等价于 ``ctx.on``）。handler 签名随事件而定。"""
    def deco(func):
        _record('on', func, event_name=event_name)
        return func
    return deco


def on_message(func):
    """订阅 message 事件（内容监听型插件常用，等价于 ``@on('message')``）。"""
    _record('on', func, event_name='message')
    return func


def on_raw_message(func):
    """注册原始消息处理器（等价于 ``ctx.on_raw_message``）。"""
    _record('on_raw_message', func)
    return func


def hook(point, priority=50):
    """在内核扩展点挂接处理器（等价于 ``ctx.hook``）。"""
    def deco(func):
        _record('hook', func, point=point, priority=priority)
        return func
    return deco


def task(cron_expr, description=None):
    """注册定时任务（等价于 ``ctx.task``）。"""
    def deco(func):
        _record('task', func, cron_expr=cron_expr, description=description)
        return func
    return deco


def api(path, methods=None, auth=True, description=None):
    """注册自定义 REST 路由（等价于 ``ctx.register_api``）。handler 为 Flask 视图函数。"""
    def deco(func):
        _record('api', func, path=path, methods=methods, auth=auth, description=description)
        return func
    return deco


def dashboard_card(title, icon=None, priority=50):
    """注册仪表盘卡片（等价于 ``ctx.dashboard_card``）。"""
    def deco(func):
        _record('dashboard_card', func, title=title, icon=icon, priority=priority)
        return func
    return deco


def webui(title, entry='index.html', icon=None, order=50, sidebar=False):
    """注册插件 WebUI 页面（等价于 ``ctx.webui``，直接调用、不装饰函数）。"""
    _record('webui', None, title=title, entry=entry, icon=icon, order=order, sidebar=sidebar)


def override_webui():
    """接管整个 Web 前端（等价于 ``ctx.override_webui``，直接调用）。"""
    _record('override_webui', None)


def group_extension(key, title, ext_type='column'):
    """注册 WebUI 群组管理页扩展（等价于 ``ctx.register_group_extension``）。"""
    def deco(func):
        _record('group_extension', func, key=key, title=title, ext_type=ext_type)
        return func
    return deco


def user_extension(key, title, ext_type='column'):
    """注册 WebUI 用户管理页扩展（等价于 ``ctx.register_user_extension``）。"""
    def deco(func):
        _record('user_extension', func, key=key, title=title, ext_type=ext_type)
        return func
    return deco
