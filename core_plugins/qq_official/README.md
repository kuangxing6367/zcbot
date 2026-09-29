# QQ 官方机器人接入端

QQ 开放平台官方 OpenAPI + WSS 网关接入端（官方插件）。不依赖 botpy，仅用
`websockets` + `requests`。

## 接入流程

1. `getAppAccessToken` 获取 `access_token`（有效期 7200s，自动刷新，提前 60s 续期）
2. `GET /gateway/bot` 取 WSS 地址，Hello → Identify → 心跳 → Dispatch
3. 群 / 单聊事件归一化为内部事件 dict；出站 `send_msg` 翻回 OpenAPI

## 元信息

- 优先级：5
- 进程归属：core
- 默认开关：禁用（`qq_official.enabled` 必须为 `true` 才开启）
- 提供的服务：`protocol_adapter`、`api_caller`

## 消息约定（内部）

- 群：`message_type=group`，`group_id=group_openid`，`user_id=member_openid`
- 单聊：`message_type=private`，`user_id=user_openid`
- 出站图片：`base64://` / `file://` / `http(s)` / 本地路径 → 群文件上传 → `msg_type=7`

## 可配置项（config.yaml 段 `qq_official`）

| 键 | 说明 | 默认值 |
|---|---|---|
| `enabled` | 是否启用 | `false` |
| `app_id` | 机器人 AppID | 空 |
| `app_secret` | 机器人 AppSecret | 空 |
| `bot_name` | Bot 名称 | `qq_official` |
| `intents` | 订阅意图位 | `1 << 25` |
| `reconnect_interval` | 重连间隔（秒） | `5` |
| `api_base` | OpenAPI 基址（迁移窗口期可覆盖） | `https://api.sgroup.qq.com` |

取令牌域名 `https://bots.qq.com`，正式 API `https://api.sgroup.qq.com`，
沙箱 `https://sandbox.api.sgroup.qq.com`；旧域名 `https://api.bot.qq.com` 保留作回退
（新域名网络异常 / 404 / 5xx 时自动用旧域名重试一次，4xx 业务错误不换域名）。

## 被动回复窗口

按会话类型各自维护被动回复 id（Resend 重放幂等去重窗口同步）：

- 群：300 秒
- 单聊：3600 秒

未显式 `active=True` 时，`send_msg` 自动挂被动 `msg_id`；`active=True` 则按主动消息发送。

## 支持的动作

`send_msg` / `send_group_msg` / `send_private_msg` / `send_text` 以及状态查询类
`get_status` / `get_login_info` / `get_online_clients`。图片发送失败会向调用方反馈错误，
不会静默伪装成功。
