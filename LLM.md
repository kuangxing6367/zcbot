# ZCBOT LLM 文档（面向大模型 / Agent 的单文件项目上下文）

> 本文档是给 LLM 读的：把整个项目压缩成一份可独立消化的上下文，供 AI 写插件、
> 改框架、答问题时直接引用。**信息密度优先，无营销表述**；所有签名与行为以
> v1.7.0 源码为准。人类请读 [README.md](README.md) 与 [docs/](docs/)。

---

## 1. 硬事实速览

- **定位**：事件驱动的 IM 平台。极小内核（framework/）+ 扩展点契约 + 官方插件（core_plugins/）+ 用户插件（plugins/）。
- **运行**：Python 3.10+（验证覆盖 3.10–3.14），`python main.py` 启动，可选配置路径参数。
- **协议**：内核零 OneBot 代码。OneBot 11 是默认接入端（`onebot_adapter`，反向 WS，端口 6830）；另有 `qq_official` / `telegram` / `discord` / `ws_client` / `http_inject` / `http_api`（均默认关）。
- **Web 后台**：`webui` 插件，默认 `127.0.0.1:8080`，初始 `admin/admin123`。
- **数据库**：SQLite（`data/zcbot.db`，默认）/ MySQL 双方言，由 `db.query/execute` 统一接口访问，SQL 方言自动翻译。
- **依赖**：`requirements.txt` 与 `pyproject.toml` 两处同步维护；启动时自检缺包自动补装。
- **版本**：`VERSION` 文件 + `pyproject.toml` + README 版本行三处同步；发版流程 = 提交 `release: vX.Y.Z` → 打同名 tag → GitHub Release。

## 2. 目录结构

| 路径 | 职责 |
| ---- | ---- |
| `framework/` | 平台内核：加载、路由、事件、ctx、权限、数据库、扩展点注册表。无具体 IM 实现 |
| `framework/core/` | Framework 主体（mixin 拆分：base/dispatch/runtime…）、`event_buffer.py` 三层缓冲 |
| `framework/messaging/` | `router.py` 消息路由、`event_bus.py` 事件总线、`event.py` Event/文本提取、`protocol.py` 适配器契约与服务注册表 |
| `framework/loader/` | 插件加载器（合成包名 `plugin_<id>`、热重载、`_conf_schema.json` 配置） |
| `framework/ctx/` | PluginContext（base/messaging/events mixins）——插件拿到的 `ctx` |
| `framework/perm/` | 节点式权限引擎（三态 + 组继承 + 审计） |
| `framework/api/` | Web 后台 REST API（Flask，登录 + API Key 双鉴权） |
| `core_plugins/` | 官方插件，每目录一个 `main.py`，合成模块名 `core_plugin_<name>` |
| `plugins/` | 用户插件，每目录一个 `main.py`，合成包名 `plugin_<id>` |
| `core_plugins.yaml` | 官方插件开关与配置中心（启动自动扫描同步、回写、合并进主配置） |
| `config.yaml` | 框架全局配置（数据库/日志/安全/缓冲/插件目录） |
| `data/` | 运行数据：`logs/`、`zcbot.db`、`event_buffer.db`、`plugins_dat/<插件>/` 插件私有数据 |
| `docs/` | VitePress 文档站（`npm run docs:build` 构建） |
| `tests/` | 混合两类：pytest 用例（`test_smoke.py`、`test_html_assembler.py`）与脚本式回归（`test_plugin_imports.py` 等，模块级 `sys.exit`，**不能**被 pytest 收集） |

## 3. 事件流水线（按此顺序，勿凭想象）

```
适配器回调 dispatch_event(event)
  → hooks: event.before_dispatch（返回 False 丢弃）
  → EventBuffer 分层缓冲（默认异步；worker 未启动时同步退化）
      L1 内存队列(512KB/2000条) → L2 sqlite 溢出(data/event_buffer.db) → L3 内存兜底(4MB) → 全满告警丢弃
  → N 个 _event_worker_loop 消费（默认 workers=1，base.py: eq_cfg.get('workers', 1)）
  → _process_event 按 event.type 分流：
      meta_event  → event_bus.aemit('meta.<sub_type>')
      message     → ① raw handlers（按插件 priority 升序，返回 True=接管终止）
                    ② _extract_text 提取纯文本 → log_broker 记录
                    ③ stats_writer.register_user（入队后台批量落库）
                    ④ router.route(event, bot_name)
      notice      → aemit('notice.<notice_type>')；群成员增减走批量同步队列
      request     → aemit('request.<request_type>')
  → hooks: event.after_dispatch（必触发）
```

**router.route 热路径（零 DB）**：内存路由表（后台每 5s 重建，DB 在线程）→ 遍历插件
（群级开关查内存缓存）→ 插件内按 priority 匹配预编译命令（正则/前缀/别名）→ 命中执行
handler（sync 自动转线程）→ 未命中依次：广播 `message` 事件 → 系统关键词回复
（dynamic_commands 表）→ `router.message_unmatched` 扩展点。

**命令 pattern 判定**：`framework/messaging/router_match.py::_is_regex`——含正则元字符
按 re.compile 处理，否则前缀匹配（`match.group(1)` = 命令后参数）。`is_dynamic=1` 的命令
仅展示不参与匹配。

## 4. 插件契约（写插件的权威最小集）

```python
# plugins/<plugin_id>/main.py，plugin_id 全小写下划线
__plugin_meta__ = {
    "name": "插件中文名", "version": "1.0.0", "author": "…",
    "desc": "一句话", "priority": 50,      # 越小越先；守卫类 2-10，普通命令 50
    # 可选 "process": "core" —— 双进程模式下归属核心进程（默认宿主 host）
}

def register(ctx):
    ctx.command("/签到", handle, priority=50, alias="/sign",
                description="…", require_admin=False, require_superuser=False,
                require_perm="myplugin.sign")        # require_perm 权限节点，优先生效
    ctx.on_raw_message(on_raw)      # handler(raw: dict, bot_name: str) -> bool|None；True=接管
    ctx.task("*/10 * * * *", job)   # cron 定时任务
    ctx.on("message", listener)     # 订阅事件总线；handler 返回 True 视为已处理
    ctx.hook("action.after", fn)    # 挂扩展点（见 §6）
    ctx.register_api("/api/my", flask_view, methods=["GET"])  # 复用框架鉴权的 REST 路由

async def handle(event, match):     # 命令 handler：(event, match)；match.group(1)=参数
    await ctx.asend_msg(user_id=event.user_id, group_id=event.group_id,
                        message="文本，支持 CQ 码与消息段数组")

def unregister():                   # 可选：插件卸载清理
    ...
```

- 同步/异步双份约定：普通 `def` 用同步方法；`async def` 用 `a` 前缀（`asend_msg` / `aapi` / `await_for`…）。
- 同插件同 handler 重复注册自动去重；插件可热重载，`register` 内重建状态。
- 数据写在 `ctx.get_data_dir()`（→ `data/plugins_dat/<插件名>/`），**禁止**写插件代码目录（更新会覆盖）。
- 多文件插件：直接 `import 同目录模块` 即可，加载器做了包名合成与相对导入兼容。

## 5. ctx 与 event 常用面（其余见 docs/api/basic/ctx.md、event.md）

| 类别 | 成员 |
| ---- | ---- |
| 发送 | `send_msg` / `asend_msg(user_id=None, group_id=None, message=…)`、`actions`（协议动作封装）、`api(action, bot=None, **params)` / `aapi` |
| 会话 | `await wait_for(event, prompt=None, timeout=60, handler=None)`（官方 session 插件提供）、`create_session(event)` |
| 数据 | `get_data_dir()`、`db`（`db.query(sql, params)` / `db.execute(sql, params)`，双方言）、`get_config(key, default)` / `get_all_config()`（读插件配置，TTL 缓存 30s） |
| 权限 | `has_perm(user_id, node)` / `check_perm`、`audit_log(action, …)` |
| 事件 | `on` / `emit` / `aemit`、`hook(point, handler, priority=50)` / `unhook(point)` |
| 注册 | `command` / `on_raw_message` / `task` / `register_api` / `webui(...)` |
| 日志 | `log(msg, level='info')`、`logger`（命名 `zcbot`） |

event（命令 handler 第二参）：`message`（提取后纯文本）、`raw_message`、`user_id`、`group_id`、
`is_group`、`sender`、`role`（`super>owner>admin>member>blacklist`）、`is_admin`、
`segments`（消息段列表）、`reply_text()` / `stop_propagation()` / `continue_route()`。

## 6. 扩展点（HookPoints，`framework/hooks.py`）

| 点位 | 时机 | 短路 |
| ---- | ---- | ---- |
| `lifecycle.startup` / `lifecycle.shutdown` | 进程启停 | 否 |
| `http.before_request` / `http.after_request` | Web 请求前后 | before 可返回 Response |
| `event.before_dispatch` / `event.after_dispatch` | 事件进核前后 | before 返回 False 丢弃 |
| `command.before` / `command.after` | 命令执行前后 | before 返回 False 跳过 |
| `message.before_send` / `message.after_send` | 主动发文本前后 | before 返回 False 取消 |
| `action.before` / `action.after` | 任意协议动作前后 | 否（通知/审计） |
| `router.before_route` / `router.after_route` / `router.message_unmatched` | 路由三阶段 | unmatched 返回 True 接管 |

handler 签名随点位不同（见 docs/api/advanced/hooks.md），支持 sync/async；同点位同名去重。

## 7. 官方插件与常用服务名（`fw.services.get(name)`）

| 插件 | 服务/用途 | 默认 |
| ---- | ---- | ---- |
| `onebot_adapter` | OneBot 11 反向 WS 接入端（`onebot_api`） | 开 |
| `webui` | Web 后台 | 开 |
| `session` | `session_manager`（多轮会话） | 开 |
| `scheduler` | APScheduler cron 调度（`scheduler`） | 开 |
| `html_assembler` | `html_assembler`（单文件 HTML 装配：`render_html/render_to_file/embed_image/HtmlAssembler`） | 开 |
| `image_renderer` | 图片渲染（Rust 原生 + PIL 回退，`core_plugins/image_renderer/native/bin/<平台>/`） | 开 |
| `http_api` / `http_inject` | REST API / HTTP 事件注入（`:1145` / `:8901/hook`） | 关 |
| `qq_official` / `telegram` / `discord` / `ws_client` | 其余接入端 | 关 |

## 8. 高频坑（LLM 生成代码前必读）

1. **不要绕过框架直跑** `python plugins/xxx/main.py`——模块名合成、ctx 注入、DB 全依赖加载器。
2. **跨插件访问**用 `sys.modules.get("plugin_<id>")`（官方插件是 `core_plugin_<name>`；
   `html_assembler` 另有别名 `plugin_html_assembler`）。拿不到返回 None，**不要** `[]` 直取；
   依赖加载顺序时订阅 `system.plugin.loaded` 事件。
3. **raw handler 返回 True 会终止该消息的全部后续处理**（含命令匹配）——谨慎。
4. `event_bus.aemit` 默认串行保序；监听型互不依赖的场景可 `parallel=True`。
5. SQLite 仅适合小环境/开发；多群高并发上 MySQL（`config.yaml → database.type`）。
6. 配置中心行为：启动扫描 `core_plugins/` → 已安装未列出的自动补默认块 → 已卸载的自动删块 → 回写 yaml → 合并进主 config（`fw.config.get('onebot')` 等读法不变）。
7. 双进程模式（`dual_process.enabled`）：`process: "core"` 的官方插件与 Web/DB 在核心进程，用户插件在宿主进程，跨进程调用走远程代理——插件代码无需感知。
8. 消息事件里**命令参数**在 `match.group(1)`，完整原文在 `event.message`；@机器人 前缀已在 Event 构造时剥离。
9. 写 SQL 用参数占位符，MySQL/SQLite 方言差异交给 db 层翻译；批量写用官方插件的队列模式（参考 `stats_writer` / 群成员同步）。

## 9. 测试与 CI

- pytest 用例（显式列文件跑）：`python -m pytest tests/test_smoke.py tests/test_html_assembler.py -q`
- 脚本式回归（CI 直跑，**不可**被 pytest 收集）：`python tests/test_plugin_imports.py`、`test_dual_core.py`、`test_perm.py`、`test_smoke.py`（双模式兼容）。
- CI：`.github/workflows/tests.yml`（py3.10/3.11/3.12 矩阵：脚本回归 + pytest）；`build-zcbot-render.yml`（native/** 变更时编译 Rust 扩展）；`deploy-docs.yml`（文档站 Pages 部署）。

## 10. 文档地图

| 要做什么 | 读 |
| ---- | ---- |
| 写第一个插件 | docs/guide/writing-plugins.md |
| ctx / event 全量 API | docs/api/basic/ctx.md · event.md |
| 扩展点细节 | docs/api/advanced/hooks.md |
| 自写接入端 | docs/api/advanced/protocol_adapter.md |
| 权限系统 | docs/advanced/permission.md |
| 插件加载/热重载机制 | docs/advanced/loader.md |
| 部署（systemd/Docker/HTTPS） | docs/advanced/deployment.md |
| 架构与启动时序 | docs/advanced/architecture.md |
