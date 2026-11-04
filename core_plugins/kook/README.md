# KOOK 接入端

KOOK 接入端（官方插件）。走 KOOK Gateway WebSocket 收事件，REST v3 出站。

## 元信息

- 优先级：7
- 进程归属：core
- 默认开关：禁用（`kook.enabled` 必须为 `true` 才开启）
- 提供的服务：`protocol_adapter`、`api_caller`

## 接入方式

1. 在 [KOOK 开发者后台](https://developer.kookapp.cn/) 创建应用；
2. 应用 → 机器人 → 复制 Token 填入 `core_plugins.yaml` 的 `kook.token`；
3. 「机器人」页把连接模式设为 **WebSocket**（与 Webhook 互斥，选了 Webhook
   就不能再用 WS）；
4. 把机器人邀请进服务器，并给目标频道「发送消息」权限。

```yaml
core_plugins:
  kook:
    enabled: true
    token: '你的 bot token'
    bot_name: kook
```

## 协议要点（实现依据）

- 网关地址不写死，走 `GET /api/v3/gateway/index?compress=0` 获取。
  `compress` 默认是 1（zlib），本端固定传 0 取明文，少一层解压失败点。
- 信令：`s=0` 事件（带 `sn`）、`1` HELLO、`2` PING、`3` PONG、`4` RESUME、
  `5` RECONNECT、`6` RESUME ACK。客户端只发 2 和 4。
- 心跳 `30s ± 5s` 随机，**6 秒内没收到 PONG 判定超时**并断线重连。
- 断线恢复：在网关地址后拼 `resume=1&sn=<已处理最大 sn>&session_id=<会话>`。
  成功后服务端补发离线消息，最后给 RESUME ACK。
- 收到 `RECONNECT(s=5)` 必须清空 `sn` 与会话、重新取网关，否则消息会错乱。
- `sn` 乱序或重复：重复的直接丢弃（官方明确要求）。

## 已过滤的情况

- `extra.author.bot = true` 的消息一律不进管线。官方文档专门警告过：
  不过滤会形成两个机器人互相回复的死循环，进而触发封禁。
- `author_id` 等于自身 id 的消息。

## 能力边界

| 能力 | 支持 | 说明 |
| --- | --- | --- |
| 频道文本 | 是 | `POST /api/v3/message/create`，type=9（KMarkdown） |
| 引用回复 | 是 | 传 `quote` = 被引用消息 id |
| 撤回 | 是 | `POST /api/v3/message/delete` |
| 频道图片/视频 | 否 | KOOK 要求资源必须由本机器人上传后才能引用，外部 URL 会被拒 |
| 主动私聊 | 否 | 需要 `chat_code`，只能从私聊事件取得，本端未实现 |

## 常见问题

**日志出现 `40101 token 无效` / `40103 token 已过期`**
本端会直接停止重连并打印原因。KOOK 的 token 重置后旧值立即失效，
到开发者后台重新复制，改配置后重启接入端。

**频率限制**
KOOK 返回 429 时，本端返回 `retcode=-4` + `retry_after`（读 `Retry-After` 头），
与出站闸门的「被限流」语义一致，插件不需要区分平台。

**发送量配额**
官方对每个开发者账号下所有机器人限制单日 10000 条（北京时间 12:00 重置）。
对 5 分钟内用户消息的首次回复会有大幅折扣，所以优先用「回复」而不是「主动发」。
