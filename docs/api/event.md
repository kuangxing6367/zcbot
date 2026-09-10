# Event 事件对象

> **本篇面向**：角色 B。`Event` 是接入端归一化后的事件对象（默认接入端 OneBot 11，字段最丰富）；其它接入端按同一结构归一化。

`Event`（`framework/event.py`）是接入端归一化后、传给命令处理器与
`message` 类订阅处理器的事件对象（默认接入端为 OneBot 11，字段最丰富；其它接入端按同一结构归一化）。
原始消息处理器（`ctx.on_raw_message`）拿到的则是**未封装的原始 dict**，注意区分。

## 一、基本属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `event.post_type` | `str` | 事件大类：`message` / `notice` / `request` / `meta_event` |
| `event.message_type` | `str` | `"group"` 或 `"private"` |
| `event.sub_type` | `str` | 事件子类型（如群成员变动的 `approve/invite`） |
| `event.user_id` | `int` | 发送者 用户 ID |
| `event.group_id` | `int` | 群号（私聊为 `0`/None，用 `is_group` 判断） |
| `event.self_id` | `int` | 机器人自身 用户 ID |
| `event.message` | `str` | 提取后的纯文本内容 |
| `event.raw_message` | `str` | 原始消息文本（CQ 码字符串形式） |
| `event.message_id` | `int` | 消息 ID |
| `event.sender` | `dict` | 发送者原始信息（nickname/card/role/title…） |
| `event.bot_name` | `str` | 来源 OneBot 实例名（多账号区分） |
| `event.font` | `int` | 客户端字体（一般用不到） |
| `event.segments` | `list[dict]` | 消息段数组，每项 `{"type": ..., "data": {...}}` |
| `event._raw` / `event.raw` | `dict` | 原始 OneBot 事件 dict |

## 二、类型判断（属性）

| 属性 | 说明 |
|------|------|
| `event.is_group` | 是否群消息 |
| `event.is_private` | 是否私聊消息 |
| `event.is_admin` | 是否具备管理身份（超管/群主/管理员均为 True） |
| `event.is_superuser` | 是否框架超级管理员 |
| `event.is_group_owner` | 是否群主 |
| `event.is_group_admin` | 是否群管理员（不含群主） |
| `event.is_blacklisted` | 是否黑名单（超管即使被拉黑 role 仍为 super） |
| `event.role` | 身份字符串：`super/owner/admin/member/blacklist` |

发送者便捷属性：

| 属性 | 说明 |
|------|------|
| `event.sender_nickname` | `sender.nickname` |
| `event.sender_card` | `sender.card`（群名片） |

## 三、消息段 segments

```python
for seg in event.segments:
    t = seg.get("type")           # text/image/at/reply/face/record/video/file/share...
    data = seg.get("data", {})
    if t == "text":
        text = data.get("text", "")
    elif t == "image":
        url = data.get("url")
```

### 富媒体判断与提取（属性）

| 属性 | 返回 | 说明 |
|------|------|------|
| `event.has_image` | `bool` | 是否含图片 |
| `event.images` | `list[dict]` | 全部图片段的 data（含 file/url 等） |
| `event.first_image` | `dict` | 第一张图片 data，没有则 `{}` |
| `event.has_at` | `bool` | 是否含 @ |
| `event.at_list` | `list[int]` | 被 @ 的用户 ID 列表（不含“全体”） |
| `event.at_all` | `bool` | 是否 @全体成员 |
| `event.has_at_bot` | `bool` | 是否 @ 了机器人本身 |
| `event.has_reply` | `bool` | 是否为回复消息 |
| `event.reply_id` | `int/None` | 被回复消息的 ID |
| `event.has_voice` | `bool` | 是否含语音（消息段类型 `record`） |
| `event.has_video` | `bool` | 是否含视频 |
| `event.has_file` | `bool` | 是否含文件 |
| `event.has_face` | `bool` | 是否含表情 |
| `event.has_share` | `bool` | 是否含分享卡片 |
| `event.share` | `dict` | 分享卡片 data（title/url/desc），没有则 `{}` |

```python
if event.has_at_bot and "签到" in event.message:
    ...
if event.has_reply:
    origin = event.reply_id
```

## 四、传播控制

### event.stop_event()

停止继续传播，本插件之后的插件不再收到该事件：

```python
async def handle(event, match):
    event.stop_event()
    await ctx.asend_msg(..., message="已拦截")
```

### event.is_stopped() -> bool

事件是否已被停止。

### event.continue_route()

命令命中后默认“独占”消息（系统关键词自动回复不再尝试）；
调用本方法放行，让关键词回复继续匹配。

### event.is_continue_route() -> bool

是否声明了继续路由。

## 五、权限（权限组轴）

身份判断用上面的 `event.role`；LuckPerms 风格的权限节点用下面这套，
首次调用时解析并缓存，普通消息零开销：

| 成员 | 说明 |
|------|------|
| `event.has_perm(node) -> bool` | 是否拥有节点（未定义按拒绝），支持 `chat.*`、`*` 通配 |
| `event.check_perm(node)` | 三态：`True` 授予 / `False` 显式否决 / `None` 未定义 |
| `event.perms` | 完整权限快照 `PermissionSet`（`.groups/.nodes/.primary_group`） |
| `event.perm_groups` | 生效权限组列表（含继承，按 weight 降序） |
| `event.primary_group` | 权重最高的非内置权限组 |

```python
if not event.has_perm("sign.admin"):
    await ctx.asend_msg(..., message="权限不足")
```

## 六、sender 原始字段

```python
sender = event.sender
nickname = sender.get("nickname", "")
card     = sender.get("card", "")        # 群名片
role     = sender.get("role", "member")  # owner/admin/member（OneBot 原始字段）
title    = sender.get("title", "")       # 群头衔
```

:::tip 身份以 event.role 为准
`sender.role` 是 OneBot 客户端上报的原始字段；框架综合超管名单、黑名单等得到的
最终身份请用 `event.role` / `event.is_admin` 等属性。
:::

## 七、调试输出

`repr(event)` 会输出类型、用户、群号与消息前 30 字，便于日志排查：

```python
ctx.log(f"收到事件: {event!r}")
```
