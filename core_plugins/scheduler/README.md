# 定时任务调度器

定时任务调度插件（官方插件）。基于 APScheduler 的 cron 定时任务调度，供插件注册周期性任务。

## 元信息

- 优先级：0
- 进程归属：host（单进程与双进程宿主均加载）
- 默认开关：启用（`scheduler.enabled` 设为 `false` 可禁用）
- 提供的服务：`scheduler`（禁用时注册为 `None`）

## 注册方式

插件在 `register(ctx)` 时通过 `ctx.task(...)`（或装饰器 `@task`）声明定时任务，最终由
调度器 `add_plugin_task` 处理：按 cron 表达式拆分分钟 / 小时 / 日 / 月 / 星期，从插件模块
取出 handler，包装为 `_wrapper` 后加入 `AsyncIOScheduler`。

- 支持 async 与同步 handler（自动区分）
- job id 形如 `插件名:handler名`，`replace_existing=True` 避免重复注册
- `misfire_grace_time=600`，错过触发在 10 分钟内仍会补跑

## 生命周期

插件注册发生在工作线程，而 `AsyncIOScheduler.start` 必须在事件循环线程执行。因此启动做了
兜底：循环未就绪时由守护线程等待就绪后再线程安全地 `call_soon_threadsafe` 派发；
等待超时（60s）则记错误日志，定时任务不启动。

## 可配置项（config.yaml 段 `scheduler`）

| 键 | 说明 | 默认值 |
|---|---|---|
| `enabled` | 是否启用 | 启用 |

## 说明

调度器本身只提供 cron 调度内核，具体任务由各插件声明。会话管理器等插件会借此注册清理任务。
