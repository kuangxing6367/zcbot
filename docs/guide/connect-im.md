# 对接 IM 平台

你想让 ZCBOT 连上一个真实的聊天软件，让机器人能收发消息——这就是这篇要解决的问题。

框架本身协议无关：「消息怎么来、怎么发」全由**接入端插件**负责。默认开的是 `onebot_adapter`（OneBot 11，常用来接 QQ）；另外还有 QQ 官方、Telegram、Discord、出站 WS 等，默认关闭，填好凭证再开。

:::tip 先确认要不要读完
框架能启动、命令能注册、定时任务能跑——**都不需要**接聊天平台。
只有「要和某个聊天软件收发消息」时才需要这篇。
:::

## 接入端是怎么工作的（以默认 OneBot 为例）

```
┌──────────┐   反向 WebSocket    ┌─────────────────┐
│ NapCat / │  ────────────────►  │  ZCBOT          │
│ Lagrange │  连到 :6830        │  onebot_adapter  │
│ (OneBot) │  ◄────────────────  │  → 内核 → 插件   │
└──────────┘   回复也走这条      └─────────────────┘
```

1. 框架起一个 **WebSocket 服务端**（默认 `0.0.0.0:6830`）；
2. OneBot 实现端（NapCat、Lagrange…）以**反向 WebSocket** 连进来；
3. 消息推给框架，插件处理完再经同一连接发回平台。

换一个 `ProtocolAdapter` 就能接别的事件源。这里默认走 `onebot_adapter`。

## 其它官方接入端（默认全关）

| 插件 | 平台 | 怎么开（`core_plugins.yaml`） |
| ---- | ---- | ------------------------------ |
| `onebot_adapter` | QQ（经 NapCat 等） | 默认已开；设 `access_token` 更安全 |
| `qq_official` | QQ 官方机器人 | `enabled: true` + `app_id` / `app_secret` |
| `telegram` | Telegram | `enabled: true` + `token`（找 @BotFather 要） |
| `discord` | Discord | `enabled: true` + `token`（开发者门户；`MESSAGE_CONTENT` Intent 需开） |
| `ws_client` | 任意 WS 总线 | `enabled: true` + `url`（主动连出去） |
| `http_inject` | 不接 IM / Webhook | `enabled: true` + 地址端口 |

这些接入端都实现了统一连接自描述，后台「接入端连接」页可以改配置；图片统一支持 `base64://`。

**能力差异（先看再选）：**

- 收发消息、文本回复：各端统一走事件与 `send_text` / 动作名。
- **群管理（踢人 / 禁言等）**：目前完整支持在 OneBot 11；QQ 官方 / Telegram / Discord 等端对这类动作多返回「不支持」，群管类插件换端后请先验证。
- 限流 / 富媒体下行：各端按自身 API 实现，暂无统一 429 退避。

---

## 默认路线：OneBot 11（QQ）

### 1. 确认接入端已启用

编辑根目录 `core_plugins.yaml`（首次启动会自动生成）：

```yaml
core_plugins:
  onebot_adapter:
    enabled: true
    listen_host: 0.0.0.0
    listen_port: 6830
    access_token: ''    # 公网务必改成强随机字符串
```

| 字段 | 说明 |
| ---- | ---- |
| `enabled` | 关掉则完全不启这个接入端（纯定时场景可关） |
| `listen_host` / `listen_port` | 服务端监听地址——**客户端连这里** |
| `access_token` | 连入校验令牌；本地调试可空，公网必须设 |

改完重启框架，日志应出现类似：

```
OneBot 适配器已启动 (ws://0.0.0.0:6830)
```

### 2. 让客户端连进来

以 **NapCat** 为例：网络配置 → 新增「反向 WebSocket 客户端」：

| 设置项 | 填什么 |
| ------ | ------ |
| 目标地址 | `ws://127.0.0.1:6830` |
| AccessToken | 与 `access_token` 一致（本地可留空） |

等价 JSON：

```json
{
  "WebSocketReverse": {
    "Enable": true,
    "URL": "ws://127.0.0.1:6830",
    "AccessToken": ""
  }
}
```

Lagrange、go-cqhttp 等同理：填同一个 `ws://地址` 和令牌即可。

### 3. 验证连上了

框架日志出现：

```
[bot_xxxxxx] OneBot 客户端已连接
```

多账号时每个客户端独立；回复会自动用「消息来源那个号」，插件一般不用手动选 bot。

### 4. 发一条消息试试

对机器人发：

```
/echo 你好
```

回 `你好` 就通了。`/help` 看全部命令。

---

## 其它平台（简要）

以 Telegram 为例，三步：

1. `core_plugins.yaml` 里 `telegram.enabled: true`，填 `token`
2. 重启，日志确认接入端就绪
3. 给机器人发 `/echo 你好`

Discord、QQ 官方同理：**开插件 → 填 token/app 凭证 → 重启 → 发消息自测**。
详细字段说明见 [配置系统 · 各接入端](./configuration.md)。

---

## 插件里怎么发消息

消息进框架后，插件只用统一的 `ctx`，不必关心底下是 OneBot 还是别的协议。

### 通用文本

```python
ctx.send_msg(user_id=uid, group_id=None, message="你好")      # 私聊
ctx.send_msg(group_id=gid, user_id=None, message="大家好")     # 群
# 异步 handler 里用 await ctx.asend_msg(...)
```

### 富媒体（OneBot / CQ 码）

```python
ctx.send_msg(group_id=gid, message="[CQ:image,file=file:///path/to/a.png]")
ctx.send_msg(group_id=gid, message=f"[CQ:at,qq={uid}] 看这里")
ctx.send_msg(group_id=gid, message=f"[CQ:reply,id={msg_id}] 收到")
```

> CQ 码是 OneBot 11 的写法。其它平台图片语法不同，但**文本发送接口不变**。

### 群管 / 查询（依赖 OneBot 接入端）

```python
ctx.ban(group_id, user_id, duration=600)      # 禁言（秒，0 解禁）
await ctx.akick(group_id, user_id)            # 踢出
ctx.mute_all(group_id, True)                  # 全员禁言
members = ctx.get_member_list(group_id)

# 或经 ctx.onebot 调任意 OneBot action
ctx.onebot.send_group_msg(group_id=123, message="hi")
info = ctx.onebot.get_login_info()
```

### 在命令匹配前截获原始消息

```python
def register(ctx):
    ctx.on_raw_message(raw_handler)

async def raw_handler(raw_event, bot_name):
    if 需要接管:
        return True     # True = 吃掉这条，后续命令/关键词都不跑
    # 其它情况返回 None/False，继续正常流程
```

多个原始处理器按插件 `priority` 升序执行，单个异常不影响别人。

---

## 安全须知

- **公网部署**：`web.host` 保持 `127.0.0.1` + 反代；`access_token` 设强随机值；按需 IP 白名单
- 启动时若提示「监听 0.0.0.0 且 token 为空」，请立刻处理
- 多账号时注意权限边界，别把超管能力随便开给所有 bot

## 下一步

- [编写插件](./writing-plugins.md) —— 业务怎么写
- [开始使用](./getting-started.md) —— 不接 IM 的两条路线
- [配置系统](./configuration.md) —— 各接入端完整字段
- [架构 · 事件总线](../advanced/architecture.md) —— 事件在框架里怎么流
