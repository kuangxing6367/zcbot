# 运维插件（ops）

终端运维扩展插件（官方插件）。以 core_plugins 官方插件线路提供终端命令，不侵入
`framework/terminal` 源码。

## 元信息

- 优先级：90
- 进程归属：无 process 标记（单进程 standard 加载、双进程宿主加载）
- 默认开关：由 `core_plugins.yaml` 的 `ops` 段控制（`enabled: true`）

## 提供的终端命令

| 命令 | 别名 | 作用 |
|---|---|---|
| `restart` | - | 重启框架（单进程 `os.execv` 原地重启） |
| `reload` | `rl` | 重载插件（修复内置 reload 的 bug，支持 `reload [插件名]`，缺省重载全部） |
| `shell` | `sh` | 执行本机 shell 命令（带超时 / 输出截断） |
| `dbdump` | - | 导出数据库快照（复用 `tools/export_db_snapshot.py`） |

## 容错约定

1. 命令 handler 一律为同步函数——`/api/terminal/exec` 直接同步调用，async handler 会返回
   未 await 的协程导致异常
2. 每个 handler 自带 try/except + 超时 + 输出截断，语法错误或子进程异常只打印错误文本，
   不向线程 / 事件循环抛异常
3. `restart` 防御双进程宿主角色：`host` 进程 `execv` 成 `main.py` 会变成第二个核心进程
   与现存 core 冲突，因此 host 角色拒绝执行并提示改用核心进程或 Web 面板 `/api/restart`

## 可配置项（core_plugins.yaml 段 `ops`）

| 键 | 说明 | 默认值 |
|---|---|---|
| `enabled` | 是否启用 | 由列表开关决定 |
| `shell_timeout` | shell 命令超时（秒） | `10` |
| `max_output` | 命令输出截断长度（字符） | `4000` |

## 双进程说明

ops 不带 process 标记，单进程 standard 与双进程宿主均加载。核心进程终端输入 `reload`
走内置转发链路（`target=host`）到宿主执行同样生效；`restart` / `shell` / `dbdump` 在核心
终端不可用时（双进程），请通过 Web 面板（`/api/restart`）或把 ops 加入 `config.yaml` 的
`dual_process.core_plugins` 名单。
