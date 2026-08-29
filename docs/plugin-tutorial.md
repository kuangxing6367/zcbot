# 插件开发详解：一个示例插件，逐行讲透每个语法

> 面向**第一次写插件**的同学。我们用"每日签到"这个真实例子，把插件里出现过的**每一个语法**都拆开讲清楚：它是什么、为什么这么写、不写会怎样。
>
> 想快速上手先看 [快速入门](getting-started.md)；本教程是它的"加长讲解版"。

---

## 0. 我们要做一个什么插件

**每日签到**：群里发 `/签到` → 记一次签到，累计积分；再发 `/我的积分` → 查看自己攒了多少分。

它麻雀虽小五脏俱全，用到了插件开发的**全部核心能力**：

| 能力 | 用在哪 |
| ---- | ---- |
| 命令注册 | `/签到` `/我的积分` 两个命令 |
| 发消息 | 回复签到结果 |
| 数据库 | 存签到记录和积分 |
| 配置 | 单次签到给多少分（可改） |
| 定时任务 | 每天 0 点重置"今天已签到"标记 |
| 事件订阅 | 新成员入群送 100 分 |
| 日志 | 记录签到操作 |

---

## 1. 完整代码（先看全貌，后面逐行讲）

```python
# plugins/sign_in/main.py

__plugin_meta__ = {
    "name": "每日签到",
    "version": "1.0.0",
    "author": "你的名字",
    "desc": "每日签到领积分，连续签到有奖励",
    "priority": 50,
}


def register(ctx):
    """框架加载插件时调用：在这里注册命令、任务、事件"""
    ctx.command("/签到", handle_sign_in, alias="/sign", description="每日签到")
    ctx.command("/我的积分", handle_my_score, description="查看我的积分")
    ctx.task("1 0 * * *", reset_daily_signin, description="每日重置签到")
    ctx.on("group_member_increase", on_new_member)


def handle_sign_in(event, match):
    """每日签到，获得 1-10 积分"""
    today = __import__("time").strftime("%Y-%m-%d")

    # 查今天有没有签过
    signed = ctx.db_query_one(
        "SELECT id FROM sign_in_records WHERE user_id = %s AND day = %s",
        (event.user_id, today)
    )
    if signed:
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message="你今天已经签到过了",
        )
        return

    # 随机给 1-10 分
    score = __import__("random").randint(1, 10)
    ctx.db_execute(
        "INSERT INTO sign_in_records (user_id, day, score) VALUES (%s, %s, %s)",
        (event.user_id, today, score)
    )
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=f"签到成功！获得 {score} 积分",
    )


def handle_my_score(event, match):
    """查看我的积分"""
    row = ctx.db_query_one(
        "SELECT SUM(score) AS total FROM sign_in_records WHERE user_id = %s",
        (event.user_id,)
    )
    total = row["total"] if row and row["total"] else 0
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=f"你当前积分：{total}",
    )


def reset_daily_signin():
    """定时任务：每天 00:01 执行（注意：定时任务没有 event 参数）"""
    # 连续签到判断逻辑这里省略，真实插件可在此清理过期数据
    pass


def on_new_member(payload):
    """事件订阅：新成员入群时触发"""
    group_id = payload.get("group_id")
    user_id = payload.get("user_id")
    if group_id and user_id:
        ctx.db_execute(
            "INSERT INTO sign_in_records (user_id, day, score) VALUES (%s, 'welcome', 100)",
            (user_id,)
        )
        ctx.send_msg(group_id=group_id, message=f"欢迎新成员 {user_id}，赠送 100 积分")
```

---

## 2. 逐行讲解

### 2.1 `__plugin_meta__`：插件的"身份证"

```python
__plugin_meta__ = {
    "name": "每日签到",
    "version": "1.0.0",
    "author": "你的名字",
    "desc": "每日签到领积分，连续签到有奖励",
    "priority": 50,
}
```

| 字段 | 作用 | 必填 |
| ---- | ---- | ---- |
| `name` | 插件显示名，会出现在 Web 面板和帮助菜单里 | ✅ |
| `version` | 版本号，升级插件时改它 | ✅ |
| `author` | 作者名 | ✅ |
| `desc` | 一句话说明这插件是干嘛的 | 否 |
| `priority` | **加载优先级**：数字越小越先加载、越先收到消息 | 否（默认 50） |

> 为什么要 `priority`？框架收到一条消息后，按插件优先级从小到大挨个尝试匹配。两个插件都想处理 `/help` 时，`priority` 小的赢。**命令注册顺序和加载顺序都跟它有关。**

### 2.2 `def register(ctx)`：插件的"注册入口"

```python
def register(ctx):
    ctx.command("/签到", handle_sign_in, alias="/sign", description="每日签到")
    ctx.task("1 0 * * *", reset_daily_signin, description="每日重置签到")
    ctx.on("group_member_increase", on_new_member)
```

**`register` 是框架规定必须存在的函数**，插件加载时框架会调用它一次，把参数 `ctx`（PluginContext，插件上下文）交给你——**所有能力都通过 `ctx` 调用**。

- **`ctx.command(命令名, 处理函数, ...)`**：注册一个命令。用户发 `/签到` 时，框架会调用 `handle_sign_in(event, match)`。
  - `alias="/sign"`：给命令起别名，发 `/sign` 也会触发。多个别名用逗号：`alias="/a,/b"`
  - `description="..."`：这个命令的说明，会显示在 `/help` 和 Web 面板里
- **`ctx.task(定时规则, 函数, ...)`**：注册定时任务。`"1 0 * * *"` 是 cron 表达式，意思是**每天 0 点 1 分**执行 `reset_daily_signin`。格式是"分 时 日 月 周"，所以 `"*/5 * * * *"` 是每 5 分钟。
- **`ctx.on(事件名, 函数)`**：订阅一个**系统事件**。`"group_member_increase"` 是"新成员进群"事件，触发时调用 `on_new_member(payload)`。

> **为什么 `register` 只注册、不干活？** 因为框架需要一个"清单"：你到底有哪些命令、任务、事件。注册好之后框架才能在你发消息时找到对应函数。**没有 `register`，插件不会被加载。**

### 2.3 处理函数签名：`def handle_sign_in(event, match)`

```python
def handle_sign_in(event, match):
```

这是**命令处理函数的固定格式**，两个参数：

| 参数 | 是什么 | 举例 |
| ---- | ---- | ---- |
| `event` | 这条消息的**事件对象**，包含谁发的、在哪发的、发了什么 | `event.user_id` = 发送者 QQ 号 |
| `match` | 命令匹配结果，`match.group(1)` 取**命令后面的参数** | 发 `/签到 明天`，`match.group(1)` = `"明天"` |

- `match` 可能是 `None`（命令没带参数时），所以要用 `if match:` 判断后再取 `match.group(1)`
- **支持异步**：函数也可以是 `async def handle_sign_in(event, match):`，那样里面就能 `await` 了

> **注意**：`ctx` 呢？为什么处理函数里能直接用 `ctx.send_msg`？
> 因为框架加载插件时会把 `ctx` **注入到模块的全局变量里**，所以 `main.py` 里任何函数都能直接用 `ctx`。你**不需要**（也不应该）在参数里加 `ctx`。

### 2.4 发消息：`ctx.send_msg(...)`

```python
ctx.send_msg(
    user_id=event.user_id,
    group_id=event.group_id if event.is_group else None,
    message="你今天已经签到过了",
)
```

| 参数 | 作用 |
| ---- | ---- |
| `user_id` | 私聊发给谁（QQ 号） |
| `group_id` | 群聊发到哪个群（群号） |
| `message` | 要发的文本内容 |

**关键写法**：`group_id=event.group_id if event.is_group else None`
- 用户在群里 → `event.is_group` 为 `True` → 回**群**
- 用户私聊 → `event.is_group` 为 `False` → `group_id=None` → 回**私聊**

这样一条代码就做到了"群里回群、私聊回私"，不用自己判断。**只填 `user_id` 或 `group_id` 其中一个是安全的，两个都填会冲突。**

异步版本：`await ctx.asend_msg(...)`，效果相同，只是不阻塞主流程（推荐在 `async def` 里用）。

### 2.5 查数据库：`ctx.db_query_one`

```python
signed = ctx.db_query_one(
    "SELECT id FROM sign_in_records WHERE user_id = %s AND day = %s",
    (event.user_id, today)
)
```

| 方法 | 用途 | 返回 |
| ---- | ---- | ---- |
| `ctx.db_query(sql, params)` | 查询**多条**记录 | `list[dict]`，如 `[{'id': 1}, {'id': 2}]` |
| `ctx.db_query_one(sql, params)` | 查询**单条**记录 | `dict`，没查到是 `None` |
| `ctx.db_execute(sql, params)` | 增/删/改 | 受影响行数 |
| `ctx.db_insert(sql, params)` | 插入并返回自增 ID | `int` |

**两个关键约定：**

1. **占位符用 `%s`，不要拼字符串**：
   ```python
   # ✅ 正确：参数用 %s 占位，值放第二个参数元组里
   ctx.db_query("SELECT * FROM t WHERE user_id = %s", (event.user_id,))
   # ❌ 错误：直接拼进 SQL，有 SQL 注入风险
   ctx.db_query(f"SELECT * FROM t WHERE user_id = {event.user_id}")
   ```
2. **框架自动适配 SQLite / MySQL**：你统一写 `%s`，框架翻译成对应数据库的语法。所以 SQL 可以按 MySQL 习惯写（如 `ON DUPLICATE KEY UPDATE`），框架会自动转成 SQLite 语法。

### 2.6 建表：`ctx.create_table`

上面的代码在查询 `sign_in_records` 表，**但表还没建**！需要在 `register` 里先建表：

```python
def register(ctx):
    ctx.create_table(
        "CREATE TABLE IF NOT EXISTS sign_in_records ("
        "id INTEGER PRIMARY KEY AUTO_INCREMENT, "
        "user_id VARCHAR(32), day VARCHAR(16), score INTEGER DEFAULT 1)"
    )
```

- `CREATE TABLE IF NOT EXISTS`：表不存在才建，重复加载不会报错（**必须加**，插件会被反复热加载）
- 你写 `AUTO_INCREMENT`（MySQL 语法）也没关系，框架会自动翻译成 SQLite 的 `AUTOINCREMENT`
- **加前缀避免冲突**：表名建议带插件名，如 `sign_in_records`，别叫 `users`（和框架的表重名）

### 2.7 定时任务：`def reset_daily_signin()`

```python
def reset_daily_signin():
    """定时任务：每天 00:01 执行（注意：定时任务没有 event 参数）"""
    pass
```

**定时任务函数不能带 `event` 参数**——它不是一个消息，没有"谁发的"这个概念，是框架到点自动调的。签名固定为**无参函数**：

```python
def reset_daily_signin():   # ✅ 无参
def reset_daily_signin(event):   # ❌ 错误！会报参数不匹配
```

### 2.8 事件订阅：`def on_new_member(payload)`

```python
def on_new_member(payload):
    group_id = payload.get("group_id")
    user_id = payload.get("user_id")
```

- 事件回调收到的是一个 **`dict`**（不是 Event 对象），里面是事件的具体数据
- 用 `.get("key")` 取值而不是 `payload["key"]`，**键不存在时返回 None 而不是报错**

### 2.9 常用 event 字段速查

处理函数里 `event` 常用字段：

| 字段 | 作用 |
| ---- | ---- |
| `event.user_id` | 发送者 QQ 号 |
| `event.group_id` | 群号（私聊为 0） |
| `event.message` | 消息纯文本 |
| `event.is_group` / `event.is_private` | 是群聊 / 是私聊 |
| `event.role` | 完整身份：`super/owner/admin/member/blacklist` |
| `event.is_admin` | 超管/群主/管理员 = True |
| `event.has_image` / `event.has_at` | 含图片 / 含 @ |
| `event.reply_id` | 回复的那条消息 ID |

---

## 3. 配置项：让用户能在 Web 面板改

想让"单次签到给 1-10 分"变成可配置的？两步：

### 3.1 建 `plugins_dat/sign_in/_conf_schema.json`

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

### 3.2 代码里读取

```python
import random

def handle_sign_in(event, match):
    score_max = ctx.get_config("score_max", 10)   # 第二个参数是默认值
    score = random.randint(1, score_max)
```

- `ctx.get_config("score_max", 10)`：读取配置，**没配置时用默认值 10**
- 用户改配置**不用重启**，热生效

---

## 4. 把插件装上去

1. 文件夹建在 `plugins/` 下，名字用英文小写
2. 文件放进去 → Web 面板「插件」页点 **重载**（或重启框架）
3. 群里发 `/签到` 测试

**改代码后**：插件改了 main.py → 再点一次「重载」即可，不用重启整个机器人。

---

## 5. 常见报错对照

| 报错 / 现象 | 原因 | 修法 |
| ---- | ---- | ---- |
| 插件列表里没有它 | 缺 `register` 函数 / 目录名带中文 / main.py 语法错误 | 检查这三样 |
| 命令不触发 | pattern 写错 / 命令被别的插件抢了（priority） | 看 `/help` 里有没有 |
| 发命令没反应但日志报错 | 处理函数抛异常了 | 加 try/except 或看日志 |
| 定时任务报参数错误 | 定时函数带了 `event` 参数 | 改成无参函数 |
| 数据库报 SQL 错 | 占位符用错了 / 表没建 | 用 `%s` + 建表 |

---

## 6. 下一步

- [API 参考](api-reference.md) — 全部 `ctx` 方法
- [示例合集](examples.md) — 更完整的签到插件（含排行榜、连续签到、审计日志）
- [最佳实践](best-practices.md) — 错误处理、异步、权限校验
- 不想自己写？装了 AI 助手插件（`llm_plugin_gen`）后，可以让它代劳，见 [插件文档](https://github.com/kuangxing6367/zcbot_plugins/blob/main/plugins/llm_plugin_gen/docs/INDEX.md)