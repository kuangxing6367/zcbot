# 定时任务

ZCBOT 的定时能力由官方插件 `core_plugins/scheduler`（基于 APScheduler
`AsyncIOScheduler`）提供，用户插件通过 `ctx.task()` 注册，开箱即用。

## 基本用法：ctx.task()

```python
def register(ctx):
    ctx.task("0 8 * * *", daily_report, description="每日 8 点报告")
    ctx.task("*/5 * * * *", heartbeat, description="每 5 分钟")
    ctx.task("0 0 1 * *", monthly, description="每月 1 日 0 点")
    ctx.task("30 9 * * 1-5", weekday, description="工作日 9:30")
```

### cron 表达式

标准 5 段：`分 时 日 月 周`，由 APScheduler `CronTrigger` 解析：

| 位置 | 取值 | 例子 |
|------|------|------|
| 分 | 0–59、`*/n`、`a,b,c`、`a-b` | `*/30` |
| 时 | 0–23 | `9` |
| 日 | 1–31 | `1` |
| 月 | 1–12 | `*` |
| 周 | 0–6（0=周一，APScheduler 约定）或 `mon-fri` | `1-5` |

:::tip 周字段注意
APScheduler 的 `day_of_week` 用 `0=monday … 6=sunday`，也接受
`mon,tue,wed,thu,fri,sat,sun`，和部分 crontab “0=周日”的习惯不同。
:::

### 处理函数要求

```python
async def daily_report():           # 同步 def / async def 都支持
    caller = ctx.services.get("api_caller")   # 需要框架能力时从服务注册表取
    if caller:
        await caller.call("send_group_msg", group_id=123456, message="日报")
    ctx.log("日报已发送")
```

- **任务函数无参数**，也不接收 `event/match`；
- 函数必须定义在插件主模块顶层（调度器按 `handler.__name__` 从主模块取函数对象）；
- 异步函数直接 await，同步函数在线程中执行；单任务异常被捕获并记录，不影响其他任务；
- 任务补触发宽限 `misfire_grace_time=600` 秒（进程短暂卡住后，错过 10 分钟内的任务仍补跑一次）。

## 注册与同步机制

- `register(ctx)` 里声明的任务先写入 `tasks` 表，再注册到 APScheduler；
- APScheduler 任务 ID 为 `<插件名>:<函数名>`，重复注册 `replace_existing`，
  因此同一函数多次注册不会产生重复任务；
- 插件卸载/重载时，属于该插件的任务会被整体移除后按新代码重建；
- 心跳重新 `register(ctx)` 时同样先清后建，保持代码与调度一致。

## 高级：直接使用底层 APScheduler

`ctx.task()` 只覆盖最常用的 **cron** 触发。需要 interval（固定间隔）、
date（指定时刻执行一次）等触发器时，可取到底层原生 Scheduler：

```python
def register(ctx):
    scheduler = ctx._framework.services.get("scheduler")
    aps = scheduler._scheduler      # 原生 apscheduler.schedulers.asyncio.AsyncIOScheduler

    from apscheduler.triggers.interval import IntervalTrigger
    from datetime import datetime
    from apscheduler.triggers.date import DateTrigger

    aps.add_job(my_job, IntervalTrigger(seconds=30),
                id="myplugin:poll_30s", replace_existing=True)
    aps.add_job(once_job, DateTrigger(run_date=datetime(2026, 1, 1, 0, 0)),
                id="myplugin:once")
```

底层原生 API（来自 APScheduler）：

| 方法 | 作用 |
|------|------|
| `add_job(fn, trigger, id=..., replace_existing=True)` | 添加任务 |
| `remove_job(job_id)` | 移除任务 |
| `pause_job(job_id)` / `resume_job(job_id)` | 暂停 / 恢复 |
| `reschedule_job(job_id, trigger=..., **kw)` | 修改触发器 |
| `get_jobs()` | 列出全部任务 |
| `scheduler.get_jobs()`（封装层） | 返回 `[{id, next_run}]` |

:::warning 任务 ID 约定
自己 `add_job` 时请用 `<插件名>:<业务名>` 前缀，这样插件卸载时
`remove_plugin_tasks` 才能按前缀把它一起清掉，避免“幽灵任务”。
:::

## 管理接口

- Web 面板「定时任务」页可查看任务、下次执行时间并手动启停；
- 封装层方法：`scheduler.remove_task(job_id)`、`scheduler.get_jobs()`；
- 孤儿任务（插件已删除但任务残留）会被框架周期性自检清理。

## 注意事项

:::warning 常见坑
- 任务函数里不要依赖某次消息的 `event`，任务没有消息上下文；
- 任务里发消息优先用 `services.get("api_caller")` 或 `ctx.onebot`；
- 任务要能幂等重跑（补触发/重启后可能立即执行一次）；
- 长时间不返回的任务会占用 worker，耗时操作请自行拆分或加超时。
:::
