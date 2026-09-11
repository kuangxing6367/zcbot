# ZCBOT

> **微内核式事件驱动服务宿主**：内核只做最小必要的事——加载扩展、路由事件、提供公共服务；
> 接入平台、Web 后台、会话、定时任务、权限、数据库……全都是**挂载在内核扩展点上的扩展**。
> 你只写业务，骨架（含扩展点契约）由内核提供。
>
> **OneBot 11 是它的一个默认扩展，但不是它的身份。** 换一个 `ProtocolAdapter`，它可以是 Telegram / Discord 机器人、
> HTTP Webhook 接收器、纯定时任务服务，或任何"事件 → 扩展 → 响应"的程序。

**当前正式版：v1.3.8** ｜ 版本演进见 [CHANGELOG.md](CHANGELOG.md)

- 项目地址：https://github.com/kuangxing6367/zcbot
- 官方插件仓库：https://github.com/kuangxing6367/zcbot_plugins
- 反馈交流：群组 **1060129201**

---

## 一、它是什么，不是什么

### 一句话定位

ZCBOT 是一个**微内核式的通用服务宿主**：极小内核（加载、路由、公共服务、扩展点契约）+ 层层叠加的扩展。
所有具体功能（接入平台、开网页后台、管会话、跑定时、做权限）都是可插拔的扩展，通过内核的**扩展点（hook）**
挂到几乎每一个运行环节。

### 它适合谁（三类目标用户）

| 你是…… | 你能用 ZCBOT 做什么 | 从哪里开始 |
| ------ | ------------------- | ---------- |
| **① 想开箱搭一个 托管机器人的使用者**（不一定会编程） | 默认扩展就是 OneBot，启动 + 连一个 NapCat/Lagrange 就能用；后台点点鼠标装插件、改配置、管权限 | [快速开始](#五快速开始约-5-分钟) |
| **② 写业务功能的 Python 开发者** | 白拿依赖注入、权限引擎、双方言数据库、多轮会话、定时任务、Web 扩展、**扩展点切面**，只专注写 `register(ctx)` 里的业务 | [编写插件](docs/guide/writing-plugins.md) |
| **③ 需要"事件→扩展→响应"通用宿主的开发者** | 接非 IM 事件源：HTTP Webhook（内置 `http_inject`）、纯定时（`scheduler`）、或自写 `ProtocolAdapter` 接 Telegram/Discord/MQTT 等 | [协议适配器](docs/api/advanced/protocol_adapter.md)、[扩展点](docs/api/advanced/hooks.md) |

### 它**不**是什么（非目标，避免选错工具）

- **不是** NapCat / Lagrange / go-cqhttp 这类协议端——它**不直接入平台**，需要 OneBot 实现端以"反向 WebSocket"连入。
- **不是**分布式/多节点中台：它是单进程（可选 core/host 双进程）宿主，不内置集群、消息队列编排。
- **不提供**跨语言 SDK：业务扩展用 Python 编写；跨语言交互请走它暴露的 HTTP API / Webhook。
- 内核不绑定任何 IM：接入平台 只是因为官方默认带了一个 `onebot_adapter` 扩展，把它关掉就是个通用宿主。

### 核心理念：微内核 = 最小核心 + 扩展点 + 扩展

- **最小核心（`framework/`）**：插件加载器、事件总线、消息路由、服务注册表（DI）、**扩展点注册表（HookRegistry）**、
  权限引擎、数据库抽象、运行时上下文。**不实现任何具体业务，也不含任何 OneBot 代码**。
- **官方扩展（`core_plugins/`）**：随项目提供的基础能力，开关与配置集中在根目录 **`core_plugins.yaml`**，按需加载。
- **用户扩展（`plugins/`）**：你自己的业务逻辑，每个一个文件夹。
- **扩展点（hook）**：内核在启动/关闭、Web 请求、事件分发、命令执行、协议动作、出站文本等环节预留的插槽；
  `ctx.hook(point, handler)` 即可往里插逻辑，是"微内核"真正区别于普通框架的地方。详见 [扩展点](#四扩展点extension-points)。

> 提示：想要**纯 托管机器人**？什么都不用关，开箱即用。想要别的形态？在 `core_plugins.yaml` 里切换接入端、换一套业务插件即可，权限、后台、持久化、会话这些骨架原样保留。

---

## 二、内核与扩展层

ZCBOT 的能力分两层：**内核只负责"运转"**，其余都是"挂在运转各环节上的扩展"。

| 层 | 内容 | 属于 |
| ---- | ---- | ---- |
| **内核（framework/）** | 加载器 · 事件总线 · 消息路由 · 服务注册表(DI) · **扩展点注册表** · ctx · 权限引擎 · 数据库抽象 · 运行时上下文 | 微内核（不可关） |
| **公共服务扩展（core_plugins/，可开关）** | Web 后台 / 接入端 / 会话 / 定时 / HTTP 注入 / 独立 API | 官方扩展 |
| **业务扩展（plugins/）** | 你的命令 / 定时任务 / 仪表盘 / WebUI / 切面 | 用户扩展 |

五类公共能力由内核统一提供，扩展按需取用：

| 能力 | 由谁提供（内核/扩展） |
| ---- | ---- |
| **持久化层** | 内核：SQLite / MySQL 双方言自动翻译、自动建表、schema 迁移、连接池、同步/异步双接口 |
| **运行时层** | 内核：插件加载器、事件总线、消息路由（优先级管线）、服务注册表、依赖自愈、内存看门狗、孤儿任务清理、可靠热重载 |
| **鉴权层** | 内核：LuckPerms 风格权限引擎（三态 + 组继承 + 上下文 + 时效 + 轨道 + 审计）、双令牌体系（会话 token + API Key） |
| **接入层** | 扩展：协议无关的 `ProtocolAdapter` 抽象 + 服务注册表；官方实现 `onebot_adapter`（OneBot 反向 WS）、`http_inject`（HTTP 事件注入）、`http_api`（对外 HTTP API）、终端模拟注入 |
| **表现层** | 扩展：WebUI（可被插件整体接管、官方侧边栏可开关）、插件 WebUI（可注册独立侧边栏入口 `ctx.webui(..., sidebar=True)`）、仪表盘卡片、群组/用户页扩展、CLI 终端、以及**扩展点切面**（审计/限流/中间件等） |

---

## 三、架构总览

```
┌──────────────────────────────────────────────────────┐
│              framework/ 微内核（零 OneBot 代码）        │
│  加载器 · 事件总线 · 消息路由 · ctx · 服务注册表(DI)     │
│  扩展点注册表(HookRegistry) · 权限引擎(perm) · 数据库(db) │
│                       │ 扩展点 / 服务注册 / 事件总线      │
└───────────────────────┬──────────────────────────────┘
                        │ 启动时按 core_plugins.yaml 加载扩展
        ┌───────────────┼───────────────────────┐
        ▼               ▼                        ▼
┌────────────────┐ ┌────────────────┐      ┌────────────────┐
│ onebot_adapter │ │ webui          │      │ scheduler/session│  core_plugins/（可开关的扩展）
│ 默认接入端(可关)│ │ http_inject    │      │ http_api(默认关) │
└────────────────┘ └────────────────┘      └────────────────┘
                        │ 加载
                        ▼
                ┌────────────────┐
                │ plugins/ 用户扩展│  ← 你的业务 + 扩展点切面
                └────────────────┘
```

> 上图以默认接入端 `onebot_adapter` 为例；`http_inject`、自写 `ProtocolAdapter` 都在同一位置把事件归一化后送入内核，
> 后续流程完全一致——**内核不区分事件来自哪个接入端**，也不区分逻辑来自哪个扩展。

---

## 四、扩展点（Extension Points）

> 这是微内核对外最核心的契约，也是"允许几乎各个地方插入"的实现方式。完整文档见 [扩展点（Hook 系统）](docs/api/advanced/hooks.md)。

内核在运行流程上预留了一组**扩展点（hook point）**。扩展用 `ctx.hook(point, handler)` 往插槽里插函数，
内核跑到那个环节就按优先级依次调用。`handler` 可以是普通函数或 `async def`；同名（同扩展内）重复注册自动去重。

| 扩展点 | 触发时机 | 能否短路 |
| ------ | -------- | -------- |
| `lifecycle.startup` / `lifecycle.shutdown` | 进程启动 / 关闭 | 否 |
| `http.before_request` / `http.after_request` | 每个 Web 请求前后 | before 可返回 Response 短路 |
| `event.before_dispatch` / `event.after_dispatch` | 事件进入内核前后 | before 返回 `False` 丢弃事件 |
| `command.before` / `command.after` | 命令执行前后 | before 返回 `False` 跳过该命令 |
| `message.before_send` / `message.after_send` | 框架主动发文本前后 | before 返回 `False` 取消发送 |
| `action.before` / `action.after` | 任意协议动作调用前后（send_msg / 禁言 / 查询…） | 否（通知/审计） |

最小示例：审计每一次出站动作。

```python
def register(ctx):
    ctx.hook('action.after', audit)

def audit(action, params, bot, result):
    ctx.log(f"[审计] {action} -> {result.get('status')}")
```

进阶示例：在 Web 响应上统一加头、做命令耗时切面、启动预热缓存、过滤黑名单来源事件——
全部见 [扩展点（Hook 系统）](docs/api/advanced/hooks.md)。

---

## 五、快速开始（约 5 分钟）

> 以默认接入端（OneBot）为例。全程命令很少，复制粘贴即可。不懂的词点旁边的 **「什么是 XX」** 展开。

### 第 1 步：准备环境

- **Python 3.10+**（验证环境覆盖 3.10–3.14），Windows / Linux / macOS 均可。
- 要接入聊天平台的话，再准备一个 OneBot 实现端（[NapCat](https://github.com/NapNeko/NapCatQQ)、Lagrange、go-cqhttp 任选）。**只用定时/Webhook 则不需要它。**

<details>
<summary><b>什么是 OneBot 实现端？什么是反向 WebSocket？</b>（点我展开）</summary>

聊天平台官方通常不提供"把账号变成机器人"的接口，社区用 **OneBot 实现端**接管一个账号，再把消息转发给 ZCBOT。

- **WebSocket（WS）**：一条常驻连接，消息即时推送，类似"打电话"而非"发短信"。
- **反向连接**：ZCBOT 当**服务端**（在 `6830` 端口等），OneBot 端当客户端主动连进来，所以叫"反向"。你只需在 OneBot 端填对地址。

</details>

### 第 2 步：下载代码 + 装依赖

```bash
git clone https://github.com/kuangxing6367/zcbot.git
cd zcbot
pip install -r requirements.txt
```

> 提示：依赖缺了不用慌：启动时会自检 `requirements.txt`，缺失依赖走内置镜像源自动补装；插件自己的依赖在加载时也会自动安装。

### 第 3 步：启动

```bash
python main.py                 # 也可指定配置：python main.py D:\config\zcbot.yaml
```

看到这行就说明跑起来了

```
框架启动完成，等待消息...
```

首次启动会自动生成全局配置 `config.yaml`、官方扩展配置中心 `core_plugins.yaml` 与 `data/` 目录。

<details>
<summary><b>config.yaml 和 core_plugins.yaml 怎么分工？</b>（点我展开）</summary>

- **`config.yaml`**：框架全局设置——数据库、日志、安全、插件目录等。
- **`core_plugins.yaml`**：**官方扩展的开关与配置中心**。启动时自动扫描 `core_plugins/`：新装扩展补块、卸载扩展删块、自动回写，并合并进主配置（所以代码里 `fw.config.get('onebot')` 这类读法不变）。

常用默认端口/账号：

| 配置（编辑 `core_plugins.yaml`） | 默认值 | 作用 |
| ------ | ------ | ---- |
| `onebot_adapter.listen_port` | `6830` | OneBot 端**反向 WS** 连入端口（仅接入平台 需要） |
| `onebot_adapter.access_token` | 空 | 接入令牌，**公网必须设强随机值** |
| `webui.host` / `webui.port` | `127.0.0.1` / `8080` | Web 后台地址端口 |
| `http_inject`（默认关） | `127.0.0.1:8901/hook` | HTTP 事件注入 |
| `http_api`（默认关） | `127.0.0.1:1145` | 独立对外 HTTP API |
| `config.yaml → database.type` | `sqlite`（`data/zcbot.db`） | `sqlite` 零配置或 `mysql` |

Web 后台默认登录账号 `admin` / `admin123`（**首次登录后立即改密**）。

</details>

### 第 4 步：接入平台（仅默认接入端需要）

在 OneBot 实现端新增「反向 WebSocket 客户端」：

| 设置项 | 填什么 |
| ------ | ------ |
| 目标地址 | `ws://127.0.0.1:6830` |
| AccessToken | 与 `core_plugins.yaml → onebot_adapter.access_token` 一致（本地调试可留空） |

日志出现 `[bot_xxxxxx] OneBot 客户端已连接` 即成功。多账号相互独立，回复时自动选用消息来源账号，插件无需手动指定 bot。

### 第 5 步：打开 Web 后台

浏览器访问 `http://127.0.0.1:8080`，用 `admin / admin123` 登录。可启停/重载扩展、管理命令与定时任务、管理用户/群组权限、看仪表盘与日志、在线编辑扩展配置。

> 注意：公网部署：`webui.host` 保持 `127.0.0.1` 并经反向代理暴露，设好 `access_token`、改默认密码、按需配置 IP 白名单。详见[部署文档](docs/advanced/deployment.md)。

---

## 六、验证与常见玩法

### 1. 让它回话

向机器人发送 `/echo 你好`，原样返回即链路正常；`/help`（或 `/帮助`、`/菜单`）查看命令。

### 2. 接大模型聊天

装上官方插件仓库的 `llm_chat`，填好模型地址与密钥，@机器人或发 `/chat` 即可，支持长期记忆与人设。

### 3. 让 AI 帮你写插件

官方插件 `llm_plugin_gen` 支持"用自然语言描述需求 → 自动生成并加载插件"。它在插件仓库独立维护，见
[插件文档](https://github.com/kuangxing6367/zcbot_plugins/blob/main/plugins/llm_plugin_gen/docs/INDEX.md)。

### 4. 不接 IM：用 HTTP 注入事件（内置 `http_inject`）

在 `core_plugins.yaml` 把 `http_inject.enabled` 改为 `true` 并重启，外部系统 POST 一条 JSON 即可注入事件，业务扩展照常处理——接 GitHub/支付回调、搭内部工具都不需要 IM：

```bash
curl -X POST http://127.0.0.1:8901/hook \
     -H "Content-Type: application/json" \
     -d '{"user_id": 10001, "message": "/echo 来自 HTTP"}'
```

### 5. 纯定时服务（连接入端都不要）

在 `core_plugins.yaml` 关掉 `onebot_adapter`、保留 `scheduler`，用 `ctx.task()` 注册定时任务，就变成一个纯定时宿主。要接 Telegram/Discord/MQTT 等，写一个 `ProtocolAdapter` 即可（见[协议适配器](docs/api/advanced/protocol_adapter.md)），权限/后台/持久化能力不变。

---

## 七、写一个扩展（预览）

手把手长教程见 **[编写插件](docs/guide/writing-plugins.md)**。最小扩展只需在 `plugins/hello/main.py` 写：

```python
__plugin_meta__ = {"name": "Hello", "version": "1.0.0",
                   "author": "你", "desc": "打招呼", "priority": 50}

def register(ctx):
    ctx.command("/hello", handle_hello, description="打个招呼")

async def handle_hello(event, match):
    await ctx.asend_msg(user_id=event.user_id,
                        group_id=event.group_id if event.is_group else None,
                        message="Hello, World!")
```

放进 `plugins/` 后在后台点「重载」即可。权限控制支持声明式与手动判断（三态：授予 `True` / 显式否决 `False` / 未定义 `None`）：

```python
ctx.command("/ban", handle_ban, require_perm="myplugin.ban")   # 声明式，框架自动拦截
# 或 handler 内：event.has_perm("myplugin.ban") / event.check_perm(...)
```

想"插到几乎每个地方"？用 `ctx.hook(...)`（见[扩展点](#四扩展点extension-points)）。

---

## 八、扩展从哪来

### 随项目内置的用户扩展（plugins/）

| 扩展 | 作用 |
| ---- | ---- |
| **echo** | `/echo 内容` 原样返回，链路自测 |
| **help** | `/help` 生成图片帮助菜单 |
| **image_renderer** | 通用图片渲染引擎（卡片、文字图，Rust 原生加速、缺失回退 PIL） |
| **runtime_status** | `/status` `/info` 运行状态（含图片状态卡） |
| **message_guard** | 消息防护：唤醒词/白名单/限流/敏感词 |
| **plugin_depgraph** | 插件依赖关系扫描（`/依赖`、`/依赖图`） |
| **session_waiter** | 多轮会话基础设施示例 |
| **ui_ext_demo** | 群组/用户管理页扩展演示 |

### 官方扩展仓库（20+ 现成扩展）

把 `config.yaml → plugin.dir` 指向 [zcbot_plugins](https://github.com/kuangxing6367/zcbot_plugins) 仓库，或在后台插件市场安装。常用：

| 扩展 | 作用 | 扩展 | 作用 |
| ---- | ---- | ---- | ---- |
| `llm_chat` | 大模型对话 | `qqadmin` | 群管理全套（禁言/踢人/撤回/审批/违禁词） |
| `llm_plugin_gen` | AI 写插件 | `fun_score` | 签到积分/排行榜 |
| `video_parse` | 视频链接解析卡片 | `send_like` | 点赞/自动点赞 |
| `broadcast` | 消息批量广播 | `file` | 服务器文件管理 |
| `custom_ui` | 接管/换肤后台 | `hitokoto` | 随机一言 |
| `plugin_memmon` | 插件内存监控 | `llm_blacklist` | LLM 对话黑名单 |

> 完整命令与用法见插件仓库说明。

---

## 九、权限系统（LuckPerms 风格）

在原有「单一 `role` 身份轴」之外，平行提供一套对齐 Minecraft LuckPerms v5 的**权限节点**模型，两者并存、互不冲突：老命令可继续用 `require_level`，新功能推荐 `require_perm`。

- **节点 node**：`plugin.action.sub` 形式，三态（授予 / 显式否决 / 未定义）。
- **组 group**：节点集合，带 `weight`；组之间用 `group.xxx` 节点继承。
- **上下文 context**：`group=<群号>` / `bot=<接入端实例名>` / `msgtype=group|private`，NULL 表示全局。
- **临时 / 否决 / 通配**：节点可带 `expire_at` 过期；`value=0` 否决优先；`a.b.*`、`*` 通配。

内置身份映射为一条虚拟继承链（运行时注入、不入库）：

```
__member(w0) ← __admin(w20) ← __owner(w30) ← __super(w100)
# require_level='admin'  ≡  检查节点 zcbot.role.admin
```

后台「权限管理」页（`/permissions`）含权限组 / 用户 / 轨道 / 校验器 / 审计 5 个标签页，所有变更写入 `perm_audit`。节点命名建议：业务用 `插件名.动作.子项`，内置身份用 `zcbot.role.*`。完整机制见[权限系统文档](docs/advanced/permission.md)。

---

## 十、接口令牌（API Key）与两类 HTTP 接口

ZCBOT 有**两类** HTTP 接口，别混淆端口：

| 接口 | 由谁提供 | 默认地址 | 认证 | 用途 |
| ---- | -------- | -------- | ---- | ---- |
| **后台 REST 接口**（`/api/**`，含权限/扩展/配置管理） | 官方扩展 `webui` | `127.0.0.1:8080` | 登录会话 token 或 API Key | 给管理后台与受信脚本用 |
| **独立对外 API**（`/send`、`/db/query` 等） | 官方扩展 `http_api`（**默认关闭**） | `127.0.0.1:1145` | 单一共享 token（`?token=`） | 给外部系统集成用 |

原先只有随登录轮换、会过期的会话 token；新增的 **API Key** 独立有效、不随登录轮换，专供脚本长期调用：

- `secrets.token_hex(32)`（64 字符），存 `api_tokens` 表，可设绝对过期或永不过期，可即时吊销（软删除）；
- 创建后**仅明文返回一次**；仅 `super` 角色可创建/吊销；
- 调用时带请求头 `Authorization: Bearer <token>`，`_verify_token` 同时兼容会话 token 与 API Key，**既有接口无需改动**即可用 API Key 调。

```bash
# 后台 REST 接口（webui，默认 8080）
curl -H "Authorization: Bearer <你的API_KEY>" \
     http://127.0.0.1:8080/api/perm/groups
```

后台在「接口令牌」页（`/apikeys`）创建/吊销；端点：`GET /api/apikeys`、`POST /api/apikeys`、`POST /api/apikeys/<id>/revoke`。

> 注意：独立 `http_api` 扩展若开启 `allow_db: true`，其 `db/query`、`db/execute` 可执行**任意 SQL（含写库/删表）**，无表级粒度：请固定 token、只绑内网 host、尽量用只读账号或反代限权。

---

## 十一、项目结构与开发者说明

### 11.1 目录结构

```
.
├── main.py                 # 启动入口：python main.py [自定义配置路径]
├── config.yaml             # 全局配置（首次启动生成）
├── core_plugins.yaml       # 官方扩展配置中心（启动自动扫描 core_plugins/ 同步、回写、合并）
├── requirements.txt
├── framework/              # 微内核（不含任何 OneBot 实现）
│   ├── core.py             # 内核主体：加载/生命周期/双进程分派/中立回复
│   ├── config.py           # 配置加载 + core_plugins.yaml 配置中心
│   ├── hooks.py            # 扩展点注册表（HookRegistry）—— 微内核契约
│   ├── db.py               # 数据库抽象（SQLite/MySQL 双方言、连接池）
│   ├── event.py            # Event 事件对象（富媒体/传播控制/权限查询）
│   ├── ctx.py              # PluginContext（扩展可用能力，含 ctx.hook 扩展点注册）
│   ├── router.py           # 命令路由（require_perm / require_level）
│   ├── loader.py           # 用户扩展加载器（合成包/相对导入/可靠热重载）
│   ├── protocol.py         # ProtocolAdapter 抽象基类 + ActionProxy + 服务注册表
│   ├── runtime.py          # 中立运行时上下文（current_source_var，兼容 current_bot_var）
│   ├── perm.py             # LuckPerms 风格权限引擎（无第三方依赖）
│   ├── api/                # 后台 REST 功能域分包（可插入路由注册表）
│   └── ipc/                # core/host 双进程 JSON-RPC（dual_process 门控）
├── core_plugins/           # 官方扩展（可在 core_plugins.yaml 开关）
│   ├── onebot_adapter/     #   OneBot 11 接入端（反向 WS + onebot_api.py 动作封装）
│   ├── http_inject/        #   HTTP 事件注入接入端（默认关）
│   ├── http_api/           #   独立对外 HTTP API（默认关）
│   ├── webui/              #   Web 管理后台
│   ├── session/            #   多轮会话
│   └── scheduler/          #   定时任务
├── plugins/                # 用户扩展（每个一个目录，含 main.py）
├── webui/                  # 后台前端源码（Vue 3 + Vite + Element Plus）
├── web/                    # 前端构建产物（由 webui/ 构建）
├── sql/                    # 建表 SQL（init.sql / init_mysql55.sql）
├── data/                   # 运行数据：zcbot.db、logs/、plugins_dat/（扩展私有数据）
└── docs/                   # 开发文档（见下方导航）
```

### 11.2 权限开发接口速览

- **Event 上**（handler 内最常用）：`ev.has_perm(node)`、`ev.check_perm(node)`（三态）、`ev.perms`（快照）、`ev.perm_groups`、`ev.primary_group`，上下文自动从事件构造。
- **ctx 上**（非事件场景，如定时任务）：`ctx.has_perm(uid, node, context=..., role=...)`、`ctx.check_perm(...)`、`ctx.user_groups(...)`。
- **命令级**：`@ctx.command(..., require_perm="x.y", require_level="admin")`，两者任一满足即放行；解析顺序：用户直接节点 → 所属组（weight 降序）→ 精确度（精确 > 段通配 > `*`）→ 同精度否决优先。
- **缓存**：权限解析 60s TTL、组快照 10s，Web 端改动后自动失效。底层能力都在 `framework/perm.py`。

### 11.3 前端构建

后台前端源码在 `webui/`，产物输出到 `web/`：

```bash
cd webui
npm install
npm run build      # 产物输出到 ../web/
```

数据库表结构见 `sql/init.sql`（SQLite）与 `sql/init_mysql55.sql`（MySQL 5.5 兼容），启动时自动建表补缺。

---

## 十二、文档导航

**入门（使用者）**

- [安装](docs/guide/installation.md) · [开始使用](docs/guide/getting-started.md) · [配置系统](docs/guide/configuration.md)

**扩展开发（Python 开发者）**

- [编写插件（手把手）](docs/guide/writing-plugins.md) · [多轮会话](docs/guide/session.md)
- [PluginContext (ctx) 参考](docs/api/basic/ctx.md) · [Event 事件对象](docs/api/basic/event.md)
- [服务注册表（DI）](docs/api/basic/services.md) · [Framework 核心](docs/api/basic/framework.md)

**当通用宿主用 / 接入其它源（高级开发者）**

- [扩展点（Hook 系统）](docs/api/advanced/hooks.md) · [协议适配器（写自己的接入端）](docs/api/advanced/protocol_adapter.md)
- [官方最佳实践](docs/guide/best-practices.md)
- [架构详解](docs/advanced/architecture.md) · [插件加载与模块机制](docs/advanced/loader.md)
- [数据库](docs/advanced/database.md) · [定时任务](docs/advanced/scheduler.md)
- [权限系统](docs/advanced/permission.md) · [部署上线](docs/advanced/deployment.md)

完整总目录见 [docs/guide/README.md](docs/guide/README.md)；版本历史见 [CHANGELOG.md](CHANGELOG.md)。

---

## 十三、双核心实验版（core/host 双进程，实验特性）

> 这是一条**长期实验线**：把一次启动拆成两个进程，隔离用户扩展故障、降低核心内存占用。
> 默认关闭，行为完全不变；开启前请先读 [双核心开发文档](docs/advanced/dual-core.md)。

**概念**：单进程宿主拆成「核心进程」+「宿主进程」，中间用标准库 IPC（回环 TCP + authkey）通信，零第三方依赖。

- **核心进程（Core）**：真实数据库、协议接入端、Web/WebUI、IPC 服务端，并监督宿主存活。
- **宿主进程（Host）**：加载执行全部用户扩展，经 IPC 远程代理访问数据库/发消息/注册路由/推送日志。

**开启方式**：在 `config.yaml` 末段设置 `dual_process.enabled: true`，重启即可。

```yaml
dual_process:
  enabled: true        # 开启双进程
  max_restarts: 5      # 宿主崩溃重启限流（窗口内最大次数）
  restart_interval: 30 # 限流窗口（秒）
```

**适用场景**：扩展较多/不稳定、希望接入端与 Web 在扩展崩溃时仍在线、或想压低核心常驻内存。

**已知限制**：`db.get_connection()` 在双进程下不可用（改用 `db.transaction()` 或 `ctx` 的 db 系列）；目前为单宿主，多接入端并发、跨机部署尚未实现。详见 [双核心开发文档](docs/advanced/dual-core.md)。

---

## 开源协议

MIT + Apache 2.0 双协议，任选其一适用。

> 本项目以 AI 生成为主、人工辅助完成。用着顺手的话，给个 Star 吧！
