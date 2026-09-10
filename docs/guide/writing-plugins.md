# 编写插件

> **本篇面向**：角色 B（写业务插件的 Python 开发者）。假设你已能按[安装](./installation.md)把宿主跑起来。

本章从零开始，手把手教你写一个完整的 ZCBOT 插件，并覆盖多文件拆分、配置、
数据库、定时任务、事件订阅、生命周期等实际开发会遇到的全部主题。

## 插件目录结构

每个插件是 `plugins/` 下的一个子目录，**必须包含 `main.py`**，其余按需添加：

```
plugins/
└── my_plugin/
    ├── main.py             # 插件入口（必须），里面定义 register(ctx)
    ├── plugin.yaml         # 插件元信息/依赖/更新源（推荐）
    ├── _conf_schema.json   # Web 配置项 schema（可选）
    ├── requirements.txt    # 第三方 Python 依赖（可选，启动时自动安装）
    ├── helper.py           # 同目录子模块（可选，多文件拆分）
    ├── core/               # 子包（可选，需要 __init__.py）
    │   ├── __init__.py
    │   └── engine.py
    ├── web/                # 内嵌 Web 页面（可选，配合 ctx.webui）
    │   └── index.html
    └── assets/             # 字体/图片等静态资源（可选）
```

:::tip 代码目录与数据目录分离
插件**代码**放在 `plugins/my_plugin/`；运行期产生的配置、缓存、用户数据放在
`data/plugins_dat/my_plugin/`，用 `ctx.get_data_dir()` 获取，不要写进代码目录，
否则插件更新覆盖时代户数据会丢失。配置类文件（`plugin.yaml`、`_conf_schema.json` 等）
在安装/更新时会被框架自动迁移到 `plugins_dat`。
:::

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
    "priority": 50,            # 优先级（越小越先加载、越先匹配）
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | 插件显示名 |
| `version` | 是 | 语义化版本号 |
| `author` | 是 | 作者名 |
| `desc` | 否 | 一句话描述 |
| `priority` | 否 | 加载/匹配优先级，默认 50 |

元数据也可以写在 `plugin.yaml` 里，且 **`plugin.yaml` 的值会覆盖
`__plugin_meta__`**（便于不改代码改版本号）。

### `register(ctx)`：注册入口

**`register` 是框架规定的唯一入口**，插件（重新）加载时框架调用它，
所有命令、任务、事件订阅都在这里登记：

```python
def register(ctx):
    ctx.command("/hello", handle_hello)   # 命令
    ctx.task("0 8 * * *", daily)          # 定时任务
    ctx.on("notice.group_increase", on_join)   # 事件订阅
    ctx.on_raw_message(raw_handler)      # 原始消息处理器
    ctx.dashboard_card("在线数", get_count)    # 仪表盘卡片
```

:::warning register 会被重复调用
心跳检测到文件变化、Web 面板重载时都会重新执行 `register(ctx)`。
这里只做“登记”，不要写只能执行一次的副作用（比如在这里建无限循环线程）。
一次性初始化逻辑放到 `on_loaded(ctx)` 钩子里。
:::

### 处理函数签名

```python
def handle(event, match):       # 同步
    ...

async def handle(event, match):  # 异步（推荐）
    ...
```

| 参数 | 说明 |
|------|------|
| `event` | `Event` 对象，含 `user_id`、`group_id`、`message`、`segments` 等，详见 [Event API](../api/event.md) |
| `match` | 正则匹配结果；命令后的参数统一用 `match.group(1)` 获取 |

同步 handler 会被自动丢到线程执行，`async def` handler 直接在事件循环里跑。
**推荐异步**，并使用 `await ctx.asend_msg(...)` / `await ctx.aapi(...)` 等异步方法，
避免阻塞消息处理。

## 多文件插件：子模块怎么 import

插件做大后必然要拆文件。ZCBOT 支持标准 Python 包式的**相对导入**（推荐），
也兼容旧的短名绝对导入：

```python
# plugins/chatroom/main.py
from .ws_server import WsServer     # 推荐：相对导入同目录模块
from . import utils                # 推荐：导入整个兄弟模块
from .core.engine import Engine    # 推荐：导入子包模块
from ws_server import WsServer     # 兼容：旧写法仍可用（短名绝对导入）
```

```python
# plugins/chatroom/core/engine.py
from ..utils import log            # 推荐：回到上一层
```

:::tip 机制速记
框架会把 `main.py` 加载成一个“合成包” `plugin_<插件名>`（带 `__path__`），
子模块同时拥有 `plugin_chatroom.ws_server`（相对导入用）和 `ws_server`
（短名导入用）两个名字，指向同一对象。**新代码一律用相对导入**，
彻底避免多个插件存在同名文件时互相串模块。完整原理见
[插件加载与模块机制](../advanced/loader.md)。
:::

## 注册命令

### 基本用法与别名

```python
def register(ctx):
    ctx.command("/hello", handle_hello, description="打招呼")
    ctx.command("/time", handle_time, description="查看时间")
    ctx.command("/help_me", handle_help, alias=["/h", "/帮助"], description="帮助")
```

`alias` 支持列表或逗号分隔字符串。如果没传 `description`，
框架会取 handler docstring 的第一行作为描述。

### 完整参数

```python
ctx.command(
    pattern,                       # 命令名或正则
    handler,                       # (event, match) -> None
    priority=50,                   # 匹配优先级，越小越优先
    dynamic=False,                 # True=仅在“动态命令”展示，不参与路由
    alias=None,                    # 别名，列表或 "/a,/b"
    description=None,              # 描述
    require_admin=False,           # 需 群主/管理员/超管
    require_superuser=False,       # 需超级管理员（优先级高于 admin）
    require_perm=None,             # 需指定权限节点，如 'myplugin.ban'
)
```

`require_admin` / `require_superuser` 是身份轴判定，`require_perm` 是权限组判定，
两者可叠加（同时满足）。需要「A 或 B」这类组合时，在 handler 内自行用
`event.has_perm()` / `event.role` 判断。

### 匹配规则与取参数

- **普通命令名**（如 `/echo`，不含正则元字符）：前缀匹配；
- **正则模式**（含 `^ $ . * + ?` 等）：用 `re.search()` 匹配；
- 命令后的参数统一从 `match.group(1)` 取，因此正则里通常用一个捕获组兜住参数：

```python
def register(ctx):
    ctx.command(r"/echo\s+(.+)", handle_echo, description="回声")

async def handle_echo(event, match):
    text = match.group(1).strip()
    await ctx.asend_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=text,
    )
```

发送 `/echo 你好世界`，`match.group(1)` 即 `"你好世界"`。

## 发送消息与调用接入端 API（默认 OneBot）

### 快捷发送

```python
# 同步
ctx.send_msg(user_id=..., group_id=..., message="文本或CQ码", auto_escape=False)
# 异步（推荐）
await ctx.asend_msg(user_id=event.user_id,
                    group_id=event.group_id if event.is_group else None,
                    message="Hello!")
```

同时给 `user_id` 和 `group_id` 时按群聊处理；只给 `user_id` 为私聊。
不指定 `bot` 时自动使用当前消息来源的 bot 实例（多账号安全）。

### 群管快捷方法（均有同步/`a` 异步两个版本）

| 同步 | 异步 | 说明 |
|------|------|------|
| `ctx.ban(g, u, duration=600)` | `ctx.aban(...)` | 禁言秒数，0 为解禁 |
| `ctx.kick(g, u)` | `ctx.akick(...)` | 踢出成员（可 `reject_add_request`） |
| `ctx.mute_all(g, True)` | `ctx.amute_all(...)` | 全员禁言 |
| `ctx.set_card(g, u, card)` | `ctx.aset_card(...)` | 设置群名片 |
| `ctx.get_member_list(g)` | `ctx.aget_member_list(g)` | 群成员列表 |
| `ctx.get_member_info(g, u)` | `ctx.aget_member_info(g, u)` | 成员信息 |

### 通用 API：任意接入端 action

`ctx.api/aapi` 走协议无关的 `api_caller`：默认接入端是 OneBot，动作名即 OneBot action；换成其它接入端后用该接入端的动作名。快捷方法没覆盖的动作都能这样直接调：

```python
await ctx.aapi("set_group_leave", group_id=123456)
data = await ctx.aapi("get_group_member_list", group_id=123456)
```

## 事件订阅

```python
def register(ctx):
    ctx.on("message", on_message)
    ctx.on("notice.group_increase", on_member_join)

async def on_message(payload):
    """命令未命中时的文本消息 / 对应类型通知事件"""
    ...
```

`ctx.on()` 的 handler 同步异步均可。常用内置事件见
[架构详解 - 事件总线](../advanced/architecture.md#事件总线)。
插件之间也可以用 `ctx.emit()` / `await ctx.aemit()` 自定义事件通信。

### 原始消息处理器：在命令匹配之前接管

```python
def register(ctx):
    ctx.on_raw_message(raw_handler)

async def raw_handler(raw_event: dict, bot_name: str):
    # raw_event 是未经提取文本的原始 OneBot 事件（消息段数组原样保留）
    if 想接管:
        return True        # 返回 True = 本消息被接管，后续命令/关键词/广播全部跳过
    # 返回 None/False = 继续正常流程
```

多个原始处理器按插件 `priority` 升序执行，单个异常不影响其他插件。

## 控制事件传播

```python
async def handle(event, match):
    event.stop_event()        # 本插件之后的插件不再收到此事件
    await ctx.asend_msg(..., message="已处理")
```

- `event.stop_event()` / `event.is_stopped()`：停止向下传播；
- `event.continue_route()`：命令命中后默认不再触发系统关键词自动回复，
  调用它可放行让关键词回复继续尝试。

## 读取富媒体消息

`event.segments` 是消息段数组，框架还提供了一组便捷属性：

```python
event.has_image / event.images / event.first_image
event.has_at / event.at_list / event.at_all / event.has_at_bot
event.has_reply / event.reply_id
event.has_voice / event.has_video / event.has_file / event.has_face / event.has_share
```

示例：

```python
async def handle_img(event, match):
    if not event.has_image:
        await ctx.asend_msg(..., message="请发一张图片")
        return
    url = event.first_image.get("url")
```

完整属性见 [Event API](../api/event.md)。

## 配置项

### 1. 声明 schema：`_conf_schema.json`

```json
{
  "api_key": {"type": "string", "default": "", "description": "API 密钥", "hint": "在第三方平台获取"},
  "timeout": {"type": "number", "default": 30, "description": "超时秒数"},
  "mode": {
    "type": "select", "default": "auto", "description": "工作模式",
    "options": [{"label": "自动", "value": "auto"}, {"label": "手动", "value": "manual"}]
  }
}
```

### 2. 在代码里读取

```python
api_key = ctx.get_config("api_key", default="")   # 带 30s TTL 缓存
cfg = ctx.get_all_config()                        # 一次性取全部
```

配置由 Web 面板写入数据库，`get_config` 自动做 JSON 反序列化，读不到时返回 default。

## 数据库

### 建表（自动适配方言）

```python
def register(ctx):
    ctx.create_table("""
        CREATE TABLE IF NOT EXISTS sign_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            day TEXT,
            score INTEGER DEFAULT 0
        )
    """)
```

统一按 MySQL 风格写（`%s` 占位、`AUTOINCREMENT`），框架自动翻译成 SQLite 方言，
插件无需判断当前数据库类型。

### 增删改查（同步 / 异步）

```python
row  = ctx.db_query_one("SELECT * FROM t WHERE user_id=%s", (uid,))
rows = ctx.db_query("SELECT * FROM t WHERE group_id=%s", (gid,))
n    = ctx.db_execute("UPDATE t SET score=score+1 WHERE id=%s", (id_,))
new_id = ctx.db_insert("INSERT INTO t (user_id) VALUES (%s)", (uid,))
# 异步版本（async handler 推荐，走 DB 专用线程池，不阻塞事件循环）
row = await ctx.db_query_one_async(sql, params)
await ctx.db_execute_async(sql, params)
```

### 事务

```python
conn = ctx.db_connection()
try:
    cur = conn.cursor()
    cur.execute("UPDATE account SET balance=balance-%s WHERE id=%s", (100, a))
    cur.execute("UPDATE account SET balance=balance+%s WHERE id=%s", (100, b))
    conn.commit()
except Exception:
    conn.rollback()
finally:
    conn.close()   # 连接池模式下为归还连接
```

详见 [数据库](../advanced/database.md)。

## 定时任务

```python
def register(ctx):
    ctx.task("0 8 * * *", daily_report, description="每日 8 点报告")
    ctx.task("*/5 * * * *", heartbeat, description="每 5 分钟")
```

cron 格式为 `分 时 日 月 周`。任务 ID 自动生成为 `<插件名>_<函数名>`。
注意：**任务函数不接收参数、也不要依赖 handler 里的局部变量**，
需要框架能力时在函数内部通过服务注册表获取，详见 [定时任务](../advanced/scheduler.md)。

## 多轮会话

```python
async def handle_survey(event, match):
    name = await ctx.wait_for(event, prompt="你叫什么名字？", timeout=60)
    if name is None:
        await ctx.asend_msg(..., message="超时了")
        return
    # 更复杂的多轮用 async with ctx.create_session(...)
```

完整用法见 [多轮会话](./session.md)。

## 生命周期钩子

除了 `register(ctx)`，主模块还可以定义两个可选钩子：

```python
def on_loaded(ctx):
    """插件首次加载/完全重载、register 完成后触发一次（适合做一次性初始化）"""

def on_unload():
    """插件被卸载/完全重载前触发（关闭线程、连接、文件句柄等清理工作）"""
```

:::warning 心跳重注册不会触发 on_loaded
`on_loaded` 只在真正（重新）加载后触发一次，框架用标记位保证不会每次心跳都跑。
:::

## 耗时操作别堵住事件循环

图片渲染、文件处理、外部 HTTP 等耗时活，丢到内置线程池后台跑：

```python
def render_and_send():
    path = renderer.render(...)
    ctx.send_msg(..., message=f"[CQ:image,file=file:///{path}]")

ctx.run_async(render_and_send)   # 返回 concurrent.futures.Future
```

## 权限控制

```python
async def handle_ban(event, match):
    if not event.has_perm("admin.ban"):      # 权限节点，支持通配符 chat.*
        await ctx.asend_msg(..., message="权限不足")
        return
```

`ctx.has_perm(uid, node)` / `ctx.check_perm(uid, node)`（三态）/
`ctx.is_superuser(uid)` / `ctx.get_user_role(g, u)` 等详见
[权限系统](../advanced/permission.md)。

## WebUI 能力

```python
def register(ctx):
    # 在管理后台加一个插件页面（资源放插件 web/ 目录）
    ctx.webui(title="我的面板", entry="index.html", icon="", order=50)
    # 在「群组管理」页加一列/一个详情面板
    ctx.register_group_extension("sign_days", "签到天数", get_sign_days)
    # 在「用户管理」页加扩展
    ctx.register_user_extension("level", "等级", get_user_level)
```

`ctx.override_webui()` 还能让插件整体接管管理后台前端。

## 日志与审计

```python
ctx.log("启动完成")
ctx.log("外部接口超时", level="warning")     # debug/info/warning/error
ctx.audit_log("sign_in", target_type="user", target_name=str(event.user_id))
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
    ctx.create_table("""
        CREATE TABLE IF NOT EXISTS sign_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            day TEXT,
            score INTEGER DEFAULT 0
        )
    """)
    ctx.command("/签到", handle_sign, description="每日签到")
    ctx.command("/积分", handle_score, description="查看积分")

async def handle_sign(event, match):
    import time, random
    today = time.strftime("%Y-%m-%d")
    row = await ctx.db_query_one_async(
        "SELECT id FROM sign_records WHERE user_id=%s AND day=%s",
        (event.user_id, today))
    if row:
        await ctx.asend_msg(user_id=event.user_id,
                            group_id=event.group_id if event.is_group else None,
                            message="今天已经签到过了")
        return
    score = random.randint(1, 10)
    await ctx.db_insert_async(
        "INSERT INTO sign_records (user_id, day, score) VALUES (%s,%s,%s)",
        (event.user_id, today, score))
    await ctx.asend_msg(user_id=event.user_id,
                        group_id=event.group_id if event.is_group else None,
                        message=f"签到成功！获得 {score} 积分")

async def handle_score(event, match):
    row = await ctx.db_query_one_async(
        "SELECT COALESCE(SUM(score),0) AS total FROM sign_records WHERE user_id=%s",
        (event.user_id,))
    total = row["total"] if row else 0
    await ctx.asend_msg(user_id=event.user_id,
                        group_id=event.group_id if event.is_group else None,
                        message=f"当前积分：{total}")
```

## 常见错误自查

| 现象 | 原因与处理 |
|------|-----------|
| `attempted relative import with no known parent package` | 绕过框架直接运行了脚本，或框架过旧；用相对导入并由框架加载，详见[模块机制](../advanced/loader.md) |
| `缺少 register(ctx) 函数` | `main.py` 必须定义可调用的 `register(ctx)` |
| 改了函数逻辑没生效 | 心跳只重注册；函数体改动需在面板点「重载」 |
| `无可用协议适配器` | 当前没有任何已就绪接入端；在 `core_plugins.yaml` 启用一个接入端（默认即 onebot_adapter），或等其就绪后再调 API |
| 多插件同名文件互相串 | 改用相对导入 `from .xxx import`，不要依赖短名 |
| handler 里阻塞导致机器人卡顿 | 改 `async def` + 异步 DB/API，耗时活用 `ctx.run_async` |

## 下一步

- [多文件插件与模块机制](../advanced/loader.md) —— 相对导入/短名/热重载原理
- [多轮会话](./session.md) —— 交互式对话
- [配置系统](./configuration.md) —— config.yaml 与插件配置
- [Event 对象](../api/event.md) / [ctx 全量 API](../api/ctx.md)
- [定时任务](../advanced/scheduler.md) / [权限系统](../advanced/permission.md)
