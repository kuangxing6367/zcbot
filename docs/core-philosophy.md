# 内核设计哲学

> 本仓库所有架构决策都遵循下面这组信条。改 `framework/`、加官方插件、写用户插件之前，先读它。
> 它不是"建议"，而是内核存在的理由：**内核只负责"运转"，其余都是"挂在运转各环节上的扩展"**。

## 为什么需要一份设计哲学

ZCBOT 不是一个"什么都往里塞"的框架。它的全部价值在于：**用极小的、稳定的内核，托住无限可变的扩展**。
没有这层信条，代码会慢慢滑向"在内核里加特例、为某个接入端写 `if`、把业务塞进 `framework/`"——届时内核不再稳定，扩展也不再自由。
这份文档是给"该把代码放哪、该不该改内核"这个问题的最终裁决依据。

## 十条信条

### 1. 最小必要内核

`framework/` 只做四件事：加载扩展、路由事件、提供公共服务（数据库 / 权限 / 服务注册 / 事件总线 / 运行时上下文）、维护扩展点契约。
**不实现任何具体业务，也不含任何 OneBot 代码。**

- 落点：`framework/core/base.py` 的 `Framework.__init__` 只装配 `loader / event_bus / router / db / hooks / services`；OneBot 实现在 `core_plugins/onebot_adapter/`。
- 判断标准：一段代码若"知道某个具体平台 / 某个业务"的细节，它就不该在内核里。

### 2. 扩展点即契约

`HookRegistry`（`framework/hooks.py`）是内核真正区别于普通框架的地方。
内核在 `lifecycle / http / event / command / message / action / router` 各环节预留插槽，`ctx.hook(point, handler)` 即可往里插逻辑；同名重复注册自动去重，handler 可为 `def` 或 `async def`。
这是"允许几乎各个地方插入"的实现机制，也是内核对扩展开放的**唯一正式通道**。

- 扩展点清单见 [扩展点（Hook 系统）](hooks.md)。

### 3. 三层叠加

能力分三层，越往下越不可关：

1. **内核（`framework/`）**：不可关，提供运转本身。
2. **官方扩展（`core_plugins/`）**：随项目的基础能力，开关集中在 `core_plugins.yaml`。
3. **用户扩展（`plugins/`）**：你的业务逻辑，每个一个目录。

换形态（机器人 ↔ 纯定时 ↔ HTTP 宿主）**只动 `core_plugins.yaml`**，骨架（权限、后台、持久化、会话）原样保留。

### 4. 协议无关、事件归一

内核不区分事件来自哪个接入端（OneBot / Telegram / Discord / HTTP 注入 / WS 客户端 / 双进程 IPC），也不区分逻辑来自哪个扩展。
所有源先归一化为 `Event`，再走同一条管线。接入端差异被 `ProtocolAdapter` 契约（`framework/messaging/protocol.py`）吸收，内核不写死任何协议字段。

- 落点：连接状态由各接入端的 `get_connection_info() / get_connected_bots()` 自描述；`protocol_adapter / api_caller` 是协议无关的通用槽位。

### 5. 热路径零开销

扩展点系统跑在每一条消息的热路径上（每事件约 5 次 `trigger`，绝大多数点位无人注册），因此**空触发必须零成本**。

- 落点：`HookRegistry` 用**写时复制快照**——注册/注销低频、触发高频；变更时失效快照，热路径无锁读不可变 tuple。无订阅者的点位直接返回空结果，省去循环与短路判断。
- 含义：新增扩展点时，别在热路径引入锁或分配；保持"无订阅即无代价"。

### 6. 优雅降级与故障隔离

系统在任何单点失败时**不崩、不丢关键能力、能自愈**：

- **DB 降级**：SQLite/MySQL 不可用时，降级到 `data/db` 下的 JSON/YAML 文件运行（`db.degraded`）；`database.type: debug` 用本地模拟 SQL（仅开发）。
- **进程隔离**：`core/host` 双进程把用户扩展故障隔离在宿主进程，接入端与 Web 在核心进程仍在线。
- **运行期自愈**：依赖自愈（补装后再试）、孤儿任务/命令清理（`self_check_orphans`）、**插件级内存监控**（每 3s 采样，单插件连续 2 次超 `plugin.max_memory_mb`（默认 64MB）即自动卸载）、**进程级内存看门狗**（每 `memory.check_interval`（默认 30s）采样 RSS，超 `memory.limit_mb`（默认 120MB）做 GC+trim 三层回收，只回收不卸载）、可靠热重载（心跳增量注册）。
- **事件缓冲兜底**：`EventBuffer` **L1** 内存主缓冲（512KB / 2000 条）→ **L4** 写缓冲（4MB 攒批，批量落盘降 sqlite 单写者压力）→ **L2** sqlite 持久化（`data/event_buffer.db`）→ **L3** 内存兜底（4MB）→ 全满告警丢弃，**绝不因为缓冲满而阻塞适配器**（消费优先级为 L1 → L3 → L4 → L2）。

### 7. 短路语义

`before_*` 类扩展点返回 `False` 即短路，把"过滤 / 鉴权 / 黑名单 / 去重"前置到管线入口：

- `http.before_request` 返回 Response 即短路；
- `event.before_dispatch` 返回 `False` 丢弃事件；
- `command.before` 返回 `False` 跳过该命令；
- `message.before_send` 返回 `False` 取消本次发送；
- `router.before_route` 返回 `False` 跳过本次路由。

- 落点：`framework/hooks.py` 的 `_SHORT_CIRCUIT` 表。短路扩展点遇到首个 `False` 提前终止，不再触发后续 handler。

### 8. 单一事实源

配置两份，权威唯一：

- `config.yaml`：框架全局设置（数据库、日志、安全、插件目录）。
- `core_plugins.yaml`：**官方扩展开关与配置中心**，启动自动扫描 `core_plugins/` 同步/回写/合并进主配置。
- **开关权威性**：`core_plugins.yaml` 每块的 `enabled` 以 `config.yaml` 的 `core_plugins:` 段为准并回写；两处全关即真不加载；`enabled: "false"` 等字符串按严格布尔解析。
- 含义：别在代码里硬编码"某插件是否开启"；读 `fw.config` 即可，真相在配置。

### 9. 同步/异步双轨

同一扩展点要在两种上下文生效，因此分两套触发：

- `trigger_async`：在事件循环内调用，可安全 `await` async handler。
- `trigger_sync`：在 Web / 线程上下文（Flask 请求）调用；sync handler 直接执行，async handler 经内核 loop `run_coroutine_threadsafe` **fire-and-forget，绝不阻塞请求线程**。

- 含义：扩展点 handler 要么写成纯 sync，要么写成 `async def` 并在内部自行 `await`；不要假设自己运行在哪个线程。

### 10. 声明式权限

权限是节点式模型（`plugin.action.sub` 形式，三态：授予 / 显式否决 / 未定义），带组继承、上下文、时效、通配。

- 命令级声明式：`@ctx.command(..., require_perm="x.y", require_level="admin")`，框架自动拦截，handler 内无需手写判断。
- 运行时：`ev.has_perm(node) / ev.check_perm(node)`（三态），上下文自动从事件构造。
- 含义：权限判定走引擎，别在业务里手写 `if user == admin`。

## 铁律（落到代码上的判断）

新增任何能力，先问一个问题：

> **它是内核的"最小必要能力"，还是某个扩展的责任？**

- 若是**内核最小必要能力**（加载、路由、公共服务、扩展点契约）→ 放 `framework/`。
- 若是**某个平台 / 某种业务** → 放 `core_plugins/`（官方）或 `plugins/`（用户），通过 **hook / 服务注册表** 接入。
- **禁止**：往内核塞业务、为某个接入端写 `if` 特判、在 `framework/` 里引用具体协议字段。

这条规则是这份哲学的全部用意：**守住内核的小，才能换得扩展的自由。**

## 延伸阅读

- 架构链路与启动时序：[架构详解](architecture.md)
- 扩展点清单与签名：[扩展点（Hook 系统）](hooks.md)
- 给 AI 编码助手的浓缩版：[框架结构（LLM）](llm-framework.md)
