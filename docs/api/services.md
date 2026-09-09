# ServiceRegistry 服务注册表

服务注册表（`framework/protocol.py → ServiceRegistry`）是框架核心与官方插件之间的
解耦层：核心不直接 import 官方插件，官方插件在 `register(ctx)` 时把能力“注册”进来，
用户插件按需“取用”。

## 为什么需要它

像“调用 OneBot 发消息”这种能力由 `core_plugins/onebot_adapter` 提供，
而该插件可以被禁用。直接 import 会造成硬依赖；通过服务注册表，插件可以
优雅降级：服务在就用，不在就提示“部分功能不可用”。

## API

| 方法 | 说明 |
|------|------|
| `services.register(name, service)` | 注册（重复注册会覆盖并告警），一般只有官方插件用 |
| `services.get(name, default=None)` | 取服务，**不存在或被禁用时返回 None** |
| `services.has(name) -> bool` | 是否注册过（注意：禁用的官方插件会注册 `None`，判空更稳妥） |
| `services.remove(name)` | 移除服务 |
| `services.all() -> dict` | 全部服务的副本 |

在插件里通过 `ctx._framework.services` 访问：

```python
services = ctx._framework.services
api = services.get("api_caller")
if api is None:
    ctx.log("协议适配器未启用", level="warning")
```

## 内置服务清单

| 服务名 | 提供者 | 类型/能力 |
|--------|--------|-----------|
| `protocol_adapter` | onebot_adapter | `ProtocolAdapter` 实现，协议层抽象 |
| `api_caller` | onebot_adapter | API 调用器，`.call(action, **kw)` / `.acall(...)` |
| `onebot_api` | onebot_adapter | 面向对象的 OneBot API 封装（即 `ctx.onebot`） |
| `ws_server` | onebot_adapter | WebSocket 服务端实例 |
| `scheduler` | scheduler | APScheduler 封装（`add_plugin_task`、`get_jobs` 等） |
| `session_manager` | session | 多轮会话管理器（支撑 `ctx.wait_for/create_session`） |
| `web_server` | webui | 管理后台 Web 服务 |
| `http_api` | http_api | 独立 HTTP API 服务（默认关闭） |

:::warning 禁用的服务会注册为 None
官方插件被 `core_plugins.xxx: false` 关闭时，会用 `None` 占位注册。
因此判断“能不能用”请用 `if services.get("scheduler"):`，
而不是只看 `services.has("scheduler")`。
:::

## 典型用法

### 1. handler 里直接用 ctx 快捷封装（首选）

绝大多数场景 `ctx.send_msg / ctx.aapi / ctx.task / ctx.wait_for` 已经够用，
不需要直接碰服务注册表。

### 2. 任务函数等没有 ctx 的场景

定时任务函数不接收参数，需要能力时从服务取：

```python
def daily_report():
    caller = ctx._framework.services.get("api_caller")
    if caller:
        caller.call("send_group_msg", group_id=123456, message="日报")
```

### 3. 等服务就绪后再初始化

插件加载顺序不保证官方服务已就绪，监听加载完成事件最稳妥：

```python
def register(ctx):
    ctx.on("system.plugin.loaded", on_all_loaded)

def on_all_loaded(_payload):
    if ctx._framework.services.get("api_caller"):
        ctx.log("协议层已就绪")
```

## 注册自定义服务（高级）

如果你的插件本身就是“基础设施提供方”，也可以注册服务供别的插件使用：

```python
def register(ctx):
    ctx._framework.services.register("my_cache", MyCache())
```

取用方约定好服务名与接口契约即可；插件卸载时记得
`services.remove("my_cache")`（可在 `on_unload` 中处理）。
