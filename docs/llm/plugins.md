# 插件开发（给 LLM）

## 最小模板(照抄)

```python
# plugins/<plugin_id>/main.py,plugin_id 全小写下划线
__plugin_meta__ = {"name": "中文名", "version": "1.0.0", "author": "…",
                   "desc": "一句话", "priority": 50}   # 越小越先,守卫类 2-10

def register(ctx):
    ctx.command("/签到", handle, alias="/sign", description="…",
                require_admin=False, require_superuser=False,
                require_perm="myplugin.sign")          # 权限节点,可选
    ctx.on_raw_message(on_raw)   # (raw: dict, bot_name) -> True=接管全部后续
    ctx.task("*/10 * * * *", job)
    ctx.on("message", listener)  # 返回 True=已处理,路由终止
    ctx.hook("action.after", fn)

async def handle(event, match):      # match.group(1)=命令后参数
    await ctx.asend_msg(user_id=event.user_id, group_id=event.group_id, message="文本")

def unregister(): ...                # 可选
```

约定:sync 用同步方法,async 用 `a` 前缀(`asend_msg`/`aapi`);数据只写
`ctx.get_data_dir()`(= `data/plugins_dat/<插件>/`),不写代码目录;重复注册自动去重。

## ctx 常用面

| 类别 | 成员 |
| ---- | ---- |
| 发送 | `send_msg/asend_msg(user_id, group_id, message)`、`api/aapi(action, bot=None, **params)`、`actions` |
| 会话 | `await wait_for(event, prompt, timeout)`、`create_session(event)`(session 插件) |
| 数据 | `db.query/execute(sql, params)`(SQLite/MySQL 双方言)、`get_config(key, default)` |
| 权限 | `has_perm(user_id, node)`、`audit_log(action)` |
| 其它 | `on/emit/aemit`、`hook/unhook`、`register_api(path, view)`、`webui(...)`、`log(msg)` |

event 字段:`message`(提取后文本)、`raw_message`、`user_id/group_id/is_group`、`sender`、
`role`、`is_admin`、`segments`、`reply_text()`、`stop_propagation()`。

## 跨插件访问

`sys.modules.get("plugin_<id>")`(官方插件 `core_plugin_<name>`;html_assembler 兼容别名
`plugin_html_assembler`)。拿不到返回 None,不要 `[]` 直取;依赖顺序时订阅
`system.plugin.loaded`。

## 坑

1. 别绕过框架直跑 main.py(模块名/ctx/DB 全靠加载器)。
2. raw handler 返回 True 会吞掉该消息的命令匹配,谨慎。
3. @机器人 前缀已在 Event 构造时剥离,`event.message` 是干净文本。
4. SQLite 只适合小环境,多群上 MySQL(`config.yaml → database.type`)。
5. SQL 用参数占位符;批量写参考 `stats_writer` 的队列模式,别在热路径直查库。
