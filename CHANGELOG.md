# 更新日志（CHANGELOG）

本文件记录 ZCBOT 每一个版本的变化，**按代际（发展阶段）组织、版本内按时间倒序**。
版本事实以 GitHub [Releases](https://github.com/kuangxing6367/zcbot/releases) 与 Git Tag 为准，
日期取 Tag 创建日期（UTC+8）；未发布的在研变化放在最顶部「开发中」一节。

版本号遵循语义化版本：`主版本.次版本.修订号`，`-alpha/-beta` 为预发布，`build.N` 为 Alpha 期的持续构建号。

---

## 代际主线（一句话看懂 ZCBOT 的演进）

| 代际 | 版本区间 | 时间 | 这一代在解决什么 |
|------|----------|------|------------------|
| **第一代 · 诞生** | v0.0.1-alpha（build.4–23） | 2026-08-04 ~ 08-10 | 从 0 搭出「基于 OneBot v11 的事件驱动的 IM 平台」：反向 WS、插件热加载、Web 面板、双方言数据库，并用连续 build 快速夯实稳定性 |
| **第二代 · 公测与界面现代化** | v0.0.1-beta、v0.1.0-beta | 2026-08-11 ~ 08-20 | 公测首发、补齐基础插件；Web 管理后台从原生 HTML/JS 全量重写为 Vue 3 + Element Plus |
| **第三代 · 正式版与健壮性** | v1.0.x、v1.1.x | 2026-08-20 ~ 08-30 | 发布首个正式版，前端可被插件接管；集中修复内存/性能/安全，引入内存看门狗与插件管理修复 |
| **第四代 · 权限与治理** | v1.2.0-beta | 2026-08-30 ~ 09-08 | 引入 节点式权限引擎与接口令牌（API Key），插件孤儿任务自检，框架开始具备"可治理的多用户后台"骨架 |
| **第五代 · 事件驱动 IM 平台** | v1.3.x | 2026-09-09 起 | 官方能力全部下沉为 `core_plugins`，框架回归"极简内核"；补齐终端、相对导入、可靠热重载；**v1.3.5 起框架核心零 OneBot 实现，OneBot 11 退为可插拔的默认接入端** |
| **第六代 · 多协议接入 + 定位改版** | v1.6.x | 2026-09-24 起 | 内置 QQ 官方 / Telegram / Discord / 出站 WS 四接入端，连接页与发送路径协议中立；全库统一为「事件驱动 IM 平台」定位 |

> 主线叙事：ZCBOT 起步于「OneBot v11 接入端的事件驱动 IM 平台」，但插件化、权限、持久化、Web 后台这些骨架从一开始就是通用的。
> 第五代（v1.3.x）把这条路线收口——**内核 = 极简内核 + 扩展点契约 + 官方插件集（core_plugins）+ 用户插件（plugins）**，
> 换一个 `ProtocolAdapter` 就能接入 HTTP Webhook、定时事件或任意其它 IM，OneBot 只是默认接入端，不再是身份。
> v1.3.8 起内核正式确立**扩展点（Hook）系统**，允许扩展挂到启动/关闭、Web 请求、事件分发、命令执行、协议动作、出站文本等几乎每一个运行环节。

---

## 开发中（未发版）

### 修复
- **v1.6.0 预览体检硬伤（P0）**：
  - `framework/core/dispatch.py` 补 `log_broker` 等导入，消息分发不再 `NameError`；
  - `framework/core/runtime.py` / `base.py` 路径 `dirname` 由 2 层改为 3 层，`core_plugins` / `plugins` / `config.yaml` 指向仓库根，官方插件可被发现；`runtime` 补 `asyncio`/`gc`/`importlib.util`；
  - `framework/database/db_conn.py` 去掉顶层 `import pymysql`（干净 SQLite 环境可启动），重连常量下沉并由 `db.py` re-export；
  - `framework/loader/config.py` 定义 `_CONFIG_FILE_EXTS`（`base.py` re-export）；`lifecycle.py` 补 `pip_install_all`；`ui.py` 补 logging/os/time。
- **多接入端串线**：`ServiceRegistry.adapter_for_source` 按事件来源选适配器；`reply_text` 与 `ctx.actions` 优先走来源对应端（多端并存时不再固定 onebot / 后加载者）。
- **http_api 群管**：`kick`/`ban`/`unban` 改走 `api.acall` 并检查 `status`，不再对 `api_caller` 直调方法导致 `AttributeError`→500；终端 `ban`/`kick` 检查返回结果，失败不假报成功。
- **ws_client**：`websockets` 14+ 用 `additional_headers`，12/13 用 `extra_headers`，装 12/13 不再建连即 `TypeError`。
- **qq_official**：`msg_seq` 改进程内计数器（同秒多回复不碰撞）；本地图片读取经 `asyncio.to_thread`，不再阻塞事件循环。

### 测试
- 新增 `tests/test_smoke.py`：路径解析、关键模块 import、分发+回复冒烟、按来源路由、适配器补丁断言。

### 文档
- 首页/对接 IM 诚实化：群管等协议专有动作按端能力说明，去掉「换接入端插件不用改」等超前概括。

---

## v1.6.0（2026-09-24）

> 主题：**多协议接入端 + 协议中立内核 + 全库「事件驱动 IM 平台」定位改写**。

### 新增
- **官方接入端 `ws_client`（出站 WebSocket）**：与反向 WS 的 onebot_adapter 互补，
  作为**客户端**主动连出到外部 WS 服务；远端 JSON 事件归一化入核，`send_msg` /
  `send_text` 出站，**图片消息段支持 `base64://`**（CQ 码与消息段数组均可解析）。
  完整实现 5 抽象方法 + `get_connection_info()`（连接页动态表单），断线自动重连、
  连接前出站有界积压。默认 `enabled: false`（外连安全），配置块
  `core_plugins.yaml → ws_client`（url / bot_name / token / reconnect_interval）。
  无新增三方依赖（复用 `websockets`）。
- **官方接入端 `qq_official`（QQ 官方机器人）**：`getAppAccessToken` 自动刷新
  access_token → `GET /gateway/bot` 取 WSS → Hello/Identify(op2)/心跳(op1)/Resume(op6)；
  群 `GROUP_AT_MESSAGE_CREATE` / 单聊 `C2C_MESSAGE_CREATE` 归一化入核，
  出站 `send_msg` 走 OpenAPI（群 `/v2/groups/{openid}/messages`、单聊
  `/v2/users/{openid}/messages`），**图片 base64:// 经文件上传 → msg_type=7**，
  被动回复带 `msg_id`/`msg_seq`。默认 `enabled: false`，配置块
  `app_id`/`app_secret`/`bot_name`/`intents`（默认 33554432=GROUP_AND_C2C_EVENT）。
  无新增三方依赖（`websockets`+`requests`，手写协议不依赖 botpy）。
- **官方接入端 `telegram`**：`getUpdates` 长轮询（offset 自增、断线重试）+
  `sendMessage`/`sendPhoto`；群/超级群 → `message_type=group`，私聊 → `private`；
  出站图片 `base64://`/`file://`/http(s)/本地路径 → multipart `sendPhoto`。
  默认 `enabled: false`，配置块 `token`/`bot_name`/`polling_timeout`。
  无新增三方依赖（`requests`）。
- **官方接入端 `discord`**：Gateway WSS（Hello/Identify/Heartbeat/Resume、
  心跳 ACK 丢失自动重连）+ REST v10 `POST /channels/{id}/messages`；
  `MESSAGE_CREATE` 归一化（guild→group，DM→private），出站图片 `base64://`
  → multipart `files[0]`。默认 intents `37377`
  （GUILDS|GUILD_MESSAGES|DIRECT_MESSAGES|MESSAGE_CONTENT，特权 Intent 需门户开启）。
  默认 `enabled: false`，配置块 `token`/`bot_name`/`intents`/`reconnect_interval`。
  无新增三方依赖（`websockets`+`requests`）。
  三者均：**enabled=true 时即使凭证未填也注册到连接页**（WebUI 可补配，填完重启后才建连）。

### 重构
- **内核彻底去 OneBot 硬编码（连接页/状态/发送路径协议中立）**：
  - `ProtocolAdapter` 新增可选 `get_connection_info()` 连接自描述
    （id/name/config_section/fields/restart_keys/endpoint_hint/guide/status_extra），
    `ServiceRegistry` 按 `adapter_id` 汇总多接入端（`protocol_adapters()` / `primary_adapter()`）。
  - `/api/connection` GET 改为返回 `adapters[]` 动态描述（兼容保留扁平 `config`/`status`）；
    PUT 按 `body.adapter` 与适配器声明的字段白名单写入，不再写死 `onebot` 段与
    `listen_host/listen_port/access_token`。仪表盘/运行状态/终端 `status` 的在线列表
    一律取 `protocol_adapter.get_connected_bots()`，`fw.ws_server` 降为兼容别名
    （优先接入端自报的 `ws_server`）。
  - `fw.protocol_backend` 属性别名改为 `fw.protocol_adapter`；停止顺序纳入
    `protocol_adapter`（兼容保留 `ws_server` 键）。
  - `ctx.actions` 成为协议中立动作面（推荐新代码）；`ctx.onebot` 保留为别名。
    `ctx.send_msg/ban/kick/...` 快捷方法改走 `ctx.actions`。
  - 双进程宿主 `IpcAdapter.send_text` 经新增 RPC `api.send_text` 转发核心接入端，
    不再硬编码 `send_group_msg/send_private_msg`；`http_api` 发送/广播/状态错误文案中立化。
  - 适配器实现：`onebot_adapter` / `http_inject` / `ipc` 均提供 `get_connection_info()`。
  - WebUI：连接页按 `adapters[]` 多卡片动态渲染配置表单与在线状态；
    仪表盘标题用 `adapter_name`，运行状态改为「接入端连接」，侧边栏改为「接入端连接」，
    设置页 OneBot 标签改为「接入端（onebot 段）」。
  - 顺带修复：`framework/messaging/protocol.py` 缺 `HookPoints` 导入、
    `framework/core/dispatch.py` 缺 `asyncio`/`HookPoints`/`ProtocolAdapter` 导入。
- **framework/ 根目录按域分包（第七轮）**：根目录 29 个散落 .py 收拢为
  `core/`（base · dispatch · runtime · stats_writer）、`ctx/`（base · messaging ·
  events · webui · db）、`loader/`（base · config · lifecycle · runtime · ui）、
  `deps/`（__init__ · pip）、`perm/`（__init__ · admin · groups · tracks）五个子包；
  根目录仅保留 `config/hooks/log_broker/dual_auth/scheduler/runtime/tls/apis/stats_writer`
  等入口与 shim。所有旧导入路径（`from framework.core import Framework`、
  `from framework.loader import pip_install_*`、`from framework.ctx import PluginContext`、
  `from framework.perm import resolve` 及懒加载属性、`from framework.stats_writer import
  AsyncStatsWriter` 等）经子包 `__init__.py` re-export 保持兼容。
- **core 史山剥离（第六轮）**：
  - `framework/core.py` 按职责拆为 base + 两个 mixin：事件/命令/通知分派与中立回复
    → `core_dispatch.py`（`FrameworkDispatchMixin`）；核心插件加载、依赖自愈、心跳/内存看门狗、
    内置任务 → `core_runtime.py`（`FrameworkRuntimeMixin`）；`Framework` 继承两者，
    公开方法与双进程角色分派（`_read_plugin_process_tag` / `_core_plugin_is_core_side`）行为不变。
  - `framework/deps.py` 中 pip 镜像安装/requirements 解析/版本说明符 → `deps_pip.py`
    （`_PIP_MIRRORS` / `_RE_PKG_NAME` 等常量随迁）；`deps.py` 保留 `PluginDepsMixin`
    与底部 `pip_install_*` re-export，`from framework.deps import pip_install_*` 兼容。
  - `framework/database/db.py` 连接管理/查询执行 mixin → `db_conn.py`（`DatabaseConnMixin`）；
    模块级 `init_db` / `_parse_sqlite_type` / 单例 `db` 与 `from framework.database.db import Database, init_db`
    保持兼容。
  - `framework/messaging/router.py` 匹配子系统 → `router_match.py`（`RouterMatchMixin` +
    `SimpleMatch` / `_PluginRoute`）；`MessageRouter` 继承 `RouterMatchMixin` + 关键词子系统
    `KeywordReplyMixin`，热路径行为不变。
  - `framework/loader.py` 加载/卸载/合成包预载/字节码清理 → `loader_lifecycle.py`
    （`PluginLifecycleMixin`）；`_PluginSourceLoader` 随迁并经 `loader.py` re-export，
    `from framework.loader import pip_install_*` 与加载器公开面不变。
- **core 史山剥离（第五轮）**：
  - `framework/ctx.py`（893 行 `PluginContext` 上帝对象，77 方法）按域拆为
    base + 四个 mixin：`ctx_messaging.py`（OneBot 快捷动作/身份/群级开关）、
    `ctx_events.py`（命令/任务/事件/扩展点/协议 API）、`ctx_webui.py`（仪表盘/WebUI/
    管理页扩展）、`ctx_db.py`（同步/异步数据库）；`PluginContext` 继承四个 mixin，
    全部公开方法名与签名不变（311 行 base）。
  - `framework/perm.py`（846 行）拆为 core 解析（`resolve`/`PermissionSet`/缓存，
    531 行）+ `perm_admin.py`（审计/过期清理/上下文构造）+ `perm_groups.py`
    （组 CRUD + 节点管理）+ `perm_tracks.py`（升降级轨道）；子模块单向依赖 core，
    `perm.py` 经模块级 `__getattr__` 懒加载 re-export，`from framework.perm import X`
    与 `perm.X` 属性访问均保持兼容。
- **core 史山剥离（第四轮）**：
  - `framework/terminal/builtins.py`（669 行 `register_builtins` 巨型函数）按域拆为
    `cmd_core.py`（help/status/plugins/log/clear/exit）+ `cmd_plugin.py`
    （enable/disable/config/reload）+ `cmd_msg.py`（send/recv/ban/unban/kick/broadcast）
    + `cmd_info.py`（users/groups/tasks）+ `cmd_update.py`（update）+ `helper.py`；
    `register_builtins(fw)` 保留为编排入口，20 条内置命令注册行为不变。
  - `framework/api/framework_ops.py`（343 行）：检查更新 / 源码更新拆至
    `framework_update.py`（262 行）；`framework_ops.py`（109 行）保留 version /
    restart / terminal_exec 并编排子模块，webapp 调用点不变。
  - `framework/loader.py`（1282 → 933 行）：配置 schema 能力（`read_config_schema` /
    `init_plugin_configs` / `get_plugin_config_files`）→ `loader_config.PluginConfigMixin`；
    运行时监控（`_start_memory_monitor` / `heartbeat_register` / `self_check_orphans`）
    → `loader_runtime.PluginRuntimeMixin`。
  - `framework/messaging/router.py`（707 → 571 行）：关键词自动回复子系统
    （`_KeywordRule` + 加载/匹配/handler/命中计数）→ `router_keywords.KeywordReplyMixin`；
    `MessageRouter` 改为继承该 mixin，热路径行为不变。
- **core 史山剥离（第三轮）**：
  - `framework/api/webapp.py`（1000 行）：`WebServer` 拆至 `webserver.py`；版本/yaml/
    插件市场/GitHub 下载等 24 个共享辅助闭包外提为 `app_helpers.make_app_helpers()` 工厂
    （键名与原 ctx 完全一致，1000 → 约 400 行）。
  - `framework/api/plugins.py`（1074 行）按域拆为 `plugins.py`（生命周期 + 依赖/venv +
    编排入口）+ `plugin_market.py`（GitHub 更新 / 市场源 / 安装）+ `plugin_meta.py`
    （README / 配置 / schema / 命令 / 依赖图）；`plugins.register(ctx)` 继续作唯一入口。
  - `framework/core.py` 的 `AsyncStatsWriter` 剥离至 `framework/stats_writer.py`
    （`from framework.core import AsyncStatsWriter` re-export 兼容）。
  - `framework/loader.py`（1604 → 1282 行）UI 能力抽为 `loader_ui.py` 三个 mixin：
    `PluginUiExtensionsMixin` / `PluginWebuiMixin` / `PluginGroupSettingsMixin`。
  - `framework/api/perm_api.py`：20 处函数内 `from framework import perm as perm_mod`
    提升为模块级单次导入。
- **core 史山剥离（第二轮）**：`framework/database/db.py`（1306 行，五职合一）拆为
  `dialect.py`（SQL 方言翻译，纯函数）/ `schema.py`（自动建表 + 运行时迁移）/
  `db.py`（连接管理 + 查询执行 + 事务 + 单例，523 行）；写路径 `execute` /
  `execute_many` / `insert` 合并为统一 `_write` 管线，消除三段复制粘贴与散落的
  `if db_type == 'sqlite'` 方言分支（统一走 `_translate_sql` / `_translate_write_sql`）。
- **loader 依赖职责剥离**：`framework/loader.py`（2177 → 1604 行）中的 pip 镜像安装、
  requirements 解析、版本说明符解析与插件依赖检查/隔离 venv 迁至新模块
  `framework/deps.py`（自由函数 + `PluginDepsMixin` 混入）；
  `from framework.loader import pip_install_*` 等旧导入路径经 re-export 保持兼容。
- **新增 `pyproject.toml`（PEP 621）**：项目元数据与依赖声明；`requirements.txt`
  继续作为 `main.py` 启动自检的安装入口，两处依赖同步维护（兼容保留）。

### 文档 / 配置
- **明确 SQLite 适用边界**：SQLite **仅适合小环境与开发环境**（单写多读、单文件），
  **不适合大环境**（多群、高并发、多进程部署）——`config.yaml` 注释、默认配置模板、
  启动日志、`docs/advanced/database.md`、`docs/guide/configuration.md`、README 统一标注，
  大环境一律切 `database.type: mysql`。
- 同步过期文档路径：`docs/advanced/database.md` 改指 `framework/database/*`；
  `architecture.md` / `event.md` / `services.md` / `protocol_adapter.md` / `session.md` /
  `loader.md` / `best-practices.md` 中的 `framework/{event,protocol}.py` 旧路径改指
  `framework/messaging/*`；README 目录树补齐 `pyproject.toml`、
  `framework/{database,messaging,terminal,deps,loader_*,stats_writer,api/{webserver,app_helpers,framework_update,plugin_market,plugin_meta}}`；
  `docs/advanced/loader.md` 维护者速查注明 deps / loader_ui / loader_config / loader_runtime 分工。
- **全库定位改写为「事件驱动的 IM 平台」**：README / CHANGELOG / pyproject / docs /
  vitepress config / main.py / framework 注释 / sql / WebUI 登录与权限页文案统一口径；
  清除「插件化服务宿主 / 借鉴 / 参考 AstrBot / 参考 Koishi / LuckPerms 风格 / xxx风格」等措辞；
  权限系统与 SQL 注释改为「节点式权限」中性表述。
- **微内核设计归属说明**：README 注明微内核设计独立在项目
  [zernus / Zero_Nexus](https://github.com/kuangxing6367/Zero_Nexus)，
  本仓库是构建于其上的 IM 平台与官方插件集；文中自称改为「平台内核 / 极简内核」。
- WebUI 登录页副标题改为「事件驱动 IM 平台 · 统一管理后台」，权限页去掉品牌风格提法。

---

## v1.5.1（2026-09-16）

> 主题：**群内 @机器人 命令修复**——修正消息归一化把 @机器人 前缀编码为 `[@self_id]` 污染命令匹配，导致群内以 `@机器人 命令` 触发的指令被静默吞掉。

### 修复
- **@机器人 命令被前缀吞掉**：`framework/messaging/event.py` 的 `_extract_text()` 将 `at` 段编码为 `[@qq]` 拼入文本，`router.py` 的 `route()` 又自行调用 `_extract_text(event.get('message'))` 而非复用 `Event.message`，导致群内 `@机器人 查订单` 变成 `[@机器人]查订单`，前缀无法匹配任何命令而被丢弃。
  改为在 `Event` 构造后剥离 leading `[@self_id]` 及其尾随空格，并让路由层复用已处理过的 `ev.message`；`segments` / `has_at_bot` / `at_list` 等原始信息均保留，@他人、文本在前 @ 在后、纯 @机器人 等场景行为不变。
- 验证：`@机器人 查订单`、`@机器人  查订单`（带空格）、`@机器人` 前置纯文本、@他人、纯 @机器人 五种用例均符合预期；`test_perm` 43/43、`test_dual_core` 4/4、`test_plugin_imports` 25/25 全过。

---

## v1.5.0（2026-09-12）

> 主题：**框架路径与静态资源路由修复**——修正 `init_db` 的 SQL 文件查找路径与插件 WebUI 静态资源路由，消除数据库全表缺失与插件页面资源 404 引发的连锁报错。

### 修复
- **`init_db` SQL 路径查找**：`_find_sql_file()` 原只上溯到 `framework/` 目录，找不到项目根 `sql/init.sql`，导致 SQLite 自动建表被跳过、系统表一张未建，进而 `plugins` / `admin_users` / `tasks` / `commands` 等全链路报 `no such table`。改为从 `framework/database` 向上回溯至多 4 级，优先匹配 `项目根/sql/<file>`，已实测命中。
- **插件 WebUI 静态资源路由 404**：`/api/plugin_webui/<plugin>/assets/<path:filename>` 路由中 `/assets/` 为字面段，Flask 将 `filename` 解析为 `mf.js`（不含 `assets/`），原 handler 却去 `web/mf.js` 查找（真实文件在 `web/assets/mf.js`）返回 404，导致复用 `mf_core/assets` 的业务插件页 `MF` 全局对象未定义、`MF.crudPage(...)` 抛 `ReferenceError`。改为拼到 `web/assets/<filename>` 下返回，URL 契约不变。

---

## v1.4.0（2026-09-11）

> 主题：**HTTPS/WSS 与可自定义界面**——开启 HTTPS/WSS（SSL 证书路径可配，相对/绝对），
> WebUI 左侧栏支持自定义显示，双核心补上跨进程终端，并让原生图片渲染扩展可在 CI 构建。

### 新增
- **HTTPS / WSS（SSL 证书）**：新增顶层 `ssl` 配置段（`enabled` / `cert` / `key`）。`cert`/`key` 支持
  **相对项目根目录**或**绝对路径**；启用后 Web 管理后台走 **https**、OneBot 反向 WS 走 **wss**（共用同一证书）。
  可在后台「设置 → SSL / TLS」修改（改动需重启生效）。实现：`framework/tls.py` 构建 SSLContext；
  Web 在启用 SSL 时改用 werkzeug 提供 TLS（waitress 本身不支持 TLS），未启用时行为不变。
- **WebUI 左侧栏支持自定义显示**：`web.sidebar`（`order` 顺序 / `hidden` 隐藏）控制官方菜单项的显示与顺序；
  后台新增「设置 → 侧边栏」可视化调整（勾选显示 + 上移/下移），保存后即时生效。
  插件 `ctx.webui(..., sidebar=True)` 注册的入口仍自动入栏；`/api/menu` 一并返回该配置。
- **原生扩展 CI 构建**：`.github/workflows/build-zcbot-render.yml`，在 GitHub Actions 上构建
  `image_renderer` 的 Rust 扩展（Windows 出 `zcbot_render.pyd`、Linux 出 `zcbot_render.so`），
  补上 README 已引用但仓库中缺失的自动构建工作流；产物已回填 `native/bin/`，win64 `.pyd` 经 Python 3.13 实测可加载并出图。
- **文档站首页（`bot.zgric.top` 宣传页）重写**为事件驱动 IM 平台定位：明确「使用人群 / 使用范围」，
  参考示例补齐非 IM 场景（纯定时任务、HTTP Webhook 事件源、自写接入端、扩展点切面）。

### 修复
- **文档站（`bot.zgric.top`）排版错乱**：VitePress `base` 由 `'/zcbot/'`（面向旧的 `kuangxing6367.github.io/zcbot/` 项目页）
  改为 `'/'`。站点已切到自定义域名 `bot.zgric.top`（根路径托管），沿用旧的 `/zcbot/` 前缀会让 css/js 与站内链接
  全部指向 `bot.zgric.top/zcbot/...` 而 404，页面因此失去样式；改为根路径后恢复正常。
- **双进程终端交互修复（跨进程终端）**：核心进程此前不走 `fw.start()`，导致双核心下**终端从未启动**。
  现在核心进程显式注册并启动终端；终端命令按 `target`（`core` / `host` / `both`）路由，
  `plugins` / `enable` / `disable` / `reload` / `tasks` 经 IPC `terminal.exec` 转发到宿主进程执行，
  `status` / `plugins` 两侧合并展示；单进程（`standard`）行为不变。见[双核心](docs/advanced/dual-core.md) 5.3 节。

---

## v1.3.8（2026-09-11）

> 主题：**扩展点契约 + 可插拔侧边栏**——内核正式确立「最小核心 + 扩展点」契约，
> WebUI 侧边栏开放给插件注册。

### 新增
- **扩展点系统（HookRegistry，`framework/hooks.py`）**：内核核心契约。内核在运行流程预留 12 个标准扩展点
  （`lifecycle.startup/shutdown`、`http.before/after_request`、`event.before/after_dispatch`、
  `command.before/after`、`message.before/after_send`、`action.before/after`），扩展用 `ctx.hook(point, handler)`
  往任意环节插入逻辑；支持 sync/async handler、优先级、同名去重、插件卸载自动清理。
  - `action.before/after` 覆盖每一次协议动作（send_msg / 禁言 / 查询…），适合统一审计 / 限流 / 中间件；
  - `event.before_dispatch` 返回 `False` 可丢弃事件；`command.before` 返回 `False` 跳过该命令；`http.before_request` 返回 Response 可短路请求。
- **Ctx 新增 `ctx.hook()` / `ctx.unhook()`**：插件侧统一注册 / 注销扩展点。
- **WebUI 可插拔侧边栏**：插件现在可以用 `ctx.webui(title, entry, icon, order, sidebar=True)` 在
  **侧边栏注册独立入口**（点击跳转 `/plugin/<插件名>` 直接打开该插件页面），不再局限于「插件页面」聚合页；
  `sidebar=False`（默认）保持向后兼容，仍归入聚合入口。前端新增 `/plugin/:name` 路由，`GET /api/menu` 返回侧边栏结构。
- **官方默认侧边栏支持开关**：新增 `config.yaml` → `web.official_sidebar`（默认 `true`），
  关闭后侧边栏仅显示插件注册项与「设置」；可在「设置 → Web 服务 → 显示官方侧边栏」中切换，保存后**即时生效，无需重启**。
- **文档与 README 内核叙事重构**：
  - README 从「插件化框架」升级为「内核 + 扩展点」叙事，新增「扩展点（Extension Points）」章节，保留全部原有详解（快速开始、权限、API Key、目录结构、双核心等）。
  - `docs/api/` 重组为 **基础参考**（`basic/`：ctx / event / framework / services）与 **进阶扩展**（`advanced/`：扩展点 / 协议适配器）两大块，原有详解完整保留。
  - 新增 `docs/api/advanced/hooks.md`（扩展点完整文档）与 `docs/api/index.md`（API 总览）。

### 修复
- **管理后台前端（WebUI）构建改用相对路径**：`base` 由 `'/'` 改为 `'./'`，logo 采用 `import.meta.env.BASE_URL` 拼接，
  使 `web/` 产物部署到任意子路径时都不会因绝对路径 `/js/`、`/css/` 404 而丢样式。

---

## v1.3.7（2026-09-10）

> 主题：**双核心实验版进入文档与发版流程（默认关闭）+ 修复终端命令在异步线程中的协程调度错误**。
> 双核心是长期实验线，默认单进程行为完全不变；终端修复解决了 `send`/`recv`/`ban`/`unban`/`kick`/`broadcast` 在 worker 线程里因无事件循环而「发送失败 / coroutine never awaited」的问题。

### 新增
- **双核心架构（core/host 双进程，实验特性）**：把一次启动拆成「核心进程 + 宿主进程」，经标准库 IPC（回环 TCP + authkey）通信，零第三方依赖。
  - `config.yaml` 暴露 `dual_process` 开关（`enabled` / `core_plugins` / `max_restarts` / `restart_interval`），**默认关闭，单进程行为完全不变**。
  - 官方插件按 `__plugin_meta__['process']` 标记自动分派：`onebot_adapter` / `http_inject` / `http_api` / `webui` 留在核心进程，其余与用户插件在宿主进程加载。
  - 新增开发文档 `docs/advanced/dual-core.md`，README 增加「十二、双核心实验版」章节。
- 新增 `tests/test_dual_core.py`：插件归属解析、IPC 协议往返、远程数据库约束；实测双进程可正常拉起（核心 spawn 宿主、IPC 握手成功、RemoteDatabase 代理生效）。

### 修复
- **终端命令异步调度修复**（`framework/terminal.py`）：`send`/`recv`/`ban`/`unban`/`kick`/`broadcast` 原在 `asyncio.to_thread` 的 worker 线程内定义闭包协程并 `ensure_future`，该线程无事件循环，导致 `There is no current event loop in thread 'asyncio_0'` 与 `coroutine was never awaited`。现改为 `async def` 直接在事件循环内 `await`，同步 DB 操作包进 `asyncio.to_thread`，不再抛错。

### 兼容性
- 双核心默认关闭，单进程启动、配置键、服务名、数据库 schema 全部不变；老业务插件零改动。
- 终端命令对外行为（命令名、参数格式、输出文案）不变，仅内部调度方式修正。

### 测试
- `compileall` 全量编译通过；`tests/test_dual_core.py` 4/4 通过。

---

# 第五代 · 通用插件化能力沉淀（v1.3.x）

## v1.3.6（2026-09-10）

> 主题：**框架层去除 QQ / OneBot 品牌绑定**——框架本体不再自称或被描述为「QQ 机器人框架」，
> OneBot 只以 `onebot_adapter` 插件的身份存在，框架自身保持协议中立。

### 变更
- 启动入口与内核文案中立化：`main.py`、`framework/__init__.py`、`framework/config.py` 顶部的
  「OneBot QQ机器人框架」改为「事件驱动 IM 平台」。
- 源码注释与文档字符串中的 QQ 品牌词全部改为中立表述：`QQ 号` → `用户 ID`、`QQ 群` → `群组`、
  `QQ 特有字段` → `平台特有字段`、`不局限于 QQ` → `不局限于单一平台` 等。
- 数据库脚本注释同步中立化（`sql/init.sql`、`sql/init_mysql55.sql`）：**表结构、字段名、索引均不变**，老库无需迁移。
- WebUI 文案：`QQ号` → `用户 ID`；登录页「OneBot QQ 机器人统一管理平台」改为「事件驱动 IM 平台 · 统一管理后台」。
- README 与 docs 去除 QQ 品牌字样；OneBot 仅作为默认接入端插件（`onebot_adapter`）的协议名出现。

### 修复
- 重新构建前端产物，清掉 `web/js/Connection.*.js` 里残留的合并冲突标记（`>>>>>>>> b1464e2...`）。

### 兼容性
- **纯文案 / 注释层变更**，无 API、配置键、服务名、数据库 schema 变更。
- `ctx.onebot`、配置段 `onebot`、服务名 `onebot_api`、`onebot_adapter` 插件目录名全部保持不变，老业务插件零改动。
- `core_plugins/onebot_adapter` 内部保留 OneBot 11 协议相关字样——它是该协议的实现端插件，与框架本体无关。

## v1.3.5（2026-09-10）

> 说明：本版本在研阶段以 v1.3.4 记录，发布时统一并入 v1.3.5（版本号由 v1.3.3 直接跳至 v1.3.5）。

> 主题：**官方插件配置中心 + 协议层彻底解耦，框架核心不再包含任何 OneBot 实现**。
> 全程保持前后向兼容：老插件的 `ctx.onebot` / `send_msg` / `ban` / `kick`、`bot=` 关键字、
> 事件结构、服务名、`fw.config.get('onebot')` 等公开用法全部不变。

### 新增
- **官方插件配置中心 `core_plugins.yaml`**：独立文件集中管理全部官方插件的 `enabled` 开关与配置项；
  启动时自动扫描 `core_plugins/` 目录——新装补块、卸载删块、自动回写，并合并进主配置，
  插件既有的 `fw.config.get('onebot')` 读取方式不变。`config.yaml` 中失效的旧 `core_plugins` 段已清理。
- **`framework/runtime.py` 运行时上下文**：新增协议无关的 `current_source_var`（当前事件来源），
  并保留 `current_bot_var` 别名以兼容旧代码。
- **协议接入抽象增强**（`framework/protocol.py`）：`ProtocolAdapter` 基类提供统一的
  `call/acall/send_text` 默认实现，以及协议无关的 `ActionProxy` 与 `ServiceRegistry`，
  自定义接入端只需实现最小契约即可获得 `ctx.api()` 与框架中立回复能力。
- **中立回复通道 `reply_text`**：路由层不再硬编码 OneBot `send_msg`，改走"当前接入端 → 通用回复"，
  IPC 适配器同样补齐 `send_text`。
- **基于插件元信息的进程分派**：官方插件通过 `__plugin_meta__['process']`（`core`/`host`）静态标记
  由 AST 静态分析自动分派到双进程，取代原先硬编码的 `_core_whitelist` 白名单；
  `dual_process.core_plugins` 显式名单仍可覆盖。

### 变更 / 重构
- **OneBot 实现整体迁出框架内核**：删除 `framework/onebot_api.py`、`framework/onebot_caller.py`、
  `framework/websocket_handler.py`；38 个 OneBot 动作迁入 `core_plugins/onebot_adapter/onebot_api.py`，
  `onebot_adapter/main.py` 自包含反向 WS 连接、事件归一化与 ApiCaller，并实现 `send_text`。
- **`ctx.onebot` 变为纯服务查找**：优先取 `onebot_api` 服务，缺失时以协议无关 `ActionProxy` 兜底，
  连接入端都没有才抛错；`ctx.api/aapi` 走通用 `api_caller`，不再与某个协议绑定。
- 终端命令的硬编码插件名单改为目录扫描，并修复原本调用不存在方法的 send/ban/kick/broadcast。
- 6 个官方插件补齐 `process` 标记：`onebot_adapter/http_inject/http_api/webui = core`，
  `session/scheduler = host`。

### 兼容性
- 所有既有公开 API、服务名、配置键、事件结构保持不变；老业务插件**零改动**即可运行。
- 关闭 `onebot_adapter` 后，框架可作为纯定时 / HTTP Webhook / 其它接入端的可扩展 IM 平台运行。

### 测试
- `compileall` 全量编译通过；`tests/test_plugin_imports.py` 31/31、`tests/test_perm.py` 43/43；
- 23 项解耦专项冒烟、三角色加载矩阵与旧白名单逐插件等价核对全部通过；
- 真实冷启动验证：Web 后台（8080）、反向 WS（6830）握手与事件注入正常，优雅停机退出码 0。

### 文档 / 表述
- 全项目文档去除 emoji：README 与 6 篇 docs 统一改为文字标记（必填 / 推荐 / 兼容、提示 / 注意、正确与错误示例），表格与列表结构不变。
- 版本来源统一：`VERSION` 文件是唯一版本出处（WebUI、`/api/version`、终端 `version`、`http_api` 元数据均读它），本次发布 v1.3.5。

## v1.3.3（2026-09-10）

> 主题：彻底修掉"完全热重载后仍跑旧代码"的字节码缓存问题。

### 修复
- CPython 默认按「源码整数秒 mtime + 文件大小」校验 `.pyc`，同一秒内把模块改成相同字节数时会误判字节码有效、复用旧代码。
- 新增 `_PluginSourceLoader`：主模块与顶层子模块每次加载都现场从 `.py` 源码编译，且不写 `__pycache__`，从根上绕开字节码缓存，同秒、同尺寸改写也能立即生效。
- 保留 `_clear_plugin_bytecode_cache()`：建包前清掉本插件各层 `__pycache__` 并 `invalidate_caches()`，覆盖由原生导入机制懒加载的深层嵌套包。
- 卸载/重载的 `sys.modules` 清理（`_purge_plugin_modules`）经压测确认无残留、不误伤其他同名插件。

### 测试 / 文档
- `test_plugin_imports.py` 新增「同秒同尺寸快速重载」回归，共 31 项全过；新增卸载/重载压测（双插件同名模块交错卸载、反复重载、运行时懒加载嵌套包）全部通过。
- `docs/advanced/loader.md` 增补「为什么插件不使用 `__pycache__`」并扩充"改了代码不生效"FAQ。

## v1.3.2（2026-09-10）

> 主题：插件支持 Python 原生相对导入，文档体系全面扩写。

### 新增 / 修复
- 插件加载支持原生相对导入（`from .xxx import Y` / `from . import xxx`），不再报 `attempted relative import with no known parent package`。
- 主模块 `plugin_<名>` 同时充当带 `__package__/__path__` 的合成包，作为插件内部相对导入的父包。
- 子模块规范名改为点分层级 `plugin_<名>.<模块>`，并保留旧下划线唯一名与短名别名（指向同一模块对象），旧的短名绝对导入写法完全兼容；多插件同名模块零冲突。
- 新增统一的 `_purge_plugin_modules`，卸载、重试与各失败路径都会回滚 `sys.modules`，避免半初始化模块残留；合入调度器为 None 时的守卫，避免空指针。

### 文档 / 测试
- 新增《插件加载与模块机制》（`docs/advanced/loader.md`）：合成包、三层模块名、相对/绝对导入规则、热重载与卸载清理、排错 FAQ。
- 全面扩写编写插件、ctx、Event、架构、数据库、定时任务、权限、会话、配置、部署等 16 篇文档，并订正与代码不符的旧描述。
- 新增 `tests/test_plugin_imports.py`（29 项断言全过）。

## v1.3.1（2026-09-09）

### 新增
- 独立 HTTP API 插件 `core_plugins/http_api`（**默认关闭**，默认 `127.0.0.1:1145`，共享 token 认证）。
- 终端新增 `update` 命令，支持框架一键更新。
- WebUI 日志页新增终端命令输入框与 `/api/terminal/exec` 接口。

### 修复
- 路由表 `post_type/type` 字段名不匹配导致消息丢失。
- 更新白名单补充 `core_plugins/webui`；心跳跳过核心插件。
- `session` 定时任务 handler 找不到；`core_plugins` 缺 `priority` 字段导致路由表刷新 KeyError。

## v1.3.0（2026-09-09）

> 主题：**插件化架构重构**——把协议接入、WebUI、会话、调度等官方能力从内核拆为 `core_plugins`，框架回归"壳 + 插件"；同期落地终端交互。
> （内部经历过 `v1.3.0-beta.0`、`v1.3.0-beta.0-alpha.0` 两个未单独外发的里程碑。）

### 新增
- 官方能力插件化：`onebot_adapter`（反向 WS 接入）、`webui`、`session`、`scheduler` 等以 `core_plugins` 形式加载、可在配置中开关。
- 框架终端交互（`terminal`）：可在控制台执行命令；修复 `exit` 不退出进程、`_start_time` 未定义、uptime 显示等问题。

### 重构
- 内核按"加载器 / 路由 / 事件 / 上下文 / 协议适配"重新分层，为后续协议解耦（v1.3.5）打基础。

---

# 第四代 · 权限与治理（v1.2.0-beta）

## v1.2.0-beta.1（2026-09-08，预发布）—— 权限系统 + 接口令牌

### 新增
- **节点式权限系统**（`framework/perm.py`）：权限节点模型、三态判定、通配 `*`、显式否决、组继承 `group.*`、上下文隔离（group/bot/msgtype）、临时权限（expire_at）、Tracks 晋升轨道、审计日志、60s 缓存。
- 内置角色组 `__member <- __admin <- __owner <- __super`（映射 `zcbot.role.*`）。
- `Event`/`ctx` 新增 `has_perm / check_perm / perms / perm_groups / primary_group`；`command(require_perm=)` 与既有 `require_level` 双轨并存。
- **接口令牌 API Key**：`api_tokens` 表 + `/api/apikeys` 创建/吊销（需超管，token 仅明文返回一次）；鉴权兼容 2048 位会话 token 与 ≥40 位 API Key。
- Web 后台新增「权限管理」（5 标签页）与「接口令牌」页；框架定时清理过期权限节点。

### 修复 / 文档
- 修复 FileBrowser 面包屑丢失前导 `/` 导致相对路径 400。
- 补权限系统开发文档与 API 文档的权限/接口令牌章节。

## v1.2.0-beta.0（2026-08-30，预发布）—— 插件孤儿任务自检

### 新增
- **插件孤儿任务自动自检校正**：`PluginLoader.self_check_orphans()` 随心跳（默认每分钟，受 `plugin.heartbeat_interval` 控制）执行——
  - 清理 `asks/commands` 表中代码目录已不存在的孤儿条目（任务同时移出调度器）；
  - 移除调度器中属于"当前未加载插件"的任务；
  - 清理后令路由缓存失效，保证内存与数据库对齐。
- 基线包含 v1.1.2 全部修复（重启加载禁用插件、ZIP 上传符号链接、A1 事件顺序、A13 黑名单拦截）。

---

# 第三代 · 正式版与健壮性（v1.0.x / v1.1.x）

## v1.1.2（2026-08-30）

### 修复
- 修复重启后仍加载已禁用插件等一批已知问题（含 ZIP 上传符号链接校验、A1 事件顺序、A13 黑名单拦截，详见 v1.2.0-beta.0 基线说明）。

## v1.1.1（2026-08-22）—— 插件管理修复

### 修复
- **插件上传不再残留 `.bak` 目录**：原先每次上传都把旧目录备份为 `.bak.{时间戳}` 且从不清理，反复上传会堆积、Windows 上难删除；现上传前清理历史 `.bak`，上传后立即删除当前 `.bak`。
- **插件更新后配置项正确同步**：`_conf_schema.json` 旧逻辑在 `plugins_dat` 已有时不覆盖，导致升级后新增/修改的配置项不显示；现随 `plugin.yaml` 始终覆盖，并自动删除废弃 key、补全新字段默认值、保留用户已改值。
- 清理遗留：删除已被 `webui/` 完全替代的 `web_legacy/` 目录。

## v1.1.0-beta.0（2026-08-22，预发布）—— 内存看门狗

### 新增
- 进程 RSS 看门狗：每 30s 检查进程内存，超过 `memory.limit_mb`（默认 120MB）时清空框架级角色缓存、统计聚合计数并强制 `gc.collect()`，打印清理前后 RSS；阈值/频率可在 `config.yaml → memory` 调整。

## v1.0.1（2026-08-22）—— 稳定性 / 内存 / 安全

### 优化
- 图片渲染字体缓存改为 LRU 上限，修复无限增长的内存泄漏（约 8MB/字号）。
- `ctx.get_config` 增加 30s TTL 缓存，async handler 不再每次同步查库阻塞事件循环。

### 修复 / 安全
- 不再直接信任 `X-Forwarded-For`：仅采信 `security.trusted_proxies` 白名单内反代的 XFF，防伪造 IP 绕过黑名单/限速/远程封禁。
- Web API 增加 CORS 支持，可用 `security.cors_allowed_origins` 收紧来源。

## v1.0.0（2026-08-20）—— 首个正式版

> 重点强化 Web 前端的可扩展性、健壮性与个性化。

### 重大特性
- 插件可整体接管前端（`override_webui`）：`/` 自动重定向到插件网页入口，插件被禁用/卸载时自动回退默认前端。
- custom_ui 个性化前端插件：从 GitHub 拉取网页模板（zip），支持模板选择/下载/安装/切换，刷新网页生效。
- 快刷检测与恢复页：同一 IP 5 秒内刷新 ≥5 次自动重定向到 `/reset` 恢复页。

### 框架更新
- 更新支持指定版本号（可下拉选择，默认最新）；本地已最新也允许更新；移除备份/回滚。

### 修复
- 插件管理页 `applyFilter` 崩溃；插件更新后 `plugin.yaml` 元信息被旧缓存遮挡；custom_ui 路由热重载时 `add_url_rule` 报错。

---

# 第二代 · 公测与界面现代化（v0.0.1-beta / v0.1.0-beta）

## v0.1.0-beta.2（2026-08-20，预发布）

### 修复
- 插件管理页崩溃：`Plugins.vue` 模板引用未定义的 `applyFilter`，组件渲染时报 `withKeys undefined`、页面空白；已补全定义并重建前端产物。

## v0.1.0-beta.1（2026-08-20，预发布）

### 界面体验
- 亮/暗主题一键切换，跟随系统偏好并记忆选择（localStorage），全站适配（含登录页）。
- 路由切换淡入淡出 + 位移动效；卡片悬浮、按钮按压、侧边菜单过渡、滚动条等微交互。
- 恢复品牌 Logo（登录页与侧边栏），新增 `/img/` 静态资源路由。

### 修复
- 移除命令管理中的"关键词回复"界面（该能力交由插件，如 keyword_api）；前端构建产物同步。

## v0.1.0-beta.0（2026-08-20，预发布）—— WebUI 现代化重写

### 重大变化
- Web 管理面板**全量重写**：原生 HTML/JS → Vue 3 + Vite + Element Plus，覆盖全部 14 个管理页面（仪表盘/插件市场/插件管理/命令管理/用户管理/群组管理/定时任务/运行状态/连接管理/文件管理/运行日志/数据库管理/插件 WebUI/全局设置）。
- 构建产物按需分包（element-plus、vue 独立 chunk），首页 JS 仅约 8KB；旧前端保留为 `web_legacy/`，`webui/` 为需 Node 构建的源码工程。

### 文档 / 修复
- 新增 KNOWN_ISSUES.md、debugging.md、plugin-tutorial.md，扩写 best-practices、重写 INDEX、更新 README。
- 修复插件页切换路由 key 解析失败；修复 `Event.role` 缓存缺 global 声明导致的权限降级。

## v0.0.1-beta.1（2026-08-19，预发布）

### 框架修复
- contextvars 多 bot 并发修复（回复不错发）；事件订阅去重（心跳重注册不再重复回复）；多 bot 连接注册表泄漏修复 + X-Self-ID 识别。
- OneBot 协议补齐（`send_like` 封装 / meta 事件广播）；修复内存泄漏 7 处（角色缓存/任务引用/队列/nonce 等）。
- 图片渲染内存优化（字体缓存 + malloc_trim 定期归还 OS）；仪表盘卡片超时隔离、数据库统计缓存。

### 新增基础插件
- `session_waiter`（多轮会话，等待用户下一条消息）、`message_guard`（唤醒词/白名单/限流/敏感词）、
  `plugin_depgraph`（插件依赖图，WebUI + `/依赖` `/依赖图`）、`ui_ext_demo`（群组/用户管理页扩展演示）。

### 其他
- `/status` 图片版状态卡；文档全面重写（手把手入门）；WebUI 群组/用户管理页插件扩展接口。

## v0.0.1-beta.0（2026-08-11，公测版）

公测首发。基于 OneBot v11 协议的事件驱动的 IM 平台，提供插件化架构、Web 管理面板、双数据库支持等核心能力。

### 框架核心
- 全异步架构（消息处理/API 调用/定时任务均不阻塞事件循环）；插件 handler 支持 `async def`，旧同步插件自动桥接到线程池。
- 插件热加载/热卸载、动态注册指令；反向 WebSocket 服务端，兼容 Lagrange/NapCat/go-cqhttp 等 OneBot v11 客户端。
- 内存路由表（热路径零 DB 查询、零线程切换）；统计批量异步写库；数据库专用线程池与消息处理隔离。

### 数据库
- 默认 SQLite 零配置；可选 MySQL，自动翻译方言 SQL（`ON DUPLICATE KEY UPDATE`→`ON CONFLICT`、`NOW()`→参数化、ENUM→TEXT 等）。
- MySQL DBUtils 连接池（有上限、空闲回收、坏连接重建）、自动重连；`ctx.create_table()` 统一建表入口，长列索引自动改前缀索引。

### Web 管理面板 / 安全 / 插件开发
- 侧边导航 + 深色主题，覆盖仪表盘/插件/命令/用户/群组/任务/日志/设置；ZIP 安装、热重载、依赖安装、隔离虚拟环境、GitHub 更新检查。
- 系统级关键词自动回复（动态命令，四种匹配方式）；SSE 实时日志；审计日志；仪表盘卡片；插件 WebUI；群级插件开关。
- Bearer Token（2048 位 hex）+ 双请求防破解（蜜罐探针 + nonce 挑战）；登录防爆破、IP 黑名单持久化、内网豁免；角色体系 超管>群主>管理员>普通用户>黑名单。
- `PluginContext(ctx)` 全能力 + 同步/异步双 API；代码与数据分离；依赖自动安装（多镜像回退）与独立虚拟环境；`plugin.yaml`/`_conf_schema.json`；生命周期钩子。

### 内置插件
- echo、help、image_renderer（Rust+pyo3 原生渲染，按架构绑定、缺失回退 PIL）、restart_manager、runtime_status。

### 已知限制
- 公测版可能不稳定；SSE 日志推送需用 fetch + ReadableStream（EventSource 不能自定义请求头）；未实现 API 限流（建议反代限流）；原生渲染扩展仅支持 Win x64 / Linux x64 / Linux aarch64。

---

# 第一代 · 诞生（v0.0.1-alpha，持续构建）

> 2026-08-04 首发，随后以 `build.N` 高频迭代（08-06 ~ 08-10）。下列构建号均有对应 Git Tag（无 build.12 / build.16 标签）。

## v0.0.1-alpha.0-build.4（2026-08-04）—— 首版

- OneBot v11（反向 WebSocket）全异步架构，同步插件自动兼容；插件化热加载/热卸载、依赖冲突检测与隔离虚拟环境。
- 内置 Web 管理面板（仪表盘/插件/命令/用户/群组/任务/日志/设置）；角色权限 + 登录防爆破、SSE 实时日志。
- SQLite 零配置，可选 MySQL 自动翻译方言；内置插件 echo / help / runtime_status / image_renderer。

## v0.0.1-alpha.1-build.5 ~ build.23（2026-08-06 ~ 08-10）

| 构建号 | 日期 | 要点 |
|--------|------|------|
| build.5 | 08-06 | 数据库自动重连；框架 Web 面板一键更新（ZIP，仅覆盖框架代码、自动备份）；VERSION 文件驱动的 Release 版本检测；公开 `/api/version`；插件市场改按 GitHub API 文件树索引下载；IP 黑名单持久化 + 内网豁免；双请求防破解认证 |
| build.6 | 08-06 | image_renderer 引入 Rust+pyo3 原生渲染（Win x64/Linux x64/aarch64，缺失回退 PIL），三平台二进制入库；第三方插件与 llm_plugin_gen 移出框架仓库、改走官方插件市场 |
| build.7 | 08-06 | 加载 main.py 时注册 `sys.modules`（修复热重载残留）；更新检测走 GitHub Release |
| build.8 | 08-06 | 日志页 SSE 改轮询，修复占满 waitress 线程导致 WebUI 卡死；插件市场改进（GitHub 加速、磁盘判定"已安装"、失败回滚、缓存兜底、幽灵插件标记） |
| build.9 | 08-07 | MySQL 改用 DBUtils `PooledDB`，修复连接只增不减、全 Sleep；连接参数真正生效、坏连接透明重建 |
| build.10 | 08-07 | 框架更新走 Release tag ZIP；下载逐候选校验 ZIP 魔数，跳过镜像返回的 HTML 错误页 |
| build.11 | 08-07 | **重要修复**：卸载插件不再删除 `plugin_configs`，重载/更新/禁用不再重置用户配置（仅真正删除插件时清配置） |
| build.13 | 08-07 | **框架防堵塞**：连接池有界等待（`blocking=False` + 借连超时）、新增 DB 专用线程池与默认线程池隔离，DB 繁忙不再拖垮消息处理 |
| build.14 | 08-07 | 内存路由表（5s 快照、热路径零 DB）；群开关/插件启用内存缓存与失效钩子；角色 60s TTL 缓存；生命周期钩子 `on_loaded/on_error/after_message_sent`；更新自动装依赖、备份/回滚入口 |
| build.15 | 08-07 | 修复插件 WebUI iframe 401：token 同步 HttpOnly Cookie 兜底鉴权 |
| build.17 | 08-07 | 深/浅主题切换（CSS 变量、跟随系统、无闪烁）；文件管理升级为"宝塔风"（面包屑/新建/上传/重命名/删除/下载/搜索）及对应 `/api/files/*` 接口 |
| build.18 | 08-07 | 401 自动种 Cookie 免重登；文件浏览右键菜单；新增 `/api/files/copy` |
| build.19 | 08-08 | 无文本消息（分享卡片等）广播 `message.<段类型>` / `message.media`；`Event` 新增 `has_share/share` |
| build.20 | 08-08 | **系统级动态命令（关键词自动回复）落地**：exact/prefix/contains/regex 四种匹配，正则预编译进内存路由表；`/api/dynamic-commands` 与命令管理页卡片 |
| build.21 | 08-08 | 框架改进 6 项：分享/JSON 卡片链接提取、`message` 统一文本事件、`continue_route()`、关键词 handler 回调、MySQL 前缀索引、`ctx.create_table()` 统一入口 |
| build.22 | 08-10 | 修复插件页切换路由 key 解析失败（先截断 `?` 再取段）；侧边栏进入插件页自动带 name |
| build.23 | 08-10 | 修复 GitHub 加速候选 URL 双重协议拼接 bug；默认加速代理改为 `gh.jasonzeng.dev`，失败回退内置镜像与直连 |

---

## 版本编号与发布说明

- 正式 Release 与历史构建号均可在 GitHub Tags/Releases 查到：<https://github.com/kuangxing6367/zcbot/releases>。
- `VERSION` 文件记录当前框架版本；插件通过 WebUI/`/api/version` 看到的版本以此为准。
- 文档与代码如有出入，以当前代码与最新 Release 说明为准，并欢迎在 Issue 指出。
