# ZCBOT 框架架构与开发指南

> 本篇讲「**框架本身**」：模块如何协作、消息怎么流动、插件怎么被加载、如何扩展框架。
> 插件开发的具体 API 看 [API 参考](api-reference.md)；从零写一个插件看 [插件开发详解](plugin-tutorial.md)。
> 官方插件的完整实现文档见 [第九章](#九官方插件开发实例) 与各插件自带 `docs/`。

---

## 一、整体架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                         OneBot 11 客户端（QQ 机器人）                   │
│             反向 WebSocket / HTTP 上报  ──┐                             │
└──────────────────────────────────────────┼────────────────────────────┘
                                            │ 上报事件
                                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          接入层（framework/）                           │
│  websocket_handler.py  建立 WS 连接、收上报                            │
│  onebot_api.py         封装 38 个 OneBot 11 标准 API + 扩展兜底        │
└──────────────────────────────────────────┬───────────────────────────┘
                                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          核心层（framework/）                           │
│  core.py          Framework 主体：持有配置/插件/连接，编排启动          │
│  router.py        消息路由：命令匹配（正则/前缀）、优先级、stop_event   │
│  event_bus.py     事件总线：emit / on，插件间解耦通信                   │
│  scheduler.py     定时任务：cron 表达式调度                             │
│  event.py         Event 对象：封装上报事件，提取文本/富媒体/权限        │
│  loader.py        PluginLoader：扫描 plugins/、加载、热重载、卸载       │
│  ctx.py           PluginContext：注入给插件，提供全部能力               │
└──────────────────────────────────────────┬───────────────────────────┘
                                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          插件层（plugins/<name>/main.py）               │
│  每个插件：register(ctx) 注册命令/任务/事件；通过 ctx 调用框架能力      │
└──────────────────────────────────────────┬───────────────────────────┘
                                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      数据层 / Web 层（framework/）                      │
│  db.py        SQLite / MySQL 自动适配（统一 %s 占位符）                 │
│  config.py    config.yaml 读取、github_proxy、市场源配置               │
│  apis.py      Flask HTTP API（/api/*）+ Web 面板后端                   │
│  web/         框架默认 Web 管理面板（Element Plus 前端）               │
│  custom_ui    插件可接管 Web 面板（见 9.1）                            │
└──────────────────────────────────────────────────────────────────────┘
```

**设计要点**：插件永远通过 `ctx`（PluginContext）访问框架能力，从不直接 import 框架内部模块；框架通过 `register(ctx)` 收集插件的命令/任务/事件，运行时按优先级调度。

---

## 二、模块职责一览

| 模块 | 职责 | 关键符号 |
| ---- | ---- | ---- |
| `core.py` | 框架主体，编排启动顺序、持有全局状态 | `Framework` 类 |
| `loader.py` | 扫描 `plugins/`，导入 `main.py`，调用 `register(ctx)`，热重载/卸载 | `PluginLoader` |
| `ctx.py` | 插件上下文，暴露全部框架能力 | `PluginContext` |
| `router.py` | 消息路由与命令匹配，按 `priority` 升序尝试 | `Router` |
| `event.py` | 上报事件封装（文本/富媒体/权限/类型） | `Event` |
| `event_bus.py` | 进程内事件总线 | `emit` / `on` |
| `scheduler.py` | 基于 cron 的定时任务 | `TaskScheduler` |
| `onebot_api.py` | OneBot 11 API 封装 + `ctx.api()` 兜底 | `OneBotAPI` |
| `websocket_handler.py` | OneBot 反向 WS 连接与上报分发 | `WSHandler` |
| `db.py` | SQLite/MySQL 适配，连接池 | `db_query` / `db_execute` |
| `config.py` | `config.yaml` 读取、代理/市场源 | `Config` |
| `apis.py` | Flask 路由（Web 后端 HTTP API） | `@app.route` |
| `dual_auth.py` | 双因子/令牌鉴权 | `require_auth` |
| `log_broker.py` | 日志聚合与转发 | `LogBroker` |
| `init_db.py` | 首次启动建表 | `init_database` |

---

## 三、启动与加载流程

1. **读配置**：`config.py` 读取 `config.yaml`（机器人账号、数据库连接、github_proxy、市场源）。
2. **建库**：`init_db.py` 建系统表（`plugins`、`plugin_configs`、`commands`、`audit_logs`…）。
3. **起核心**：`core.py` 创建 `Framework`，建立 OneBot 反向 WS（`websocket_handler.py`）。
4. **加载插件**：`loader.py` 扫描 `plugins/` 子目录 → `import main.py` → 把 `ctx` 注入模块全局 → 调用 `register(ctx)` 收集命令/任务/事件。
5. **起调度**：`scheduler.py` 注册所有 `ctx.task` 的 cron 任务。
6. **起 Web**：`apis.py` 启动 Flask，提供 `/api/*` 与 Web 面板；`custom_ui` 若启用且接管，则根路由 `/` 重定向到其模板。
7. 就绪，等待 OneBot 上报。

---

## 四、消息处理流水线

```
OneBot 上报（message 事件）
   │
   ▼
websocket_handler → 构造 Event（event.py）
   │
   ▼
on_raw_message 注入点（插件可完全接管，返回 True 则终止后续）
   │  （未接管）
   ▼
router 命令匹配：
   ├─ 遍历已注册命令（按插件 priority 升序）
   ├─ 前缀匹配 / 正则匹配（re.search）
   ├─ 命中 → 调用 handler(event, match)
   │            └─ handler 内 event.stop_event() 可阻止后续插件
   └─ 未命中 → 关键词自动回复兜底
   │
   ▼
event_bus.emit("message", ...) 等系统事件
   │
   ▼
ctx.send_msg / ctx.api → onebot_api → 上报发送
```

> `match.group(1)` 统一表示命令后的参数文本；`event.stop_event()` 用于阻止同一条消息继续传递给优先级更低的插件。

---

## 五、插件生命周期

| 阶段 | 触发 | 框架动作 |
| ---- | ---- | ---- |
| 发现 | 启动 / 重载 | `loader` 扫描 `plugins/` 下含 `main.py` 的目录 |
| 加载 | 发现后 | `import main.py` → 把 `ctx` 注入模块全局 → 调用 `register(ctx)` |
| 热重载 | Web 面板「重载」 | `unload`（移除该插件注册的命令/任务/事件）→ 重新 `import` → `register` |
| 卸载 | 禁用插件 | 清理注册表、断开事件订阅 |
| 优先级 | `plugin.yaml` 的 `priority` | 数字越小越先加载、越先收到消息、命令匹配越优先 |

> **约定**：`register(ctx)` 是插件必须的入口；表名/配置键加插件前缀避免冲突；`CREATE TABLE` 必须 `IF NOT EXISTS`（插件会被反复热加载）。

---

## 六、PluginContext 能力总览

`ctx` 是插件与框架交互的唯一入口，全部方法见 [API 参考](api-reference.md)。速览：

- **命令**：`ctx.command(pattern, handler, priority=, alias=, description=, require_admin=, require_superuser=)`
- **发消息**：`ctx.send_msg(user_id=, group_id=, message=)` / `await ctx.asend_msg(...)`
- **数据库**：`ctx.db_query` / `ctx.db_query_one` / `ctx.db_execute` / `ctx.db_insert` / `ctx.db_execute_many` / `ctx.create_table` / `ctx.db_connection`
- **事件**：`ctx.on(name, fn)` / `ctx.emit(name, payload)` / `ctx.on_raw_message(fn)`
- **配置**：`ctx.get_config(key, default=)` / `ctx.get_all_config()`
- **权限**：`ctx.is_superuser` / `ctx.is_group_admin` / `ctx.is_group_owner` / `ctx.get_user_role`
- **群/用户管理页扩展**：`ctx.register_group_extension` / `ctx.register_user_extension`
- **WebUI**：`ctx.webui(title=, entry=, icon=, order=)`
- **仪表盘卡片**：`ctx.dashboard_card(title, getter, icon=, priority=)`
- **审计**：`ctx.audit_log(action=, target_type=, target_name=, detail=, result=)`
- **OneBot API**：`ctx.onebot.<action>(**params)` 或 `ctx.api(action, **params)` 兜底

---

## 七、配置系统

| 文件 / 表 | 作用 |
| ---- | ---- |
| `plugin.yaml` | 插件元信息：`name`/`version`/`author`/`desc`/`priority`，以及 `dependencies`（Python 依赖）、`github`（更新源 repo/branch/path） |
| `_conf_schema.json` | Web 配置表单 schema：`{key: {type, default, description, hint, options}}` |
| `plugin_configs` 表 | 用户实际配置值（Web 面板写入），`ctx.get_config` 读取，热生效 |

> **接管前端/接管来源**：`plugin.yaml` 的 `github` 段在安装时被写回 `plugins_dat/<name>/plugin.yaml`，供 `check_plugin_update` / `update` 定位仓库（详见插件市场小节）。

---

## 八、Web 层与 WebUI 接管

- **后端**：`apis.py` 提供全部 `/api/*`（登录、插件、命令、配置、日志、市场源…），用 `@require_auth` 鉴权。
- **插件 WebUI**：`ctx.webui(title="我的面板", entry="index.html")` 注册后，框架经 `/api/plugin_webui/<plugin_name>/` 提供 `web/index.html` 及静态资源。
- **接管整个面板**：`custom_ui` 插件调用 `ctx.override_webui()` 让根路由 `/` 重定向到插件模板（见 9.1）。框架内置「刷新过快保护」与自动回退，异常时引导到 `/reset` 恢复默认前端。

---

## 九、官方插件开发实例（引入部分官方插件文档）

> 官方插件的完整开发文档已整合到 [官方插件开发实例](official-plugins-dev.md)，消除散落在各插件目录的割裂。
> 该篇给出「能力 → 实现步骤 → 代码示例」，可直接照做：

- **9.1 custom_ui — 模板化接管 Web 面板**：模板 zip 结构、URL 规则、模板内调用框架 API、打包发布、`override_webui` 接管机制、`/api/custom_ui/*` 全部接口、二次开发要点。
- **9.2 image_renderer — 原生扩展（Rust + pyo3）**：架构绑定、`Canvas` 链式图元 API、图像处理函数、`render_list`/options、CI 与本地编译、自动回退 PIL 机制。
- **9.3 复杂插件模式（llm_chat / broadcast）**：配置面板 + 数据库 + WebUI 控制台 + 定时/事件的组合骨架。

---

## 十、插件市场与发布

官方/第三方插件统一通过 `registry.json` 分发（借鉴 AstrBot 的多源 + Koishi 的版本对比）：

- **registry 条目**：`{name, version, author, description, repo, branch, sub_path}`，`version` 用于精准「可更新」检测。
- **安装**：`_download_and_extract_plugin` 优先下载单插件 zip `gh-pages/packages/<name>.zip`（镜像加速），失败回退文件树/整仓。
- **更新**：`check_plugin_update` 对比 `registry.json` 的 `version` 与本地 `plugin.yaml` 的 `version`；`update_plugin_from_github` 复用单 zip 下载。
- **第三方源**：`system_config` 表存自定义源 URL，市场列表合并默认源 + 镜像源 + 自定义源（多源混用）。
- **发布**：插件仓库 `registry.json` 登记 + `scripts/build_plugin_zips.py` 打包单插件 zip 到 `gh-pages/packages/`。

---

## 十一、如何扩展框架本身

| 扩展点 | 做法 |
| ---- | ---- |
| 新增 `ctx` 能力 | 在 `ctx.py` 的 `PluginContext` 上加方法，自动注入所有插件 |
| 新增 HTTP API | `apis.py` 加 `@app.route`，用 `@require_auth` / `@require_super` 保护 |
| 新增系统事件 | 在框架合适位置 `event_bus.emit("name", payload)`，插件用 `ctx.on("name", fn)` 订阅 |
| 新增 OneBot API | 在 `onebot_api.py` 封装动作，或插件用 `ctx.api("action", **params)` 直接调 |
| 新增接入协议 | 仿 `websocket_handler.py` 写上报分发，构造 `Event` 后走同一 `router` |

---

## 十二、文档导航（消除割裂）

| 文档 | 内容 | 何时看 |
| ---- | ---- | ---- |
| 本篇 `architecture.md` | 框架架构、模块、消息流、生命周期、扩展框架、官方插件实例 | 想懂「框架怎么跑」 |
| [插件开发详解](plugin-tutorial.md) | 每日签到实例，逐行讲透每个语法 | 第一次写插件 |
| [API 参考](api-reference.md) | `ctx` 全部方法 + `Event` 字段 | 写代码时查 API |
| [快速入门](getting-started.md) | 最小可运行插件 | 想先跑通 |
| [插件目录结构](plugin-structure.md) | 代码/数据分离、生命周期钩子 | 搭骨架 |
| [配置系统](configuration.md) | `plugin.yaml` / `_conf_schema.json` | 加可配置项 |
| [最佳实践](best-practices.md) | 错误处理、异步、安全、自查 | 提交前 |
| [调试指南](debugging.md) | 日志、DEBUG、热重载 | 出 bug |
| [示例合集](examples.md) | 完整签到插件 | 要参考代码 |
| [官方插件使用手册](official-plugins.md) | 每个官方插件的用法 | 装了插件 |
| 官方插件源码文档 | `plugins/custom_ui/docs/INDEX.md`、`plugins/image_renderer/README.md` | 做同类插件 |

> 想用 AI 写插件：装 `llm_plugin_gen` 插件，其文档（给 LLM 的上下文）在 `plugins/llm_plugin_gen/docs/`。
