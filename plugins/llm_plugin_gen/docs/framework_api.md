# ZCBOT 框架核心 API 说明

本文件说明框架提供给插件的能力。需要时用 `ls` 读取。

## 插件加载机制

- 插件目录：`plugins/<插件名>/`，核心文件 `main.py`
- 框架启动时扫描 `plugins` 目录，逐个加载；`plugin.yaml` 声明元信息与依赖
- `register(ctx)` 在加载时调用，负责注册命令/任务
- 修改代码后可用 `reload_plugin` 工具热重载（卸载→重新加载）

## 插件配置

- 插件有自己的配置面板（Web UI），配置键在 `_conf_schema.json` 中声明
- 插件代码内用 `ctx.get_config(key, default)` 读取，值可能是 string/number/array
- 需要持久化自定义数据时用 `ctx.db_execute` 写数据库，不要写本地文件

## 路由（命令）

- `ctx.command(pattern, handler, priority, alias, description, require_admin, require_superuser)`
- pattern 以 `/` 开头，如 `/echo`；alias 为别名列表
- handler 签名 `def handler(event, match)`，match 为正则匹配对象
- 权限：`require_superuser=True` 仅超级管理员可用；`require_admin=True` 管理员可用

## 定时任务

- `ctx.task(cron_expr, executor)`，cron_expr 如 `0 8 * * *`（每天 8 点）
- executor 为无参函数，出错需自行 try/except

## 数据库

- `ctx.db_query(sql, params)` → list[dict]
- `ctx.db_execute(sql, params)` → 执行写操作
- 建表用 `CREATE TABLE IF NOT EXISTS`，主键 `id INTEGER PRIMARY KEY AUTOINCREMENT`
- 插件间表名建议加前缀避免冲突

## OneBot API

- `ctx.api(action, **params)` 调用 OneBot 标准 API，如 `ctx.api('send_group_msg', group_id=..., message=...)`
- 发送消息优先用 `ctx.send_msg(user_id=..., group_id=..., message=...)`（自动适配）

## 日志

- `ctx.log(msg, level='info')`，level 可选 info/warning/error/debug
