# 接入端契约与规范消息

这一页回答一个问题：**为什么插件不需要知道消息来自 QQ 官方、OneBot、Telegram 还是 Discord。**

## 问题从哪来

各协议对"一条消息"的表达完全不同：

| 接入端 | 图片 | 语音 | 附件位置 |
|---|---|---|---|
| OneBot 11 | `image` 段 | **`record`** 段 | `message` 段数组 |
| QQ 官方 | `msg_type=7 + media` | 独立 `file_type` | `attachments[]` |
| Telegram | `photo[]` | `voice` / `audio` | message 对象的各个字段 |
| Discord | `attachments[]` | `attachments[]`（audio/*） | `attachments[]` |

差别不只是名字。Telegram 和 Discord 的附件根本不在消息正文里——如果接入端不做翻译，插件拿到的就只有一串文本，想知道"用户到底发了张图还是一段语音"，只能去翻 `event['raw']` 里的协议原始字典。这么做的结果就是：插件写死了某个协议，换个接入端就崩。

框架的解决办法是**在内核里立一套规范消息段**，让接入端在两个方向各做一次翻译，插件只面对规范形态。

## 三层结构

```
协议原生 ──normalize_incoming()──▶ 规范消息段 ──▶ ev.segments（插件读）
插件 message ──▶ 规范消息段 ──to_native()──▶ 协议原生（发出去）
```

`ProtocolAdapter` 基类已经给这两个方法提供了兜底实现（入站交给通用归一化器，出站原样透传），所以一个最小接入端**不写任何翻译也能跑**。但那样附件和语音多半会丢——契约自检会在日志里点名没实现的接入端。

## 规范消息段

形状固定：`{'type': str, 'data': dict}`。

```python
from framework.messaging import segments as S

S.normalize_message('你好[CQ:at,qq=123][CQ:image,file=a.jpg]')
# [{'type':'text','data':{'text':'你好'}},
#  {'type':'at','data':{'qq':'123'}},
#  {'type':'image','data':{'file':'a.jpg'}}]
```

### 类型名

`text` / `at` / `face` / `image` / `voice` / `video` / `file` / `sticker` / `reply` / `share` / `json` / `xml` / `location` / `poke` / `music` / `dice` / `rps` / `forward` / `markdown` / `keyboard`

**注意 `voice`**：OneBot 叫 `record`、Telegram 有 `voice` 和 `audio`、QQ 官方是独立 file_type。规范名一律是 `voice`，别名表在 `segments._TYPE_ALIASES` 里，归一化时自动归位。

### 内容型段的 data

媒体段的文件引用各协议放的地方不一样：OneBot 放 `file`、Telegram 放 `file_id`、Discord 放 `url`、QQ 官方上传后给 `file_info`。取值统一用 `media_ref()`：

```python
S.media_ref({'type':'image','data':{'file_id':'TG123'}})   # 'TG123'
S.media_ref({'type':'voice','data':{'url':'https://x/a.ogg'}})  # 'https://x/a.ogg'
S.media_ref({'type':'image','data':{'base64':'AAAA'}})     # 'base64://AAAA'
```

约定键：`file` / `url` / `path` / `base64` / `file_id` / `file_info` / `name` / `mime` / `size` / `duration` / `width` / `height` / `thumb`。除此之外允许携带协议私有字段。

## 插件侧怎么读

```python
async def handler(ev, match):
    if ev.has_voice:              # 所有协议的语音都归到这里
        seg = ev.first_voice      # {'file_id':..., 'duration': 5}
        ref = ev.media_url(seg)   # 文件引用（URL/路径/file_id/base64://）
    if ev.has_image:
        for img in ev.images:
            ...
    if ev.is_media_only:          # 只有媒体没有文字
        ...
```

`Event` 上可用的属性：`segments` / `text` / `images` / `voices` / `videos` / `files` / `stickers` / `first_image` / `first_voice` / `first_video` / `first_file` / `media` / `media_url()` / `is_media_only` / `has_*` / `reply_id` / `adapter`。

`reply_id` 能转成 int 就返回 int，QQ 官方那种字符串 ID 直接返回字符串——不会因为 `int()` 失败就变成 `None`。

## 接入端侧怎么写

### 1. 如实自述能力

```python
from framework.messaging.contract import (
    CAP_IMAGE, CAP_KEYBOARD, CAP_TEXT, CAP_VOICE, Capabilities)

def capabilities(self):
    return Capabilities(
        inbound=[CAP_TEXT, CAP_IMAGE, CAP_VOICE],
        outbound=[CAP_TEXT, CAP_IMAGE, CAP_KEYBOARD],
        actions=['recall'],
    )
```

插件可以问了再发，而不是发出去才发现不支持：

```python
adapter.supports('voice', 'in')      # 能不能收语音
adapter.supports('keyboard', 'out')  # 能不能发按钮
adapter.supports('group_admin')      # 能不能禁言踢人
```

### 2. 入站翻译

```python
def normalize_incoming(self, raw_message) -> list:
    """协议原生 → 规范消息段"""
    segs = []
    if raw_message.get('text'):
        segs.append({'type': 'text', 'data': {'text': raw_message['text']}})
    for att in raw_message.get('attachments') or []:
        segs.append({'type': 'image', 'data': {'url': att['url']}})
    return segs
```

### 3. 出站翻译

```python
def to_native(self, segments: list):
    """规范消息段 → 协议原生结构；返回什么由你自己定"""
```

QQ 官方的 `msg_type` 是互斥的（文本 0 / markdown 2 / 富媒体 7，按钮只能挂 markdown），这类协议限制应该在 `to_native()` 里消化掉，别让插件去背。

### 4. 事件补齐（可选）

接入端在 `handle_event()` 尾部调用 `self.finalize_event(event, bot_name)`，框架会补齐缺失字段（sender、message_type、segments、纯文本…）。**只补缺失，不覆盖**接入端已给出的值。

## 通知事件

通知事件在内核里**广播两次**：协议原名与规范名。

```python
notice.group_increase          # 原名，既有插件订阅的这个
notice.group_member_increase   # 规范名，新插件订阅这个
```

两者同名时只广播一次。事件字典里 `notice_type` 保持协议原名（改动会让既有订阅集体失效），规范名放在 `notice_type_canonical`。

规范通知名见 `contract.NOTICE_TYPES`：`group_member_increase` / `group_member_decrease` / `group_ban` / `group_admin` / `group_upload` / `group_essence` / `group_card` / `message_recall` / `poke` / `friend_add` / `friend_delete` / `interaction` / `callback_query`。

## 契约自检

接入端注册时框架自动跑一遍 `validate_adapter()`，问题直接打进日志：

```
WARNING 接入端契约 [onebot]: OneBotAdapter.capabilities() 抛异常: cannot import name ...
WARNING 接入端契约 [foo]: FooAdapter.normalize_incoming() 应返回 list，实际 dict
```

检查项：必需方法是否可调用、`normalize_incoming` / `to_native` 的返回形状与是否抛异常、`capabilities()` 能否解析、`get_connection_info()` 有没有 id。

**只告警不拦人**——第三方接入端可能是最小实现，拦了直接起不来。但问题必须在日志里看得见。

排错时也可以在运行时拉一份摘要：

```python
ctx._framework.services.adapter_contracts()
# {'onebot': {'id':'onebot','capabilities':{...},'issues':[...]}, ...}
```

## 写一个新接入端的最小清单

1. 继承 `ProtocolAdapter`，实现 `handle_event` / `call_api` / `get_connected_bots` / `start` / `stop`
2. 实现 `capabilities()` 如实自述
3. 有附件/语音就实现 `normalize_incoming()`；出站结构特殊就实现 `to_native()`
4. 可选：实现 `get_connection_info()` 让连接页自动生成配置表单
5. 跑一遍启动看日志里有没有契约告警

## 相关源码

| 文件 | 作用 |
|---|---|
| `framework/messaging/segments.py` | 规范消息段定义与归一化 |
| `framework/messaging/contract.py` | 能力常量、事件补齐、契约自检 |
| `framework/messaging/protocol.py` | `ProtocolAdapter` 基类钩子 |
| `framework/messaging/event.py` | `Event` 上的跨协议读取面 |
| `tests/test_message_contract.py` | 契约测试（80 条） |
