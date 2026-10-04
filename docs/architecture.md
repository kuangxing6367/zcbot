# 架构详解

你在群里发出一条命令，插件几乎立刻回复——这中间经历了分层加载、事件归一、路由匹配和一次服务注册表查询。把这条链路拆开看，才能在插件出问题时知道该往哪一层查。

> **设计哲学先行**：本仓库所有架构决策都遵循一组内核设计信条，见 [内核设计哲学](core-philosophy.md)。改框架、写扩展前先读它——它决定了"代码该放哪、该不该动内核"。

## 分层总览

```
┌─────────────────────────────────────────────────┐
│      Core Framework（framework/ 极简内核）          │
│  插件加载器 loader · 事件总线 event_bus          │
│  消息路由 router · 上下文 ctx · 数据库 db         │
└──────────────────────┬──────────────────────────┘
                       │ 先加载，提供基础服务
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│onebot_adapter│ │  scheduler   │ │ session/webui│  core_plugins/（可开关）
└──────────────┘ └──────────────┘ └──────────────┘
                       │ 后加载
                       ▼
                ┌──────────────┐
                │  plugins/    │ 用户插件（每个一个目录，含 main.py）
                └──────────────┘
```

上图以默认接入端 `onebot_adapter` 为例；`http_inject`、自写 `ProtocolAdapter` 都在同一位置把事件归一化后送入内核，后续流程完全一致——内核不区分事件来自哪个接入端。

## 启动时序

`main.py → Framework.start()`（`framework/core/base.py` 的 `Framework`；调度/心跳/看门狗在 `framework/core/runtime.py`）：

1. 打印安全提示：`_warn_insecure_config()`——仅当 Web 面板 `web.host` 为 `0.0.0.0`/`::` 时告警，建议改为 `127.0.0.1`；各接入端的令牌提示由适配器注册时给出，内核不在此处理 token；
2. 启动基础后台循环：路由表周期刷新、事件队列消费 worker、L4 写缓冲 flush、统计批量写库器、群成员同步批量写库、心跳任务、内存看门狗（均在加载插件前就绪，不阻塞插件加载）；
3. 注册终端命令并启动内置终端（仅非宿主进程启动交互输入）；
4. `_load_plugins_sync()`（同步、放入后台线程执行）：
   a. `_load_core_plugins()`：按 `core_plugins.yaml`（启动时由 `_autoload_core_plugins` 合并进主配置 `core_plugins` 段）的开关加载官方插件，它们向服务注册表注册基础能力；
   b. 创建 `data/plugins_dat/`，把旧版散落在代码目录的配置迁移过去；
   c. `plugin_loader.load_all()`：发现并加载全部未被禁用的用户插件（依赖检查/自动安装 → 建合成包 → 预载子模块 → 执行 main.py）；
   d. 依赖自愈：首轮缺依赖失败的插件，补装后再尝试一次；
   e. 逐个 `register_commands()`：执行 `register(ctx)`，落库命令/任务/卡片，触发 `on_loaded`；
5. `_finalize_startup()`：路由表预热（`router._rebuild_routes`）→ 广播 `system.plugin.loaded` → 触发 `LIFECYCLE_STARTUP` 扩展点 → 置就绪标记。

关闭 `Framework.stop()`：依次取消后台插件加载、触发 `LIFECYCLE_SHUTDOWN` 扩展点、停止终端、事件缓冲、统计写库器、群成员同步、路由表刷新、心跳、内存看门狗、官方插件服务（协议/Web/调度），最后广播 `system.plugin.unloaded` 并关闭数据库线程池。

## 消息处理流程

```
OneBot 客户端
    │  WebSocket 反向连接
    ▼
WebSocket 服务端 (core_plugins/onebot_adapter)
    │
    ▼
事件标准化为 Event (framework/messaging/event.py)
    │
    ▼
框架核心 (framework/core/ · base · dispatch · runtime)
    │
    ├─→ 原始消息处理器 (ctx.on_raw_message)
    │       │  返回 True 则被接管，流程终止
    │       ▼  未接管继续
    ├─→ 消息路由器 (router.py)
    │       ├─→ 插件命令匹配（按 priority 升序）
    │       │       │  命中且未 continue_route → 终止路由
    │       │       ▼  未命中（且未 stop_event）
    │       ├─→ message 事件广播（ctx.on("message")，文本统一监听通道）
    │       │       │  handler 返回 True → 终止路由
    │       │       ▼  未接管
    │       ├─→ 关键词自动回复（dynamic_commands）
    │       │       ▼
    │       └─→ 未匹配扩展点（ROUTER_MESSAGE_UNMATCHED）
    │
    ├─→ 通知事件 notice
    └─→ 请求事件 request
```

命令/事件处理器支持 `def`（转线程）与 `async def`（事件循环内）两种写法；
`event.stop_event()` 可截断后续传播。

## 服务注册表

极简内核通过 `services` 注册表解耦官方插件与用户插件：

```python
# 官方插件侧：注册能力
fw.services.register('api_caller', api_caller)

# 用户插件侧：按需取用（可能尚未就绪，必要时监听 system.plugin.loaded）
caller = ctx._framework.services.get('api_caller')
```

| 服务名 | 提供者 | 说明 |
|--------|--------|------|
| `protocol_adapter` | 当前接入端（onebot_adapter / http_inject / ws_client / qq_official / telegram / discord / IPC） | 协议适配器抽象 |
| `api_caller` | 当前接入端 | 通用动作调用器（`call/acall`） |
| `onebot_api` | onebot_adapter | OneBot API 面向对象封装（`ctx.actions`/`ctx.onebot` 优先取它，否则 ActionProxy 兜底） |
| `ws_server` | onebot_adapter | WebSocket 服务端（兼容键；`fw.ws_server` 优先取接入端自报实例） |
| `scheduler` | scheduler | 定时任务调度器（APScheduler） |
| `session_manager` | session | 多轮会话管理器 |
| `web_server` | webui | Web 管理后台服务 |
| `http_api` | http_api | 独立对外 HTTP API（默认关闭） |
| `rust_accel` | rust_accel | Rust 加速层（可选高性能事件处理接入，与 onebot_adapter 互斥） |
| `html_assembler` | html_assembler | HTML 模板渲染转图片服务 |
| `llm_core` | llm_load | LLM 核心服务（Provider/Agent/MCP 门面） |

`protocol_adapter` / `api_caller` 是协议无关的通用槽位：默认由 onebot_adapter 填充；换成其它接入端后由新接入端填充，业务插件的取用方式不变。
连接页 `/api/connection` 与仪表盘状态由各接入端的 `get_connection_info()` / `get_connected_bots()` 自描述，内核不写死任何协议字段。

详见 [ServiceRegistry](services.md)。

## 插件加载机制（要点）

- 每个用户插件的主模块注册为 `plugin_<插件名>`，它同时是一个带 `__path__`
  的合成包，因此插件内部可以用 `from .xxx import Y` 做相对导入；
- 子模块同时拥有 `plugin_<名>.<模块>`（相对导入）、`plugin_<名>_<模块>`（旧唯一名）、
  `<模块>`（短名绝对导入）三个名字，指向同一对象；
- 卸载按模块 `__file__` 前缀一次性扫净 `sys.modules`。

完整原理、导入规则、热重载行为、排错见
[插件加载与模块机制](loader.md)。

## 插件优先级

数字越小越先加载、越先收到原始消息、命令匹配越优先。

| 优先级 | 典型用途 |
|--------|----------|
| 0 | 官方插件（onebot_adapter、session 等） |
| 1–20 | 基础设施类用户插件（session_waiter、message_guard、依赖图等） |
| 50 | 默认（大多数用户插件） |
| 100+ | 低优先级/展示类（如 help=100） |

## 心跳、增量注册与热重载

- 每 `plugin.heartbeat_interval`（默认 60s）执行一次 `heartbeat_register()`：
  扫描插件目录 `.py` 文件的最大 mtime，仅对发生变化的插件重新执行
  `register(ctx)`，不重新 import；
- 因此改了注册结构（新增命令/任务）靠心跳即可刷新，
  而改了函数体逻辑需要 Web 面板的完全重载（unload + load，重新读盘）；
- 心跳后路由缓存失效并兜底重建，保证命令表与内存快照一致。

## 自检与自愈

- 依赖自愈：启动时对缺依赖导致加载失败的插件，在补装依赖后自动再试；
- 孤儿自检 `self_check_orphans`：周期性清理代码目录已不存在的命令/任务，
  以及调度器里属于未加载插件的幽灵任务；
- **插件级内存监控**（`framework/loader/runtime.py` 的 `_start_memory_monitor`）：每 3 秒采样一次，按模块全局变量估算单插件内存占用，**连续 2 次超过 `plugin.max_memory_mb`（默认 64MB）即自动卸载该插件**（进程总内存超阈值 1.5 倍时另出告警）；
- **进程级内存看门狗**（`framework/core/runtime.py` 的 `_memory_watchdog_loop`）：每 `memory.check_interval`（默认 30s）采样进程 RSS，超过 `memory.limit_mb`（默认 120MB）时做三层防御——超限硬清（清框架级缓存 + GC + trim）、峰值回落主动 trim、检测到「内存地板」进入持续回收模式；**只回收内存，不卸载插件**。

  两者是彼此独立的两套机制：前者按插件卸载，后者按进程回收，互不替代。

## 事件总线

```python
ctx.emit("user_sign_in", {"user_id": 123456})      # 同步
await ctx.aemit("user_sign_in", {...})             # 异步
ctx.on("notice.group_increase", on_member_join)    # 订阅（同步/异步 handler 均可）
```

### 内置事件

| 事件名 | 触发时机 |
|--------|----------|
| `message` | 文本消息且命令未匹配时广播（关键词自动回复与未匹配扩展点在 message 事件之后触发）；`ctx.on("message")` 的 handler 返回 True 可终止路由 |
| `notice.group_increase` | 新成员入群 |
| `notice.group_decrease` | 成员退群 |
| `request.friend` | 好友请求 |
| `request.group` | 加群请求 |
| `meta.heartbeat` | OneBot 心跳包（`meta_event` 的 `sub_type=heartbeat`） |
| `system.plugin.loaded` | 本轮插件全部加载注册完成（payload 含插件列表） |
| `system.plugin.unloaded` | 框架停止时广播 |
| `after_message_sent` | 消息发送完成后 |

插件可自定义任意事件名，通过 `emit/aemit` 在插件间解耦通信。
