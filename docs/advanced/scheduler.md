# 定时任务

ZCBOT 内置 APScheduler，插件可注册 cron/interval/date 定时任务。

## 基本用法

### ctx.task() — 简单注册

```python
def register(ctx):
    ctx.task("0 8 * * *", daily_report, description="每日 8 点报告")
    ctx.task("*/5 * * * *", heartbeat, description="每 5 分钟心跳")
    ctx.task("0 0 1 1 *", yearly, description="每年 1 月 1 日")
```

cron 格式：`分 时 日 月 周`

### 异步处理函数

```python
async def daily_report():
    # 可以访问框架能力
    caller = ctx.services.get('api_caller')
    if caller:
        await caller.send_group_msg(group_id=123456, message="日报已生成")
    ctx.log("每日报告已发送")
```

## 高级用法

### 直接使用 Scheduler 服务

```python
def register(ctx):
    ctx.on("system.plugin.loaded", _setup_tasks)

async def _setup_tasks(_):
    scheduler = ctx.services.get('scheduler')
    if not scheduler:
        return
    
    # cron 触发器
    scheduler.add_cron_job(
        my_func, "0 8 * * *",
        id="morning_report",
        name="早报"
    )
    
    # interval 触发器
    scheduler.add_interval_job(
        check_status, seconds=300,
        id="status_check",
        name="状态检查"
    )
    
    # date 触发器（指定时间执行一次）
    from datetime import datetime
    scheduler.add_date_job(
        send_reminder,
        run_date=datetime(2026, 1, 1, 0, 0),
        id="new_year",
        name="新年提醒"
    )
```

### 动态增删任务

```python
# 添加任务
scheduler.add_cron_job(handler, "30 9 * * 1-5", id="weekday_job")

# 移除任务
scheduler.remove_job("weekday_job")

# 暂停任务
scheduler.pause_job("weekday_job")

# 恢复任务
scheduler.resume_job("weekday_job")

# 修改任务
scheduler.reschedule_job("weekday_job", trigger="cron", hour=10)
```

## 任务 ID 规范

```python
f"{plugin_name}_{function_name}"
# 例: weather_daily_report, user_cleanup
```

框架自动为通过 `ctx.task()` 注册的任务生成 ID，格式为 `{plugin_name}_{function_name}`。

## 注意事项

:::warning 注意
- 任务函数不能使用 `ctx`，需通过 `ctx.services.get()` 获取服务
- 任务函数执行前，框架会确保 ctx 已初始化
- 超过 5 分钟未完成的任务会自动取消并记录日志
:::
