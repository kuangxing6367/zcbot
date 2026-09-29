# 出站 WebSocket 接入端

出站 WebSocket 接入端（官方插件）。与 `onebot_adapter`（反向 WS 服务端，等客户端连入）
相反：本适配器作为**客户端**主动连出到外部 WebSocket 服务，把远端 JSON 事件归一化送入内核，
并把 `send_msg` / `send_text` / `base64` 图片等出站动作翻译回远端协议。

## 元信息

- 优先级：10
- 进程归属：core
- 默认开关：禁用（`ws_client.enabled` 必须为 `true` 才开启，避免误连外网）
- 提供的服务：`protocol_adapter`、`api_caller`

## 典型用途

- 桥接到自研网关 / 中控 / 多机器人总线
- 与另一套 IM 网关做双向透传
- 本地联调：一条 `ws://` 即可模拟完整收发链路

## 消息约定（JSON text frame）

入站（远端 → 本端）：

```json
{"type":"message","message_type":"private","user_id":1,"group_id":null,"message":"文本或消息段数组","sender":{}}
```

其它 `type`（`notice` / `request` / 自定义）原样透传并补 `bot_name`。

出站（本端 → 远端）：

```json
{"action":"send_msg","user_id":..,"group_id":..,"message":..}
```

`message` 支持纯文本 / 消息段数组；`image` 段的 `file` 支持 `base64://...`、
`file://`、http(s)、本地路径。未知动作在已连接时透传给远端。

## 可配置项（config.yaml 段 `ws_client`）

| 键 | 说明 | 默认值 |
|---|---|---|
| `enabled` | 是否启用 | `false` |
| `url` | 远端 WS 地址（须 `ws://` 或 `wss://`） | 空 |
| `bot_name` | Bot 名称 | `ws_client` |
| `token` | 鉴权令牌（Bearer） | 空 |
| `reconnect_interval` | 重连间隔（秒） | `5` |
| `max_queue` | 未连接时出站积压上限（条，有界） | `256` |

未连接时出站消息进入有界队列，连接建立后由 `_connect_once` 冲刷，超过 `max_queue`
则丢弃当前条。

## 支持的动作

`send_msg` / `send_group_msg` / `send_private_msg` / `send_text` /
`send_forward_msg`，状态查询 `get_status` / `get_login_info` / `get_online_clients`，
以及未知动作透传。
