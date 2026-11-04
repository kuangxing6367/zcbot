# Matrix 接入端

Matrix 接入端（官方插件）。走 Client-Server API v3 长轮询收事件，REST 出站。
不依赖 matrix-nio 等 SDK，只用 requests。

## 元信息

- 优先级：7
- 进程归属：core
- 默认开关：禁用（`matrix.enabled` 必须为 `true` 才开启）
- 提供的服务：`protocol_adapter`、`api_caller`

## 接入方式

1. 用任意 Matrix 客户端登录机器人账号；
2. 取 access token（Element：设置 → 帮助与关于 → 访问令牌）；
3. 把机器人账号邀请进目标房间（不邀请就收不到任何事件）；
4. 填入配置：

```yaml
core_plugins:
  matrix:
    enabled: true
    homeserver: 'https://matrix.org'
    access_token: 'syt_xxx'
    user_id: '@mybot:matrix.org'
    bot_name: matrix
    sync_timeout: 30
    rooms: []          # 空 = 所有已加入房间
```

自建 homeserver 把 `homeserver` 换掉即可，协议完全一致。

## 协议要点（实现依据）

- 收：`GET /_matrix/client/v3/sync?since=<token>&timeout=30000`，
  响应的 `next_batch` 作为下次的 `since`。
- **`since` 必须落盘**（`data/matrix_sync.json`）。不放的话每次重启都是一次
  「首次 sync」，会把房间最近的历史消息重新投一遍，用户收到重复回复；
  而入站去重窗口重启后已清空，挡不住。
- `since` 记录里带 homeserver + user_id 指纹，换账号不会沿用旧游标
  （沿用会静默跳过一段消息，比重复更危险）。
- 发：`PUT /_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txnId}`。
  `txnId` 是幂等键，**重试必须复用同一个值**，换掉就是真的发第二条。
- 限流：HTTP 429 + `{"errcode":"M_LIMIT_EXCEEDED","retry_after_ms":N}`，
  本端按 `retry_after_ms` 等待，不自己拍间隔。
- 致命错误：`M_UNKNOWN_TOKEN` / `M_MISSING_TOKEN` / `M_FORBIDDEN` /
  `M_USER_DEACTIVATED` 直接停止轮询并打印原因，重试不会自愈。

## 能力边界

| 能力 | 支持 | 说明 |
| --- | --- | --- |
| 房间文本 | 是 | `m.text`，支持 `m.relates_to` 引用回复 |
| 图片/语音/视频/文件入站 | 是 | 段里带 `mxc://` 与转换后的 HTTPS 地址 |
| 媒体出站 | 否 | 需先 `POST /_matrix/media/v3/upload` 再发 `m.image`，本端未实现 |
| 私聊 | 视房间 | Matrix 的私聊也是 room，机器人被邀请即可收发 |
| 加密房间 | 否 | 需要 olm/megolm 端到端加密，不在本端范围 |

## 常见问题

**能连上但收不到消息**
九成是机器人没被邀请进房间。另外检查 `rooms` 是否把目标房间排除在外。

**重启后收到一堆旧消息**
检查 `data/matrix_sync.json` 是否可写。写不进去时本端只告警不中断收消息，
但下次启动会回到「首次 sync」。

**私有媒体的 URL 打不开**
`mxc://` 转出来的 HTTPS 地址仍需要带 `Authorization: Bearer` 才能下载，
不是公开链接。需要直接给用户看的话，由插件自行下载后转存。
