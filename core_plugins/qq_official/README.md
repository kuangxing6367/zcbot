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
| `intents` | 订阅意图位 | `(1 << 25) \| (1 << 26)` |
| `reconnect_interval` | 重连间隔（秒） | `5` |
| `api_base` | OpenAPI 基址（迁移窗口期可覆盖） | `https://api.sgroup.qq.com` |
| `auto_ack_interaction` | 自动确认按钮回调（type 11/12） | `true` |
| `enable_notice_events` | 生命周期事件转 notice 分发 | `true` |

取令牌域名 `https://bots.qq.com`，正式 API `https://api.sgroup.qq.com`，
沙箱 `https://sandbox.api.sgroup.qq.com`；旧域名 `https://api.bot.qq.com` 保留作回退
（新域名网络异常 / 404 / 5xx 时自动用旧域名重试一次，4xx 业务错误不换域名）。

## 被动回复窗口

按会话类型各自维护被动回复 id（Resend 重放幂等去重窗口同步）：

- 群：300 秒
- 单聊：3600 秒

未显式 `active=True` 时，`send_msg` 自动挂被动 `msg_id`；`active=True` 则按主动消息发送。

## 支持的动作

`send_msg` / `send_group_msg` / `send_private_msg` / `send_text` /
`delete_msg`（撤回）/ `ack_interaction`（互动回调确认）以及状态查询类
`get_status` / `get_login_info` / `get_online_clients`。图片发送失败会向调用方反馈错误，
不会静默伪装成功。

---

## 出站能力（一条消息只能有一种主体）

QQ 官方规定 `msg_type` 互斥：文本(0) / markdown(2) / 富媒体(7)，
`keyboard`（按钮）只能挂在 markdown 上。适配器按下列优先级自动选型：

1. **富媒体**（段里出现 image / record / voice / video / file）→ `msg_type=7`
   - `file_type`：图 1、视频 2、语音 3、文件 4
   - 数据来源三选一：`base64://`、http(s) 直传、本地路径（含 `file://`）
2. **markdown**（段里有 markdown 或调用参数带 `markdown`）→ `msg_type=2`
   - 模板形态：`{'type':'markdown','data':{'template_id':'TPL','params':[...]}}`
   - 或 `call_api('send_msg', markdown='# 标题', markdown_template_id='TPL')`
3. **按钮**（段里有 keyboard 或参数带 `buttons`）→ 没有 markdown 时自动把文本包成
   markdown 再挂按钮
4. 其余 → 纯文本 `msg_type=0`

`at` 段官方不支持，退化为 `@昵称` 文本（不会整条被吞）。

### 写法示例

```python
# 文本
ctx.api('send_msg', group_id=gid, message='你好')

# markdown + 按钮
ctx.api('send_msg', group_id=gid,
        markdown='**选一个**',
        buttons=[{'text': '确认', 'data': 'ok'},
                 {'text': '跳转', 'link': 'https://example.com'}])

# 语音 / 视频 / 文件（走富媒体上传）
ctx.api('send_msg', group_id=gid,
        message=[{'type': 'record', 'data': {'file': 'https://x/a.silk'}}])

# 撤回
ctx.api('delete_msg', group_id=gid, message_id=msg_id)
```

按钮回调数据从 `notice.interaction` 事件取（见下）。

---

## 入站事件：互动与生命周期

除消息事件外，适配器把两类事件转成框架 notice 事件，走事件总线
（`ctx.on('notice.interaction', handler)` 等订阅）。

### 互动事件 `INTERACTION_CREATE` → `notice.interaction`

- 字段：`interaction_id` / `button_data` / `button_id` / `interaction_type` /
  `scene` / `group_id` / `user_id` / `timestamp`
- **自动 ACK**：只有 type=11（消息按钮）与 type=12（快捷菜单）需要回应，
  适配器自动 PUT `/interactions/{id}`；其余类型（13 反馈 / 14 清空会话 / 18-20 授权）
  官方明确无需回应，回了会报"已回应过"
- 回调的 `interaction_id` 会写进被动回复缓存，插件可直接回这条回调
- 用 `auto_ack_interaction: false` 关掉自动 ACK，改由插件自己调 `ack_interaction`

### 生命周期事件 → `notice.<type>`

| QQ 事件 | 内部 notice_type |
|---|---|
| `GROUP_ADD_ROBOT` | `group_add_robot` |
| `GROUP_DEL_ROBOT` | `group_del_robot` |
| `GROUP_MSG_REJECT` / `GROUP_MSG_RECEIVE` | `group_msg_reject` / `group_msg_receive` |
| `C2C_MSG_REJECT` / `C2C_MSG_RECEIVE` | `c2c_msg_reject` / `c2c_msg_receive` |
| `GROUP_MEMBER_ADD` / `GROUP_MEMBER_REMOVE` | `group_increase` / `group_decrease` |
| `FRIEND_ADD` / `FRIEND_DEL` | `friend_add` / `friend_del` |
| `GROUP_JOIN_REQUEST` | `group_join_request` |
| `SUBSCRIBE_MESSAGE_STATUS` | `subscribe_status` |

开关：`enable_notice_events`（默认 true）。

### 意图位（intents）

默认值 `(1<<25) | (1<<26)` = `100663296`：

- `1<<25` GROUP_AND_C2C_EVENT —— 群/单聊消息 + 群与好友生命周期
- `1<<26` INTERACTION —— 互动事件（按钮回调）

只想收消息就配回 `33554432`（仅 `1<<25`）。
`GROUP_MEMBER_ADD/REMOVE` 属于 `GROUP_MEMBER_EVENT(1<<24)`，需在开放平台开通权限，
未开通时网关会以 4014 拒绝连接——遇到这种情况把 intents 改回不含该位即可。
