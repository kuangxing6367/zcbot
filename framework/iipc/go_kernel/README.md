# zcbot IIPC 内核（EV Kernel / 事件内核）

Go 编写的内核进程，是 IIPC 的**事件驱动宿主**：事件总线 + DB + 插件进程编排 + 扩展面。
由 `python main.py -iipc` 拉起（编译产物 `zcbot_kernel.exe`），Python 插件经 TCP 行分隔 JSON 接入。

## 分层地图

所有源文件同属 `package main`（见下「为什么不拆 internal 包」）。按职责归为五层：

### core（内核骨架 / 事件流转）
| 文件 | 职责 |
|---|---|
| `main.go` | 入口：装配 bus / srv / extensions / plugin_mgr，启动监听，注册扩展面 |
| `bus.go` | 事件总线：按类型维护订阅连接集合；扇出前跑 `preHook` 与会话配对、`transformHook` 全局变换 |
| `ipc_server.go` | IPC 服务端：连接管理、按方法分派、向订阅者推送事件、插件连接索引 |
| `handlers.go` | IIPC 方法注册：适配器注册 / 调试注入 / 插件注册 / DB / 发送 / api / 配置 / 权限 / 事件 |
| `protocol.go` | 信封协议方法常量（`MethodXxx`）与 JSON 信封格式 |
| `schema.go` | 核心表结构（SQLite 方言），与 python 侧 `init.sql` 列名保持一致 |
| `scheduler.go` | 定时任务 cron 调度器（对齐 scheduler 插件 cron 语义） |
| `session.go` | 多轮会话：wait_for / create_session 的挂起与配对 |
| `logger.go` | 结构化日志：级别 + 时间戳 + 组件，输出 stdout（+ 可选文件） |

### store（持久化）
| 文件 | 职责 |
|---|---|
| `db.go` | DB 层抽象：全部在 Go 进程内执行（modernc.org/sqlite，纯 Go 无 cgo） |
| `sqlite_store.go` | SQLite 存储实现（含坏库隔离 `isolateCorruptDB`） |
| `sqldialect.go` | SQL 方言翻译：插件按 MySQL `%s` 写，SQLite 只认 `?` |
| `services.go` | 内核编排服务容器（调度器 / 会话 / 群开关 / 权限解析）统一挂接 |

### ext（扩展面）
| 文件 | 职责 |
|---|---|
| `extensions.go` | 内核扩展面：服务注册表 / 插件间 RPC / 共享 KV / 指标 / 能力声明 / 全局变换管线 |
| `perm.go` | 节点式权限解析（角色 / 节点 / 上下文） |
| `group_switch.go` | 群级插件开关：按群启用 / 禁用 |
| `manifest.go` | 插件清单：每个插件目录可放 `plugin.json` 声明元数据 |

### plugin（插件进程编排）
| 文件 | 职责 |
|---|---|
| `plugin_mgr.go` | 发现 `plugins/` 下子目录为插件，各自独立进程启动（LangBot 式 GIL 隔离）、看门狗重启、优雅关闭 |
| `go_plugin.go` | Go 原生插件路径（T5）：Windows 不支持 `plugin` 包（c-shared），走独立进程 |
| `go_plugin_example.go` | Go 原生插件示例（事件→Go handler→动作，零 IPC 路径） |

### util（工具）
| 文件 | 职责 |
|---|---|
| `util.go` | 内核侧通用工具函数 |
| `truthy.go` | 真值判定（"true"/"1"/"yes" 等归一到 bool） |

测试文件：`*_test.go`（`go_plugin_test.go` / `ipc_server_test.go` / `store_test.go`），`go test ./...` 运行。

## 调用路径（一次消息的一生）

```
adapter → launcher → zcbot_kernel.exe (main)
  → bus.Publish(ev)
      ├─ preHook          会话配对（先于插件）
      ├─ transformHook    全局变换节点（按 priority 改写/丢弃 event.data）
      └─ 扇出给订阅该类型的插件连接
          → ipc_server 推 "event" 给 Python 插件进程
              → sdk._on_event：pre 中间件 → raw → command → on → post 中间件
                  → handler 内 ctx.send_msg → 本地队列 5ms 聚合 → msg.send_batch 回内核
```

## 为什么不拆 internal 包

28 个文件刻意保持在同一个 `package main`：内核各模块通过大量包级全局（bus 实例、
plugin 连接表、扩展面注册表）紧耦合，拆成 `internal/core`、`internal/store` 等需要把
这些全局改成导出符号并逐文件改 import，回归面大且对单静态二进制构建无收益。
**可读性靠本文件的分层地图 + 每个文件首行注释保证**，需要更严格的包边界时再评估拆分。

## 与 Python 侧（py_sdk）的对应

- `go_kernel/extensions.go` ↔ `py_sdk/ext.py`（扩展面：服务/RPC/KV/指标/能力/变换）
- `go_kernel/bus.go` + `handlers.go` ↔ `py_sdk/sdk.py`（事件分发 / 动作回传）
- `go_kernel/protocol.go` 的 `MethodXxx` ↔ `py_sdk/ext.py` 的调用方法名（一一对应）
- 插件进程入口：`py_sdk/entry.py` → `py_sdk/sdk.py:run_plugin`
