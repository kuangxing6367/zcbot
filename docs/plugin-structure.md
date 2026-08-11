# 插件结构

## 目录结构

ZCBOT 严格分离「代码」和「数据」：

```
项目根目录/
├── plugins/                  # 插件代码目录（.py 文件，可被 GitHub 更新覆盖）
│   └── my_plugin/
│       ├── main.py           # 插件入口（必需）
│       ├── utils.py          # 辅助模块（可选）
│       └── requirements.txt  # 插件依赖（可选）
│
└── plugins_dat/              # 插件数据/配置目录（用户数据，不被覆盖）
    └── my_plugin/
        ├── _conf_schema.json # 配置 schema（Web UI 展示用）
        ├── plugin.yaml       # 插件元信息、GitHub 更新源、依赖声明
        └── data.json         # 插件自定义数据文件
```

**关键约定**：
- `plugins/<plugin_name>/` 中的文件在 GitHub 更新时会被覆盖，**不要**在这里存放用户配置
- `plugins_dat/<plugin_name>/` 是用户数据目录，由 `ctx.get_data_dir()` 获取路径，**永久保留**
- 框架启动时会自动把旧版插件配置（位于 `plugins/` 中的 yaml/json）迁移到 `plugins_dat/`

## 插件元信息

在 `main.py` 顶部定义 `__plugin_meta__` 字典：

| 字段       | 类型   | 必填 | 说明                                       |
| ---------- | ------ | ---- | ------------------------------------------ |
| `name`     | str    | 是   | 插件显示名                                 |
| `version`  | str    | 是   | 插件版本号（建议语义化版本，如 `1.0.0`）   |
| `author`   | str    | 是   | 作者                                       |
| `desc`     | str    | 否   | 插件描述                                   |
| `priority` | int    | 否   | 加载优先级（越小越优先，默认 50）          |

`plugin.yaml` 中的元信息会覆盖 `__plugin_meta__`。

## 插件生命周期

ZCBOT 插件生命周期：

1. **发现**：框架扫描 `plugins/` 目录下的子目录，每个含 `main.py` 的目录视为一个插件
2. **加载**：调用 `importlib` 动态导入 `main.py`，并调用 `register(ctx)` 函数
3. **注册**：`register(ctx)` 中通过 `ctx.command()` / `ctx.task()` 等方法注册命令和任务
4. **心跳刷新**：每 60 秒（可配置）重新刷新命令表，保证数据库中命令状态与代码一致
5. **消息分发**：收到 OneBot 消息时，按插件优先级顺序匹配命令并调用 handler
6. **卸载**：调用 `unload_plugin()` 时清理命令、任务、事件订阅，并 `gc.collect()` 释放资源

可选的生命周期钩子（在 `main.py` 中定义）：

```python
def on_load(ctx):
    """插件加载时调用（register 之前）"""
    ctx.logger.info("插件正在加载...")

def on_unload(ctx):
    """插件卸载时调用"""
    ctx.logger.info("插件正在卸载...")
    # 清理文件句柄、关闭连接等
```
