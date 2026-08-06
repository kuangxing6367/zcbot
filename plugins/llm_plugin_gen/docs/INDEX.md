# ZCBOT 插件开发文档索引

本目录存放框架插件开发资料。**编写或修改插件前先看此索引，按需用 `ls` 读取对应文档，不要一次读完所有文件。**

| 文档 | 内容 | 何时读取 |
|---|---|---|
| `plugin_dev.md` | 插件代码结构、ctx API、事件字段、硬性要求、工作流程 | 每次写/改插件必读 |
| `framework_api.md` | 框架核心 API 详细说明（加载、路由、配置、定时任务） | 用到对应能力时 |

## 快速导航

- 写新插件 → 读 `plugin_dev.md`
- 改现有插件 → 先 `ls plugins/<插件名>/main.py` 看代码，需要时读 `plugin_dev.md`
- 不清楚框架能做什么 → 读 `framework_api.md`
- 加载/重载插件 → 用 `load_plugin` / `reload_plugin` 工具，无需读文档
