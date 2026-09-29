# OneBot 11 适配器

OneBot 11 协议接入端（官方插件）。将 OneBot 反向 WebSocket 事件转换成框架内部事件，
并通过 OneBot API 动作完成消息收发。

本模块是 OneBot 协议的专属实现，框架核心（`framework/`）不包含任何 OneBot 代码：
WebSocket 接入、连接管理、API 调用通道都在本插件内；标准动作封装在同级 `onebot_api.py`。

## 元信息

- 优先级：0
- 进程归属：core（仅核心进程与单进程加载，宿主进程排除）
- 默认开关：启用（`onebot.enabled` 设为 `false` 可禁用）
- 提供的服务：`protocol_adapter`、`api_caller`、`onebot_api`、`ws_server`

## 接入方式

作为反向 WebSocket 服务端等待客户端连入。OneBot 客户端（NapCat / Lagrange /
LLOneBot 等）添加「反向 WebSocket」连接，填写服务端地址即可接入。

- 默认监听地址 `0.0.0.0`，端口 `6830`
- 启用 `config['ssl']` 后走 `wss://`，与 Web 后台共用证书
- 支持 `access_token` 鉴权（URL 参数 `access_token` 或 `Authorization: Bearer`）

## 可配置项（config.yaml 段 `onebot`）

| 键 | 说明 | 默认值 |
|---|---|---|
| `enabled` | 是否启用 | 启用 |
| `listen_host` | 监听地址 | `0.0.0.0` |
| `listen_port` | 监听端口 | `6830` |
| `access_token` | 鉴权令牌 | 空 |
| `max_pending_events` | 分发层积压上限（条） | `4096` |
| `max_pending_bytes` | 分发层字节上限（字节，防大事件撑爆内存） | `64 * 1024 * 1024`（64MB） |
| `max_frame_size` | WS 单帧上限（字节，防 base64 大图 1009 断链） | `16 * 1024 * 1024`（16MB） |

条数与字节双闸门：单条 OneBot 原始 payload 可达数 MB，仅按条数限流可能在未达条数上限时
已吃掉大量内存，因此同时配备字节上限。

## 事件归一化

`normalize_event` 将 OneBot 11 事件转换为框架内部事件 dict，并预存 `_est_size`
（事件尺寸估算）供 `EventBuffer` 水位判断直接 O(1) 取值，避免每条事件重复估算。

## 安全提示

当 Web 面板监听 `0.0.0.0` 且 OneBot `access_token` 为空时，插件会打印告警：
公网部署存在被接管风险。建议设置 `onebot.access_token` 并将 `web.host` 改为 `127.0.0.1`。

## 与其它插件的关系

- 与 `rust_accel` 互斥：两者都监听反向 WS 并注入事件，同时启用会导致重复事件与端口竞争。
  启用 `rust_accel` 时建议禁用本插件。
- 作为 `protocol_adapter` 注册后，框架内核统一通过 `fw.services.get('protocol_adapter')`
  取用，`api_caller` 提供 `send_msg` / `send_group_msg` / `send_private_msg` 等动作通道。
