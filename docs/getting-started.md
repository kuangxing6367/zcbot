# 快速入门

一个最简单的插件只需要一个 `main.py` 文件：

```python
# plugins/hello/main.py
__plugin_meta__ = {
    "name": "Hello",
    "version": "1.0.0",
    "author": "your-name",
    "desc": "打招呼插件示例",
    "priority": 50,
}

def register(ctx):
    ctx.command("/hello", handle_hello, description="打招呼")

def handle_hello(event, match):
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message="你好！",
    )
```

将上述文件放到 `plugins/hello/main.py`，重启框架即可。框架启动时会自动扫描 `plugins/` 目录并加载所有插件。

## 异步支持

框架核心为全异步架构（消息处理不阻塞事件循环）。插件 handler 支持两种写法：

**异步 handler（推荐）** —— 使用 `async def`，配合异步 API 不占用线程：

```python
async def handle_weather(event, match):
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message="今天晴，气温 25℃",
    )
    await ctx.aapi("get_group_member_list", group_id=event.group_id)
    rows = await ctx.db_query_async("SELECT * FROM users WHERE user_id = %s", (event.user_id,))
```

**同步 handler（兼容旧插件）** —— 普通 `def` 依旧可用，框架会自动在线程池中执行，不会阻塞事件循环：

```python
def handle_echo(event, match):
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=match.group(1),
    )
```

> 同步 handler 内使用 `ctx.send_msg()` / `ctx.api()` / `ctx.db_query()` 等同步方法即可，框架内部会自动桥接到主事件循环。异步 handler 建议使用 `asend_msg()` / `aapi()` / `db_query_async()` 等异步方法以获得最佳性能。

## 异步 API 速查表

| 同步（兼容） | 异步（推荐） |
|---|---|
| `ctx.send_msg(...)` | `await ctx.asend_msg(...)` |
| `ctx.api(action, **params)` | `await ctx.aapi(action, **params)` |
| `ctx.onebot.send_group_msg(...)` | `await ctx.onebot.acall("send_group_msg", ...)` |
| `ctx.db_query(sql, params)` | `await ctx.db_query_async(sql, params)` |
| `ctx.db_execute(sql, params)` | `await ctx.db_execute_async(sql, params)` |
| `ctx.emit(event, payload)` | `await ctx.aemit(event, payload)` |
| `ctx.ban/kick/mute_all/set_card...` | `ctx.aban/akick/amute_all/aset_card...` |

定时任务 `ctx.task()` 的 executor 同样支持 `async def`。事件订阅 `ctx.on()` 的 handler 也支持 `async def`。
