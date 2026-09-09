# PluginContext (ctx)

`ctx` 是插件与框架交互的唯一入口，传递给 `register(ctx)` 函数。

## 命令注册

### ctx.command()

```python
ctx.command(
    pattern: str,              # 正则表达式或命令名
    handler: Callable,         # 处理函数 (event, match) -> None
    priority: int = 50,        # 优先级，越小越优先
    dynamic: bool = False,     # 是否动态命令（仅展示，不路由）
    alias: str = None,         # 别名："/h,/help" 或 ["/h", "/help"]
    description: str = None,   # 命令描述
    require_admin: bool = False,
    require_superuser: bool = False,
    require_perm: str = None,  # 权限节点
)
```

**用法**：

```python
def register(ctx):
    ctx.command("/hello", handle_hello, description="打招呼")
    ctx.command("/echo", handle_echo, alias="/e", description="回显")
    ctx.command("/admin_only", handle_admin, require_admin=True)
```

**匹配规则**：
- 简单命令名（如 `/hello`）：前缀匹配
- 正则模式（含 `^$.*+?` 等）：`re.search()` 匹配
- `match.group(1)` 统一表示命令后的参数

## 消息发送

### ctx.send_msg()

```python
ctx.send_msg(
    user_id: int = None,       # 私聊目标
    group_id: int = None,      # 群聊目标
    message: str = None,       # 消息内容
    auto_escape: bool = False, # 不解析 CQ 码
    bot: str = None,           # 指定 bot 实例
)
```

### ctx.asend_msg()

异步版本，推荐在 `async def` 中使用：

```python
await ctx.asend_msg(
    user_id=event.user_id,
    group_id=event.group_id if event.is_group else None,
    message="Hello!"
)
```

### 其他快捷方法

| 方法 | 说明 |
|------|------|
| `ctx.ban(group_id, user_id, duration=600)` | 禁言（0 解禁） |
| `ctx.kick(group_id, user_id)` | 踢出群成员 |
| `ctx.mute_all(group_id, enable=True)` | 全员禁言 |
| `ctx.set_card(group_id, user_id, card)` | 设置群名片 |
| `ctx.get_member_list(group_id)` | 获取群成员列表 |
| `ctx.get_member_info(group_id, user_id)` | 获取群成员信息 |

## 数据库

### ctx.db_query()

```python
rows = ctx.db_query(
    "SELECT * FROM users WHERE group_id = %s",
    (event.group_id,)
)
```

### ctx.db_query_one()

```python
row = ctx.db_query_one(
    "SELECT * FROM users WHERE user_id = %s",
    (event.user_id,)
)
```

### ctx.db_execute()

```python
affected = ctx.db_execute(
    "UPDATE users SET nickname = %s WHERE user_id = %s",
    ("新昵称", event.user_id)
)
```

### ctx.db_insert()

```python
new_id = ctx.db_insert(
    "INSERT INTO records (user_id, score) VALUES (%s, %s)",
    (event.user_id, 100)
)
```

### 异步版本

```python
rows = await ctx.db_query_async(sql, params)
row = await ctx.db_query_one_async(sql, params)
affected = await ctx.db_execute_async(sql, params)
```

### ctx.create_table()

```python
ctx.create_table("""
    CREATE TABLE IF NOT EXISTS my_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        value TEXT
    )
""")
```

自动适配 SQLite 和 MySQL 方言。

## 事件

### ctx.on()

```python
ctx.on("message", on_message)
ctx.on("notice.group_increase", on_member_join)
```

### ctx.emit()

```python
ctx.emit("user_sign_in", {"user_id": event.user_id})
```

### ctx.on_raw_message()

```python
def register(ctx):
    ctx.on_raw_message(my_handler)

async def my_handler(raw_event, bot_name):
    # raw_event 是原始 OneBot 事件 dict
    # 返回 True 接管消息，返回 None/False 继续
    pass
```

## 配置读取

### ctx.get_config()

```python
api_key = ctx.get_config("api_key", default="")
timeout = ctx.get_config("timeout", default=30)
```

### ctx.get_all_config()

```python
config = ctx.get_all_config()
# {"api_key": "xxx", "timeout": 30}
```

## 权限

### ctx.has_perm()

```python
if ctx.has_perm(user_id, 'myplugin.ban'):
    # 有权限
    pass
```

### ctx.check_perm()

```python
result = ctx.check_perm(user_id, 'myplugin.use')
# True / False / None
```

### 快捷方法

```python
ctx.is_superuser(user_id)
ctx.is_group_admin(group_id, user_id)
ctx.is_group_owner(group_id, user_id)
ctx.get_user_role(group_id, user_id)
```

## 多轮会话

### ctx.wait_for()

```python
reply = await ctx.wait_for(event, prompt="请输入：", timeout=60)
```

### ctx.create_session()

```python
async with ctx.create_session(event, timeout=120) as sess:
    name = await sess.ask("你叫什么？")
    sess.data['name'] = name
```

## 定时任务

### ctx.task()

```python
ctx.task("0 8 * * *", daily_report, description="每日报告")
ctx.task("*/5 * * * *", check_status, description="状态检查")
```

cron 格式：`分 时 日 月 周`

## 日志

### ctx.log()

```python
ctx.log("插件已加载")
ctx.log("出错了", level="error")
```

## 数据目录

### ctx.get_data_dir()

```python
data_dir = ctx.get_data_dir()
# 返回 plugins_dat/<plugin_name>/ 的绝对路径
```

## 仪表盘卡片

### ctx.dashboard_card()

```python
def register(ctx):
    ctx.dashboard_card("在线用户", get_online_count, icon="👥")

def get_online_count():
    row = ctx.db_query_one("SELECT COUNT(*) as cnt FROM users WHERE online = 1")
    return {"title": "在线用户", "value": row['cnt'], "label": "人"}
```

## WebUI 扩展

### ctx.webui()

```python
def register(ctx):
    ctx.webui(title="我的面板", entry="index.html", icon="⚙️", order=10)
```

### ctx.override_webui()

```python
ctx.override_webui()  # 接管整个 Web 前端
```

### Flask 路由注册

**重要：Flask `add_url_rule()` 只能在首次请求前调用。** 心跳重载时再注册会报错。

正确做法——在 `register(ctx)` 中直接注册：

```python
def register(ctx):
    app = ctx._framework.web_server.app
    if app:
        app.add_url_rule("/api/my/data", endpoint="my_data",
                         view_func=my_handler, methods=["GET"])
```

错误做法——在心跳/定时任务中注册（会抛 `add_url_rule` 异常）。

## 数据库操作

| 方法 | 说明 |
|------|------|
| `ctx.db_query(sql, params)` | 查询多条 |
| `ctx.db_query_one(sql, params)` | 查询单条 |
| `ctx.db_execute(sql, params)` | 执行写操作 |
| `ctx.db_insert(sql, params)` | 插入并返回 ID |
| `ctx.db_execute_many(sql, params_list)` | 批量执行 |
| `ctx.db_connection()` | 获取连接（事务用） |
| `ctx.create_table(ddl)` | 建表（自动适配方言） |
