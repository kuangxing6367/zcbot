# 对接 IM 平台

你想让 ZCBOT 连上一个真实的聊天软件，让机器人能收发消息——这就是这篇要解决的问题。

框架本身协议无关：「消息怎么来、怎么发」全由**接入端插件**负责。所有接入端默认都是**关闭**的，需显式启用；最常用的是 `onebot_adapter`（OneBot 11，常用来接 QQ）；热路径性能敏感时可换 `rust_accel`（同一协议的 Rust 加速接入端，默认关，见下表）；另外还有 QQ 官方、Telegram、Discord、出站 WS 等，同样默认关闭，填好凭证再开。

:::tip 先确认要不要读完
框架能启动、命令能注册、定时任务能跑——**都不需要**接聊天平台。
只有「要和某个聊天软件收发消息」时才需要这篇。

如果你的插件要处理图片、语音、视频、文件，或者要禁言、踢人、撤回，
直接跳到 [插件里怎么发消息](#插件里怎么发消息)——那几节讲的是跨协议写法。
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
| `onebot_adapter` | QQ（经 NapCat 等） | 默认关闭，需启用；设 `access_token` 更安全 |
| `rust_accel` | QQ（同上，OneBot 11） | Rust 加速接入端（默认关）：`enabled: true`；启用前先停 `onebot_adapter`，监听独立端口 6831 |
| `qq_official` | QQ 官方机器人 | `enabled: true` + `app_id` / `app_secret` |
| `telegram` | Telegram | `enabled: true` + `token`（找 @BotFather 要） |
| `discord` | Discord | `enabled: true` + `token`（开发者门户；`MESSAGE_CONTENT` Intent 需开） |
| `kook` | KOOK | `enabled: true` + `token`（开发者后台，连接模式选 WebSocket） |
| `matrix` | Matrix（含自建 homeserver） | `enabled: true` + `homeserver` / `access_token` / `user_id` |
| `ws_client` | 任意 WS 总线 | `enabled: true` + `url`（主动连出去） |
| `http_inject` | 不接 IM / Webhook | `enabled: true` + 地址端口 |

这些接入端都实现了统一连接自描述，后台「接入端连接」页可以改配置。

收发能力差异很大，具体到每种媒体能不能发、发的时候吃什么引用，见
[能力矩阵](#能力矩阵谁能发什么)。一句话版本：**入站全部统一，出站各家不同**。

## 失败是怎么报给你的

各平台的错误模型不一样，接入端会把它们统一成同一套 `retcode`，插件不需要按平台分支：

| retcode | 含义 | 触发例子 |
| --- | --- | --- |
| `0` | 成功 | — |
| `-1` | 失败（平台业务错误，或接入端未连接） | Telegram `chat not found`、Discord 403 缺权限、KOOK `code=40000` |
| `-2` | 请求超时 | OneBot 等待响应超 10s |
| `-3` | 网络/连接异常 | 发不出去、连接已断 |
| `-4` | 被平台限流 | HTTP 429；带 `throttled: true` 与 `retry_after`（秒） |
| `-10` | 该端不支持此动作 | 例如 KOOK 主动私聊、Matrix 未实现的动作 |

几个容易踩的点，接入端已经处理：

- **HTTP 200 不代表成功。** QQ 官方失败时仍返回 200，业务码在 body 的 `code` 里；
  官方文档明确写了 `message` 只用于人工排查、内容可能随时调整，所以判据只取 `code`。
- **限流必须按平台给的等待时间。** 自己拍一个更短的间隔重试会被继续限，越试越久。
  各端都会读 `retry_after` / `retry_after_ms` / `Retry-After`，原样放在返回值里。
- **凭证类错误不重试。** Discord 的 4004/4013/4014、KOOK 的 40101/40103、
  Matrix 的 `M_UNKNOWN_TOKEN`、Telegram 的 401，都会停止重连并打印原因——
  这类错误重连一万次也不会好，还会把配额烧光（Discord 限 1000 次 IDENTIFY/天）。
- **Telegram `getUpdates` 与 webhook 互斥。** 配过 webhook 的 bot 轮询会一直返回 409，
  接入端会只提示一次并给出 `deleteWebhook` 的修法。

---

## 默认路线：OneBot 11（QQ）

### 1. 确认接入端已启用

编辑 `data/core_plugins.yaml`（首次启动会自动生成；旧布局下这个文件在项目根目录，
两处指向同一份清单）：

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

> 这条 `/echo` 由内置的 **echo 自检插件**（`core_plugins/echo`，默认开启）提供，
> 覆盖**所有**接入端（OneBot / QQ 官方 / Telegram / Discord / WS / HTTP 注入），
> 发什么回什么——专门用来验证「消息进得来、命令路由得通、回复发得出」整条链路。
>
> 同理，`/help`（及别名 `/帮助`、`/菜单`）由内置的 **help 插件**（`core_plugins/help`，默认开启）
> 提供，列出当前所有已注册命令。

---

## 其它平台（简要）

以 Telegram 为例，三步：

1. `core_plugins.yaml` 里 `telegram.enabled: true`，填 `token`
2. 重启，日志确认接入端就绪
3. 给机器人发 `/echo 你好`（内置 echo 自检插件响应）

Discord、QQ 官方同理：**开插件 → 填 token/app 凭证 → 重启 → 发 `/echo 你好` 自测**。
详细字段说明见 [配置系统 · 各接入端](./配置系统.md)。

---

## 插件里怎么发消息

消息进框架后，插件只用统一的 `ctx`，不必关心底下是 OneBot 还是别的协议。

### 文本

```python
ctx.send_msg(user_id=uid, group_id=None, message="你好")      # 私聊
ctx.send_msg(group_id=gid, user_id=None, message="大家好")     # 群
# 异步 handler 里用 await ctx.asend_msg(...)
```

本节的其余能力，同步方法都有一个 `a` 前缀的异步版本（`send_msg` → `asend_msg`）。
**async handler 里请一律用异步版**，否则网络请求会占住事件循环，别的插件和定时任务都得等它。

---

## 富媒体：先认这套规范段

一条消息在框架里是一串**规范消息段**：

```python
{'type': 'image', 'data': {'url': 'https://…/a.png'}}
```

`type` 是语义类型，`data` 是这一段的字段。所有接入端收到的消息都会被翻译成这个形状，
所以插件只写一套代码就能读所有平台的图片、语音、文件。

### 段类型总表

**内容型**（承载可读内容）

| type | 含义 | 备注 |
| --- | --- | --- |
| `text` | 纯文本 | `data.text` |
| `markdown` | Markdown 正文 | QQ 官方 / 飞书 |
| `image` | 图片 | 静态图 |
| `voice` | 语音 / 音频 | OneBot 的 `record`、Telegram 的 `voice` 与 `audio` 都归到这里 |
| `video` | 视频 | |
| `file` | 文件 | 任意附件 |
| `sticker` | 贴纸 | Telegram / Discord |
| `video_note` | 视频便签（圆形短视频） | Telegram |
| `audio` ⚠️ | 音频文件 | **保留名，不会出现**，见下方说明 |
| `animation` ⚠️ | 动图 GIF | **保留名，不会出现** |

**交互型**（表达动作 / 关系，不是内容）

| type | 含义 |
| --- | --- |
| `at` | 提及某人与 @全体（`data.qq == 'all'`） |
| `face` | 平台表情（QQ 小黄脸这类） |
| `reply` | 回复某条消息（`data.id`） |
| `reaction` | 表情回应（对既有消息的反应，不是新消息） |
| `quote` ⚠️ | 引用 / 话题引用（**保留名**） |
| `poke` | 戳一戳 |
| `dice` / `rps` | 骰子 / 猜拳 |

**结构型**（承载结构化数据）

`share`、`json`、`xml`、`location`、`venue`、`music`、`forward`（合并转发）、
`post`（富文本多段落）、`contact`（名片）、`poll`（投票）、`keyboard`（按钮组）、
`button`、`embed`（富嵌入）、`system`（入群 / 移出等系统消息）、
`ephemeral`（仅指定接收者可见）、`card` ⚠️（**保留名**）

### 四个「保留名」——写了也取不到

`audio`、`animation`、`card`、`quote` 这四个类型在归一阶段就并入了别的类型：

| 你看到的 | 实际变成 | 为什么 |
| --- | --- | --- |
| `audio` | `voice` | 兼容阶段把 OneBot 的 `audio` 与 `record` 都当语音 |
| `animation` | `video` | 早期把 GIF 当视频处理，改了会静默改变存量插件行为 |
| `card` | `json` | 交互卡片当初按 json 段收的 |
| `quote` | `reply` | 引用与回复共用同一套字段 |

所以 `ev.audios` 这种属性是不存在的——`ev.voices` 拿到的就是**全部音频**。
要区分「语音消息」和「音频文件」，按 `ev.adapter` 分支读原始事件（`ev.raw`），
或看段里的 `duration` / `name` 字段。

这不是遗漏，是刻意的边界：**入站归一优先，宁可粗一点也不要让存量插件行为漂移。**

### data 里能放什么

媒体段的 `data` 有一些约定键，跨协议通用：

| 键 | 含义 |
| --- | --- |
| `file` | 统一文件引用（URL / 本地路径 / `file_id` / `base64://…`） |
| `url` | 可直接下载的 http(s) 地址 |
| `path` | 本地磁盘路径 |
| `base64` | 裸 base64（不带 `data:` 前缀） |
| `file_id` | 协议侧文件标识（Telegram / QQ 官方） |
| `file_info` | QQ 官方上传后返回的文件信息 |
| `name` | 文件名 |
| `mime` | MIME 类型 |
| `size` | 字节大小 |
| `duration` | 音视频时长（秒） |
| `width` / `height` / `thumb` | 尺寸与缩略图 |

没列出的键允许存在（接入端会塞协议私有字段），但**不要依赖**——
只有上表这些是跨协议有保证的。

---

## 富媒体：怎么读

事件对象上有一组现成属性，不用自己遍历 `segments`（下文的 `ev` 就是 handler
接到的 `event` 参数）：

| 属性 | 拿到什么 |
| --- | --- |
| `ev.segments` | 全部规范段（`list[dict]`） |
| `ev.images` / `ev.voices` / `ev.videos` / `ev.files` / `ev.stickers` / `ev.video_notes` | 该类型所有段的 **`data` 列表** |
| `ev.first_image` / `first_voice` / `first_video` / `first_file` / `first_video_note` | 第一个的 `data`（没有则 `{}`） |
| `ev.has_image` / `has_voice` / `has_video` / `has_file` / `has_sticker` / `has_face` / `has_share` / `has_video_note` | 布尔判断 |
| `ev.media` | 所有内容型段（图片 / 语音 / 视频 / 文件 / 贴纸） |
| `ev.is_media_only` | 是否「只有媒体、没有文字」 |
| `ev.has_at` / `has_at_bot` / `at_all` / `at_list` | @ 相关 |
| `ev.reply_id` | 被回复的消息 id（没有则 `None`） |
| `ev.share` | 分享卡片数据 |
| `ev.text` | 消息的纯文本（媒体段被剔掉） |

**取文件本体用 `ev.media_url()`**，它会自动在各协议的字段之间找引用：

```python
ref = ev.media_url(ev.first_image)
# 可能是 'https://…/a.png'、'/data/cache/a.image'、'file_id:AgAC…' 或 'base64://…'
```

三种引用的含义：

- `http(s)://…` —— 可以直接下载（各平台给的临时地址一般几小时就过期）；
- 本地路径 / `file://` —— 接入端已经把文件落到本机了；
- `file_id:…` —— 只有接入端能换出真实文件，别自己拼 URL。

### 例子：收到图就回一句话

```python
def register(ctx):
    ctx.command('/看图', on_view, description='收到图片就回一句')

async def on_view(event, match):
    if not event.has_image:
        await ctx.asend_msg(group_id=event.group_id, message='要带图哦')
        return
    await ctx.asend_msg(group_id=event.group_id,
                        message=f'收到 {len(event.images)} 张图')
```

两点约定，容易在这里写错：

- **handler 签名是 `(event, match)`**。`ctx` 是加载器注入的模块级变量，不是参数。
  写成 `def on_view(ctx, ev)` 会把事件对象当成 ctx、把 match 当成 event，报错时很难看出原因。
- **`ctx.command` 是方法，不是装饰器**。第一个参数是命令名或正则，第二个是 handler。

`ev.is_media_only` 用于「用户只发了图、一个字都没打」这种消息：它同时要求
存在媒体段且文本为空。纯图片消息的 `ev.text` 是空串，所以按文本匹配的命令不会触发。

---

## 富媒体：怎么发

### 写法一：规范段构造函数（推荐）

```python
from framework.messaging.segments import at, image, text, voice, video, file_seg, reply

await ctx.asend_msg(group_id=gid, message=[
    at(uid), text(' 看这个'),
    image(url='https://example.com/a.png'),
])

await ctx.asend_msg(group_id=gid, message=[
    reply(msg_id), file_seg(path='/data/report.pdf', name='月报.pdf'),
])
```

每个构造函数的签名：

| 函数 | 参数 |
| --- | --- |
| `text(s)` | 文本 |
| `at(qq, name='')` | `qq='all'` 即 @全体 |
| `at_all()` | @全体 |
| `image(file=None, url='', base64='', **extra)` | 三种引用任选其一 |
| `voice(...)` | 同上，另有 `duration=秒` |
| `video(...)` | 同上 |
| `file_seg(file=None, url='', name='', **extra)` | 同上，另有文件名 |
| `reply(message_id)` | 引用某条消息 |
| `face(face_id)` | 平台表情 |
| `seg(type_name, data)` | 任意类型的通用构造 |

这种写法**跨接入端通用**：框架会把段翻译成各平台自己的结构。

### 写法二：CQ 码字符串（仅 OneBot）

```python
ctx.send_msg(group_id=gid, message="[CQ:image,file=https://example.com/a.png]")
ctx.send_msg(group_id=gid, message=f"[CQ:at,qq={uid}] 看这里")
ctx.send_msg(group_id=gid, message=f"[CQ:reply,id={msg_id}] 收到")
```

CQ 码只有 OneBot 11 认。字符串里带 `[CQ:` 的消息，其它接入端会当普通文本发出去——
看着「发出去了」，实际发的是那串字面量。

:::warning 关于 `auto_escape`
`ctx.send_msg(..., auto_escape=True)` 表示「把消息当纯文本，不解析 CQ 码」。
这个参数只在 OneBot 系有意义；并且开着它时框架不做段转换。
:::

### 写法三：平台原生 action

需要平台私有字段（比如 QQ 官方的 ark、Discord 的 embed）时，直接调 action：

```python
ctx.onebot.send_group_msg(group_id=123, message=[{'type': 'image', 'data': {'file': 'https://…'}}])
```

`ctx.onebot` 就是 `ctx.actions` 的别名：OneBot 来源时是类型化的 API 封装，
其它来源时是协议中立的转发代理（`ctx.actions.get_group_info(...)` 之类会被原样
翻译成该接入端的同名动作）。

### 能力矩阵：谁能发什么

| 接入端 | 图片 | 语音 | 视频 | 文件 | 贴纸 | 吃什么引用 | 一条带几个媒体 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| OneBot 11（`onebot_adapter` / `rust_accel`） | ✅ | ✅ | ✅ | ✅ | ✅ | URL / 本地路径 / `base64://` | 全部 |
| Telegram | ✅ | ✅ | ✅ | ✅ | ✅ | URL / 本地路径 / `file_id` | 全部（文本作 caption） |
| Discord | ✅ | ✅ | ✅ | ✅ | ❌ | 本地路径 / `base64://`（远程 URL 退化为正文链接） | 最多 10 个附件 |
| QQ 官方 | ✅ | ✅ | ✅ | ✅ | ❌ | 本地路径 / URL（需先走富媒体上传） | **只发第一个** |
| KOOK | ✅ | ⚠️ 降级为文件 | ✅ | ✅ | ❌ | URL / 本地路径 / `base64://` | 各自成条 |
| Matrix | ✅ | ⚠️ 记为 `m.audio` | ✅ | ✅ | ❌ | 本地路径 / `base64://`（**不吃 http URL**） | 各自成条 |

几个必须知道的坑：

- **QQ 官方一条消息只带一个媒体。** 传了三个 `image` 段，只有第一个会发出去，
  多余的是静默丢弃——调用方以为全发了。要发多张就分多次调。
- **Discord 的远程 URL 当不了附件。** 它只能上传本地字节，所以 `image(url=…)`
  会被降级成正文里的一个链接。
- **Matrix 只吃本地路径与 base64。** 给 http URL 会直接报「无法读取媒体引用」，
  要先 `ctx.adownload_media()` 落到本地（见下一节）。
- **KOOK 一条消息只能有一种 type。** 文本先单独发一条，媒体各自成条；
  语音没有对外发送类型，会被当成文件发。
- **OneBot 的 `voice` 会被翻回 `record`。** 你用规范名写，框架负责转，
  不用自己记协议名。

---

## 落盘：把媒体存到本地

读到了引用不等于拿到了文件。要处理内容（读像素、转码、归档），先用
`ctx.download_media()` 落盘：

```python
res = await ctx.adownload_media(ev.first_image)
if res['status'] == 'ok':
    path, size = res['path'], res['size']
else:
    ctx.logger.warning('取图失败: %s (%s)', res['msg'], res['status'])
```

返回结构固定，四种状态：

| `status` | 含义 | `msg` 里是什么 |
| --- | --- | --- |
| `ok` | 已落盘，`path` / `size` 有效 | 空 |
| `failed` | 失败（无引用 / 超限 / 解码失败 / 文件不存在 / HTTP 非 2xx） | 具体原因 |
| `unsupported` | `file_id` 换不出可下载地址 | 该接入端不支持 |
| `needs_adapter` | 只可能出现在底层函数里，`ctx` 层已处理 | — |

**参数**：`dest_dir`（默认 `<状态目录>/media`）、`filename`、`max_bytes`
（默认 50MB）、`timeout`（默认 30 秒）、`bot`。

### 安全行为（这些是刻意的，不是限制）

- **先预检再写盘。** base64 按长度估算（超限不进解码器），HTTP 看
  `Content-Length`，本地文件先 `getsize`。超限直接拒绝，**不截断保存**——
  截断出来的「成功文件」比失败更难查。
- **流式写入 + 边写边计。** 服务端不给 `Content-Length` 时也拦得住：
  累计超过上限立刻停手并删掉半成品。
- **写 `.part` 再转正。** 写到一半断网，磁盘上不会留下一个看起来正常的残文件。
- **文件名强制净化。** 消息发送方能完全控制 `data['name']`，`../../config.yaml`
  是普通输入。落盘前切掉所有路径分隔符、控制字符、Windows 保留名（`CON`、`NUL`…），
  并再次校验最终路径没越出目标目录。
- **同名不覆盖。** 目标目录已有同名文件时自动加 `-1`、`-2`。

---

## 群操作

群管类动作目前**完整支持在 OneBot 系**（`onebot_adapter` / `rust_accel`）；
QQ 官方部分支持；Telegram / Discord / KOOK / Matrix 多数返回 `-10` 不支持。
写群管插件换端前务必先验一遍。

### 快捷方法

每个都有同步 / 异步两份：

| 同步 | 异步 | 做什么 |
| --- | --- | --- |
| `ctx.ban(gid, uid, duration=600)` | `ctx.aban(...)` | 禁言（秒；`0` 解禁） |
| `ctx.kick(gid, uid, reject_add_request=False)` | `ctx.akick(...)` | 踢出（可顺带拒绝再加群） |
| `ctx.mute_all(gid, enable=True)` | `ctx.amute_all(...)` | 全员禁言 / 解禁 |
| `ctx.set_card(gid, uid, card)` | `ctx.aset_card(...)` | 改群名片（空串清除） |
| `ctx.get_member_list(gid)` | `ctx.aget_member_list(...)` | 群成员列表 |
| `ctx.get_member_info(gid, uid)` | `ctx.aget_member_info(...)` | 单个成员信息 |
| `ctx.recall(message_id)` | `ctx.arecall(...)` | 撤回消息 |

### 角色判断

```python
ctx.is_group_admin(gid, uid)    # 管理员或群主
ctx.is_group_owner(gid, uid)    # 群主
ctx.is_superuser(uid)           # 框架超管（config.yaml 里配的）
ctx.is_blacklisted(uid)         # 在黑名单里
ctx.get_user_role(gid, uid)     # "super" > "owner" > "admin" > "member" > "blacklist"
```

这些查的是**框架自己的数据库**（`group_members` / `users` 表），不发网络请求，
所以很快，但前提是成员信息已经同步过。

### 例子：撤回 + 禁言

```python
def register(ctx):
    ctx.command('/处理', on_handle, require_admin=True)

async def on_handle(event, match):
    await ctx.arecall(event.message_id)                      # 撤回触发消息
    await ctx.aban(event.group_id, event.user_id, duration=600)  # 禁言 10 分钟
```

`require_admin=True` 让框架在派发前就判权限，handler 里不用再查——
比在 handler 开头写 `if not ctx.is_group_admin(...): return` 更省事，
也不会因为忘了写判断就把群管动作暴露给所有人。

### 更多动作走原生

快捷方法没覆盖的（设管理、改群名、设专属头衔、退群、处理加群申请、
取群列表、取历史消息…），用 `ctx.onebot.*` 直通 OneBot 11 的标准 action：

```python
ctx.onebot.set_group_admin(group_id=gid, user_id=uid, enable=True)
ctx.onebot.set_group_name(group_id=gid, group_name='新群名')
ctx.onebot.set_group_leave(group_id=gid, is_dismiss=False)
ctx.onebot.set_group_add_request(flag=flag, sub_type=sub, approve=True)
ctx.onebot.get_group_list()
ctx.onebot.get_msg(message_id=msg_id)
```

`ctx.onebot` 上没有显式定义的动作名也会被动态转发，所以 OneBot 11 协议里
有、这里没列的 action 一样能调。

---

## 在命令匹配前截获原始消息

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

## 能力边界

写插件时最容易踩的坑是「在 OneBot 上测通了，换 Telegram 就不工作」。当前承诺的范围：

| 能力 | 跨协议 | 说明 |
| --- | --- | --- |
| 收文本 / 发文本 | ✅ 全部接入端 | `send_msg` / `ev.text`，接口不变 |
| 读入站富媒体 | ✅ 全部接入端 | `ev.segments` 与各类属性统一；认不出的段以原名保留，不丢事件 |
| 媒体落盘 | ✅ 全部接入端 | `ctx.download_media` 吃 url / base64 / 本地路径；`file_id` 需接入端支持 |
| 发图片 / 语音 / 视频 / 文件 | ⚠️ 多数端支持，细节不同 | 见 [能力矩阵](#能力矩阵谁能发什么) |
| 发贴纸 | ⚠️ 仅 Telegram | 其它端会当未知段 |
| 群管（禁言 / 踢人 / 全员禁言 / 成员列表 / 改名片） | ⚠️ 仅 OneBot 系（QQ 官方部分） | 底层是 OneBot action |
| 撤回 | ⚠️ 仅 OneBot 系 / KOOK | `ctx.recall` |
| 任意平台扩展 action | ⚠️ 各自接入端 | `ctx.onebot.*` 只对 OneBot 生效 |

原则是：**入站尽量归一，出站按矩阵来**。富媒体的发送语法天然是平台私有的，
框架不假装统一——真要写「一次覆盖所有平台」的插件，请在发送前按
`ev.adapter`（消息来源接入端名）分支处理差异。

---

## 安全须知

- **公网部署**：`web.host` 保持 `127.0.0.1` + 反代；`access_token` 设强随机值；按需 IP 白名单
- 启动时若提示「监听 0.0.0.0 且 token 为空」，请立刻处理
- 多账号时注意权限边界，别把超管能力随便开给所有 bot
- 落盘的媒体来自不可信来源：**不要**把 `download_media` 的目录直接当静态资源目录对外暴露，
  文件名已净化但内容未扫描

## 下一步

- [编写插件](./编写插件.md) —— 业务怎么写
- [接入端契约](./接入端契约.md) —— 自己实现一个接入端
- [开始使用](./快速开始.md) —— 不接 IM 的两条路线
- [配置系统](./配置系统.md) —— 各接入端完整字段
- [架构 · 事件总线](../进阶/架构总览.md) —— 事件在框架里怎么流
