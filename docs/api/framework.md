# Framework 核心对象

> **本篇面向**：角色 C（需要触碰底层容器的高级开发者）。绝大多数插件只用 `ctx` 即可，不必读本篇。

`Framework`（`framework/core.py`）是整个宿主的运行容器，插件里通过
`ctx._framework` 拿到它的引用。绝大多数插件只需要 `ctx`，本页供需要访问
底层能力（服务注册表、加载器、事件循环等）的高级场景参考。

## 主要属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `fw.config` | `dict` | 加载并合并 `core_plugins.yaml` 后的全局配置 |
| `fw.config_path` | `str` | 实际使用的配置文件绝对路径 |
| `fw.services` | `ServiceRegistry` | 服务注册表，详见 [ServiceRegistry](./services.md) |
| `fw.db` | `Database` | 数据库实例，详见 [数据库](../advanced/database.md) |
| `fw.event_bus` | `EventBus` | 事件总线（`subscribe/aemit/emit`） |
| `fw.router` | `MessageRouter` | 消息路由器 |
| `fw.plugin_loader` | `PluginLoader` | 插件加载器（加载/卸载/重载/发现） |
| `fw.terminal` | `TerminalInput` | 内置终端交互 |
| `fw.stats_writer` | `AsyncStatsWriter` | 统计批量写库器 |
| `fw.loop` | `asyncio.AbstractEventLoop` | 主事件循环（启动后可用） |

## 服务别名属性

为兼容旧代码，常用服务也提供了属性快捷方式（等价于 `fw.services.get(...)`）：

| 属性 | 等价 |
|------|------|
| `fw.api_caller` | `fw.services.get("api_caller")` |
| `fw.ws_server` | `fw.services.get("ws_server")` |
| `fw.scheduler` | `fw.services.get("scheduler")` |
| `fw.web_server` | `fw.services.get("web_server")` |

会话管理器、Web 服务等统一从 `fw.services.get("session_manager")`、
`fw.services.get("web_server")` 获取。

## 生命周期

```python
fw = Framework(config_path)   # 初始化：配置、日志、数据库、事件总线、加载器
await fw.start()              # 加载官方插件 → 用户插件 → 注册 → 心跳/看门狗
await fw.stop()               # 反序停止：WebSocket、调度器、Web、数据库
```

`start()` 的精确顺序见 [架构详解 - 启动时序](../advanced/architecture.md)。

## 典型用法

### 在主事件循环上调度协程

```python
async def background():
    ...
fw.loop.create_task(background())
```

### 直接访问数据库（不推荐，优先 ctx.db_*）

```python
fw.db.query_one("SELECT COUNT(*) AS c FROM commands")
```

### 操作插件加载器

```python
fw.plugin_loader.unload_plugin("my_plugin")
ok = fw.plugin_loader.load_plugin("my_plugin")
if ok:
    fw.plugin_loader.register_commands("my_plugin")
```

插件模块的加载/命名/热重载机制见
[插件加载与模块机制](../advanced/loader.md)。

### 跨插件事件

```python
await fw.event_bus.aemit("my_custom_event", {"k": "v"})
```

## 注意事项

:::warning
- 插件初始化（模块顶层、`register`）阶段，部分服务可能尚未就绪，
  请延迟到 handler 内或监听 `system.plugin.loaded` 后再取；
- 不要在插件里缓存 `fw.services.get(...)` 的结果作为模块级常量，
  服务可能因重载而替换，用时再取或判空；
- 直接操作 `plugin_loader`/`db` 属于高级用法，注意异常处理与状态一致性。
:::
