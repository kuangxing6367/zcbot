# ZCBOT 插件开发文档

本文档供 LLM 开发插件时按需查阅。需要时用 `ls` 工具读取。

## 插件代码结构

- 插件目录：`plugins/<插件名>/`，核心文件 `main.py`
- 必须包含 `__plugin_meta__` 字典与 `def register(ctx):` 入口
- 处理函数签名：`def handler(event, match):`（或 async def），通过模块级 `ctx` 访问上下文
- 只能有一个 register 函数，命令都在 register 里注册

```python
__plugin_meta__ = {
    "name": "插件名",
    "version": "1.0.0",
    "author": "作者",
    "desc": "插件描述",
    "priority": 50,
}

def register(ctx):
    ctx.command("/命令", handler, priority=50, alias=["/别名"],
                description="命令说明", require_admin=False, require_superuser=False)

def handler(event, match):
    ctx.send_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                 message="回复内容")
```

## ctx 常用 API

| API | 说明 |
|---|---|
| `ctx.command(pattern, handler, priority=50, alias=..., description=..., require_admin=False, require_superuser=False)` | 注册命令 |
| `ctx.send_msg(user_id=..., group_id=..., message=...)` | 发送消息（user_id/group_id 传其一） |
| `ctx.log(msg, level='info')` | 记录日志 |
| `ctx.get_config(key, default)` | 读取插件配置 |
| `ctx.db_query(sql, params)` / `ctx.db_execute(sql, params)` | 数据库操作 |
| `ctx.api(action, **params)` | OneBot API |
| `ctx.task(cron_expr, executor)` | 定时任务 |

## event 对象字段

- `event.message` 消息文本
- `event.user_id` 发送者 QQ
- `event.group_id` 群号（非群消息为 None）
- `event.is_group` 是否群消息
- `event.is_admin` / `event.is_superuser` 权限标志
- `event.message_id` 消息 ID

## 硬性要求

- 只用标准库 + 框架已装依赖（requests / flask / pyyaml / Pillow / numpy）
- 额外依赖写入 `plugins/<插件名>/requirements.txt`（每行一个包名）
- 禁止相对导入（`from .xxx`），单文件实现
- 代码要健壮：参数校验、异常捕获、不阻塞主流程
- 注释使用中文

## 工作流程（用户要求写插件时）

1. 先 `ls plugins` 查看现有插件写法（参考 `plugins/echo/main.py`）
2. `write` 创建 `plugins/<插件名>/main.py`（插件名英文小写，如 my_plugin）
3. 如需依赖包，`write` 创建 `plugins/<插件名>/requirements.txt`
4. 调用 `load_plugin` 加载插件使其生效
5. 加载失败：`ls` 查看 → `edit` 修改 → `reload_plugin` 重试
