# 对接 IM 平台（以 QQ / OneBot 11 为例）

> **本篇讲如何把「框架」接到一个真实的聊天平台上去。** 框架本身是协议无关的：消息怎么来、怎么发，全由一个**接入端插件**负责。默认内置的接入端是 `onebot_adapter`（OneBot 11 协议，常用于接入 QQ）。

先说清楚：框架能跑起来、命令能注册、定时任务能跑——这些**都不需要**任何 IM 平台。只有「要和一个聊天软件收发消息」时，才需要读完本篇。

## 0. 接入端是什么

ZCBOT 把「协议接入」做成了可插拔的官方插件（`core_plugins/` 下）。`onebot_adapter` 是其中之一：

- 它启动一个 **WebSocket 服务端**（默认 `0.0.0.0:6830`）；
- 一个 OneBot 客户端（如 NapCat、Lagrange）以**反向 WebSocket** 连进来；
- 客户端把平台消息推给框架，框架处理完再把回复通过它发回平台。

换一个 `ProtocolAdapter`，框架就能接 Telegram / Discord / 企业微信……默认给你的是 OneBot 11。本篇以它为例。

## 1. 启用 onebot_adapter

`core_plugins.yaml`（首次启动自动生成，也可手动改）：

```yaml
core_plugins:
  onebot_adapter:
    enabled: true
    listen_host: 0.0.0.0
    listen_port: 6830
    access_token: ''
```

| 字段 | 说明 |
| ---- | ---- |
| `enabled` | 是否启用。纯定时 / HTTP 注入场景可设 `false`，省掉这个接入端 |
| `listen_host` / `listen_port` | WebSocket 服务端监听地址（客户端连这个） |
| `access_token` | 客户端连入时校验的令牌，**公网部署务必设置** |

首次启动后日志会打印 `OneBot 适配器已启动 (ws://0.0.0.0:6830)`。

## 2. 让客户端连进来（NapCat / Lagrange）

以 NapCat 为例，在网络配置里新增「反向 WebSocket 客户端」：

- 目标地址：`ws://127.0.0.1:6830`
- AccessToken：与 `onebot_adapter.access_token` 一致（本地调试可留空）

等价 JSON 片段：

```json
{
  "WebSocketReverse": {
    "Enable": true,
    "URL": "ws://127.0.0.1:6830",
    "AccessToken": ""
  }
}
```

其他 OneBot 客户端（Lagrange、go-cqhttp 等）同理，填同一个 `ws://地址` 和令牌即可。

### 验证连接

框架日志出现下面这行即代表客户端已连入：

```
[bot_xxxxxx] OneBot 客户端已连接
```

多账号场景下每个客户端相互独立，发送消息时框架自动选用消息来源的账号，插件无需手动指定 bot。

## 3. 第一次对话

在接入的平台上向机器人发送：

```
/echo 你好
```

机器人回复 `你好` 即链路正常。`/help`（或 `/帮助`、`/菜单`）可查看命令列表。

## 4. 在插件里和平台交互

消息、事件进入框架后，插件用统一的 `ctx` 接口即可，不必关心底层是 OneBot 还是别的协议。

### 4.1 发消息（通用）

`ctx.send_msg` / `await ctx.asend_msg`（见 [编写插件](./writing-plugins.md#2-4-发消息ctx-send-msg--ctx-asend-msg)），按 `user_id` / `group_id` 自动路由到私聊 / 群组。

### 4.2 富媒体消息（CQ 码）

OneBot 11 用 **CQ 码** 表达图片、@、回复等，直接放进 `message` 即可：

```python
# 发图片
ctx.send_msg(group_id=gid, message="[CQ:image,file=file:///path/to/a.png]")
# @某人
ctx.send_msg(group_id=gid, message=f"[CQ:at,qq={uid}] 看这里")
# 回复某条消息
ctx.send_msg(group_id=gid, message=f"[CQ:reply,id={msg_id}] 收到")
```

> CQ 码是 OneBot 11 的协议细节。接入其它平台时富媒体写法不同，但通用文本发送不变。

### 4.3 群管 / 查询（OneBot 专属 API）

这些能力由 `onebot_adapter` 提供，依赖接入端支持：

```python
# 方式一：ctx 上的快捷方法（同步 / 异步两版）
ctx.ban(group_id, user_id, duration=600)     # 禁言（秒，0 为解禁）
await ctx.akick(group_id, user_id)           # 踢出
ctx.mute_all(group_id, True)                  # 全员禁言
ctx.set_card(group_id, user_id, "新名片")     # 设置群名片
members = ctx.get_member_list(group_id)        # 群成员列表

# 方式二：通过 ctx.onebot 调任意 OneBot action（38 个标准 + 扩展动态转发）
ctx.onebot.send_group_msg(group_id=123, message="hi")
ctx.onebot.set_group_ban(group_id=123, user_id=456, duration=600)
info = ctx.onebot.get_login_info()            # 机器人自身信息
```

`ctx.onebot` 覆盖 OneBot 11 全部标准 action（消息类、群管类、请求处理、信息查询、媒体、工具），未显式列出的扩展动作也能 `ctx.onebot.<动作名>(**参数)` 动态转发。

### 4.4 原始消息接管

想在命令匹配之前直接处理原始事件（比如自己解析消息段、做风控）？用 `ctx.on_raw_message`：

```python
def register(ctx):
    ctx.on_raw_message(raw_handler)

async def raw_handler(raw_event: dict, bot_name: str):
    # raw_event 是未经文本提取的原始 OneBot 事件（消息段数组原样保留）
    if 想接管:
        return True        # 返回 True = 本消息被接管，后续命令/关键词/广播全部跳过
    # 返回 None/False = 继续正常流程
```

多个原始处理器按插件 `priority` 升序执行，单个异常不影响其他插件。

## 5. 安全须知

- **公网部署**：把 `web.host` 保持在 `127.0.0.1` 并用反向代理暴露，同时设置 `onebot_adapter.access_token` 与 IP 白名单。
- 框架启动时会检测「`web.host` 为 `0.0.0.0` 且 `access_token` 为空」并给出警告，看到就处理掉。
- 多账号时每个 bot 独立，注意权限边界。

## 6. 下一步

- [编写插件](./writing-plugins.md) —— 业务逻辑怎么写
- [开始使用](./getting-started.md) —— 不接 IM 也能用的两条路线（定时 / HTTP 注入）
- [架构详解 · 事件总线](../advanced/architecture.md) —— 框架内部事件流
