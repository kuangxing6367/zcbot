# Discord 接入端

Discord 接入端（官方插件）。走 Discord Gateway WSS 收事件，REST v10 出站。

## 元信息

- 优先级：7
- 进程归属：core
- 默认开关：禁用（`discord.enabled` 必须为 `true` 才开启）
- 提供的服务：`protocol_adapter`、`api_caller`

## 接入方式

在 Discord Developer Portal 创建应用与 Bot，取得 token 填入配置。本端建立 Gateway
WSS 连接并维持心跳，事件归一化后送入框架；出站消息经 REST v10 发送。

## 意图（Intents）

默认订阅：

- `GUILDS`
- `GUILD_MESSAGES`
- `DIRECT_MESSAGES`
- `MESSAGE_CONTENT`

`MESSAGE_CONTENT` 需要在 Discord 开发者后台为 Bot 开启「Message Content Intent」。

## 消息约定（内部）

- `send_msg` 映射到频道 `channel_id`；群消息 `group_id` 即频道 id
- 出站图片支持 `base64://`，经 multipart 上传

## 可配置项（config.yaml 段 `discord`）

| 键 | 说明 | 默认值 |
|---|---|---|
| `enabled` | 是否启用 | `false` |
| `token` | Bot Token | 空 |
| `bot_name` | Bot 名称 | `discord` |
| `intents` | 订阅意图位 | 上述四项的组合值 |
| `reconnect_interval` | 重连间隔（秒） | `5` |

## 支持的动作

协议中立动作 `send_msg` / `send_group_msg` / `send_private_msg` / `send_text`，
以及状态查询类 `get_status` / `get_login_info` / `get_online_clients`。
