# Event 事件对象

Event 对象封装了 OneBot 11 事件，传递给命令处理函数。

## 基本属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `event.user_id` | `int` | 发送者 QQ 号 |
| `event.group_id` | `int` | 群号（私聊为 None） |
| `event.message` | `str` | 消息内容（纯文本） |
| `event.raw_message` | `str` | 原始消息（含 CQ 码） |
| `event.message_type` | `str` | `"group"` 或 `"private"` |
| `event.message_id` | `int` | 消息 ID |
| `event.sender` | `dict` | 发送者信息 |
| `event.bot_name` | `str` | 来源 bot 实例名 |
| `event.adapter` | `str` | 协议适配器名（`"onebot"`） |
| `event.raw` | `dict` | 原始 OneBot 事件 |

## 类型判断

| 属性 | 说明 |
|------|------|
| `event.is_group` | 是否群消息 |
| `event.is_private` | 是否私聊消息 |
| `event.is_admin` | 是否管理员/群主 |
| `event.is_superuser` | 是否超管 |
| `event.role` | 身份：`super`/`owner`/`admin`/`member`/`blacklist` |

## 消息段

```python
# 获取所有消息段
for seg in event.segments:
    seg_type = seg.get('type')  # text, image, at, reply, ...
    if seg_type == 'text':
        text = seg.get('data', {}).get('text', '')
    elif seg_type == 'image':
        url = seg.get('data', {}).get('url', '')
```

| 消息段类型 | 说明 |
|-----------|------|
| `text` | 文本 |
| `image` | 图片 |
| `at` | @某人 |
| `reply` | 回复 |
| `face` | 表情 |
| `record` | 语音 |
| `video` | 视频 |
| `file` | 文件 |

## 常用方法

### event.stop_event()

停止事件传播，后续插件不会收到此消息：

```python
async def handle_block(event, match):
    event.stop_event()
    await ctx.asend_msg(..., message="已拦截")
```

### event.is_stopped()

检查事件是否已被停止：

```python
if event.is_stopped():
    return  # 事件已被其他插件处理
```

### event.continue_route()

声明允许继续路由（即使当前插件已匹配）：

```python
async def handle_pass(event, match):
    event.continue_route()  # 允许关键词回复继续匹配
```

### event.has_perm()

检查当前用户是否有指定权限：

```python
if event.has_perm('admin.ban'):
    # 有权限
    pass
```

## sender 字段

```python
sender = event.sender
nickname = sender.get('nickname', '')
card = sender.get('card', '')       # 群名片
role = sender.get('role', 'member') # owner/admin/member
title = sender.get('title', '')     # 群头衔
```
