# 多轮会话

> **本篇面向**：角色 B（插件开发者）。多轮会话依赖官方插件 `session`（默认启用）。

需要官方插件 `core_plugins.session`（默认启用）。它通过一个**原始消息处理器**
拦截“正在等待中的用户”的下一条消息，从而实现“问一句、等一句”的多轮交互。

:::tip 会话的键
会话按 `用户:群号` 区分：同一用户在不同群的会话互不影响；私聊的群号为 0。
等待中的那条回复会被会话**消费掉**（不再走命令匹配）。
:::

## 方式一：ctx.wait_for()（一问一答）

```python
async def handle_survey(event, match):
    reply = await ctx.wait_for(event, prompt="你叫什么名字？", timeout=60)
    if reply is None:
        await ctx.asend_msg(user_id=event.user_id,
                            group_id=event.group_id if event.is_group else None,
                            message="超时未回复，已取消")
        return
    name = extract_text(reply)
    await ctx.asend_msg(user_id=event.user_id,
                        group_id=event.group_id if event.is_group else None,
                        message=f"你好，{name}！")
```

### 返回值是什么

`wait_for` 返回的是**框架消息事件 dict**（不是 `Event` 对象），超时返回 `None`。
纯文本要用框架的提取函数从消息段里取：

```python
from framework.event import _extract_text

def extract_text(raw):
    return _extract_text(raw.get("message", "")).strip()
```

原始 dict 里同时含 `user_id`、`group_id`、`message`（消息段数组）、
`raw_message`（CQ 码字符串）、`message_id` 等字段。

### handler 过滤

第四个参数 `handler(raw_event) -> bool` 用于决定“这条消息算不算有效回复”：
返回 `True` 消费并结束等待，返回 `False` 继续等下一条（同步/异步函数均可）。

```python
def only_number(raw):
    return _extract_text(raw.get("message", "")).strip().isdigit()

reply = await ctx.wait_for(event, prompt="请输入数字：", timeout=30, handler=only_number)
```

## 方式二：ctx.create_session()（连续多轮）

多轮对话用异步上下文管理器，`ask()` 每轮发送提示并等待，数据累积在 `sess.data`：

```python
async def handle_register(event, match):
    async with ctx.create_session(event, timeout=120) as sess:
        r = await sess.ask("第 1 步：你的昵称？")
        if r is None:
            return
        sess.data["nick"] = _extract_text(r.get("message", "")).strip()

        r = await sess.ask("第 2 步：你的年龄？（输入数字）")
        if r is None:
            return
        sess.data["age"] = _extract_text(r.get("message", "")).strip()

        # sess.ask 内部逐轮等待；也可以用 sess.wait() 不发提示直接等
        await ctx.asend_msg(user_id=event.user_id,
                            group_id=event.group_id if event.is_group else None,
                            message=f"登记完成：{sess.data}")
```

### Session 对象成员

| 成员 | 说明 |
|------|------|
| `await sess.ask(prompt, timeout=None)` | 发提示并等待下一条，返回原始事件 dict / None |
| `await sess.wait(timeout=None)` | 不发提示，直接等待下一条 |
| `sess.data` | 会话内共享的字典，可跨轮累积答案 |
| `sess.event` | 触发会话的初始事件 |
| `sess.timeout` | 默认超时秒数 |
| `sess.close()` | 提前结束会话（退出 `async with` 时自动调用） |

## 完整示例：问卷调查

```python
from framework.event import _extract_text

QUESTIONS = [
    ("nick",  "你的昵称是？"),
    ("city",  "你在哪个城市？"),
    ("hobby", "你的爱好是？"),
]

async def handle_survey(event, match):
    answers = {}
    async with ctx.create_session(event, timeout=60) as sess:
        for key, q in QUESTIONS:
            raw = await sess.ask(q)
            if raw is None:
                await ctx.asend_msg(user_id=event.user_id,
                                    group_id=event.group_id if event.is_group else None,
                                    message="已超时，问卷取消")
                return
            answers[key] = _extract_text(raw.get("message", "")).strip()

    await ctx.db_insert_async(
        "INSERT INTO survey (user_id, nick, city, hobby) VALUES (%s,%s,%s,%s)",
        (event.user_id, answers["nick"], answers["city"], answers["hobby"]))
    await ctx.asend_msg(user_id=event.user_id,
                        group_id=event.group_id if event.is_group else None,
                        message="问卷提交成功，感谢参与！")

def register(ctx):
    ctx.command("/问卷", handle_survey, description="填写问卷")
```

## 机制与限制

- 会话由 `SessionManager` 管理，等待中的 future 带过期时间，后台任务周期性清理；
- 同时存在的会话数有上限（防止内存膨胀），超限时先清理过期会话；
- 一个用户在同一会话键上同时只能有一个等待；重复发起会覆盖旧等待；
- 会话只负责“等下一条消息”，不做状态机；复杂分支流程用 `sess.data` + 循环自行编排；
- 会话等待会让出协程，期间不阻塞其他用户消息；
- 会话依赖 `api_caller` 服务发送提示；若**一个接入端都没启用**（默认接入端是 onebot_adapter），`ask` 的提示发不出去，但 `wait()` 仍可等待；换成 http_inject 等其它接入端时提示照常。

## 常见问题

| 现象 | 原因与处理 |
|------|-----------|
| 一直等到超时 | 回复被别的原始处理器先接管，或用户/群不匹配；检查会话键 |
| 拿到的是消息段数组 | 这是原始事件，用 `_extract_text(raw["message"])` 取文本 |
| 群里两个人互相干扰 | 不会，会话键含 `user_id`；同一用户多群也按群隔离 |
| 想中途取消 | 调用 `sess.close()` 或直接 `return`（退出上下文自动清理） |
