# Telegram 接入端

Telegram 接入端（官方插件）。通过 `getUpdates` 长轮询收消息，`sendMessage` /
`sendPhoto` 出站。仅用 `requests`，无新增三方依赖。

## 元信息

- 优先级：6
- 进程归属：core
- 默认开关：禁用（`telegram.enabled` 必须为 `true` 才开启）
- 提供的服务：`protocol_adapter`、`api_caller`

## 接入方式

向 @BotFather 申请 token 填入配置，本端长轮询 `getUpdates`，出站 `sendMessage` /
`sendPhoto`（图片支持 `base64://`）。

## 消息约定（内部）

- 私聊 → `message_type=private`
- 超级群 / 群 / 频道 → `message_type=group`
- 出站图片：`base64://` / `file://` / `http(s)` / 本地路径 → multipart `sendPhoto`
- `callback_query` 等事件透传为 `notice`（type=`notice`，`notice_type=callback_query`）

## 可配置项（config.yaml 段 `telegram`）

| 键 | 说明 | 默认值 |
|---|---|---|
| `enabled` | 是否启用 | `false` |
| `token` | Bot Token | 空 |
| `bot_name` | Bot 名称 | `telegram` |
| `polling_timeout` | 长轮询超时（秒） | `30` |
| `allowed_updates` | 订阅更新类型 | `['message','edited_message']` |
| `api_base` | API 基址 | `https://api.telegram.org` |

启动时会先 `getMe` 校验 token 并回写 `bot_name` 为真实用户名。

## 支持的动作

`send_msg` / `send_group_msg` / `send_private_msg` / `send_text`、状态查询
`get_status` / `get_login_info` / `get_online_clients`、以及 `get_me`。
