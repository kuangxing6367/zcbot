# ZCBOT 文档索引

ZCBOT v0.0.1-beta.0 文档总览，按阅读路径组织。

## 快速入口

| 文档 | 内容 | 适合谁 |
| ---- | ---- | ---- |
| [快速入门](./getting-started.md) | 最小可运行插件、异步支持、API 速查表 | 第一次接触 ZCBOT 的开发者 |
| [插件目录结构](./plugin-structure.md) | 代码/数据分离约定、元信息、生命周期钩子 | 准备搭建新插件骨架 |
| [API 参考](./api-reference.md) | `PluginContext (ctx)` 全部方法、`Event` 事件对象、OneBot 11 API | 写插件时查接口 |
| [配置系统](./configuration.md) | `plugin.yaml`、`_conf_schema.json`、配置读写、依赖声明 | 插件需要用户可配置项 |
| [最佳实践](./best-practices.md) | 错误处理、资源清理、性能与安全、日志规范 | 想写出可长期维护的插件 |
| [示例合集](./examples.md) | 签到插件完整实现（数据库、定时任务、事件总线、仪表盘卡片） | 需要可参考的完整代码 |

## 其他文档

| 文档 | 内容 |
| ---- | ---- |
| [Web API 接口文档](./API.md) | Web UI 后端 HTTP API 完整定义 |
| [更新日志](../CHANGELOG.md) | 版本变更记录 |

## 推荐阅读路径

1. **快速入门** → 跑通第一个 `/hello` 插件
2. **插件目录结构** → 理解 `plugins/` 与 `plugins_dat/` 的分离约定
3. **API 参考** → 查阅 `ctx.command`、`ctx.send_msg`、`ctx.db_query` 等方法
4. **配置系统** → 让插件支持 Web UI 配置
5. **示例合集** → 参考签到插件的完整实现
6. **最佳实践** → 上线前对照检查
