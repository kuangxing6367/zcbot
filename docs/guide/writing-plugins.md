# 编写插件

本章从零开始，手把手教你写一个完整的 ZCBOT 插件。

## 插件目录结构

每个插件是一个 `plugins/` 下的子目录，必须包含 `main.py`：

```
plugins/
└── my_plugin/
    ├── main.py           # 插件入口（必须）
    ├── plugin.yaml       # 插件元信息（推荐）
    └── _conf_schema.json # 配置 schema（可选）
```

## 最小插件

创建 `plugins/hello/main.py`：

```python
__plugin_meta__ = {
    "name": "Hello",
    "version": "1.0.0",
    "author": "你的名字",
    "desc": "一个简单的 Hello 插件",
    "priority": 50,
}

def register(ctx):
    ctx.command("/hello", handle_hello, description="打个招呼")

def handle_hello(event, match):
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message="Hello, World!"
    )
```

重启框架或在 Web 面板点击「重载」，发送 `/hello` 即可看到回复。

## 逐行讲解

### `__plugin_meta__`：插件身份证

```python
__plugin_meta__ = {
    "name": "Hello",           # 显示名（Web 面板用）
    "version": "1.0.0",        # 版本号
    "author": "你的名字",       # 作者
    "desc": "一个简单的插件",    # 一句话描述
    "priority": 50,            # 优先级（越小越先加载）
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | ✅ | 插件显示名 |
| `version` | ✅ | 语义化版本号 |
| `author` | ✅ | 作者名 |
| `desc` | 否 | 一句话描述 |
| `priority` | 否 | 加载优先级，默认 50 |

### `register(ctx)`：注册入口

```python
def register(ctx):
    ctx.command("/hello", handle_hello, description="打个招呼")
```

**`register` 是框架规定的唯一入口**，插件加载时框架调用它一次。

- `ctx.command()` 注册命令
- `ctx.task()` 注册定时任务
- `ctx.on()` 订阅事件
- `ctx.on_raw_message()` 注册原始消息处理器

### 处理函数签名

```python
def handle_hello(event, match):
    # event: 消息事件对象
    # match: 命令匹配结果，match.group(1) 为参数
    pass
```

| 参数 | 说明 |
|------|------|
| `event` | 消息事件，包含 `user_id`、`group_id`、`message` 等 |
| `match` | 匹配结果，`match.group(1)` 取命令后的参数 |

## 注册多个命令

```python
def register(ctx):
    ctx.command("/hello", handle_hello, description="打招呼")
    ctx.command("/time", handle_time, description="查看时间")
    ctx.command("/help_me", handle_help, alias="/h", description="帮助")
```

## 异步处理函数

```python
async def handle_hello(event, match):
    # 异步 handler 可以 await
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message="Hello!"
    )
```

:::tip 提示
推荐使用 `async def`，不阻塞事件循环，性能更好。
:::

## 命令参数

```python
async def handle_echo(event, match):
    text = match.group(1).strip() if match else ""
    if not text:
        await ctx.asend_msg(..., message="请提供要回显的文字")
        return
    await ctx.asend_msg(..., message=text)
```

用户发送 `/echo Hello World`，`match.group(1)` 返回 `"Hello World"`。

## 事件订阅

```python
def register(ctx):
    ctx.on("message", on_message)

async def on_message(payload):
    """收到任意消息时触发"""
    # payload 是 Event 对象
    pass
```

## 停止事件传播

```python
async def handle_hello(event, match):
    event.stop_event()  # 阻止后续插件收到此消息
    await ctx.asend_msg(..., message="已处理")
```

## 完整示例：每日签到

```python
__plugin_meta__ = {
    "name": "每日签到",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "每日签到领积分",
    "priority": 50,
}

def register(ctx):
    ctx.command("/签到", handle_sign, description="每日签到")
    ctx.command("/积分", handle_score, description="查看积分")

async def handle_sign(event, match):
    today = __import__("time").strftime("%Y-%m-%d")
    
    # 检查是否已签到
    row = await ctx.db_query_one_async(
        "SELECT id FROM sign_records WHERE user_id = %s AND day = %s",
        (event.user_id, today)
    )
    if row:
        await ctx.asend_msg(..., message="今天已签到过了")
        return
    
    # 随机积分
    import random
    score = random.randint(1, 10)
    await ctx.db_execute_async(
        "INSERT INTO sign_records (user_id, day, score) VALUES (%s, %s, %s)",
        (event.user_id, today, score)
    )
    await ctx.asend_msg(..., message=f"签到成功！获得 {score} 积分")

async def handle_score(event, match):
    row = await ctx.db_query_one_async(
        "SELECT SUM(score) as total FROM sign_records WHERE user_id = %s",
        (event.user_id,)
    )
    total = row['total'] if row and row['total'] else 0
    await ctx.asend_msg(..., message=f"当前积分：{total}")
```

## 下一步

- [多轮会话](./session.md) — 交互式对话
- [配置系统](./configuration.md) — 让插件支持 Web 配置
- [API 参考](../api/) — ctx 全部方法
