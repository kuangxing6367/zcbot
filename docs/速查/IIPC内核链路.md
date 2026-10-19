# IIPC 内核链路（给 LLM）

> 只在 `python main.py --iipc` 时生效。**不带该开关，主框架行为完全不变**——
> 它是与主框架（单进程 / `dual_process` 双进程）**并列的第三条启动路径**，不是替代品。

## 是什么

一句话：**把事件总线、DB、插件编排搬到 Go 进程，插件仍用 Python 写并跑在独立进程**。

| 进程 | 语言 | 职责 |
| ---- | ---- | ---- |
| Go 内核 `framework/iipc/go_kernel` | Go | 事件总线、SQLite、插件编排、权限/会话/群开关 |
| Rust 协议端 `framework/iipc/rust_adapter` | Rust | OneBot 11 协议层（正向 WS 客户端 或 反向 WS 服务端） |
| 插件 `framework/iipc/plugins/*` | Python | 每个插件独立进程，经 IIPC 协议（TCP + 行分隔 JSON）连内核 |

与 `dual_process` 的区别要分清：`dual_process` 是**同一套 Python 框架拆成两个 Python 进程**
（核心进程 + 宿主进程，用标准库 IPC）；IIPC 的内核是 **Go 进程**，DB 在 Go 侧执行，
**不占 Python GIL**。两者实验性质、代码路径都不同，性能数字**不可横向比**。

## 启动

```bash
python main.py --iipc                  # 全栈 + 进入终端
python -m framework.iipc --iipc        # 等价
python -c "from framework.iipc import build; build()"   # 只编译不启动
```

**以源码方式分发**：仓库里只有源码（约 410KB），首次运行自动编译；
产物 `go_kernel/*.exe`、`rust_adapter/target/` 已进 `.gitignore`。

## 配置（`config.yaml` 的 `iipc:` 段）

端口**不要在代码里写死**。本机往往同时跑着主框架（WebUI `web.port`、OneBot 反向 WS
`onebot.reverse_ws_port` 等），改配置即可避开占用。

```yaml
iipc:
  ipc_addr: "127.0.0.1:18730"   # Go 内核 IPC 监听（协议端与插件连它）
  db: ":memory:"                # :memory: 或文件路径
  admin: ""                     # 管理员 user_id，逗号分隔
  plugins_dir: ""               # 留空 = framework/iipc/plugins
  onebot_url: ""                # 正向 WS：适配器主动连 ws://host:port
  onebot_listen: ""             # 反向 WS：监听 [ip:]port 等连入
  onebot_token: ""
  no_rust: false                # true = 不起协议端（send 不可用，事件注入仍可）
  max_rss_mb: 1024              # 只告警不杀
```

优先级：**命令行 > `config.yaml` > 环境变量 > 内置默认**。
（环境变量：`ZCBOT_IIPC_ADDR` / `ZCBOT_ONEBOT_URL` / `ZCBOT_ONEBOT_LISTEN` / `ZCBOT_ONEBOT_TOKEN` / `ZCBOT_ADMINS`）

## 写插件：与主框架的异同

**相同的**（1:1 对齐，照抄现有写法即可）：

- `register(ctx)` + `ctx.command / on / task / api / hook / db_* / get_config / has_perm / wait_for / create_session / get_data_dir`
- 模块级装饰器 `from framework.plugin import command, on, task …`
- Event 对象（属性 + 富媒体 + `stop_event()`）
- SQL 用 `%s` 占位符，内核负责方言翻译成 `?`（SQLite）
- 命令语义：命中后**不**广播 `message`；`command.before` 返回 `False` 跳过命令；
  权限不足回「权限不足（需要 admin 权限，当前身份: {role}）」

**不同的**（踩过坑的）：

1. **handler 里裸用 `ctx` 可以**——IIPC 已对齐 `module.ctx = ctx` 注入（原框架在 `loader/base.py`）。
2. **内建服务型插件跑不了**：依赖 `ctx._framework.services` 的（如 `core_plugins/scheduler`、
   `session`）是 host 进程内 API，IIPC 插件在独立进程没有 `_framework`。能对口的只有 `plugins/*` 用户态插件。
3. **同步/异步别混**：`call_async` 是给同步 handler 用的；在 async handler 里 `fut.result()`
   会阻塞插件自己的事件循环，必然超时。
4. 插件进程的 **stdout 不进内核日志**。插件内异常要自己落文件才看得到。

**未覆盖的边界**（诚实清单）：`Event.perms/perm_groups/primary_group` 未提供（原框架依赖
`PermissionSet` 权限组展开，IIPC 无等价物）；无 WebUI / console_server 等价物；
`pluginhost` fork 路径在 Windows 未验；SQL 方言只做 `%s`→`?`；尚未对接真 OneBot（NapCat/Lagrange）。

## 基准测试

```bash
python framework/iipc/tools/run_all_bench.py              # 全跑（含编译）
python framework/iipc/tools/run_all_bench.py --skip-build  # 只跑基准
python framework/iipc/tools/run_all_bench.py --only e2e,stress
```

| 脚本 | 项数 | 测什么 |
| ---- | ---- | ------ |
| `e2e_check.py` | 25 | happy path：注册 → 事件 → 命令/会话/DB/权限/群开关 |
| `official_plugin_check.py` | 32 | 拖**真官方插件**跑（docs 签到示例 + `plugins/llm_blacklist` 逐字） |
| `api_surface_check.py` | 154 | 穷举调用 SDK 全部公开面（插件只报返回值，判定在脚本侧） |
| `adapter_e2e.py` | 13 | Rust 协议端双模式（正向 / 反向 WS） |
| `stress_check.py` | 16 | 挨打类：畸形输入 / 突发 / 掉线重连 / 插件被 kill / 异常路径 |
| `latency_probe.py` | — | 首条回传延迟探针 |

**三个性能口径互不替代**（`run_all_bench.py` 末尾会汇总）：

1. **延迟** = 轻载限速、队列不积压时的单条体验；起点取事件**入队时刻**（不是取出时刻，
   否则把排队时间算没了）。
2. **吞吐** = 饱和灌入的稳态产能，按**端到端 span**（首条发出→末条回显）算；
   不是「首尾回复时间差」那种产出速率口径（后者虚高一个量级）。
3. **可靠性** = 丢失率 + 是否存在 15s 级卡顿（`api.call` 响应丢失会让内核干等
   `defaultCallTimeout=15s`）。

**端口全部动态分配**（`tools/portutil.py`，bind :0 向 OS 要空闲端口）——开发机上跑着
别的服务时不会抢端口。需要固定可设 `ZCBOT_E2E_ADDR` / `ZCBOT_OFFICIAL_ADDR` / `ZCBOT_SURFACE_ADDR`。

## 排查

见 [调试排错](./调试排错.md#iipc-链路) 的 IIPC 症状表。
