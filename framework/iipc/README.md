# IIPC 内核链路（zcbot 核心 · 源码分发）

> **位置**：`framework/iipc/` —— 已是 zcbot 框架核心的一部分，以**源码方式**随框架分发。
> 本目录只含源码（约 410KB），**不含任何编译产物**；首次运行自动编译，也可显式 `build()`。
>
> 它与原 asyncio 框架是**并列的两条启动路径**：插件写法、DB 语义、事件契约 1:1 对齐，
> 但进程拓扑不同（插件在独立进程、SQL 在 Go 侧执行，不占 Python GIL）。
> **不带 `--iipc` 时框架行为完全不变。**

## 启动

```bash
python main.py --iipc                 # 框架主入口（推荐，仓库根目录执行）
python -m framework.iipc              # 等价的独立模块入口
```

首次运行会自动编译 Go 内核与 Rust 协议端（需 go / cargo；可用
`ZCBOT_GO_BIN` / `ZCBOT_CARGO` 指定路径）。只想编译不启动：

```bash
python -c "from framework.iipc import build; print(build())"
```

终端命令：`status | plugins | logs | send <文本> | quit`。

### 配置（`config.yaml` 的 `iipc:` 段）

端口**不写死在代码里**。本机往往同时跑着主框架（WebUI / OneBot 反向 WS），
改配置即可避开占用：

```yaml
iipc:
  ipc_addr: "127.0.0.1:18730"   # Go 内核 IPC 监听（协议端与插件连它）
  db: ":memory:"
  admin: ""                     # 管理员 user_id，逗号分隔
  plugins_dir: ""               # 留空 = framework/iipc/plugins
  onebot_url: ""                # 正向 WS：适配器主动连
  onebot_listen: ""             # 反向 WS：监听等连入
  onebot_token: ""
  no_rust: false
  max_rss_mb: 1024
```

优先级：**命令行 > `config.yaml` > 环境变量 > 内置默认**。

### 常用参数

```
--ipc-addr ADDR      内核 IPC 监听（默认 config iipc.ipc_addr / 127.0.0.1:18730）
--plugins-dir DIR    插件目录（默认 framework/iipc/plugins）
--admin IDS          管理员 user_id（逗号分隔；也可用 ZCBOT_ADMINS）
--db DSN             SQLite DSN（默认 :memory:）
--onebot-url URL     正向 WS：适配器主动连 ws://host:port/path
--onebot-listen ADDR 反向 WS：监听 [ip:]port 等 OneBot 实现连入（默认 127.0.0.1:6701）
--onebot-token TOKEN OneBot access-token
--no-rust            不启动 Rust 协议端（send 类动作不可用，事件注入仍可）
--max-rss-mb N       子进程总 RSS 告警阈值 MB（只告警不杀；0=关闭）
```

等价环境变量：`ZCBOT_ONEBOT_URL` / `ZCBOT_ONEBOT_LISTEN` / `ZCBOT_ONEBOT_TOKEN`
/ `ZCBOT_IIPC_ADDR` / `ZCBOT_ADMINS`。
`wss://` 会**明确报错**（Rust 侧 std 无 TLS，需前置反代终止），绝不静默失败。

## 目录

```
framework/iipc/
├─ __init__.py        run(argv) / build() 入口（供 main.py 与 -m 调用）
├─ __main__.py        python -m framework.iipc
├─ launcher.py        编排：日志总出口 + 终端 REPL（不承载业务）
├─ iipc_log.py        日志格式（读 config.yaml 的 log: 段，与框架同一出口）
├─ go_kernel/         Go 内核源码（事件总线 / SQLite / 插件编排 / 调度 / 权限）
├─ rust_adapter/      Rust 协议端源码（OneBot 11，正向+反向双模式 WS）
├─ py_sdk/            Python SDK：sdk.py / event.py / framework.plugin 兼容层
├─ plugins/           示例插件（echo、api_parity）
└─ tools/             联调与基准脚本
```

## 1:1 目标与现状

目标：**真 zcbot 插件拖进来零/近零改动运行**。已对齐的面：

| 面 | 状态 | 说明 |
|---|---|---|
| 插件 API（`PluginContext`） | ✅ | `command/on/task/api/dashboard_card/hook/webui/actions/onebot/create_table/db_*/get_config/has_perm/wait_for/create_session/audit_log/run_async/get_data_dir…` |
| 模块级装饰器 | ✅ | `from framework.plugin import command, on, task…`（合成包 `plugin_<id>.main`，支持插件内相对导入） |
| 模块级 `ctx` 注入 | ✅ | 对齐 `loader/base.py:231` 的 `module.ctx = ctx`；官方插件在 handler 内裸用 `ctx` 可用 |
| Event 对象 | ✅ | 属性 + 惰性权限 + 富媒体全家 + dict 式访问 + `is_group/is_private/sender_nickname/sender_card/at_all` + `stop_event/continue_route` |
| 命令匹配语义 | ✅ | 复刻 `router_match.py`：正则/别名/`/` 前缀宽容/黑名单/require_level/require_perm/命中即止 |
| SQL 方言 | ✅ | MySQL `%s`→SQLite `?`（字面量感知）；JSON 数字参数收敛 int64（修亲和性） |
| 核心表结构 | ✅ | 19 表 + 9 索引，`--admin` 同步 `users.role='super'` |
| cron 调度 | ✅ | 5 段（`* / , -`，日周 OR 语义）+ `task.tick` 定向推送；**handler 零参调用** |
| 命令扩展点 | ✅ | `command.before`：命中且权限通过后、执行前触发；单参 payload dict，返回 `False` 跳过该命令 |
| 多轮会话 | ✅ | `wait_for/create_session`，同 user+同群/私聊配对 |
| 群级插件开关 | ✅ | 内存镜像 + `group_plugin_settings` 持久化，事件扇出时真过滤 |
| 节点式权限 | ✅ | port 自 `framework/perm`：默认组/内置角色链/BFS 继承/weight 降序合并 |
| OneBot 11 协议端 | ✅ | Rust std-only WS：客户端（正向）+ 服务端（反向）双模式，echo 关联 api.call |
| 内存看门狗 | ✅ | launcher RSS 监控，**只告警不杀** |

### 全 API 面实测（tools/api_surface_check.py，154 项）

`plugins/api_surface` 把 SDK 全部公开 API 真实调用一遍（PluginContext 68 + ActionProxy 24
+ Event 属性 37 / 方法 8），逐项记录 `(name, ok, detail)` 落库。
**插件只报真实返回值，不自我判定**——判定权在 `api_surface_check.py`，按预先写死的期望值断言，
避免"插件说自己 ok 就算 ok"的自证循环。

覆盖：DB 全族（含别名与坏 SQL 异常路径）、配置往返、权限/身份、群级开关、日志/审计/数据目录、
广播、文本提取、三种发送面、typed 快捷动作、ActionProxy 24 个动作、async 全族、
Event 37 属性 + 8 方法、`wait_for`/`create_session`。

跑全量面挖出的真 bug：
4. **`Event.has_perm` 参数错位** —— 误把 `group_id` 当 `context` 位置参数传（int 调 `.items()`
   抛错被 except 吞掉），且漏传 `role`。原框架是
   `perm.resolve(db, user_id, context_from_event(ev), ev.role)`。
   后果：`ev.has_perm('zcbot.role.super')` 对超管也返回 False。已按 `context_from_event`
   构造 `{bot, group, msgtype}` 并注入 `self.role`。

### 真·官方插件实测（tools/official_plugin_check.py，32 项）

| 拖入对象 | 来源 | 结果 |
|---|---|---|
| 每日签到 | `docs/指南/编写插件.md`「完整的签到插件」**逐字照抄** | 32/32 通过（`create_table`/`alias`/`task(cron)`/`on(notice)`/`%s` 方言/裸 `ctx`/`event.is_group` 全通） |
| LLM 黑名单 | `plugins/llm_blacklist` 真仓库官方插件（仅摘除 host 进程专属的 `ctx._framework`） | MySQL 风格 DDL、`require_superuser`、正则命令、async handler、`get_config` 全通 |

跑官方插件暴露并修掉的三个真缺口（**写测试时才发现，不是靠读代码想出来的**）：
1. **缺 `module.ctx = ctx`** —— 官方示例在 handler 里裸用 `ctx`，原框架由 `loader/base.py:231` 注入；
2. **缺 `ctx.create_table`** —— 官方示例建表入口，原框架 `ctx/db.py:35` + `ipc/remote_db.py:76`；
3. **缺 `Event.is_group`** —— 签到示例 `event.is_group` 直接 `AttributeError`；
   顺带补齐 `is_private/sender_nickname/sender_card/at_all`，并把 `first_*` 无值时
   由 `None` 改为 `{}`（对齐原框架，防 `first_image.get(...)` 崩）。

已知边界（诚实清单）：
- WebUI / console_server 等价物**未做**（IIPC 形态暂无）；
- SQL 方言只做 `%s`→`?`，未覆盖全部 MySQL 语法（DDL 由内核 `TranslateSQL` 走同一通道）；
- `pluginhost` fork 路径在 Windows 未验；
- `Event.perms/perm_groups/primary_group` 未提供（原框架依赖 `PermissionSet` 权限组展开，IIPC 暂无等价物）；
- 真业务插件仅验到「用户态插件」这一层：`core_plugins/scheduler`、`session` 这类
  依赖 `ctx._framework.services` 的**框架内建服务型插件跑不了**（IIPC 插件在独立进程，无 `_framework`）。

## 插件写法契约（拖真插件前必读）

三类处理器的参数形态以框架源码为准（2026-10-14 逐字核对）：

| 处理器 | 签名 | 说明 |
|---|---|---|
| 命令 handler | `(event, match)` | `event` 为 `Event` 对象；`match` 为正则 match 或 None |
| `on(point)` handler | `(event)` | 收 `Event` 单参 |
| `task` handler | **零参 `()`** | 框架 `scheduler._run_job` 直接 `handler()`；需要的 ctx 由插件在 `register(ctx)` 时自持 |
| `hook(point)` handler | 单参 payload dict | 框架 `hooks.trigger_async(point, payload)`；`command.before` 的 payload 含 `plugin/handler/command_id/pattern/event/match`，返回 `False` 跳过该命令 |

注意 `framework/plugin.py` 文档字符串里 `def before_command(event, ctx)` 的写法**与框架实现不符**——
实际调用点（`messaging/router_match.py`）传的是单参 payload dict，以调用点为准。

## 基准与联调

```bash
# 一键总跑：构建 + go test + e2e + 适配器联调 + 延迟探针 + 压力/故障注入
python framework/iipc/tools/run_all_bench.py
python framework/iipc/tools/run_all_bench.py --skip-build   # 只跑基准
python framework/iipc/tools/run_all_bench.py --only e2e     # 只跑指定步骤

# 单跑
python framework/iipc/tools/e2e_check.py      # 25 项：命令/权限/DB/会话/群开关/perm/task.tick/command.before
python framework/iipc/tools/adapter_e2e.py    # 13 项：正向 9 + 反向 4（假 OneBot ⇄ Rust ⇄ 内核 ⇄ 插件）
python framework/iipc/tools/latency_probe.py  # 单事件 事件→send_msg 回传耗时
python framework/iipc/tools/stress_check.py   # 16 项：畸形输入/吞吐延迟/掉线重连/插件被杀/异常路径
```

### 四个脚本各自覆盖什么（互补，不是重复）

| 脚本 | 性质 | 覆盖 |
|---|---|---|
| `e2e_check.py` | 功能断言 | 不经协议端：直连内核，验 1:1 API 面（命令/权限/DB/会话/群开关/perm/task/hook） |
| `adapter_e2e.py` | 功能断言 | 全链路 happy path：假 OneBot ⇄ Rust ⇄ 内核 ⇄ 插件，正向+反向双模式 |
| `latency_probe.py` | 单点性能 | 单条事件端到端回传耗时 |
| `stress_check.py` | **挨打测试** | 让系统出错：畸形帧、突发灌满、对端掉线、插件进程被杀、内核异常调用 |

前三个是"我的实现 对着 我的断言"（自证）；`stress_check.py` 是补的那半边——
它不假设系统正常，只看它挨打后会不会死、死了能不能自己爬起来。

`tools/onebot_fake.py` 是自带假 OneBot（Python std-only，零依赖），与评测表
`qpsbench.py` 同口径：**每个 API 调用都必须带 echo 应答**（不回响应会让内核
`CallAdapter` 干等超时，SDK 的 send flush 线程被卡住，后续动作批量全延迟）。

### 实测性能（本机 Windows，:memory: SQLite，echo 插件回显链路）

| 指标 | 实测值 | 说明 |
|---|---|---|
| 轻载单条延迟 P50/P95 | **3ms / 7ms** | 限速发送、队列不积压时的真实体验 |
| 突发 1000 条端到端 | **0.26s，零丢失（3860 条/秒）** | 稳态，含大样本交叉验证 |
| 掉线→重连恢复 | 2s 级 | 适配器断线重连策略生效，恢复后命令可用 |
| 插件被 kill→重启 | 秒级 | 内核 `plugin_mgr` 自动重启，命令仍可用 |

**三个口径互不替代**（`run_all_bench.py` 末尾自动汇总，别混着比数字）：

1. **延迟** = 轻载限速、队列不积压时的单条体验；起点取事件**入队时刻**
   （不是取出时刻，否则把排队时间算没了）；
2. **吞吐** = 饱和灌入的稳态产能，按**端到端 span**（首条发出→末条回显）算。
   用「首尾回复时间差」算出来的是产出速率而非系统吞吐，会虚高一个量级；
3. **可靠性** = 丢失率 + 是否存在 **15s 级卡顿**（`api.call` 响应丢失会让内核干等
   `defaultCallTimeout=15s`，轻载下测不出来）。

> 吞吐数字是"事件→回显"链路的，不含真实业务落库；换更重的插件会显著下降。
> 基准脚本端口**全部动态分配**（`tools/portutil.py`，bind :0），不会占用本机正在跑的
> WebUI(:8080) / OneBot 反向 WS(:6830) 等端口。

## 架构与关键教训

```
python main.py --iipc
  └─ framework/iipc/launcher.py：日志总出口 + 终端 REPL（不承载业务）
       ├─ go_kernel/      Go 内核：事件总线 / SQLite / 插件进程编排
       ├─ rust_adapter/   Rust 协议端：OneBot 协议层（连入内核）
       └─ plugins/        Python 插件：每插件独立进程（IIPC 协议）
```

三种 IIPC 通道（对比旧 IPC 逐调用同步往返的 8× 削弱）：
- **事件 push**：内核 → 插件，单向广播，不等响应；
- **动作 flush**：插件 → 内核，本地队列 + 5ms 聚合成一次 `msg.send_batch`；
- **阻塞 call**：仅必须拿返回值时用（`db.query` / `api.call` / `config.get`）。

Rust 协议端并发模型（**关键教训，实测**）：WS 连接读句柄由读线程独占，
写句柄（`Arc<Mutex<TcpStream>>`）短锁共享。"持连接锁阻塞读"会让内核读线程发
`api.call` 时抢不到锁，整条回传链死锁（症状：send 全部 14s 超时）。

`api.call` 的 echo 关联（**第二个并发教训，实测**）：必须先登记
`pending.insert(echo, id)` **再** `send_text`，不能反。反过来时，对端可能在
本线程写完帧之前就回响应，读线程查不到 echo 就把响应丢了 → 内核干等 15s
（`defaultCallTimeout`）→ 表现为**突发高并发下间歇性 15 秒卡顿**（轻载永远
复现不了，因为轻载下登记总是先于响应）。`stress_check.py` 的
"突发无 15s 级卡顿（span<3s）"就是盯着这个的。

## 示例插件

- `plugins/echo/`：1:1 写法演示（`ctx.command/on/task` 实例形态、`%s` 占位、
  模块级 `on_error`、require_admin、DB 往返、通用 API 通道、配置持久化）。
- `plugins/api_parity/`：验收插件（模块装饰器半边 + register(ctx) 半边 +
  `Event.ctx_send`），覆盖正则命令/别名/会话/群开关/actions 代理/dict 访问/
  task 零参执行/command.before 返回 False 跳过命令。

拖真 zcbot 插件进来：放 `plugins/<id>/`（含 `plugin.json`），内核自动拉起；
模块级装饰器写法由 `py_sdk/framework/plugin.py` 兼容层承接。
