# HTTP 事件注入端

HTTP 事件注入接入端（官方插件）。以 `ProtocolAdapter` 形态提供 HTTP 入口，外部系统
POST JSON 即可把事件注入框架内部事件流。

## 元信息

- 优先级：10
- 进程归属：core
- 默认开关：禁用（`http_inject.enabled` 必须为 `true` 才开启）
- 提供的服务：`protocol_adapter`、`api_caller`

## 接入方式

在配置允许的端口上提供 HTTP 服务，路径缺省为 `/hook`（可配置），鉴权用 `Bearer` token。
外部系统 POST 一个 JSON 事件到该路径，插件将其翻译成内部事件 dict 并
`dispatch_event` 送入框架。

## 可配置项（config.yaml 段 `http_inject`）

| 键 | 说明 | 默认值 |
|---|---|---|
| `enabled` | 是否启用 | `false` |
| `host` | 监听地址 | `0.0.0.0` |
| `port` | 监听端口 | `8901` |
| `path` | 注入路径 | `/hook` |
| `token` | Bearer 鉴权令牌 | 空（空时不校验） |

## 用途

- 把外部系统（业务后台、监控、其他 IM 网关）的消息/通知统一经框架路由
- 与 `ws_client` 同为桥接用途，但走 HTTP 而非 WebSocket，适合低频、请求/响应模型
