# ZCBOT

> **事件驱动的插件化服务宿主**：把"事件进来 → 插件处理 → 给出响应"这类应用的骨架（插件加载、事件总线、权限、数据库、Web 后台、定时任务）全部做好，你只写业务。
>
> **OneBot 11（QQ）是它的默认接入端，但不是它的身份。** 换一个 `ProtocolAdapter`，它可以是 Telegram / Discord 机器人、HTTP Webhook 接收器、纯定时任务服务，或任何"事件 → 插件 → 响应"的程序。

**当前正式版：v1.3.5** ｜ 版本演进见 [CHANGELOG.md](CHANGELOG.md)

- 项目地址：https://github.com/kuangxing6367/zcbot
- 官方插件仓库：https://github.com/kuangxing6367/zcbot_plugins
- 反馈交流：QQ 群 **1060129201**

---

## 一、它是什么，不是什么

### 一句话定位

ZCBOT 是一个**通用的插件化服务宿主**：内核只负责"加载插件、路由事件、提供公共能力"，
所有具体功能（连 QQ、开网页后台、管会话、跑定时）都是可插拔的官方插件。

### 它适合谁（三类目标用户）

| 你是…… | 你能用 ZCBOT 做什么 | 从哪里开始 |
| ------ | ------------------- | ---------- |
| **① 想开箱搭一个 QQ 机器人的使用者**（不一定会编程） | 默认接入端就是 OneBot，启动 + 连一个 NapCat/Lagrange 就能用；后台点点鼠标装插件、改配置、管权限 | [快速开始](#四快速开始约-5-分钟) |
| **② 写业务功能的 Python 开发者** | 白拿依赖注入、权限引擎、双方言数据库、多轮会话、定时任务、Web 扩展，只专注写 `register(ctx)` 里的业务 | [编写插件](docs/guide/writing-plugins.md) |
| **③ 需要"事件→插件→响应"通用宿主的开发者** | 接非 IM 事件源：HTTP Webhook（内置 `http_inject`）、纯定时（`scheduler`）、或自写 `ProtocolAdapter` 接 Telegram/Discord/MQTT 等 | [协议适配器](docs/api/protocol_adapter.md)、[最佳实践](docs/guide/best-practices.md) |

### 它**不**是什么（非目标，避免选错工具）

- **不是** NapCat / Lagrange / go-cqhttp 这类协议端——它**不直连 QQ**，需要 OneBot 实现端以"反向 WebSocket"连入。
- **不是**分布式/多节点中台：它是单进程（可选 core/host 双进程）宿主，不内置集群、消息队列编排。
- **不提供**跨语言 SDK：业务插件用 Python 编写；跨语言交互请走它暴露的 HTTP API / Webhook。
- 内核不绑定任何 IM：连 QQ 只是因为官方默认带了 `onebot_adapter`，把它关掉就是个通用宿主。

### 核心理念：框架 = 极简壳 + 官方插件集 + 用户插件

- **核心壳（framework/）**：插件加载器、事件总线、消息路由、服务注册表（DI）、权限引擎、数据库抽象——**不实现任何具体业务，也不含任何 OneBot 代码**。
- **官方插件集（core_plugins/）**：随项目提供的基础能力，开关与配置集中在根目录 **`core_plugins.yaml`**，按需加载。
- **用户插件（plugins/）**：你自己的业务逻辑，每个一个文件夹。

> 提示：想要**纯 QQ 机器人**？什么都不用关，开箱即用。想要别的形态？在 `core_plugins.yaml` 里切换接入端、换一套业务插件即可，权限、后台、持久化、会话这些骨架原样保留。

---

## 二、五层能力

| 层 | 内容 |
| ---- | ---- |
| **持久化层** | SQLite / MySQL 双方言自动翻译、自动建表、schema 迁移、连接池、同步/异步双接口 |
| **运行时层** | 插件加载器、事件总线、消息路由（优先级管线）、服务注册表（DI）、依赖自愈、内存看门狗、孤儿任务清理、可靠热重载 |
| **鉴权层** | LuckPerms 风格权限引擎（三态 + 组继承 + 上下文 + 时效 + 轨道 + 审计）、双令牌体系（会话 token + API Key） |
| **接入层** | 协议无关的 `ProtocolAdapter` 抽象基类 + 服务注册表；官方实现：`onebot_adapter`（OneBot 反向 WS）、`http_inject`（HTTP 事件注入）、`http_api`（对外 HTTP API）、终端模拟注入 |
| **表现层** | WebUI（可被插件整体接管）、插件 WebUI、仪表盘卡片、群组/用户页扩展、CLI 终端 |

---

## 三、架构总览

```
┌──────────────────────────────────────────────────────┐
│              framework/ 极简内核（零 OneBot 代码）      │
│  插件加载器 · 事件总线 · 消息路由 · ctx · 服务注册表(DI) │
│  权限引擎(perm) · 数据库抽象(db) · 运行时上下文(runtime)│
└───────────────────────────┬──────────────────────────┘
                            │ 启动时按 core_plugins.yaml 加载
        ┌───────────────────┼───────────────────────┐
        ▼                   ▼                        ▼
┌────────────────┐ ┌────────────────┐      ┌────────────────┐
│ onebot_adapter │ │ webui          │      │ scheduler/session│
│ 默认接入端(可关)│ │ http_inject    │      │ http_api(默认关) │
└────────────────┘ └────────────────┘      └────────────────┘
                            │ 加载
                            ▼
                    ┌────────────────┐
                    │ plugins/ 用户插件│
                    └────────────────┘
```

官方插件清单（`core_plugins/`，开关见 `core_plugins.yaml`）：

| 官方插件 | 默认 | 职责 |
| -------- | ---- | ---- |
| `onebot_adapter` | 开 | OneBot 11 反向 WebSocket 接入端（连 QQ 用），含 38 个 OneBot 动作封装 |
| `webui` | 开 | Web 管理后台 + 后台 REST 接口（默认 `127.0.0.1:8080`） |
| `session` | 开 | 多轮会话（`ctx.wait_for` / `create_session`） |
| `scheduler` | 开 | 定时任务（cron/interval/date，基于 APScheduler） |
| `http_inject` | **关** | HTTP 事件注入接入端，外部 POST JSON 即可注入事件（默认 `127.0.0.1:8901/hook`） |
| `http_api` | **关** | 独立对外 HTTP API（默认 `127.0.0.1:1145`，共享 token，`db/*` 默认不开） |

---

## 四、快速开始（约 5 分钟）

> 以默认接入端（QQ / OneBot）为例。全程命令很少，复制粘贴即可。不懂的词点旁边的 **「什么是 XX」** 展开。

### 第 1 步：准备环境

- **Python 3.10+**（验证环境覆盖 3.10–3.14），Windows / Linux / macOS 均可。
- 要连 QQ 的话，再准备一个 OneBot 实现端（[NapCat](https://github.com/NapNeko/NapCatQQ)、Lagrange、go-cqhttp 任选）。**只用定时/Webhook 则不需要它。**

<details>
<summary><b>什么是 OneBot 实现端？什么是反向 WebSocket？</b>（点我展开）</summary>

QQ 官方不提供"把号变成机器人"的接口，社区用 **OneBot 实现端**接管一个 QQ 号，再把消息转发给 ZCBOT。

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

首次启动会自动生成全局配置 `config.yaml`、官方插件配置中心 `core_plugins.yaml` 与 `data/` 目录。

<details>
<summary><b>config.yaml 和 core_plugins.yaml 怎么分工？</b>（点我展开）</summary>

- **`config.yaml`**：框架全局设置——数据库、日志、安全、插件目录等。
- **`core_plugins.yaml`**：**官方插件的开关与配置中心**。启动时自动扫描 `core_plugins/`：新装插件补块、卸载插件删块、自动回写，并合并进主配置（所以代码里 `fw.config.get('onebot')` 这类读法不变）。

常用默认端口/账号：

| 配置（编辑 `core_plugins.yaml`） | 默认值 | 作用 |
| ------ | ------ | ---- |
| `onebot_adapter.listen_port` | `6830` | OneBot 端**反向 WS** 连入端口（仅连 QQ 需要） |
| `onebot_adapter.access_token` | 空 | 接入令牌，**公网必须设强随机值** |
| `webui.host` / `webui.port` | `127.0.0.1` / `8080` | Web 后台地址端口 |
| `http_inject`（默认关） | `127.0.0.1:8901/hook` | HTTP 事件注入 |
| `http_api`（默认关） | `127.0.0.1:1145` | 独立对外 HTTP API |
| `config.yaml → database.type` | `sqlite`（`data/zcbot.db`） | `sqlite` 零配置或 `mysql` |

Web 后台默认登录账号 `admin` / `admin123`（**首次登录后立即改密**）。

</details>

### 第 4 步：连上 QQ（仅默认接入端需要）

在 OneBot 实现端新增「反向 WebSocket 客户端」：

| 设置项 | 填什么 |
| ------ | ------ |
| 目标地址 | `ws://127.0.0.1:6830` |
| AccessToken | 与 `core_plugins.yaml → onebot_adapter.access_token` 一致（本地调试可留空） |

日志出现 `[bot_xxxxxx] OneBot 客户端已连接` 即成功。多账号相互独立，回复时自动选用消息来源账号，插件无需手动指定 bot。

### 第 5 步：打开 Web 后台

浏览器访问 `http://127.0.0.1:8080`，用 `admin / admin123` 登录。可启停/重载插件、管理命令与定时任务、管理用户/群组权限、看仪表盘与日志、在线编辑插件配置。

> 注意：公网部署：`webui.host` 保持 `127.0.0.1` 并经反向代理暴露，设好 `access_token`、改默认密码、按需配置 IP 白名单。详见[部署文档](docs/advanced/deployment.md)。

---

## 五、验证与常见玩法

### 1. 让它回话

向机器人发送 `/echo 你好`，原样返回即链路正常；`/help`（或 `/帮助`、`/菜单`）查看命令。

### 2. 接大模型聊天

装上官方插件仓库的 `llm_chat`，填好模型地址与密钥，@机器人或发 `/chat` 即可，支持长期记忆与人设。

### 3. 让 AI 帮你写插件

官方插件 `llm_plugin_gen` 支持"用自然语言描述需求 → 自动生成并加载插件"。它在插件仓库独立维护，见
[插件文档](https://github.com/kuangxing6367/zcbot_plugins/blob/main/plugins/llm_plugin_gen/docs/INDEX.md)。

### 4. 不碰 QQ：用 HTTP 注入事件（内置 `http_inject`）

在 `core_plugins.yaml` 把 `http_inject.enabled` 改为 `true` 并重启，外部系统 POST 一条 JSON 即可注入事件，业务插件照常处理——接 GitHub/支付回调、搭内部工具都不需要 IM：

```bash
curl -X POST http://127.0.0.1:8901/hook \
     -H "Content-Type: application/json" \
     -d '{"user_id": 10001, "message": "/echo 来自 HTTP"}'
```

### 5. 纯定时服务（连接入端都不要）

在 `core_plugins.yaml` 关掉 `onebot_adapter`、保留 `scheduler`，用 `ctx.task()` 注册定时任务，就变成一个纯定时宿主。要接 Telegram/Discord/MQTT 等，写一个 `ProtocolAdapter` 即可（见[协议适配器](docs/api/protocol_adapter.md)），权限/后台/持久化能力不变。

---

## 六、写一个插件（预览）

手把手长教程见 **[编写插件](docs/guide/writing-plugins.md)**。最小插件只需在 `plugins/hello/main.py` 写：

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

---

## 七、插件从哪来

### 随项目内置的用户插件（plugins/）

| 插件 | 作用 |
| ---- | ---- |
| **echo** | `/echo 内容` 原样返回，链路自测 |
| **help** | `/help` 生成图片帮助菜单 |
| **image_renderer** | 通用图片渲染引擎（卡片、文字图，Rust 原生加速、缺失回退 PIL） |
| **runtime_status** | `/status` `/info` 运行状态（含图片状态卡） |
| **message_guard** | 消息防护：唤醒词/白名单/限流/敏感词 |
| **plugin_depgraph** | 插件依赖关系扫描（`/依赖`、`/依赖图`） |
| **session_waiter** | 多轮会话基础设施示例 |
| **ui_ext_demo** | 群组/用户管理页扩展演示 |

### 官方插件仓库（20+ 现成插件）

把 `config.yaml → plugin.dir` 指向 [zcbot_plugins](https://github.com/kuangxing6367/zcbot_plugins) 仓库，或在后台插件市场安装。常用：

| 插件 | 作用 | 插件 | 作用 |
| ---- | ---- | ---- | ---- |
| `llm_chat` | 大模型对话 | `qqadmin` | 群管理全套（禁言/踢人/撤回/审批/违禁词） |
| `llm_plugin_gen` | AI 写插件 | `fun_score` | 签到积分/排行榜 |
| `video_parse` | 视频链接解析卡片 | `send_like` | 点赞/自动点赞 |
| `broadcast` | 消息批量广播 | `file` | 服务器文件管理 |
| `custom_ui` | 接管/换肤后台 | `hitokoto` | 随机一言 |
| `plugin_memmon` | 插件内存监控 | `llm_blacklist` | LLM 对话黑名单 |

> 完整命令与用法见插件仓库说明。

---

## 八、权限系统（LuckPerms 风格）

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

## 九、接口令牌（API Key）与两类 HTTP 接口

ZCBOT 有**两类** HTTP 接口，别混淆端口：

| 接口 | 由谁提供 | 默认地址 | 认证 | 用途 |
| ---- | -------- | -------- | ---- | ---- |
| **后台 REST 接口**（`/api/**`，含权限/插件/配置管理） | 官方插件 `webui` | `127.0.0.1:8080` | 登录会话 token 或 API Key | 给管理后台与受信脚本用 |
| **独立对外 API**（`/send`、`/db/query` 等） | 官方插件 `http_api`（**默认关闭**） | `127.0.0.1:1145` | 单一共享 token（`?token=`） | 给外部系统集成用 |

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

> 注意：独立 `http_api` 插件若开启 `allow_db: true`，其 `db/query`、`db/execute` 可执行**任意 SQL（含写库/删表）**，无表级粒度：请固定 token、只绑内网 host、尽量用只读账号或反代限权。

---

## 十、项目结构与开发者说明

### 10.1 目录结构

```
.
├── main.py                 # 启动入口：python main.py [自定义配置路径]
├── config.yaml             # 全局配置（首次启动生成）
├── core_plugins.yaml       # 官方插件配置中心（启动自动扫描 core_plugins/ 同步、回写、合并）
├── requirements.txt
├── framework/              # 极简内核（不含任何 OneBot 实现）
│   ├── core.py             # 框架主体：加载/生命周期/双进程分派/中立回复
│   ├── config.py           # 配置加载 + core_plugins.yaml 配置中心
│   ├── db.py               # 数据库抽象（SQLite/MySQL 双方言、连接池）
│   ├── event.py            # Event 事件对象（富媒体/传播控制/权限查询）
│   ├── ctx.py              # PluginContext（插件可用能力，协议无关）
│   ├── router.py           # 命令路由（require_perm / require_level）
│   ├── loader.py           # 用户插件加载器（合成包/相对导入/可靠热重载）
│   ├── protocol.py         # ProtocolAdapter 抽象基类 + ActionProxy + 服务注册表
│   ├── runtime.py          # 中立运行时上下文（current_source_var，兼容 current_bot_var）
│   ├── perm.py             # LuckPerms 风格权限引擎（无第三方依赖）
│   ├── api/                # 后台 REST 功能域分包
│   └── ipc/                # core/host 双进程 JSON-RPC（dual_process 门控）
├── core_plugins/           # 官方插件（可在 core_plugins.yaml 开关）
│   ├── onebot_adapter/     #   OneBot 11 接入端（反向 WS + onebot_api.py 动作封装）
│   ├── http_inject/        #   HTTP 事件注入接入端（默认关）
│   ├── http_api/           #   独立对外 HTTP API（默认关）
│   ├── webui/              #   Web 管理后台
│   ├── session/            #   多轮会话
│   └── scheduler/          #   定时任务
├── plugins/                # 用户插件（每个一个目录，含 main.py）
├── webui/                  # 后台前端源码（Vue 3 + Vite + Element Plus）
├── web/                    # 前端构建产物（由 webui/ 构建）
├── sql/                    # 建表 SQL（init.sql / init_mysql55.sql）
├── data/                   # 运行数据：zcbot.db、logs/、plugins_dat/（插件私有数据）
└── docs/                   # 开发文档（见下方导航）
```

### 10.2 权限开发接口速览

- **Event 上**（handler 内最常用）：`ev.has_perm(node)`、`ev.check_perm(node)`（三态）、`ev.perms`（快照）、`ev.perm_groups`、`ev.primary_group`，上下文自动从事件构造。
- **ctx 上**（非事件场景，如定时任务）：`ctx.has_perm(uid, node, context=..., role=...)`、`ctx.check_perm(...)`、`ctx.user_groups(...)`。
- **命令级**：`@ctx.command(..., require_perm="x.y", require_level="admin")`，两者任一满足即放行；解析顺序：用户直接节点 → 所属组（weight 降序）→ 精确度（精确 > 段通配 > `*`）→ 同精度否决优先。
- **缓存**：权限解析 60s TTL、组快照 10s，Web 端改动后自动失效。底层能力都在 `framework/perm.py`。

### 10.3 前端构建

后台前端源码在 `webui/`，产物输出到 `web/`：

```bash
cd webui
npm install
npm run build      # 产物输出到 ../web/
```

数据库表结构见 `sql/init.sql`（SQLite）与 `sql/init_mysql55.sql`（MySQL 5.5 兼容），启动时自动建表补缺。

---

## 十一、文档导航

**入门（使用者）**

- [安装](docs/guide/installation.md) · [开始使用](docs/guide/getting-started.md) · [配置系统](docs/guide/configuration.md)

**插件开发（Python 开发者）**

- [编写插件（手把手）](docs/guide/writing-plugins.md) · [多轮会话](docs/guide/session.md)
- [PluginContext (ctx) 参考](docs/api/ctx.md) · [Event 事件对象](docs/api/event.md)
- [服务注册表（DI）](docs/api/services.md) · [Framework 核心](docs/api/framework.md)

**当通用宿主用 / 接入其它源（高级开发者）**

- [官方最佳实践](docs/guide/best-practices.md) · [协议适配器（写自己的接入端）](docs/api/protocol_adapter.md)
- [架构详解](docs/advanced/architecture.md) · [插件加载与模块机制](docs/advanced/loader.md)
- [数据库](docs/advanced/database.md) · [定时任务](docs/advanced/scheduler.md)
- [权限系统](docs/advanced/permission.md) · [部署上线](docs/advanced/deployment.md)

完整总目录见 [docs/guide/README.md](docs/guide/README.md)；版本历史见 [CHANGELOG.md](CHANGELOG.md)。

---

## 开源协议

MIT + Apache 2.0 双协议，任选其一适用。

> 本项目以 AI 生成为主、人工辅助完成。用着顺手的话，给个 Star 吧！
