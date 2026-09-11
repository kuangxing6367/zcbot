# 双核心架构（core/host 双进程，实验版）

> 状态：**实验特性**。代码已实现核心能力并默认关闭，尚未经过大规模生产验证。
> 本文档面向需要理解或参与开发双核心的开发者（角色 C）。

## 1. 它解决什么问题

单进程宿主里，协议接入、Web 后台、数据库、用户插件全在一个进程内。一旦某个用户插件
把进程拖崩，接入端、Web、调度器会一起掉线。

双核心把一次启动拆成**两个进程**：

- **核心进程（Core）**：持有真实资源——数据库、协议接入端、Web/WebUI、IPC 服务端，并监督宿主存活。
- **宿主进程（Host）**：加载执行全部用户插件，跑事件路由 / 权限 / 会话 / 调度，通过 IPC 远程代理访问核心资源。

收益：

- **故障隔离**：宿主（用户插件）崩溃，核心自动重启它（带限流），接入端与 Web 不掉。
- **内存隔离**：插件内存压力不挤占核心的协议/Web 栈。
- **为未来打底**：多接入端、多宿主、热替换宿主进程都建立在同一 IPC 边界上。

代价：跨进程 RPC 有固定开销，单次数据库/发消息调用比进程内多一次序列化往返。

## 2. 结构总览

```
┌─────────────────────────── 进程1 · 核心 (Core) ───────────────────────────┐
│  Framework(role='core')                                                  │
│  ┌────────────┐  ┌──────────────────────┐  ┌──────────┐  ┌──────────┐   │
│  │ 真实数据库 │  │ 协议接入端（core 插件） │  │ Web/WebUI│  │ IPC 服务端│   │
│  │ Database   │  │ onebot/http_inject/   │  │          │  │ + 宿主监督│   │
│  └────────────┘  │ http_api / webui     │  └──────────┘  └──────────┘   │
│                  └──────────────────────┘                               │
└───────────────────────────────────┬───────────────────────────────────┘
                                     │  IPC JSON-RPC（回环 TCP + authkey）
┌───────────────────────────────────┴───────────────────────────────────┐
│ 进程2 · 宿主 (Host)  Framework(role='host', ipc_client=...)             │
│  ┌────────────┐  ┌──────────────────────┐  ┌──────────────────────┐   │
│  │ 用户插件   │  │ 事件路由 / Raw 处理    │  │ IpcAdapter           │   │
│  │ plugins/   │  │ 权限 / 会话 / 调度     │  │ (services:           │   │
│  └────────────┘  └──────────────────────┘  │  protocol_adapter,    │   │
│                                            │  api_caller)          │   │
│  远程代理：RemoteDatabase / RemoteApiCaller / RemoteRouteRegistry      │
└───────────────────────────────────────────────────────────────────────┘
```

进程边界用 `multiprocessing.connection`（标准库，pickle 帧，authkey 摘要鉴权），**零第三方依赖**。

## 3. 角色三态与插件分派

`Framework` 的 `role` 参数有三相，由 `config.yaml → dual_process.enabled` 门控：

| role | 何时 | 加载的官方插件 | 数据库 | 说明 |
|------|------|---------------|--------|------|
| `standard` | 未开启双进程（默认） | 全部 | 真实 Database | 现有单进程行为，完全不变 |
| `core` | 开启双进程时核心进程 | 仅 `process:'core'` 标记的 | 真实 Database | 协议/Web/DB 基础设施 |
| `host` | 开启双进程时宿主进程 | 排除 `process:'core'` 的 | `RemoteDatabase` 代理 | 用户插件 + scheduler/session |

分派逻辑在 `framework/core.py → _load_core_plugins`：先读 `dual_process.core_plugins` 显式白名单，
未配置则回退到**静态解析**每个插件 `__plugin_meta__['process']`（AST 解析，不执行代码）。

已标记 `process: 'core'` 的官方插件：`onebot_adapter`、`http_inject`、`http_api`、`webui`。
其余（`scheduler`、`session`、`image_renderer`）与全部用户插件在宿主进程加载。

## 4. IPC 协议

传输层：`multiprocessing.connection.Listener/Client`（AF_INET 回环，`authkey=secrets.token_bytes(32)`）。

信封（`framework/ipc/protocol.py → JsonRpcConnection`）：

```json
{"t": "req|res|event|ping|pong", "id": 0, "method": "...", "params": {},
 "result": null, "error": null, "channel": "...", "payload": null}
```

- `req` / `res`：请求-响应，`id` 匹配，调用方阻塞等待（同步 `call` / 异步 `acall`）。
- `event` / `notify`：单向广播，`on(channel, handler)` 订阅。

线程模型：读线程处理入站；若本端已 `set_loop`，`req` 经 `run_coroutine_threadsafe` 调度到事件循环，
`async` handler 直接 `await`，同步 handler 转 `to_thread`。

## 5. 跨进程数据流

### 5.1 事件推送（core → host）

1. 协议接入端在**核心进程**收到外部事件，触发 `framework.dispatch_event`。
2. 核心进程的 `dispatch_event` 已被替换为 IPC 推送（`framework/ipc/core_runtime.py → _ipc_dispatch`）。
3. `IpcServer.asend_event` → `notify('event', event)`。
4. 宿主进程的 `IpcAdapter._on_event` 用 `run_coroutine_threadsafe` 注入 `framework.dispatch_event(payload)`，
   走原有 `raw_handlers → router → event_bus`，**多轮会话 `wait_for` 天然跨进程可用**。

### 5.2 RPC 调用（host → core）

- **数据库**：插件 `ctx.db_query` / `db.*` → `RemoteDatabase`（宿主）→ IPC `db.<name>` → 核心真实 Database 执行。
- **发消息**：插件 `ctx.send_msg` → `services['api_caller'] = IpcAdapter` → `RemoteApiCaller` → IPC `api.call` → 核心 `onebot_adapter` 经 WebSocket 发出。
- **跨进程事务**：`db.transaction()` 内调用自动携带 `tx_id`，核心 `RemoteTxManager` 在**同一事务连接**上执行，正常提交、异常回滚。
- **远程 REST 路由**：宿主侧 `ctx.register_api` 注册的路由，核心 Flask 挂远程 stub，请求经 `http.dispatch` 转发回宿主执行。
- **日志合并**：宿主日志经 IPC `log` 事件推到核心 `log_broker`，WebUI 可见。

### 5.3 终端命令转发（core → host）

终端输入只在**核心进程**启动（它是前台、占控制台；宿主子进程无交互 stdin，避免两个进程抢控制台）。
但用户插件与调度器在**宿主进程**，所以命令按归属路由：

- 每条终端命令带一个 `target`：`core`（默认，本地执行）/ `host`（转发宿主执行）/ `both`（两侧都跑，合并视图）。
- 核心终端遇到 `host` 命令时，经 `IpcServer.arequest_host('terminal.exec', {name, args})` 转发；
  宿主侧 `host_entry` 注册 `terminal.exec`，执行命令并把 `print` 输出重定向捕获后回传。
- 归属为宿主的命令：`plugins`（列出插件）、`enable` / `disable` / `reload`（插件管理）、`tasks`（调度器）；
  `status`、`plugins` 标为 `both`，一次看全两侧视图。

```text
> plugins
--- 核心进程 ---
已加载插件 (1 个): onebot_adapter ...
--- 宿主进程 ---
已加载插件 (3 个): session / scheduler / 你的业务插件 ...
```

单进程模式（`role=standard`）没有下游进程，所有命令一律本地执行，行为与旧版完全一致。
`help` 会在宿主侧命令后标注 `[宿主进程]` / `[核心+宿主]`。

## 6. 生命周期与监督

- 入口 `main.py`：开启双进程时走 `CoreRuntime(config_path).run()`，否则单进程 `asyncio.run(amain())`。
- `CoreRuntime._amain`：先启动 IPC accept 线程 → 加载核心插件 → `spawn` 宿主进程 → 起宿主看门狗。
- **宿主崩溃**：看门狗每 2s 检测，崩溃则 `terminate` 旧进程并按限流重启（窗口内最多 `max_restarts` 次，超过则核心停机）。
- **核心崩溃**：宿主侧父进程看门狗（`os.getppid()` 比对）检测到核心退出，优雅关闭宿主。
- 关闭信号 `SIGINT/SIGTERM`：两侧均注册，`multiprocessing.freeze_support()` 防止 spawn 子进程重复执行入口。

## 7. 配置项

`config.yaml → dual_process`：

| 项 | 默认 | 说明 |
|----|------|------|
| `enabled` | `false` | 总开关，默认关闭，行为完全不变 |
| `core_plugins` | 由 `process:'core'` 标记自动判定 | 核心进程加载的官方插件白名单 |
| `max_restarts` | `5` | 宿主崩溃重启限流：窗口内最大重启次数 |
| `restart_interval` | `30` | 限流窗口（秒），窗口外计数重置 |

## 8. 已知限制（Phase 2 待补全）

- `db.get_connection()` 在双进程下**不支持**（真实连接对象无法跨进程序列化），会抛 `NotImplementedError`；
  应改用 `db.transaction()` 或 `ctx` 的 `db_query`/`db_execute` 系列。
- `transaction(conn=...)` 不支持传入外部连接（跨进程无法序列化连接对象）。
- 目前单宿主（一个宿主进程）。多接入端并发、宿主热替换、跨机部署（改为 TCP/Unix socket）均未实现。
- 单元测试覆盖薄弱（见第 9 节），生产前建议先跑手动冒烟。

## 9. 测试

### 9.1 自动化测试

`tests/test_dual_core.py` 覆盖分层：

- **分派层**：`_read_plugin_process_tag`（AST 解析）对 `onebot_adapter` 等返回 `'core'`、对 `scheduler` 返回 `None`；
  `_core_plugin_is_core_side` 验证白名单优先于标记。
- **协议层**：进程内起 `IpcServer` + `IpcClient`，`register` 一个方法后 `call` 往返、超时与 `notify`/`on` 事件订阅。
- **代理层**：`RemoteDatabase.get_connection()` 抛 `NotImplementedError`。

运行：

```bash
python -m pytest tests/test_dual_core.py -v
# 或
python tests/test_dual_core.py
```

### 9.2 手动冒烟（推荐）

开启双进程并观察两进程是否正确拉起、事件是否贯通：

```bash
# 1. 在 config.yaml 末段设置 dual_process.enabled: true
# 2. 启动（建议前台，观察日志）
python main.py

# 期望日志：
#   核心：IPC 宿主已连接 / 框架启动完成，等待消息...
#   宿主：进程2 已注册 / 插件加载完成
# 3. 另开终端观察宿主进程 pid（应是 CoreRuntime spawn 出的独立进程）
# 4. 用 http_inject 或默认接入端发一条消息，确认宿主侧插件正常响应
# 5. 杀掉宿主进程，观察核心是否在限流内自动重启它（日志出现 “宿主进程重启”）
# 6. 关闭：Ctrl+C，两侧应一起优雅退出
```

### 9.3 回归约束

- 默认 `enabled: false` 时，单进程路径不得有任何行为变化（所有 `role='standard'` 分支保持原样）。
- 改动 `framework/ipc/*` 或 `core.py` 角色分派后，必须重跑 `tests/test_dual_core.py` 与 `tests/test_plugin_imports.py`。
