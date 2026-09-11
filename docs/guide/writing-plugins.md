# 编写插件

> **本篇面向**：写业务插件的 Python 开发者。假设你已按 [安装](./installation.md) 把宿主跑起来、看到了「框架启动完成」日志。
>
> 本章用一个**完整的「每日签到」插件**带你从零写起。每个知识点都先给能跑的代码，再逐行拆讲它：为什么这么写、不写会怎样。读完你就能写任意插件。

## 0. 我们要做一个什么插件

「每日签到」：在群里发 `/签到` → 记一次签到并累计积分；发 `/我的积分` → 查看自己攒了多少分。

麻雀虽小，它用到了插件开发的全部核心能力：

| 能力 | 用在哪 |
| ---- | ---- |
| 插件元信息 | `__plugin_meta__` 声明名字、版本、作者 |
| 注册入口 | `register(ctx)` 里登记命令 |
| 命令注册 | `/签到` `/我的积分` 两个命令 |
| 发消息 | 回复签到结果 |
| 数据库 | 存签到记录与积分 |
| 配置 | 单次签到给多少分（Web 面板可改） |
| 定时任务 | 每天 0 点清理过期标记 |
| 事件订阅 | 新成员入群送初始积分 |
| 日志 / 审计 | 记录签到操作 |

## 1. 最小可运行插件

先写一个能跑的最简插件，确认环境没问题。

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

启动宿主（或到 Web 面板「插件」页点「重载」），向任意已接入的平台发送 `/hello` 即可看到回复。

> 发消息需要至少一个就绪的**接入端**（接入端负责把消息真正发到某个平台）。如果你还没对接平台，可以先读 [对接 IM 平台](./connect-im.md) 把连接打通；或者走「纯定时任务 / HTTP 事件注入」两条不需要聊天的路线（见 [开始使用](./getting-started.md)）。

## 2. 逐行讲解

### 2.1 `__plugin_meta__`：插件身份证

```python
__plugin_meta__ = {
    "name": "Hello",
    "version": "1.0.0",
    "author": "你的名字",
    "desc": "一个简单的插件",
    "priority": 50,
}
```

| 字段 | 必填 | 说明 |
| ---- | ---- | ---- |
| `name` | 是 | 显示名，出现在 Web 面板和帮助菜单 |
| `version` | 是 | 版本号，升级插件时改它 |
| `author` | 是 | 作者 |
| `desc` | 否 | 一句话描述 |
| `priority` | 否 | 加载 / 匹配优先级，数字越小越先加载、越先匹配，默认 50 |

> **为什么需要 priority？** 一条消息到来时，框架按插件优先级从小到大依次尝试匹配命令。两个插件都注册了 `/help` 时，priority 小的赢。命令的注册顺序和加载顺序也都跟它有关。

元信息也可以写在 `plugin.yaml` 里，且 **`plugin.yaml` 的值会覆盖 `__plugin_meta__`**（便于不改代码改版本号）。

### 2.2 `register(ctx)`：唯一注册入口

```python
def register(ctx):
    ctx.command("/hello", handle_hello)
```

`register` 是框架规定的**唯一入口**，插件（重新）加载时框架调用它一次，把 `ctx`（插件上下文）交给你——**所有能力都通过 `ctx` 调用**。

- `ctx.command(命令名, 处理函数)`：注册一个命令。
- 你还能在 `register` 里注册定时任务 `ctx.task(...)`、订阅事件 `ctx.on(...)`、挂仪表盘卡片 `ctx.dashboard_card(...)`。

> **为什么 register 只注册、不干活？** 框架需要一个「清单」：你到底有哪些命令 / 任务 / 事件。登记好之后它才能在消息到来时找到对应函数。**没有 register，插件不会被加载。**

:::warning register 会被重复调用
心跳检测到文件变化、Web 面板点「重载」时都会重新执行 `register(ctx)`。这里只做「登记」，不要写只能执行一次的副作用（比如建无限循环线程）。一次性初始化放到 `on_loaded(ctx)` 钩子。
:::

### 2.3 处理函数签名 `(event, match)`

```python
def handle_hello(event, match):
    ...
```

这是命令处理函数的固定格式：

| 参数 | 是什么 | 举例 |
| ---- | ---- | ---- |
| `event` | 这条消息的**事件对象**：谁发的、在哪发的、发了什么 | `event.user_id` = 发送者用户 ID |
| `match` | 命令匹配结果，`match.group(1)` 取命令后面的参数 | 发 `/echo 你好`，`match.group(1)` = `"你好"` |

- `match` 命令没带参数时可能是 `None`，用 `if match:` 判断后再取 `match.group(1)`。
- 支持异步：函数写成 `async def handle(event, match):`，里面就能 `await`。

> **注意 ctx 哪去了？** `register` 之后，框架把 `ctx` 注入到模块全局变量，所以 `main.py` 里任何函数都能直接用 `ctx`。你**不需要**（也不应该）在函数参数里加 `ctx`。

### 2.4 发消息：`ctx.send_msg` / `ctx.asend_msg`

```python
ctx.send_msg(
    user_id=event.user_id,
    group_id=event.group_id if event.is_group else None,
    message="今天已经签到过了",
)
```

| 参数 | 作用 |
| ---- | ---- |
| `user_id` | 发给哪个用户（私聊 / 群内指定目标） |
| `group_id` | 发到哪个群组；与 `user_id` 同时给时按群聊处理 |
| `message` | 文本消息内容 |

**关键写法**：`group_id=event.group_id if event.is_group else None`
- 在群里 → `event.is_group` 为 `True` → 回**群组**；
- 私聊 → `event.is_group` 为 `False` → `group_id=None` → 回**私聊**。

一条代码同时做到「群回群、私回私」，不用自己判断。

:::tip 同步 / 异步
同步版 `ctx.send_msg(...)` 会在内部丢到线程执行，不阻塞；`async def` 处理函数里推荐用异步版 `await ctx.asend_msg(...)`，效果相同且不阻塞事件循环。
:::

> 需要发图片、@、回复等富媒体，或做禁言、踢人、查群成员等**平台操作**？这些能力依赖你启用的接入端，详见 [对接 IM 平台](./connect-im.md)。

## 3. 把签到插件写完（完整代码）

下面给出可运行的全貌，之后几节再把每个陌生点拆开讲。

```python
# plugins/sign_in/main.py
import time, random

__plugin_meta__ = {
    "name": "每日签到",
    "version": "1.0.0",
    "author": "你的名字",
    "desc": "每日签到领积分，连续签到有奖励",
    "priority": 50,
}

def register(ctx):
    # 建表（插件会被反复热加载，必须 IF NOT EXISTS）
    ctx.create_table("""
        CREATE TABLE IF NOT EXISTS sign_in_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            day TEXT,
            score INTEGER DEFAULT 0
        )
    """)
    ctx.command("/签到", handle_sign, alias="/sign", description="每日签到")
    ctx.command("/我的积分", handle_score, description="查看我的积分")
    ctx.task("0 0 * * *", reset_daily, description="每日清理过期标记")  # 由 scheduler 插件提供
    ctx.on("member_increase", on_new_member)                          # 新成员入群

def handle_sign(event, match):
    today = time.strftime("%Y-%m-%d")
    signed = ctx.db_query_one(
        "SELECT id FROM sign_in_records WHERE user_id=%s AND day=%s",
        (event.user_id, today))
    if signed:
        ctx.send_msg(user_id=event.user_id,
                     group_id=event.group_id if event.is_group else None,
                     message="你今天已经签到过了")
        return
    score = random.randint(1, 10)
    ctx.db_execute(
        "INSERT INTO sign_in_records (user_id, day, score) VALUES (%s,%s,%s)",
        (event.user_id, today, score))
    ctx.send_msg(user_id=event.user_id,
                 group_id=event.group_id if event.is_group else None,
                 message=f"签到成功！获得 {score} 积分")

def handle_score(event, match):
    row = ctx.db_query_one(
        "SELECT COALESCE(SUM(score),0) AS total FROM sign_in_records WHERE user_id=%s",
        (event.user_id,))
    total = row["total"] if row else 0
    ctx.send_msg(user_id=event.user_id,
                 group_id=event.group_id if event.is_group else None,
                 message=f"你当前积分：{total}")

def reset_daily():
    # 定时任务：无 event 参数
    pass

def on_new_member(payload):
    # 事件订阅：payload 是 dict
    user_id = payload.get("user_id")
    if user_id:
        ctx.db_execute(
            "INSERT INTO sign_in_records (user_id, day, score) VALUES (%s,'welcome',100)",
            (user_id,))
```

> 上面 `ctx.task` 的调度能力由官方插件 `scheduler` 提供（默认开启）；`member_increase` 这类事件由你启用的接入端产生，具体事件列表见 [对接 IM 平台](./connect-im.md)。

## 4. 数据库：建表与查询

### 4.1 建表

```python
def register(ctx):
    ctx.create_table("""
        CREATE TABLE IF NOT EXISTS sign_in_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            day TEXT,
            score INTEGER DEFAULT 0
        )
    """)
```

- `CREATE TABLE IF NOT EXISTS`：表不存在才建，重复加载不报错（**必须加**，插件会被反复热加载）。
- 你写 `AUTOINCREMENT`（MySQL 语法）也没关系，框架自动翻译成 SQLite 的 `AUTOINCREMENT`。
- **加前缀避免冲突**：表名建议带插件名，如 `sign_in_records`，别叫 `users`（和框架表重名）。

### 4.2 增删改查

```python
row  = ctx.db_query_one("SELECT * FROM t WHERE user_id=%s", (uid,))   # 单条
rows = ctx.db_query("SELECT * FROM t WHERE group_id=%s", (gid,))        # 多条
n    = ctx.db_execute("UPDATE t SET score=score+1 WHERE id=%s", (id_,)) # 受影响行数
new_id = ctx.db_insert("INSERT INTO t (user_id) VALUES (%s)", (uid,))   # 自增 ID

# 异步版本（async handler 推荐，走 DB 专用线程池，不阻塞事件循环）
row = await ctx.db_query_one_async(sql, params)
await ctx.db_execute_async(sql, params)
```

### 4.3 两个关键约定

1. **占位符用 `%s`，不要拼字符串**：

   ```python
   # 正确：参数用 %s 占位，值放第二个参数元组
   ctx.db_query("SELECT * FROM t WHERE user_id=%s", (event.user_id,))
   # 错误：直接拼进 SQL，有注入风险
   ctx.db_query(f"SELECT * FROM t WHERE user_id={event.user_id}")
   ```

2. **框架自动适配 SQLite / MySQL**：统一写 `%s`，框架翻译成对应方言。你按 MySQL 习惯写（如 `ON DUPLICATE KEY UPDATE`），框架自动转成 SQLite 语法。

### 4.4 事务

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

## 5. 配置项：让用户能在 Web 面板改

想让「单次签到给 1–10 分」变成可配置？两步。

### 5.1 声明 schema：`_conf_schema.json`

放在插件配置目录（见下方 15 节），文件名为 `_conf_schema.json`：

```json
{
  "score_max": {
    "description": "单次签到最高积分",
    "type": "number",
    "default": 10,
    "hint": "签到随机给 1 到此值"
  }
}
```

支持 `string` / `number` / `select`（带 `options`）等类型，Web 面板据此生成表单。

### 5.2 代码里读取

```python
def handle_sign(event, match):
    score_max = ctx.get_config("score_max", 10)   # 第二个参数是默认值
    score = random.randint(1, score_max)
```

- `ctx.get_config("score_max", 10)`：读取配置，**没配置时用默认值 10**。
- 用户改配置**不用重启**，热生效。
- 一次性取全部：`cfg = ctx.get_all_config()`。

## 6. 定时任务

```python
def register(ctx):
    ctx.task("0 8 * * *", daily_report, description="每日 8 点报告")
    ctx.task("*/5 * * * *", heartbeat, description="每 5 分钟")
```

cron 格式为 `分 时 日 月 周`。任务 ID 自动生成为 `<插件名>_<函数名>`。

> **定时任务函数不能带 event 参数**——它是一条到点自动触发的指令，没有「谁发的」这个概念。签名固定为无参：
> ```python
> def daily_report():   # 正确：无参
> def daily_report(event):   # 错误！会报参数不匹配
> ```

## 7. 事件订阅

```python
def register(ctx):
    ctx.on("message", on_message)                 # 命令未命中时的文本消息
    ctx.on("member_increase", on_new_member)      # 新成员入群

def on_new_member(payload):
    user_id = payload.get("user_id")
    ...
```

- `ctx.on()` 的 handler 同步异步均可。
- 事件回调收到的是 **`dict`**（不是 Event 对象），用 `.get("key")` 取值，键不存在返回 `None` 而不是报错。
- 插件之间也能用 `ctx.emit(name, payload)` / `await ctx.aemit(...)` 自定义事件通信。
- 常用内置事件见 [架构详解 · 事件总线](../advanced/architecture.md)。具体有哪些事件（消息、通知、成员变动等）取决于你启用的接入端，见 [对接 IM 平台](./connect-im.md)。

## 8. 拆分多文件与相对导入

插件做大后必然要拆文件。ZCBOT 支持标准 Python 包式的**相对导入**（推荐），也兼容旧的短名绝对导入：

```python
# plugins/chatroom/main.py
from .ws_server import WsServer     # 推荐：相对导入同目录模块
from . import utils                # 推荐：导入整个兄弟模块
from .core.engine import Engine    # 推荐：导入子包模块
from ws_server import WsServer     # 兼容：旧写法仍可用（短名绝对导入）
```

:::tip 机制速记
框架把 `main.py` 加载成一个「合成包」`plugin_<插件名>`（带 `__path__`），子模块同时拥有 `plugin_chatroom.ws_server`（相对导入用）和 `ws_server`（短名导入用）两个名字。**新代码一律用相对导入**，彻底避免多个插件存在同名文件时互相串模块。完整原理见 [插件加载与模块机制](../advanced/loader.md)。
:::

## 9. 多轮会话

```python
async def handle_survey(event, match):
    name = await ctx.wait_for(event, prompt="你叫什么名字？", timeout=60)
    if name is None:
        await ctx.asend_msg(..., message="超时了")
        return
    # 更复杂的多轮用 async with ctx.create_session(...)
```

完整用法见 [多轮会话](./session.md)。

## 10. 生命周期钩子

除了 `register(ctx)`，主模块还能定义两个可选钩子：

```python
def on_loaded(ctx):
    """插件首次加载 / 完全重载、register 完成后触发一次（适合一次性初始化）"""

def on_unload():
    """插件被卸载 / 完全重载前触发（关闭线程、连接、文件句柄等清理工作）"""
```

:::warning 心跳重注册不会触发 on_loaded
`on_loaded` 只在真正（重新）加载后触发一次，框架用标记位保证不会每次心跳都跑。
:::

## 11. WebUI 与扩展

```python
def register(ctx):
    ctx.webui(title="我的面板", entry="index.html", icon="", order=50)        # 管理后台加一个插件页
    ctx.register_group_extension("sign_days", "签到天数", get_sign_days)       # 群组管理页加一列
    ctx.register_user_extension("level", "等级", get_user_level)               # 用户管理页加扩展
```

`ctx.override_webui()` 还能让插件整体接管管理后台前端。

## 12. 日志与审计

```python
ctx.log("启动完成")
ctx.log("外部接口超时", level="warning")     # debug/info/warning/error
ctx.audit_log("sign_in", target_type="user", target_name=str(event.user_id))
```

## 13. 权限控制

```python
async def handle_ban(event, match):
    if not event.has_perm("admin.ban"):      # 权限节点，支持通配符 admin.*
        await ctx.asend_msg(..., message="权限不足")
        return
```

`ctx.has_perm(uid, node)` / `ctx.check_perm(uid, node)`（三态）/ `ctx.is_superuser(uid)` / `ctx.get_user_role(g, u)` 等详见 [权限系统](../advanced/permission.md)。

## 14. 耗时操作别堵住事件循环

图片渲染、文件处理、外部 HTTP 等耗时活，丢到内置线程池后台跑：

```python
def render_and_send():
    path = renderer.render(...)
    ctx.send_msg(..., message=f"渲染完成：{path}")

ctx.run_async(render_and_send)   # 返回 concurrent.futures.Future
```

## 15. 把插件装上去

1. 文件夹建在 `plugins/` 下，名字用英文小写；入口文件必须叫 `main.py`。
2. 到 Web 面板「插件」页点 **重载**（或重启宿主）。
3. 发命令测试。

**改了代码后**：再点一次「重载」即可，不用重启整个宿主。

**配置 / 数据放哪**：插件的 `_conf_schema.json`、`plugin.yaml` 等配置类文件在加载时会被框架自动迁移到 `data/plugins_dat/<插件名>/`；运行期产生的缓存、用户数据用 `ctx.get_data_dir()` 获取，写进这个目录，**不要写进代码目录**（插件更新覆盖时代户数据会丢）。

## 16. 常见错误自查

| 现象 | 原因与处理 |
| ---- | ---- |
| 插件列表里没有它 | 缺 `register(ctx)` / 目录名带中文 / `main.py` 语法错误 |
| 命令不触发 | pattern 写错 / 被别的插件抢了（priority） / 没在 `register` 注册 |
| 发命令没反应但日志报错 | 处理函数抛异常了，加 try/except 或看日志 |
| 发消息报「无可用协议适配器」 | 当前没有任何就绪的接入端；先按 [对接 IM 平台](./connect-im.md) 启用并连上一个接入端 |
| 定时任务报参数错误 | 定时函数带了 `event` 参数，改成无参 |
| 数据库报 SQL 错 | 占位符用错（要用 `%s`） / 表没建（`create_table` 在 `register` 里） |
| 改了函数逻辑没生效 | 心跳只重注册；函数体改动需在面板点「重载」 |
| 多插件同名文件互相串 | 改用相对导入 `from .xxx import`，别依赖短名 |
| handler 里阻塞导致卡顿 | 改 `async def` + 异步 DB/API，耗时活用 `ctx.run_async` |

## 17. 下一步

- [对接 IM 平台（以 QQ / OneBot 11 为例）](./connect-im.md) —— 让框架真正接入聊天平台
- [多文件插件与模块机制](../advanced/loader.md) —— 相对导入 / 短名 / 热重载原理
- [多轮会话](./session.md) —— 交互式对话
- [配置系统](./configuration.md) —— config.yaml 与插件配置
- [Event 对象](../api/basic/event.md) / [ctx 全量 API](../api/basic/ctx.md)
- [定时任务](../advanced/scheduler.md) / [权限系统](../advanced/permission.md)
