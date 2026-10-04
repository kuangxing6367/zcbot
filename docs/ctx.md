# PluginContext (ctx) 完整参考

写插件时 `ctx` 上能用什么，都在这页查——当字典翻即可，不必通读。

`ctx` 是插件与框架交互的**唯一入口**，由框架在 `register(ctx)` 时注入，
并挂到插件主模块上（handler 里可直接使用全局 `ctx`）。

**同步 / 异步约定**：绝大多数能力都提供**同步**与**异步**两个版本——

- 同步方法（如 `send_msg`、`db_query`）内部桥接到异步实现或线程，普通函数 handler 可用；
- `async def` handler 中请优先使用带 `a` 前缀的异步方法
  （`asend_msg`、`aapi`、`aemit`，以及 `db_query_async` 等 `db_*_async` 系列），避免阻塞事件循环。

## 目录

**对象与命令**

1. [属性](#一属性)
2. [命令注册](#二命令注册)

**消息与事件**

3. [消息发送与接入端 API](#三消息发送与接入端-api协议中立)
4. [群管快捷方法](#四群管快捷方法)
5. [事件订阅与发布](#五事件订阅与发布)

**数据与权限**

6. [配置读取](#六配置读取)
7. [数据库](#七数据库)
8. [权限与身份](#八权限与身份)

**后台与集成**

9. [多轮会话](#九多轮会话)
10. [定时任务](#十定时任务)
11. [仪表盘与 WebUI](#十一仪表盘与-webui)
12. [工具类：日志 / 异步执行 / 审计 / 数据目录](#十二工具类)
13. [场景选型：哪个 ctx 函数适合做什么](#十三场景选型哪个-ctx-函数适合做什么)

---

## 一、属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `ctx.plugin_name` | `str` | 当前插件名（用户插件即目录名） |
| `ctx.logger` | `logging.Logger` | 标准库 logger，自动带插件名前缀 |
| `ctx.actions` | 动作封装 | **协议中立（推荐）**：按当前事件来源选对应适配器（多端不串线）；无上下文时优先 `services['onebot_api']`，否则 `ActionProxy` 转发到 `api_caller`；都没有才抛 `RuntimeError` |
| `ctx.onebot` | 动作封装 | `ctx.actions` 的兼容别名（旧插件无需改） |
| `ctx.db_pool_status` | `dict` | 数据库连接池状态 |

```python
print(ctx.plugin_name)
ctx.logger.info("用标准 logger 也可以")
```

## 二、命令注册

### ctx.command()

```python
ctx.command(
    pattern,                     # 命令名（前缀匹配）或正则（re.search）
    handler,                     # 处理函数 (event, match) -> None，同步/异步均可
    priority: int = 50,          # 匹配优先级，越小越优先
    dynamic: bool = False,       # True 仅在“动态命令”展示，不参与路由
    alias=None,                  # 别名：列表 ["/h"] 或字符串 "/h,/help"
    description: str = None,     # 描述；缺省时取 handler docstring 第一行
    require_admin: bool = False, # 需 群主/管理员/超管
    require_superuser=False,     # 需超级管理员（置位后忽略 require_admin）
    require_perm: str = None,    # 需权限节点，如 'myplugin.ban'，与上面可叠加
)
```

```python
def register(ctx):
    ctx.command("/hello", handle_hello, description="打招呼")
    ctx.command("/echo", handle_echo, alias=["/e"], require_perm="echo.use")
    ctx.command("/admin", handle_admin, require_admin=True)
    ctx.command("/god", handle_god, require_superuser=True)
```

### 静态命令与动态命令（dynamic 参数）

`ctx.command()` 的 `dynamic` 参数（默认 `False`）决定命令走哪条路径，二者用途完全不同：

| 维度 | 静态命令（dynamic=False，默认） | 动态命令（dynamic=True） |
| ---- | ---- | ---- |
| 落库表 | `commands`（`is_dynamic=0`） | `commands`（`is_dynamic=1`） |
| 是否参与路由匹配 | 是，由内存路由表按前缀 / 正则匹配，命中即触发 handler | 否，不会参与命令路由 |
| 典型用途 | 真正要响应、要执行的逻辑 | 仅在「动态命令」面板展示给用户的命令清单 / 说明 |
| handler 是否必须 | 是，必须有处理函数 | 一般只填 `pattern` + `description` 做展示，可不挂 handler |

```python
ctx.command("/签到", handle_sign, alias="/sign", description="每日签到")   # 静态：会执行
ctx.command("/菜单说明", None, dynamic=True, description="展示用，不执行")  # 动态：仅展示
```

要点：
- 动态命令只进「动态命令」展示面板，消息路由不经过它们。一个命令既想展示又想执行，写**静态命令**即可——它本身就会出现在帮助 / 命令列表里。
- `dynamic=True` 与「系统关键词自动回复」是两回事：后者写在 `dynamic_commands` 表，作为静态路由表无命中时的兜底，不通过 `ctx.command` 注册。

匹配规则：普通命令名做前缀匹配；含正则元字符的模式走 `re.search()`，
命令后参数统一用 `match.group(1)` 捕获。

## 三、消息发送与接入端 API（协议中立）

> 新代码请用 `ctx.actions.<动作>()`（如 `ctx.actions.send_msg(...)`）；`ctx.onebot` 仍可用作兼容别名。
> 框架出站文本统一走适配器的 `send_text(...)`（可覆写，默认返回 unsupported），底层动作也可直接
> `await ctx.aapi('send_msg', ...)`，不依赖任何具体协议。

### ctx.send_msg() / ctx.asend_msg()

```python
ctx.send_msg(user_id=None, group_id=None, message=None,
             auto_escape=False, bot=None)
await ctx.asend_msg(user_id=None, group_id=None, message=None,
                    auto_escape=False, bot=None)
```

- 给 `group_id` 走群聊，只给 `user_id` 走私聊；
- `message` 支持纯文本与 CQ 码；`auto_escape=True` 时不解析 CQ 码；
- `bot=None` 时自动使用当前消息来源的 bot 实例（多账号场景安全）。

```python
await ctx.asend_msg(
    user_id=event.user_id,
    group_id=event.group_id if event.is_group else None,
    message="Hello!"
)
```

### ctx.api() / ctx.aapi()：任意 action

`ctx.api/aapi` 走协议无关的 `api_caller`（默认接入端 OneBot，动作名即 OneBot action；换接入端后用该接入端动作名）。快捷方法没覆盖的动作，用通用入口直接调（参数以关键字展开）：

```python
ctx.api("set_group_leave", group_id=123456)
data = await ctx.aapi("get_group_member_list", group_id=123456)
```

适配器未加载时抛 `RuntimeError("无可用协议适配器")`。

## 四、群管快捷方法

每个方法都有同步版与 `a` 开头的异步版，参数一致；`bot=None` 表示用当前来源 bot。

| 同步 / 异步 | 说明 |
|------|------|
| `ban(g, u, duration=600)` / `aban` | 禁言秒数，`duration=0` 解禁 |
| `kick(g, u, reject_add_request=False)` / `akick` | 踢出群成员 |
| `mute_all(g, enable=True)` / `amute_all` | 切换全员禁言 |
| `set_card(g, u, card)` / `aset_card` | 设置群名片 |
| `get_member_list(g)` / `aget_member_list` | 群成员列表 |
| `get_member_info(g, u)` / `aget_member_info` | 群成员资料 |

```python
await ctx.aban(event.group_id, target_uid, 600)
```

### 群级插件开关（管理员能力）

| 方法 | 说明 |
|------|------|
| `enable_plugin_in_group(plugin_name, group_id)` | 在指定群启用某插件 |
| `disable_plugin_in_group(plugin_name, group_id)` | 在指定群禁用某插件 |
| `is_plugin_enabled_in_group(plugin_name, group_id) -> bool` | 查询启用状态 |
| `get_plugin_status_list(group_id) -> dict` | 该群所有插件的启用状态字典 |

## 五、事件订阅与发布

### ctx.on(event_name, handler)

订阅系统/自定义事件，handler 同步异步均可：

```python
ctx.on("message", on_message)
ctx.on("notice.group_increase", on_join)
```

### ctx.on_raw_message(handler)

注册**原始消息处理器**，在命令匹配之前触发：

```python
async def raw_handler(raw_event: dict, bot_name: str):
    return True     # True=接管，跳过后续全部处理；None/False=放行
```

### ctx.emit() / ctx.aemit()

发布自定义事件（同步桥接 / 异步）：

```python
ctx.emit("user_sign_in", {"user_id": 123})
await ctx.aemit("user_sign_in", {"user_id": 123})
```

## 六、配置读取

### ctx.get_config(key, default=None)

读取 `_conf_schema.json` 定义、Web 面板写入的配置值；带 30 秒 TTL 缓存，
自动 JSON 反序列化，缺值返回 `default`。

```python
api_key = ctx.get_config("api_key", default="")
timeout = ctx.get_config("timeout", default=30)
```

### ctx.get_all_config() -> dict

一次取本插件全部配置 `{key: value}`。

## 七、数据库

统一使用 `%s` 占位符（SQLite 下自动转换），查询返回 `dict` / `list[dict]`。

### 同步接口

| 方法 | 返回 | 说明 |
|------|------|------|
| `db_query(sql, params=None)` | `list[dict]` | 多行查询 |
| `db_query_one(sql, params=None)` | `dict/None` | 单行查询 |
| `db_execute(sql, params=None)` | `int` | 写操作，返回受影响行数 |
| `db_execute_many(sql, params_list)` | `int` | 批量写 |
| `db_insert(sql, params=None)` | `int` | 插入并返回自增 ID |
| `create_table(ddl)` | - | 建表，自动适配方言（MySQL 方言 DDL） |
| `db_connection()` | connection | 取连接（事务用，close 归还连接池） |

### 异步接口（async handler 推荐，走 DB 专用线程池）

`db_query_async` / `db_query_one_async` / `db_execute_async` /
`db_execute_many_async` / `db_insert_async`，参数与同步版一致。

```python
rows = await ctx.db_query_async("SELECT * FROM t WHERE group_id=%s", (gid,))
new_id = await ctx.db_insert_async("INSERT INTO t (v) VALUES (%s)", (v,))
```

### 建表与事务

```python
ctx.create_table("""
    CREATE TABLE IF NOT EXISTS my_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        value TEXT
    )
""")

conn = ctx.db_connection()
try:
    cur = conn.cursor()
    cur.execute("UPDATE a SET x=x-%s WHERE id=%s", (10, 1))
    cur.execute("UPDATE b SET x=x+%s WHERE id=%s", (10, 2))
    conn.commit()
except Exception:
    conn.rollback()
finally:
    conn.close()
```

## 八、权限与身份

| 方法 | 返回 | 说明 |
|------|------|------|
| `has_perm(user_id, node, context=None, role=None)` | `bool` | 是否拥有权限节点（未定义按拒绝），节点支持 `chat.*` 通配 |
| `check_perm(user_id, node, ...)` | `True/False/None` | 三态：授予/显式否决/未定义 |
| `user_groups(user_id, ...)` | `list` | 生效权限组（含继承，按 weight 降序） |
| `is_superuser(user_id)` | `bool` | 是否超管 |
| `is_group_admin(group_id, user_id)` | `bool` | 是否群管理员（不含群主） |
| `is_group_owner(group_id, user_id)` | `bool` | 是否群主 |
| `is_blacklisted(user_id)` | `bool` | 是否黑名单 |
| `get_user_role(group_id, user_id)` | `str` | `super/owner/admin/member/blacklist` |

事件对象上也有一套等价能力：`event.has_perm(node)`、`event.role`、
`event.is_admin`、`event.perms` 等，见 [Event](event.md)。

## 九、多轮会话

需要启用官方插件 `core_plugins.session`。

### await ctx.wait_for(event, prompt=None, timeout=60, handler=None)

发送可选提示并等待该用户下一条消息，超时返回 `None`，`handler(raw)` 可做过滤。

### ctx.create_session(event, timeout=60)

返回异步上下文会话对象：

```python
async with ctx.create_session(event, timeout=120) as sess:
    r = await sess.ask("你叫什么？")
    sess.data["name"] = r.get("message", "")
```

详见 [多轮会话](session.md)。

## 十、定时任务

### ctx.task(cron_expr, executor, description=None)

```python
ctx.task("0 8 * * *", daily_report, description="每日报告")
ctx.task("*/5 * * * *", check)
```

- cron 格式：`分 时 日 月 周`；
- 任务函数无参数、不接收事件，需要框架能力时在函数内用服务注册表；
- 自动生成任务 ID `<插件名>_<函数名>`。

更多触发器（interval/date）、动态增删见 [定时任务](scheduler.md)。

## 十一、仪表盘与 WebUI

### ctx.dashboard_card(title, handler, icon=None, priority=50)

注册仪表盘卡片，`handler()` 返回 `{title, value, label, icon, color}`：

```python
def get_count():
    row = ctx.db_query_one("SELECT COUNT(*) AS c FROM users WHERE online=1")
    return {"title": "在线用户", "value": row["c"], "label": "人"}

ctx.dashboard_card("在线用户", get_count, icon="users")
```

### ctx.webui(title, entry="index.html", icon=None, order=50, sidebar=False)

在管理后台注册一个插件页面，静态资源放插件 `web/` 目录。

- `title`：页面标题（显示在导航栏 / 侧边栏）。
- `entry`：入口文件名，默认 `index.html`。
- `icon`：图标，支持 emoji 或 HTML 实体；留空则用默认图标。
- `order`：排序权重，越小越靠前。
- `sidebar`：**是否在侧边栏注册独立入口**。
  - `sidebar=True`：插件在侧边栏拥有自己的菜单项，点击跳转到 `/plugin/<插件名>` 直接打开该插件页面（不再需要从「插件页面」聚合页里选）。
  - `sidebar=False`（默认）：插件仍归入「插件页面」聚合入口，兼容旧插件。

```python
# 在侧边栏注册一个独立入口（推荐）
ctx.webui("我的面板", entry="index.html", icon="🛠", order=10, sidebar=True)
```

官方默认侧边栏（仪表盘 / 插件市场 / 用户管理等内置菜单）可在
`config.yaml` 的 `web.official_sidebar` 开关：

```yaml
web:
  official_sidebar: true   # false 时仅显示插件注册项 + 设置
```

该开关在管理后台「设置 → Web 服务 → 显示官方侧边栏」中即可切换，保存后即时生效，无需重启。

### ctx.override_webui()

让本插件整体接管管理后台前端；插件卸载后自动回退默认前端。

### ctx.register_group_extension(key, title, handler, ext_type="column")
### ctx.register_user_extension(key, title, handler, ext_type="column")

在「群组管理 / 用户管理」页面扩展一列（`column`，handler 返回字符串）
或一个详情面板（`panel`，handler 返回 `{label: value}`）。
handler 在独立线程执行并限时 2 秒，异常显示占位，不卡 Web。

```python
ctx.register_group_extension("sign_days", "签到天数", lambda gid: query_days(gid))
```

## 十二、工具类

### 日志

```python
ctx.log("消息")                       # 默认 info
ctx.log("出错", level="error")        # debug/info/warning/error
```

### ctx.run_async(func, *args, **kwargs)

把耗时任务提交到内置线程池后台执行，返回 `concurrent.futures.Future`，
不阻塞消息处理：

```python
ctx.run_async(render_and_send)
```

### ctx.audit_log(action, target_type=None, target_name=None, detail=None, result="success", error_message=None)

以插件身份写审计日志（`admin_name` 自动记为 `plugin:<插件名>`）：

```python
ctx.audit_log("reset_data", target_type="data", detail={"by": event.user_id})
```

### ctx.get_data_dir() -> str

返回（不存在则创建）插件私有数据目录 `data/plugins_dat/<插件名>/` 的绝对路径。
配置缓存、下载的资源、SQLite 文件等都应写在这里，而不是代码目录。

---

## 十三、场景选型：哪个 ctx 函数适合做什么

按「想做什么」查对应的 ctx 能力；命令类进一步区分静态 / 动态。

| 你想实现的能力 | 用哪个 ctx 能力 | 说明 |
| ---- | ---- | ---- |
| 固定指令（`/签到`、`/help`） | `command`（静态） | 走路由，必须 handler |
| 只在面板展示的命令说明 | `command`（dynamic=True） | 不参与路由，仅展示 |
| 监听所有文本（关键词 / 反垃圾 / 统计） | `on("message")` 或 `on_message` | 命令未命中时也会触发 |
| 原始消息接管（风控 / 转译 / 审计） | `on_raw_message` | 命令匹配之前触发，可 `return True` 接管整条消息 |
| 入群 / 退群 / 撤回等通知 | `on("notice.xxx")` | payload 为 dict |
| 插件间解耦通信 | `emit` / `on` | 自定义事件名 |
| 回复消息 | `send_msg` / `asend_msg` | 群私自动 |
| 禁言 / 踢人 / 查成员 | `ban` / `kick` / `get_member_*` | 群管快捷方法 |
| 读可在 Web 面板改的配置 | `get_config` | 热生效，带默认 |
| 持久化（建表 / 增删改查） | `create_table` / `db_query` / `db_execute` | `%s` 占位符 |
| 定时跑（每日报告 / 心跳） | `task` | 无参函数 |
| 仪表盘卡片 | `dashboard_card` | handler 返回卡片数据 |
| 后台页面 | `webui` | `sidebar=True` 在侧边栏独立入口 |
| 整体接管后台前端 | `override_webui` | 卸载自动回退 |
| 管理页扩展列 / 面板 | `register_group_extension` / `register_user_extension` | WebUI 扩展 |
| 自定义 REST 接口 | `register_api` | Flask 视图 |
| 多轮会话（问答式交互） | `wait_for` / `create_session` | 需启用 session 插件 |
| 权限判断 | `has_perm` / `is_superuser` / `is_group_admin` | 见 [权限与身份](#八权限与身份) |
| 扩展内核（启动 / 分发 / 协议钩子） | `hook` | 见 [Hook 系统](hooks.md) |
| 后台跑耗时任务 | `run_async` | 提交线程池，不阻塞 |
| 写审计日志 | `audit_log` | 以插件身份记录 |

> 想用更简洁的声明式写法（把注册写在函数定义处）？见 [插件装饰器 API](plugin-decorators.md)：与上面的 `ctx.xxx(...)` 完全等价，只是少写样板。

---

> 想在这些能力之外插入自己的行为？见 [扩展点（Hook 系统）](hooks.md)：在启动/关闭、Web 请求、事件分发、命令执行、协议动作、出站文本等几乎每个运行环节挂接逻辑。
