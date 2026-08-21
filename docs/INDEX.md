# ZCBOT 文档索引

ZCBOT v1.0.0 文档总览，按阅读路径组织。

> 💡 **看不懂术语？** 每篇文档里都有「什么是 XX」的可展开解释（点一下标题就会展开）。如果还是没有，去 [快速入门](./getting-started.md) 开头找名词解释。

## 快速入口

| 文档 | 内容 | 什么时候读 |
| ---- | ---- | ---- |
| [框架架构与开发指南](./architecture.md) | 模块职责/消息流/插件生命周期/如何扩展框架 + 官方插件实例 | 想懂「框架怎么跑」、要扩展框架时 |
| [插件开发详解](./plugin-tutorial.md) | 完整示例插件 + **逐行讲解每个语法的作用** | **第一次写插件时完整读一遍** |
| [快速入门](./getting-started.md) | 最小可运行插件、异步支持、API 速查表 | 第一次接触 ZCBOT，想跑通第一个插件 |
| [插件目录结构](./plugin-structure.md) | 代码/数据分离约定、元信息、生命周期钩子 | 准备搭建新插件骨架时 |
| [API 参考](./api-reference.md) | `PluginContext (ctx)` 全部方法、`Event` 事件对象、OneBot 11 API | **写代码时遇到不确定的 API 就查** |
| [配置系统](./configuration.md) | `plugin.yaml`、`_conf_schema.json`、配置读写、依赖声明 | 插件需要用户可配置项时 |
| [最佳实践](./best-practices.md) | 错误处理、资源清理、性能与安全、日志规范、**提交前自查清单** | **写完插件、提交前对照自查** |
| [调试指南](./debugging.md) | 看日志、开 DEBUG、pdb 断点、热重载试错 | **插件出 bug 时按顺序排查** |
| [示例合集](./examples.md) | 签到插件完整实现（数据库、定时任务、事件总线、仪表盘卡片） | 需要可参考的完整代码时 |
| [官方插件使用手册](./official-plugins.md) | 官方插件仓库每个插件的调用命令与用法例子 | 装了官方插件、想知道怎么用 |
| [已知问题](./KNOWN_ISSUES.md) | 框架已知的坑 + 修复状态追踪（P0/P1/P2） | 遇到"文档明明写了却不生效"时先查这里 |

## AI 助手（不用自己写代码）

| 文档 | 内容 |
| ---- | ---- |
| [AI 助手插件文档](https://github.com/kuangxing6367/zcbot_plugins/blob/main/plugins/llm_plugin_gen/docs/INDEX.md) | 内置 `llm_plugin_gen` 插件的使用说明：AI 写插件、改需求、更新框架 |

> 想让 AI 帮你写插件：在后台插件配置填好大模型信息，再按 [插件文档](https://github.com/kuangxing6367/zcbot_plugins/blob/main/plugins/llm_plugin_gen/docs/INDEX.md) 使用即可。该功能由插件提供，非框架自带，插件位于 [官方插件仓库](https://github.com/kuangxing6367/zcbot_plugins/tree/main/plugins/llm_plugin_gen)。

## 其他文档

| 文档 | 内容 |
| ---- | ---- |
| [Web API 接口文档](./API.md) | Web UI 后端 HTTP API 完整定义 |
| [更新日志](../CHANGELOG.md) | 版本变更记录 |

## 推荐阅读路径

1. **插件开发详解** → 完整示例 + 逐行讲解，搞懂每个语法
2. **快速入门** → 跑通第一个 `/hello` 插件
3. **插件目录结构** → 理解 `plugins/` 与 `plugins_dat/` 的分离约定
4. **API 参考** → 查阅 `ctx.command`、`ctx.send_msg`、`ctx.db_query` 等方法
5. **配置系统** → 让插件支持 Web UI 配置
6. **示例合集** → 参考签到插件的完整实现
7. **最佳实践** → 写完对照自查清单检查
8. **调试指南** → 插件出问题时按症状查

> 不想写代码？装了 AI 助手插件（`llm_plugin_gen`）后，参考 [插件文档](https://github.com/kuangxing6367/zcbot_plugins/blob/main/plugins/llm_plugin_gen/docs/INDEX.md) 让它帮你写。