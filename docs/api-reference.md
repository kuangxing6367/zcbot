# API 参考

`register(ctx)` 函数接收的 `ctx` 是 `PluginContext` 实例，提供所有框架能力。

## 命令注册

```python
ctx.command(
    pattern: str,              # 正则表达式或命令名（主匹配模式）
    handler: Callable,         # 处理函数 (event, match) -> None
    priority: int = 50,        # 优先级，越小越优先
    dynamic: bool = False,     # 是否动态命令（仅展示标记，不参与路由）
    alias: str = None,         # 别名，逗号分隔字符串或列表："/h,/help"
    description: str = None,   # 命令描述（未填时自动取 handler docstring 首行）
    require_admin: bool = False,       # 需群管理员/群主权限
    require_superuser: bool = False,   # 需框架超管权限
)
```

**匹配规则**：
- 简单命令名（如 `/ping`）：纯字符串前缀匹配，返回 `SimpleMatch` 对象
- 正则模式（含 `^$.*+?()[]{}` 等元字符）：使用 `re.search()` 匹配，返回 `re.Match` 对象
- `match.group(1)` 统一表示命令后的参数文本

**handler 签名**：

```python
def handle_xxx(event: Event, match):
    """
    命令描述（自动提取为 description）

    :param event: 消息事件对象，包含 user_id/group_id/message 等
    :param match: 匹配结果，match.group(1) 为命令参数
    """
    text = match.group(1).strip() if match else ""
    # 处理逻辑...
```

**停止事件传播**：

```python
def handle_xxx(event, match):
    event.stop_event()  # 阻止后续插件收到此消息
    # 如果想让后续插件继续处理，handler 返回 False 或不调用 stop_event()
```

## 消息发送

最常用的快捷方法：

```python
# 自动判断私聊/群聊
ctx.send_msg(
    user_id=event.user_id,
    group_id=event.group_id if event.is_group else None,
    message="Hello",
)

# 发送富媒体（CQ 码）
ctx.send_msg(
    group_id=123456,
    message="[CQ:image,file=https://example.com/img.jpg]",
)
```

其他快捷方法：

| 方法                                       | 说明                                  |
| ------------------------------------------ | ------------------------------------- |
| `ctx.ban(group_id, user_id, duration=600)` | 禁言（duration=0 解禁）               |
| `ctx.kick(group_id, user_id)`              | 踢出群成员                            |
| `ctx.mute_all(group_id, enable=True)`      | 全员禁言/解禁                         |
| `ctx.set_card(group_id, user_id, card)`    | 设置群名片                            |
| `ctx.get_member_list(group_id)`            | 获取群成员列表                        |
| `ctx.get_member_info(group_id, user_id)`   | 获取群成员信息                        |

## OneBot 11 标准 API

通过 `ctx.onebot` 访问完整的 38 个 OneBot 11 标准 API：

```python
ctx.onebot.send_private_msg(user_id=123456, message="私聊消息")
ctx.onebot.send_group_msg(group_id=123456, message="群消息")
ctx.onebot.delete_msg(message_id=123456)
ctx.onebot.set_group_ban(group_id=123456, user_id=789012, duration=600)
ctx.onebot.set_group_kick(group_id=123456, user_id=789012, reject_add_request=False)
ctx.onebot.set_group_leave(group_id=123456)
ctx.onebot.get_group_list()
ctx.onebot.get_group_member_list(group_id=123456)
```

非标准/扩展 API 使用 `ctx.api()` 兜底：

```python
ctx.api("set_group_special_title", group_id=123456, user_id=789012, title="大佬")
```

完整 OneBot 11 API 列表见 [OneBot 11 标准](https://github.com/botuniverse/onebot-11/blob/master/api/public.md)。

## 数据库操作

ZCBOT 自动适配 SQLite 和 MySQL，插件无需关心差异（统一用 `%s` 占位符）：

```python
# 查询多条
rows = ctx.db_query(
    "SELECT id, name FROM users WHERE group_id = %s ORDER BY id",
    (event.group_id,)
)

# 查询单条
row = ctx.db_query_one(
    "SELECT * FROM users WHERE user_id = %s",
    (event.user_id,)
)

# 插入/更新/删除
affected = ctx.db_execute(
    "UPDATE users SET nickname = %s WHERE user_id = %s",
    ("新昵称", event.user_id)
)

# 插入并获取自增 ID
new_id = ctx.db_insert(
    "INSERT INTO sign_in (user_id, sign_date) VALUES (%s, %s)",
    (event.user_id, "2026-01-01")
)

# 批量执行
ctx.db_execute_many(
    "INSERT INTO records (user_id, score) VALUES (%s, %s)",
    [(1, 80), (2, 90), (3, 85)]
)
```

**事务控制**（高级用法）：

```python
conn = ctx.db_connection()
try:
    cursor = conn.cursor()
    cursor.execute("INSERT ...")
    cursor.execute("UPDATE ...")
    conn.commit()
except Exception:
    conn.rollback()
finally:
    cursor.close()
    conn.close()  # 归还到池
```

> MySQL 模式下 `NOW()` 会自动转为当前时间参数；`ON DUPLICATE KEY UPDATE` 会自动转译为 `ON CONFLICT DO UPDATE`。插件 SQL 直接按 MySQL 语法写即可。

## 配置读取

插件配置由 Web UI 通过 `_conf_schema.json` 定义并存储在 `plugin_configs` 表中：

```python
timeout = ctx.get_config("request_timeout", default=10)
max_retry = ctx.get_config("max_retry", default=3)

# 读取全部配置
config = ctx.get_all_config()
print(config)  # {"request_timeout": 10, "max_retry": 3, ...}
```

## 定时任务

注册基于 cron 表达式的定时任务：

```python
def register(ctx):
    ctx.task("0 8 * * *", daily_report, description="每日签到统计")
    ctx.task("*/5 * * * *", check_status, description="状态检查")

def daily_report():
    # 注意：定时任务 handler 不接收 event 参数
    ctx.logger.info("开始生成每日报告...")

def check_status():
    pass
```

cron 表达式使用 5 字段标准格式：`分 时 日 月 周`。

## 事件总线

插件之间可以通过事件总线通信：

```python
def register(ctx):
    ctx.on("user_sign_in", on_sign_in)
    ctx.on("group_member_increase", on_new_member)
    ctx.emit("custom_event", {"key": "value"})

def on_sign_in(payload):
    user_id = payload.get("user_id")
    ctx.logger.info(f"用户 {user_id} 签到了")

def on_new_member(payload):
    group_id = payload.get("group_id")
    user_id = payload.get("user_id")
    ctx.send_msg(group_id=group_id, message=f"欢迎新成员 {user_id}")
```

## 权限判断

```python
def handle_ban(event, match):
    if not event.is_admin:
        ctx.send_msg(group_id=event.group_id, message="仅管理员可执行此命令")
        return

    if event.is_superuser:
        pass  # 超管逻辑

    # 完整身份等级：super > owner > admin > member > blacklist
    role = event.role
    if role in ("super", "owner", "admin"):
        pass  # 执行管理操作
```

ctx 也提供权限快捷方法（适用于非消息场景）：

```python
ctx.is_superuser(user_id=123456)
ctx.is_group_admin(group_id=123456, user_id=789012)
ctx.is_group_owner(group_id=123456, user_id=789012)
ctx.is_blacklisted(user_id=123456)
ctx.get_user_role(group_id=123456, user_id=789012)  # 返回 "super"/"owner"/...
```

## 群级插件开关

```python
ctx.enable_plugin_in_group("my_plugin", group_id=123456)
ctx.disable_plugin_in_group("my_plugin", group_id=123456)

if ctx.is_plugin_enabled_in_group("my_plugin", group_id=123456):
    pass

status = ctx.get_plugin_status_list(group_id=123456)
# {"echo": True, "ipquery": False, ...}
```

## 日志与审计

```python
# 标准日志（带插件名前缀）
ctx.logger.info("开始处理")
ctx.logger.warning("配置缺失，使用默认值")
ctx.logger.error("请求失败")
ctx.logger.debug("调试信息")

# 简易日志
ctx.log("处理完成", level="info")

# 审计日志（记录到数据库 audit_logs 表，可在 Web UI 查看）
ctx.audit_log(
    action="sign_in",
    target_type="user",
    target_name=str(event.user_id),
    detail={"score": 10, "continuous": 5},
    result="success",
)
```

## 仪表盘卡片

注册一个在 Web UI 仪表盘展示的卡片：

```python
def register(ctx):
    ctx.dashboard_card("签到统计", get_signin_stats, icon="chart", priority=10)

def get_signin_stats():
    """返回 dict: {title, value, label, icon, color}"""
    count = ctx.db_query_one("SELECT COUNT(*) AS c FROM sign_in WHERE sign_date = CURDATE()")
    return {
        "title": "今日签到",
        "value": count['c'] if count else 0,
        "label": "人次",
        "color": "#34c759",
    }
```

## 插件 WebUI

插件可以注册自己的 Web 管理页面，嵌入到框架 Web UI 中：

```python
def register(ctx):
    ctx.webui(title="我的插件面板", entry="index.html", icon="settings", order=50)
```

插件目录下创建 `web/index.html`（及配套的 css/js），框架会通过 `/api/plugin_webui/<plugin_name>/` 路由提供访问。

## Event 事件对象

`event` 参数是 `framework.event.Event` 实例，封装 OneBot 11 上报的事件数据。

### 基础属性

| 属性               | 类型   | 说明                                              |
| ------------------ | ------ | ------------------------------------------------- |
| `event.user_id`    | int    | 发送者 QQ 号                                      |
| `event.group_id`   | int    | 群号（私聊为 0）                                  |
| `event.message`    | str    | 消息纯文本（自动从消息段提取）                    |
| `event.message_id` | int    | 消息 ID                                           |
| `event.self_id`    | int    | 机器人 QQ 号                                      |
| `event.sender`     | dict   | OneBot 上报的原始 sender 字段                     |
| `event.bot_name`   | str    | 消息来源的 OneBot 实例名（多账号场景用）          |

### 类型判断

```python
event.is_group       # 是否群消息
event.is_private     # 是否私聊
event.message_type   # "group" / "private"
```

### 发送者信息

```python
event.sender_nickname  # 昵称
event.sender_card      # 群名片
event.sender_role      # OneBot 上报的 role: owner/admin/member
```

### 权限属性

```python
event.role            # 完整身份: super/owner/admin/member/blacklist
event.is_admin        # 超管/群主/管理员 → True
event.is_superuser    # 框架超管 → True
event.is_group_owner  # 群主 → True
```

### 富媒体属性

```python
event.has_image       # 包含图片
event.has_at          # 包含 @
event.has_at_bot      # @ 了机器人
event.has_reply       # 包含回复
event.has_voice       # 包含语音
event.has_video       # 包含视频
event.has_file        # 包含文件
event.has_face        # 包含表情

event.images          # 所有图片数据列表
event.first_image     # 第一张图片数据（dict）
event.at_list         # 所有被 @ 的 QQ 号列表
event.at_all          # 是否 @全体成员
event.reply_id        # 回复的消息 ID（无则 None）
event.segments        # 原始消息段数组
```

### 事件传播控制

```python
event.stop_event()    # 阻止后续插件收到此事件
event.is_stopped()    # 是否已被停止
```
