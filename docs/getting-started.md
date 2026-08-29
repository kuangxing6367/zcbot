# 快速入门：写出你的第一个插件

> 本教程**手把手**带你从零写一个能跑的插件。跟着做，10 分钟内搞定。

---

## 0. 认识插件结构

每个插件 = `plugins/` 下的**一个文件夹**：

```
plugins/
├── hello/           ← 插件文件夹（名字 = 插件名，英文）
│   └── main.py      ← 入口文件（框架会自动找它）
└── echo/            ← 另一个插件
    └── main.py
```

**一个插件最少只有一个 `main.py`**，包含两样东西：

1. `__plugin_meta__`：插件的"身份证"（名字/版本/简介）
2. `register(ctx)`：告诉框架"我有这些命令/任务/事件"

---

## 1. 第一个命令（必做）

### 1.1 建文件夹 + 写文件

新建 `plugins/hello/main.py`，写入：

```python
# plugins/hello/main.py

__plugin_meta__ = {
    "name": "你好",
    "version": "1.0.0",
    "author": "你的名字",
    "desc": "回复 /你好",
    "priority": 50,
}


def register(ctx):
    """框架加载插件时调用：在这里注册命令、任务、事件"""
    ctx.command("/你好", handle_hi, description="打个招呼")


def handle_hi(event, match):
    """命令处理函数：用户发 /你好 时执行"""
    ctx.send_msg(
        user_id=event.user_id,          # 回给发送者
        group_id=event.group_id if event.is_group else None,  # 群聊则回群
        message="你也好呀！👋",
    )
```

### 1.2 加载插件

两种方式任选：

- **方式 A**：重启框架（`Ctrl+C` 再 `python main.py`）
- **方式 B**：Web 面板 → 插件 → 找到 hello → 点「重载」

### 1.3 测试

群里发 **`/你好`**，机器人回复 **`你也好呀！👋`** ✅

> 没反应？看文末「常见问题」。

---

## 2. 命令带参数（进阶）

命令后面的内容通过 `match.group(1)` 获取：

```python
def register(ctx):
    ctx.command("/echo", handle_echo, description="原样返回")

def handle_echo(event, match):
    # match.group(1) = 命令后面的内容；没内容时 match 可能为 None
    arg = match.group(1).strip() if match else ""
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=arg or "PONG",
    )
```

发 `/echo 你好` → 回 `你好`。

### 支持别名 / 权限

```python
ctx.command(
    "/admin_cmd",
    handle_admin,
    alias="/ac,管理命令",   # 别名，逗号分隔
    require_admin=True,       # 需要管理员权限
    require_superuser=False,  # 或需要超管
    description="管理命令",
)
```

---

## 3. 发图片（用内置 image_renderer）

插件可以生成并发送图片（推荐用框架的 image_renderer 画卡片）：

```python
import sys, os, tempfile

def handle_card(event, match):
    mod = sys.modules.get("plugin_image_renderer")
    if mod is None:
        return
    # 画一张 400x200 的卡片
    canvas = mod._get_native_or_pil_canvas(400, 200, None, None)
    canvas.rect(0, 0, 400, 200, radius=12, fill="#1a2133")
    canvas.text(20, 30, "我的卡片", font_size=24, color="#FFFFFF")
    canvas.text(20, 80, "这是图片内容", font_size=16, color="#9fb3cc")
    png = canvas.to_png()
    # 存临时文件，用 CQ 码发送，发完删除
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png)
        path = f.name.replace("\\", "/")
    try:
        ctx.send_msg(group_id=event.group_id, message=f"[CQ:image,file=file:///{path}]")
    finally:
        os.unlink(f.name)
```

---

## 4. 订阅事件（不是命令的消息也能收到）

`ctx.on(事件名, handler)` 订阅框架事件：

```python
def register(ctx):
    ctx.on("message", on_any_message)          # 任意文本消息
    ctx.on("notice.group_increase", on_new)     # 新成员进群
    ctx.on("message.image", on_image)           # 图片消息

def on_any_message(event):
    # 注意：这里是 Event 对象，不是 dict
    text = event.message
    # 返回 True 表示"已处理"，框架不再往下传
    return False

def on_new(payload):
    # notice 事件 payload 是 dict
    ctx.send_msg(group_id=payload["group_id"], message="欢迎新成员！")
```

> 框架已有事件：`message`、`notice.*`、`request.*`、`meta.heartbeat` 等。
> 还有 `ctx.on_raw_message(handler)` 能拿到**原始消息**（含完整消息段），返回 True 可接管消息。

---

## 5. 定时任务

用 cron 表达式（分 时 日 月 周）：

```python
def register(ctx):
    ctx.task("*/5 * * * *", every_5min, description="每5分钟")
    ctx.task("0 9 * * *", morning, description="每天9点")

def every_5min():
    # 这里定时执行
    pass
```

---

## 6. 数据库（自动适配 SQLite/MySQL）

插件可以建表、读写，框架自动处理方言差异：

```python
def register(ctx):
    ctx.create_table(
        "CREATE TABLE IF NOT EXISTS signin (user_id TEXT PRIMARY KEY, day TEXT, cnt INTEGER DEFAULT 1)"
    )

def handle_signin(event, match):
    uid = str(event.user_id)
    today = __import__("time").strftime("%Y-%m-%d")
    # 查询 / 插入（用 %s 占位符，框架自动适配）
    row = ctx.db_query_one("SELECT * FROM signin WHERE user_id=%s AND day=%s", (uid, today))
    if row:
        ctx.db_execute("UPDATE signin SET cnt=cnt+1 WHERE user_id=%s AND day=%s", (uid, today))
    else:
        ctx.db_insert("INSERT INTO signin (user_id, day) VALUES (%s, %s)", (uid, today))
    ctx.send_msg(group_id=event.group_id, message="签到成功！")
```

---

## 7. 插件配置（让用户在 Web 面板改）

1. 在插件数据目录 `plugins_dat/<插件名>/_conf_schema.json` 定义配置项：

```json
{
  "greeting": {
    "description": "打招呼的文案",
    "type": "string",
    "default": "你也好呀！",
    "hint": "自定义欢迎语"
  }
}
```

2. 插件里读取：

```python
def handle_hi(event, match):
    msg = ctx.get_config("greeting", "你也好呀！")
    ctx.send_msg(group_id=event.group_id, message=msg)
```

用户在 Web 面板插件页即可修改，无需改代码。

---

## 8. 调试技巧

- 用 `ctx.logger.info("...")` 打日志，在 Web 面板「日志」页实时看
- handler 出错会在日志显示，也可加 `on_error(event, error)` 兜底
- 耗时操作（HTTP、文件）放异步：`ctx.asend_msg(...)` 或 `await asyncio.to_thread(...)`

```python
def on_error(event, error):
    ctx.logger.error(f"出错了: {error}")
```

---

## 9. 常见问题

**插件没加载？** 看 Web 插件页状态或日志。常见：缺 `register` 函数、目录名有中文、`main.py` 有语法错误。

**命令没触发？** 检查 `ctx.command` 的 pattern：简单文本做前缀匹配，含正则符号（`^$.*?` 等）做正则匹配。

**match 是 None？** 命令无参数时可能为 None，用 `if match:` 判断。

**异步还是同步？** 两个都支持：`async def` 直接用 await；普通 `def` 框架自动转线程（不卡机器人）。

---

## 下一步

- 看 [API 参考](api-reference.md) 了解 ctx 全部能力
- 看 [示例合集](examples.md) 的签到插件完整实现
- 看 [最佳实践](best-practices.md) 写出可维护的插件