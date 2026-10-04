# 插件装饰器 API

框架原生的声明式注册写法。把命令、事件、任务、WebUI 等的注册信息直接写在函数定义处，
由框架在加载插件时统一落库，与在 `register(ctx)` 里调用 `ctx.xxx(...)` **完全等价**，只是更简洁。

所有装饰器从同一个模块导入：

```python
from framework.plugin import (
    command, on, on_message, on_raw_message, hook,
    task, api, dashboard_card,
    webui, override_webui, group_extension, user_extension,
)
```

## 工作机制

1. 装饰器在**模块导入期只做登记**（记录「类型 + 参数 + handler」到模块级缓冲），不触碰框架。
2. 框架加载插件、调用 `register(ctx)` 时，通过内部 `flush(module_name, ctx)` 把本模块登记项统一应用到 `ctx`。
3. 未使用本 API 的旧插件 `flush` 为空操作，完全不受影响；多文件插件（子模块）也会一并匹配应用。

因此你可以**省略 `register`**，也可以保留它做建表、权限初始化等一次性工作——二者共存时 `register` 照常执行。

## 装饰器一览

### @command(pattern, ...)

注册命令，等价于 `ctx.command(pattern, handler, ...)`。

```python
@command("/签到", alias="/sign", description="每日签到", require_perm="sign.use")
def handle_sign(event, match):
    ...
```

参数与 [ctx.command](/ctx#二命令注册) 一致：`priority`、`dynamic`、`alias`、`description`、`require_admin`、`require_superuser`、`require_perm`。
`dynamic=True` 时该命令仅展示、不参与路由（见 [静态命令与动态命令](/ctx#静态命令与动态命令dynamic-参数)）。

### @on(event_name) / @on_message / @on_raw_message

```python
@on("notice.group_increase")
def on_join(payload):
    ...

@on_message
def listen_text(event, match):
    ...

@on_raw_message
def raw(raw_event: dict, bot_name: str):
    return True   # 接管整条消息
```

- `@on(event_name)` 等价于 `ctx.on(event_name, handler)`；
- `@on_message` 等价于 `@on("message")`；
- `@on_raw_message` 等价于 `ctx.on_raw_message(handler)`（命令匹配之前触发）。

### @hook(point, priority=50)

在内核扩展点挂接处理器，等价于 `ctx.hook(point, handler, priority)`。可用扩展点见 [Hook 系统](/hooks)。

```python
@hook("command.before")
def before(event, ctx):
    ...
```

### @task(cron_expr, description=None)

注册定时任务，等价于 `ctx.task(cron_expr, executor, description)`。任务函数无参。

```python
@task("0 0 * * *", description="每日清理")
def reset_daily():
    ...
```

### @api(path, methods=None, auth=True, description=None)

注册自定义 REST 路由，等价于 `ctx.register_api(handler, path, ...)`。handler 为 Flask 视图函数。

```python
@api("/myplugin/stats", methods=["GET"])
def stats():
    return {"ok": True}
```

### @dashboard_card(title, icon=None, priority=50)

注册仪表盘卡片，等价于 `ctx.dashboard_card(handler, title, ...)`。handler 返回卡片数据 dict。

```python
@dashboard_card("在线用户", priority=10)
def get_count():
    return {"title": "在线用户", "value": 12, "label": "人"}
```

### webui / override_webui（直接调用，不装饰函数）

```python
webui("我的面板", entry="index.html", order=10, sidebar=True)
override_webui()
```

- `webui(...)` 等价于 `ctx.webui(...)`，在后台注册插件页面；`sidebar=True` 在侧边栏独立入口。
- `override_webui()` 等价于 `ctx.override_webui()`，让本插件整体接管后台前端。

### @group_extension(key, title, ext_type="column") / @user_extension(...)

在「群组管理 / 用户管理」页扩展一列或详情面板，等价于 `ctx.register_group_extension` / `ctx.register_user_extension`。

```python
@group_extension("sign_days", "签到天数")
def query_days(group_id):
    return "30 天"
```

## 与 ctx 实例方法对照表

| 装饰器 | 等价 ctx 调用 |
| ---- | ---- |
| `@command(...)` | `ctx.command(pattern, handler, ...)` |
| `@on(event)` | `ctx.on(event, handler)` |
| `@on_message` | `ctx.on("message", handler)` |
| `@on_raw_message` | `ctx.on_raw_message(handler)` |
| `@hook(point)` | `ctx.hook(point, handler, ...)` |
| `@task(cron)` | `ctx.task(cron, executor, ...)` |
| `@api(...)` | `ctx.register_api(handler, ...)` |
| `@dashboard_card(...)` | `ctx.dashboard_card(handler, ...)` |
| `webui(...)` | `ctx.webui(...)` |
| `override_webui()` | `ctx.override_webui()` |
| `@group_extension(...)` | `ctx.register_group_extension(handler, ...)` |
| `@user_extension(...)` | `ctx.register_user_extension(handler, ...)` |

完整 ctx 能力清单见 [PluginContext 参考](/ctx)。
