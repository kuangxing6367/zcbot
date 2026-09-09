# 协议适配器

框架核心不绑定具体 IM 协议。`framework/protocol.py` 定义了协议适配器抽象基类
`ProtocolAdapter` 与服务注册表，官方插件 `core_plugins/onebot_adapter`
提供 OneBot 11 的实现并注册为服务。

## ProtocolAdapter 抽象契约

自定义协议（非 OneBot）需要继承并实现以下方法：

```python
from framework.protocol import ProtocolAdapter

class MyAdapter(ProtocolAdapter):
    async def handle_event(self, raw_event: dict, bot_name: str):
        """把原始协议事件转换为框架内部事件 dict；返回 None 表示丢弃"""

    async def call_api(self, action: str, bot: str = None, **params) -> dict:
        """调用下游协议 API 并返回结果"""

    def get_connected_bots(self) -> list:
        """返回当前已连接的 bot 标识列表"""

    def start(self):
        """启动适配器（监听/连接）"""

    async def stop(self):
        """停止适配器、释放连接"""
```

实现完成后在官方/用户插件的 `register(ctx)` 中注册：

```python
fw.services.register("protocol_adapter", adapter)
fw.services.register("api_caller", adapter.api_caller)
```

## OneBot 11 实现注册的服务

`core_plugins/onebot_adapter` 启动后注册四个服务：

| 服务名 | 内容 |
|--------|------|
| `protocol_adapter` | 适配器实例本身 |
| `api_caller` | API 调用器：同步 `.call(action, **params)` / 异步 `.acall(action, **params)` |
| `onebot_api` | 面向对象封装 `OneBotAPI`（也是 `ctx.onebot`） |
| `ws_server` | WebSocket 服务端实例 |

## api_caller：通用调用

```python
caller = ctx._framework.services.get("api_caller")
# 同步（内部桥接到事件循环）
caller.call("get_group_list")
# 异步（推荐）
await caller.acall("send_group_msg", group_id=123, message="hi")
```

## OneBotAPI：类型化方法

`ctx.onebot`（即 `onebot_api` 服务）把常用 action 封成了同步方法，
最后一个参数 `bot=None` 表示用默认/当前来源实例。分类如下：

### 消息

`send_private_msg`、`send_group_msg`、`send_msg`、`delete_msg`、`get_msg`、
`get_forward_msg`、`send_like`、`mark_msg_as_read`

### 群管理

`set_group_kick`、`set_group_ban`、`set_group_anonymous_ban`、
`set_group_whole_ban`、`set_group_admin`、`set_group_card`、`set_group_name`、
`set_group_special_title`、`set_group_leave`、`set_group_anonymous`

### 好友/群请求处理

`set_friend_add_request`、`set_group_add_request`

### 信息查询

`get_login_info`、`get_stranger_info`、`get_friend_list`、`get_group_info`、
`get_group_list`、`get_group_member_info`、`get_group_member_list`

### 文件与能力

`get_record`、`get_image`、`can_send_image`、`can_send_record`、
`get_cookies`、`get_csrf_token`、`get_credentials`

### 运行状态

`get_status`、`get_version_info`、`set_restart`、`clean_cache`

```python
members = ctx.onebot.get_group_member_list(group_id=123456)
ctx.onebot.set_group_card(group_id=123456, user_id=789, card="新名片")
```

:::tip 插件里优先用 ctx 封装
`ctx.send_msg/asend_msg`、`ctx.api/aapi`、`ctx.ban/kick/...` 已经覆盖绝大多数场景，
并自动选择当前消息来源的 bot；只有这些快捷方法没覆盖的 action 才需要直接拿
`api_caller` / `OneBotAPI`。
:::

## 多账号

- 每个反向连入的 OneBot 客户端以 `bot_name` 区分，事件对象上为 `event.bot_name`；
- `ctx` 快捷方法不传 `bot` 时自动跟随当前消息来源，避免“用 A 号回 B 群”；
- 主动发起（如定时任务）时，可用 `get_connected_bots()` 选择目标实例，
  或在调用时显式传 `bot=<bot_name>`。
