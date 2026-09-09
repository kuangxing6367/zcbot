# ServiceRegistry 服务注册表

框架通过 `services` 注册表管理官方插件提供的服务。

## 方法

### register()

```python
services.register(service_name: str, service: Any)
```

注册服务。

**参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `service_name` | `str` | 服务名 |
| `service` | `Any` | 服务实例 |

### get()

```python
service = services.get(service_name: str) -> Any | None
```

获取服务实例。

**用法**：

```python
# 获取 OneBot API 调用器
api = ctx.services.get('api_caller')
if api:
    await api.send_group_msg(group_id=123456, message="hello")

# 获取调度器
scheduler = ctx.services.get('scheduler')
if scheduler:
    scheduler.add_cron_job(handler, "0 8 * * *")

# 获取会话管理器
session_mgr = ctx.services.get('session_manager')
```

### has()

```python
if services.has('scheduler'):
    # 调度器已加载
    pass
```

### list()

```python
for name in services.list():
    print(f"已注册服务: {name}")
```

## 内置服务

| 服务名 | 提供者 | 类型 | 说明 |
|--------|--------|------|------|
| `protocol_adapter` | onebot_adapter | `ProtocolAdapter` | 协议适配器接口 |
| `api_caller` | onebot_adapter | `ApiCaller` | OneBot API 调用 |
| `onebot_api` | onebot_adapter | `OneBotAPI` | OneBot API 封装 |
| `ws_server` | onebot_adapter | `OneBotWebSocketServer` | WebSocket 服务端 |
| `scheduler` | scheduler | `TaskScheduler` | APScheduler 封装 |
| `session_manager` | session | `SessionManager` | 会话管理器 |
| `web_server` | webui | `WebServer` | Flask Web 服务 |

## 在插件中使用服务

```python
def register(ctx):
    # 延迟检查服务可用性
    ctx.on("system.plugin.loaded", _setup)

async def _setup(_):
    api = ctx.services.get('api_caller')
    if api:
        ctx.log("OneBot API 可用")
    else:
        ctx.log("OneBot API 不可用，部分功能受限", level="warning")
```

:::tip 提示
服务可能在插件加载时尚未就绪，建议通过事件订阅等待 `system.plugin.loaded` 后获取。
:::
