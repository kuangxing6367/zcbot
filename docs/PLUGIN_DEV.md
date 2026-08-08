# ZCBOT 插件开发文档

本文档详细说明如何为 ZCBOT 框架开发插件，包括目录结构、生命周期、API 接口、配置系统、定时任务、事件总线等内容。

## 目录

- [快速开始](#快速开始)
- [插件目录结构](#插件目录结构)
- [插件元信息](#插件元信息)
- [插件生命周期](#插件生命周期)
- [PluginContext (ctx) API](#plugincontext-ctx-api)
  - [命令注册](#命令注册)
  - [消息发送](#消息发送)
  - [OneBot 11 标准 API](#onebot-11-标准-api)
  - [数据库操作](#数据库操作)
  - [配置读取](#配置读取)
  - [定时任务](#定时任务)
  - [事件总线](#事件总线)
  - [权限判断](#权限判断)
  - [群级插件开关](#群级插件开关)
  - [日志与审计](#日志与审计)
  - [仪表盘卡片](#仪表盘卡片)
  - [插件 WebUI](#插件-webui)
- [Event 事件对象](#event-事件对象)
- [系统级动态命令（关键词自动回复）](#系统级动态命令关键词自动回复)
- [plugin.yaml 配置文件](#pluginyaml-配置文件)
- [_conf_schema.json 配置 Schema](#_conf_schemajson-配置-schema)
- [插件依赖声明](#插件依赖声明)
- [插件开发最佳实践](#插件开发最佳实践)
- [完整示例](#完整示例)

---

## 快速开始

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

---

## 异步支持（推荐）

框架核心为全异步架构（消息处理不阻塞事件循环）。插件 handler 支持两种写法：

**1. 异步 handler（推荐）** —— 使用 `async def`，配合异步 API 不占用线程：

```python
async def handle_weather(event, match):
    # 异步发送消息（不阻塞事件循环）
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message="今天晴，气温 25℃",
    )
    # 异步调用任意 OneBot API
    await ctx.aapi("get_group_member_list", group_id=event.group_id)
    # 异步数据库操作
    rows = await ctx.db_query_async("SELECT * FROM users WHERE user_id = %s", (event.user_id,))
```

**2. 同步 handler（兼容旧插件）** —— 普通 `def` 依旧可用，框架会自动在线程池中执行，不会阻塞事件循环：

```python
def handle_echo(event, match):
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=match.group(1),
    )
```

> 同步 handler 内使用 `ctx.send_msg()` / `ctx.api()` / `ctx.db_query()` 等同步方法即可，框架内部会自动桥接到主事件循环。异步 handler 建议使用 `asend_msg()` / `aapi()` / `db_query_async()` 等异步方法以获得最佳性能。

异步 API 速查表：

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

---

## 插件目录结构

ZCBOT 严格分离「代码」和「数据」：

```
项目根目录/
├── plugins/                  # 插件代码目录（.py 文件，可被 GitHub 更新覆盖）
│   └── my_plugin/
│       ├── main.py           # 插件入口（必需）
│       ├── utils.py          # 辅助模块（可选）
│       └── requirements.txt  # 插件依赖（可选）
│
└── plugins_dat/              # 插件数据/配置目录（用户数据，不被覆盖）
    └── my_plugin/
        ├── _conf_schema.json # 配置 schema（Web UI 展示用）
        ├── plugin.yaml       # 插件元信息、GitHub 更新源、依赖声明
        └── data.json         # 插件自定义数据文件
```

**关键约定**：
- `plugins/<plugin_name>/` 中的文件在 GitHub 更新时会被覆盖，**不要**在这里存放用户配置
- `plugins_dat/<plugin_name>/` 是用户数据目录，由 `ctx.get_data_dir()` 获取路径，**永久保留**
- 框架启动时会自动把旧版插件配置（位于 `plugins/` 中的 yaml/json）迁移到 `plugins_dat/`

---

## 插件元信息

在 `main.py` 顶部定义 `__plugin_meta__` 字典：

| 字段       | 类型   | 必填 | 说明                                       |
| ---------- | ------ | ---- | ------------------------------------------ |
| `name`     | str    | 是   | 插件显示名                                 |
| `version`  | str    | 是   | 插件版本号（建议语义化版本，如 `1.0.0`）   |
| `author`   | str    | 是   | 作者                                       |
| `desc`     | str    | 否   | 插件描述                                   |
| `priority` | int    | 否   | 加载优先级（越小越优先，默认 50）          |

`plugin.yaml` 中的元信息会覆盖 `__plugin_meta__`。

---

## 插件生命周期

ZCBOT 插件生命周期：

1. **发现**：框架扫描 `plugins/` 目录下的子目录，每个含 `main.py` 的目录视为一个插件
2. **加载**：调用 `importlib` 动态导入 `main.py`，并调用 `register(ctx)` 函数
3. **注册**：`register(ctx)` 中通过 `ctx.command()` / `ctx.task()` 等方法注册命令和任务
4. **心跳刷新**：每 60 秒（可配置）重新刷新命令表，保证数据库中命令状态与代码一致
5. **消息分发**：收到 OneBot 消息时，按插件优先级顺序匹配命令并调用 handler
6. **卸载**：调用 `unload_plugin()` 时清理命令、任务、事件订阅，并 `gc.collect()` 释放资源

可选的生命周期钩子（在 `main.py` 中定义）：

```python
def on_load(ctx):
    """插件加载时调用（register 之前）"""
    ctx.logger.info("插件正在加载...")

def on_unload(ctx):
    """插件卸载时调用"""
    ctx.logger.info("插件正在卸载...")
    # 清理文件句柄、关闭连接等
```

---

## PluginContext (ctx) API

`register(ctx)` 函数接收的 `ctx` 是 `PluginContext` 实例，提供所有框架能力。

### 命令注册

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

### 消息发送

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

### OneBot 11 标准 API

通过 `ctx.onebot` 访问完整的 38 个 OneBot 11 标准 API：

```python
# 私聊
ctx.onebot.send_private_msg(user_id=123456, message="私聊消息")

# 群聊
ctx.onebot.send_group_msg(group_id=123456, message="群消息")

# 撤回消息
ctx.onebot.delete_msg(message_id=123456)

# 禁言
ctx.onebot.set_group_ban(group_id=123456, user_id=789012, duration=600)

# 踢人
ctx.onebot.set_group_kick(group_id=123456, user_id=789012, reject_add_request=False)

# 退群
ctx.onebot.set_group_leave(group_id=123456)

# 获取群列表
ctx.onebot.get_group_list()

# 获取群成员列表
ctx.onebot.get_group_member_list(group_id=123456)
```

非标准/扩展 API 使用 `ctx.api()` 兜底：

```python
ctx.api("set_group_special_title", group_id=123456, user_id=789012, title="大佬")
```

完整 OneBot 11 API 列表见 [OneBot 11 标准](https://github.com/botuniverse/onebot-11/blob/master/api/public.md)。

### 数据库操作

ZCBOT 自动适配 SQLite 和 MySQL，插件无需关心差异（统一用 `%s` 占位符）：

```python
# 查询多条
rows = ctx.db_query(
    "SELECT id, name FROM users WHERE group_id = %s ORDER BY id",
    (event.group_id,)
)
for row in rows:
    print(row['id'], row['name'])

# 查询单条
row = ctx.db_query_one(
    "SELECT * FROM users WHERE user_id = %s",
    (event.user_id,)
)
if row:
    print(row)

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

> 注意：MySQL 模式下 `NOW()` 会自动转为当前时间参数；`ON DUPLICATE KEY UPDATE` 会自动转译为 `ON CONFLICT DO UPDATE`。插件 SQL 直接按 MySQL 语法写即可。

### 配置读取

插件配置由 Web UI 通过 `_conf_schema.json` 定义并存储在 `plugin_configs` 表中：

```python
# 读取单个配置
timeout = ctx.get_config("request_timeout", default=10)
max_retry = ctx.get_config("max_retry", default=3)

# 读取全部配置
config = ctx.get_all_config()
print(config)  # {"request_timeout": 10, "max_retry": 3, ...}
```

### 定时任务

注册基于 cron 表达式的定时任务：

```python
def register(ctx):
    # 每天早上 8:00 执行
    ctx.task("0 8 * * *", daily_report, description="每日签到统计")

    # 每 5 分钟执行
    ctx.task("*/5 * * * *", check_status, description="状态检查")

def daily_report():
    # 注意：定时任务 handler 不接收 event 参数
    ctx.logger.info("开始生成每日报告...")
    # 生成报告并通过 ctx.send_msg 发送

def check_status():
    # 检查服务状态
    pass
```

cron 表达式使用 5 字段标准格式：`分 时 日 月 周`。

### 事件总线

插件之间可以通过事件总线通信：

```python
def register(ctx):
    # 订阅事件
    ctx.on("user_sign_in", on_sign_in)
    ctx.on("group_member_increase", on_new_member)

    # 发布事件
    ctx.emit("custom_event", {"key": "value"})

def on_sign_in(payload):
    # payload 是 dict
    user_id = payload.get("user_id")
    ctx.logger.info(f"用户 {user_id} 签到了")

def on_new_member(payload):
    group_id = payload.get("group_id")
    user_id = payload.get("user_id")
    ctx.send_msg(group_id=group_id, message=f"欢迎新成员 {user_id}")
```

#### 文本消息监听（message 事件）

插件命令**均未命中**时，框架会把文本消息广播为 `message` 事件（载荷为 Event 对象），内容监听型插件（关键词回复、违禁词、自动应答等）无需注册命令即可监听任意文本：

```python
def register(ctx):
    ctx.on("message", on_any_message)

def on_any_message(ev):
    # ev 是 Event 对象；返回 True = 已处理（路由终止，系统关键词不再触发）
    # 返回 None/False = 未处理，继续走系统关键词自动回复
    if "敏感词" in ev.message:
        ctx.send_msg(group_id=ev.group_id, message="请注意用词")
        return True
```

**路由优先级**：插件命令 > `message` 事件监听 > 系统关键词自动回复。

> 注意：纯富媒体消息（无文本段的分享卡片/图片）走 `message.share` / `message.media` 事件，与 `message` 事件互不干扰。

#### 非文本消息事件（分享卡片 / 图片等）

**无文本的消息**（如纯分享卡片、纯图片、纯视频）不参与命令匹配（命令匹配需要文本），框架会在收到此类消息时将其**广播到事件总线**，插件通过 `ctx.on()` 订阅即可处理：

| 事件名 | 触发条件 | 载荷 |
| --- | --- | --- |
| `message.share` | 消息含分享卡片段（`share`）且无文本 | Event 对象 |
| `message.image` | 消息含图片段（`image`）且无文本 | Event 对象 |
| `message.record` / `message.video` / `message.file` / `message.face` ... | 对应消息段且无文本 | Event 对象 |
| `message.media` | 任意非文本消息段（通用兜底事件，总与上方事件一起触发） | Event 对象 |

> 注意：
> - 事件仅在消息**整体无文本**时触发；带文本的富媒体消息仍走正常命令匹配（可通过 `event.has_image` / `event.has_share` 等属性判断）。
> - 载荷是 `Event` 对象（与命令 handler 的 `event` 同类型），**不是 dict**，可直接使用 `ev.has_share` / `ev.share` / `ev.segments` / `ev.user_id` 等属性和方法。
> - 没有插件订阅时广播是零开销的（事件总线自动跳过）。

```python
def register(ctx):
    # 订阅分享卡片事件
    ctx.on("message.share", on_share)

def on_share(ev):
    """处理分享卡片（ev 是 Event 对象）"""
    info = ev.share  # {'url': ..., 'title': ..., 'desc': ...}
    ctx.send_msg(
        user_id=ev.user_id,
        group_id=ev.group_id if ev.is_group else None,
        message=f"收到分享：{info.get('title')}\n{info.get('url')}",
    )
```

### 权限判断

```python
def handle_ban(event, match):
    # 判断发送者是否管理员
    if not event.is_admin:
        ctx.send_msg(
            group_id=event.group_id,
            message="仅管理员可执行此命令",
        )
        return

    # 判断是否超级管理员
    if event.is_superuser:
        # 超管逻辑
        pass

    # 完整身份等级：super > owner > admin > member > blacklist
    role = event.role
    if role in ("super", "owner", "admin"):
        # 执行管理操作
        pass
```

ctx 也提供权限快捷方法（适用于非消息场景）：

```python
ctx.is_superuser(user_id=123456)
ctx.is_group_admin(group_id=123456, user_id=789012)
ctx.is_group_owner(group_id=123456, user_id=789012)
ctx.is_blacklisted(user_id=123456)
ctx.get_user_role(group_id=123456, user_id=789012)  # 返回 "super"/"owner"/...
```

### 群级插件开关

```python
# 在指定群启用某插件
ctx.enable_plugin_in_group("my_plugin", group_id=123456)

# 在指定群禁用某插件
ctx.disable_plugin_in_group("my_plugin", group_id=123456)

# 检查插件在某群是否启用
if ctx.is_plugin_enabled_in_group("my_plugin", group_id=123456):
    pass

# 获取指定群所有插件状态
status = ctx.get_plugin_status_list(group_id=123456)
# {"echo": True, "ipquery": False, ...}
```

### 日志与审计

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

### 仪表盘卡片

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

### 插件 WebUI

插件可以注册自己的 Web 管理页面，嵌入到框架 Web UI 中：

```python
def register(ctx):
    ctx.webui(title="我的插件面板", entry="index.html", icon="settings", order=50)
```

插件目录下创建 `web/index.html`（及配套的 css/js），框架会通过 `/api/plugin_webui/<plugin_name>/` 路由提供访问。

---

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
event.has_share       # 包含分享卡片

event.images          # 所有图片数据列表
event.first_image     # 第一张图片数据（dict）
event.share           # 分享卡片数据 dict（url/title/desc），无则 {}
event.at_list         # 所有被 @ 的 QQ 号列表
event.at_all          # 是否 @全体成员
event.reply_id        # 回复的消息 ID（无则 None）
event.segments        # 原始消息段数组
```

### 事件传播控制

```python
event.stop_event()        # 阻止后续插件收到此事件
event.is_stopped()        # 是否已被停止
event.continue_route()    # 本插件已处理，但允许系统关键词自动回复继续尝试
event.is_continue_route() # 是否声明了继续路由
```

---

## 系统级动态命令（关键词自动回复）

框架内置「关键词 → 自动回复」能力（数据存于 `dynamic_commands` 表），无需写插件即可实现常见自动应答。**在插件命令均未命中消息时**作为兜底触发，不会与插件回复冲突。

### 匹配方式

| match_type | 说明 | 示例 |
| --- | --- | --- |
| `exact` | 消息与关键词完全相等 | 关键词 `你好` 只回复消息 `你好` |
| `prefix` | 消息以关键词开头 | 关键词 `/note` 回复 `记下了` |
| `contains` | 消息包含关键词 | 关键词 `抽奖` 回复任意包含"抽奖"的消息 |
| `regex` | 正则表达式匹配（`re.search`） | `^天气[:：]\d+` 回复天气查询 |

### 管理方式

**Web UI**：命令管理页 → 「关键词回复」卡片，支持新增 / 编辑 / 启停 / 删除，修改即时生效（无需重启）。

**API**（均需登录态，写操作需超管）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/dynamic-commands` | 列表 |
| POST | `/api/dynamic-commands` | 新增 `{keyword, response, match_type, plugin_name?}` |
| PUT | `/api/dynamic-commands/<id>` | 更新 |
| POST | `/api/dynamic-commands/<id>/toggle` | 启停 |
| DELETE | `/api/dynamic-commands/<id>` | 删除 |

> 回复内容支持 CQ 码（如 `[CQ:image,file=...]`）。关键词规则由后台任务周期性加载进内存（默认 5s），API/Web 修改后即时重建，命中计数批量落库。

### 关键词 handler 回调（动态生成回复）

`dynamic_commands.handler` 字段支持 `plugin:func` 格式，命中关键词时调用插件函数**动态生成**回复内容（失败自动回退静态 `response`）：

```python
# 表中配置：keyword=天气, response=（兜底文本）, handler=my_plugin:gen_weather, match_type=prefix

def gen_weather(rule, message):
    """rule 为关键词规则对象（含 keyword/response/match_type），message 为触发消息"""
    city = message.replace("天气", "").strip()
    return f"{city} 今日晴，气温 25℃"   # 返回 None 则用静态 response
```

### 插件建表统一入口

插件需要建表时使用 `ctx.create_table(ddl)`，自动适配方言，无需判断数据库类型：

```python
ctx.create_table("""
    CREATE TABLE IF NOT EXISTS my_rules (
        id INT AUTO_INCREMENT PRIMARY KEY,
        keyword VARCHAR(300) NOT NULL,
        params TEXT,
        enabled TINYINT(1) DEFAULT 1,
        INDEX idx_kw (keyword)
    )
""")
```

- **SQLite**：自动翻译（ENUM→TEXT、AUTO_INCREMENT→AUTOINCREMENT、INDEX 移除等）
- **MySQL**：自动将长列（`TEXT` / `VARCHAR`>191）索引改写为前缀索引 `` `col`(191) ``，避免错误 1170/1064

### 与插件的协作（continue_route）

插件命令命中后默认"独占"消息（系统关键词不再触发）。若希望**插件处理完仍允许系统关键词自动回复继续尝试**，在 handler 内调用 `event.continue_route()`：

```python
def handle_xxx(event, match):
    event.continue_route()  # 本插件已处理，但允许关键词自动回复继续
    return True
```

---

## plugin.yaml 配置文件

放在 `plugins_dat/<plugin_name>/plugin.yaml`，提供 Web UI 识别所需的元信息、GitHub 更新源、依赖声明等：

```yaml
# 插件元信息（覆盖 __plugin_meta__）
name: my_plugin
version: 1.0.0
author: your-name
description: 我的插件
priority: 50

# GitHub 更新源
github:
  repo: your-name/my_plugin   # 支持 user/repo 简写或完整 URL
  branch: main                # 分支名
  path: /                     # 仓库内插件所在子目录
  auto_check: true            # 是否启用自动更新检查

# 插件配置项（Web UI 可展示和修改）
config:
  - key: api_key
    label: API Key
    type: string
    default: ""
    description: 第三方 API 密钥
  - key: timeout
    label: 请求超时
    type: number
    default: 10
    description: HTTP 请求超时秒数

# 依赖
dependencies:
  python:
    - requests>=2.28.0
    - beautifulsoup4

# 配置文档（Web UI 可查看）
docs:
  - file: README.md
    title: 使用说明
```

---

## _conf_schema.json 配置 Schema

AstrBot 风格的配置 schema，用于 Web UI 动态渲染配置表单。放在 `plugins_dat/<plugin_name>/_conf_schema.json`：

```json
{
  "api_key": {
    "description": "第三方 API 密钥",
    "type": "string",
    "default": "",
    "hint": "在 https://example.com 申请"
  },
  "timeout": {
    "description": "请求超时（秒）",
    "type": "number",
    "default": 10,
    "hint": "建议 5-30 秒"
  },
  "enable_feature": {
    "description": "启用实验功能",
    "type": "boolean",
    "default": false,
    "hint": "开启后可使用 /experimental 命令"
  }
}
```

**支持的 type**：`string`、`number`、`boolean`。

插件代码中通过 `ctx.get_config("api_key", default="")` 读取。

---

## 插件依赖声明

### 方式一：requirements.txt

在 `plugins/<plugin_name>/requirements.txt` 中声明：

```
requests>=2.28.0
beautifulsoup4>=4.11.0
lxml>=4.9.0
```

### 方式二：plugin.yaml

在 `plugins_dat/<plugin_name>/plugin.yaml` 中：

```yaml
dependencies:
  python:
    - requests>=2.28.0
    - beautifulsoup4
```

**安装时机**：
- 启动时自愈（`plugin.auto_install_deps_on_startup: true`）：自动安装缺失依赖
- Web UI 手动安装：在插件管理页点击「安装依赖」按钮

**镜像源**：默认使用清华源，失败自动回退到阿里云、华为云、官方源。可在 Web UI 设置中切换。

**依赖冲突**：当多个插件依赖同一包的不同版本时，框架可为冲突插件创建独立虚拟环境（`.venv`），在 Web UI 插件管理页点击「创建隔离环境」即可。

---

## 插件开发最佳实践

### 1. 代码与数据分离

```python
# 错误：把配置写到 plugins/my_plugin/config.json
# GitHub 更新时会覆盖用户配置！

# 正确：使用 plugins_dat/ 目录
import os, json

def load_config(ctx):
    data_dir = ctx.get_data_dir()  # 返回 plugins_dat/my_plugin/
    config_path = os.path.join(data_dir, "config.json")
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}
```

### 2. 异常捕获

```python
def handle_xxx(event, match):
    try:
        result = call_external_api()
        ctx.send_msg(group_id=event.group_id, message=result)
    except requests.Timeout:
        ctx.send_msg(group_id=event.group_id, message="请求超时，请稍后重试")
    except Exception as e:
        ctx.logger.exception(f"处理失败: {e}")
        ctx.send_msg(group_id=event.group_id, message="内部错误，请联系管理员")
```

### 3. 避免阻塞事件循环

耗时操作（HTTP 请求、文件 IO）建议放到独立线程：

```python
import threading

def handle_long_task(event, match):
    def worker():
        result = expensive_operation()
        ctx.send_msg(group_id=event.group_id, message=result)
    threading.Thread(target=worker, daemon=True).start()
```

### 4. 权限校验

```python
def handle_admin_cmd(event, match):
    if not event.is_admin:
        return  # 静默忽略非管理员
    # 管理操作...
```

或使用 `require_admin` 参数：

```python
ctx.command("/admin_cmd", handle_admin_cmd, require_admin=True)
ctx.command("/super_cmd", handle_super_cmd, require_superuser=True)
```

### 5. 资源清理

```python
def on_unload(ctx):
    # 关闭文件句柄、数据库连接、HTTP 会话等
    if hasattr(ctx, '_http_session'):
        ctx._http_session.close()
    ctx.logger.info("资源已清理")
```

### 6. 别名支持

```python
ctx.command("/help", handle_help, alias="/h,/?", description="查看帮助")
```

---

## 完整示例

以下是一个功能完整的示例插件，展示命令注册、配置读取、定时任务、事件订阅、数据库操作的组合用法：

```python
# plugins/sign_in/main.py
"""
每日签到插件 - 演示 ZCBOT 插件开发完整流程
"""
import datetime
import random

__plugin_meta__ = {
    "name": "每日签到",
    "version": "1.0.0",
    "author": "zcbot",
    "desc": "每日签到领积分，连续签到有奖励",
    "priority": 50,
}


def register(ctx):
    # 静态命令
    ctx.command("/签到", handle_sign_in, alias="/sign", description="每日签到")
    ctx.command("/签到排行", handle_rank, alias="/rank", description="签到排行榜")
    ctx.command("/我的积分", handle_my_score, description="查看我的积分")

    # 定时任务：每天 00:01 重置签到状态
    ctx.task("1 0 * * *", reset_daily_signin, description="每日重置签到")

    # 订阅事件：新成员入群时赠送初始积分
    ctx.on("group_member_increase", on_new_member)


def handle_sign_in(event, match):
    """每日签到，随机获得 1-10 积分"""
    today = datetime.date.today().isoformat()

    # 检查今日是否已签到
    signed = ctx.db_query_one(
        "SELECT id FROM sign_in_records WHERE user_id = %s AND sign_date = %s",
        (event.user_id, today)
    )
    if signed:
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message="你今天已经签到过了",
        )
        return

    # 计算积分（连续签到加成）
    last_sign = ctx.db_query_one(
        "SELECT sign_date, continuous_days FROM sign_in_records "
        "WHERE user_id = %s ORDER BY sign_date DESC LIMIT 1",
        (event.user_id,)
    )
    if last_sign:
        last_date = datetime.date.fromisoformat(last_sign['sign_date'])
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        continuous = last_sign['continuous_days'] + 1 if last_date == yesterday else 1
    else:
        continuous = 1

    base_score = random.randint(1, 10)
    bonus = min(continuous - 1, 5)  # 连签加成，最多 +5
    total = base_score + bonus

    # 写入数据库
    ctx.db_execute(
        "INSERT INTO sign_in_records (user_id, sign_date, score, continuous_days) "
        "VALUES (%s, %s, %s, %s)",
        (event.user_id, today, total, continuous)
    )

    # 更新用户总积分
    ctx.db_execute(
        "INSERT INTO user_scores (user_id, total_score) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE total_score = total_score + %s",
        (event.user_id, total, total)
    )

    # 审计日志
    ctx.audit_log(
        action="sign_in",
        target_type="user",
        target_name=str(event.user_id),
        detail={"score": total, "continuous": continuous},
    )

    # 发布事件
    ctx.emit("user_sign_in", {"user_id": event.user_id, "score": total})

    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=f"签到成功！获得 {total} 积分（基础 {base_score} + 连签加成 {bonus}）\n"
                f"已连续签到 {continuous} 天",
    )


def handle_rank(event, match):
    """签到排行榜"""
    rows = ctx.db_query(
        "SELECT user_id, total_score FROM user_scores "
        "ORDER BY total_score DESC LIMIT 10"
    )
    if not rows:
        ctx.send_msg(group_id=event.group_id, message="暂无排行数据")
        return

    lines = ["积分排行榜 TOP 10:"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. QQ {row['user_id']} - {row['total_score']} 分")
    ctx.send_msg(group_id=event.group_id, message="\n".join(lines))


def handle_my_score(event, match):
    """查看我的积分"""
    row = ctx.db_query_one(
        "SELECT total_score FROM user_scores WHERE user_id = %s",
        (event.user_id,)
    )
    score = row['total_score'] if row else 0
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=f"你当前积分：{score}",
    )


def reset_daily_signin():
    """每日 00:01 执行（无 event 参数）"""
    ctx.logger.info("开始执行每日签到重置")
    # 可在此清理过期数据、生成报表等


def on_new_member(payload):
    """新成员入群事件"""
    group_id = payload.get("group_id")
    user_id = payload.get("user_id")
    if group_id and user_id:
        ctx.db_execute(
            "INSERT INTO user_scores (user_id, total_score) VALUES (%s, 100) "
            "ON DUPLICATE KEY UPDATE total_score = total_score + 100",
            (user_id,)
        )
        ctx.send_msg(group_id=group_id, message=f"欢迎新成员 {user_id}，赠送 100 初始积分")


def on_unload(ctx):
    """插件卸载时清理资源"""
    ctx.logger.info("签到插件已卸载")
```

对应的配置文件 `plugins_dat/sign_in/_conf_schema.json`：

```json
{
  "base_score_max": {
    "description": "单次签到最高基础积分",
    "type": "number",
    "default": 10,
    "hint": "签到随机积分范围 1 到此值"
  },
  "continuous_bonus_max": {
    "description": "连签加成上限",
    "type": "number",
    "default": 5,
    "hint": "连续签到每日额外加成上限"
  }
}
```

对应的 `plugins_dat/sign_in/plugin.yaml`：

```yaml
name: sign_in
version: 1.0.0
author: zcbot
description: 每日签到领积分
priority: 50

github:
  repo: your-name/sign_in_plugin
  branch: main
  path: /
  auto_check: true

config:
  - key: base_score_max
    label: 最高基础积分
    type: number
    default: 10
  - key: continuous_bonus_max
    label: 连签加成上限
    type: number
    default: 5

dependencies:
  python: []

docs:
  - file: README.md
    title: 使用说明
```

---

## 更多示例

- [Echo 插件](../plugins/echo/main.py) - 最简单的命令回显
- [IP 查询插件](../plugins/ipquery/main.py) - HTTP 请求 + 多 API 源回退
- [运行状态插件](../plugins/runtime_status/main.py) - 系统监控 + plugin.yaml 配置
- [API 接口文档](./API.md) - Web UI 后端 API 完整定义
