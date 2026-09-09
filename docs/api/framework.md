# 核心框架

核心框架 (`framework/core.py`) 是 ZCBOT 的最小壳，负责插件加载、事件分发和服务注册。

## Framework 类

### 初始化

```python
from framework.core import Framework

fw = Framework(work_dir=".")
```

### start()

```python
await fw.start()
```

启动流程：
1. 创建数据库连接
2. 自动建表
3. 加载 core_plugins（按 config.yaml 开关）
4. 加载 plugins/ 用户插件
5. 启动定时任务服务
6. 等待 OneBot 客户端连接

### stop()

```python
await fw.stop()
```

优雅关闭：停止 WebSocket、定时任务、数据库连接。

## 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `fw.plugins_dir` | `str` | 插件目录 |
| `fw.work_dir` | `str` | 工作目录 |
| `fw.db` | `DatabaseManager` | 数据库管理器 |
| `fw.services` | `ServiceRegistry` | 服务注册表 |
| `fw.logger` | `Logger` | 日志记录器 |
| `fw.scheduler` | `TaskScheduler` | 定时任务调度器 |
| `fw.session_manager` | `SessionManager` | 会话管理器 |
| `fw.webui` | `WebServer` | Web 服务 |

## dispatch_event()

```python
await fw.dispatch_event(event_data)
```

分发事件到所有已订阅的处理器。

## _load_core_plugins()

```python
fw._load_core_plugins()
```

从 `core_plugins/` 目录加载官方插件，受 `config.yaml` 中 `core_plugins:` 控制。

## 框架生命周期

```
__init__()
  ├─ 创建 Logger
  ├─ 创建 ServiceRegistry
  ├─ 创建 DatabaseManager
  └─ 定位 plugins 目录

start()
  ├─ db.create_tables()
  ├─ _load_core_plugins()
  ├─ loader.load_plugins()
  ├─ scheduler.start()
  ├─ webui 启动（空城计模式）
  └─ ws_server.start()

stop()
  ├─ ws_server.stop()
  ├─ scheduler.stop()
  ├─ webui.stop()
  └─ db.close()
```
