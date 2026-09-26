# 文档总入口

**ZCBOT 是一个事件驱动的 IM 平台**：接上聊天软件或定时/Webhook 事件，用 Python 插件处理，再发回去。
权限、Web 后台、数据库、多轮会话都是内置的，业务逻辑写在 `register(ctx)` 里。

---

## 推荐阅读顺序

1. [安装](./installation.md) —— 下载、依赖、目录
2. [开始使用](./getting-started.md) —— 启动、确定事件来源、进后台
3. [对接 IM 平台](./connect-im.md) —— 连 NapCat / Telegram / Discord 等（不接 IM 可跳过）
4. [编写插件](./writing-plugins.md) —— 从零跟做一条完整命令
5. [配置系统](./configuration.md) —— 改端口、开关插件、换数据库
6. [部署上线](../advanced/deployment.md) —— 放到服务器长期跑时再看

写插件后需要多文件拆分时读 [加载与模块机制](../advanced/loader.md)；
要存数据、定时、权限时查对应进阶篇；要接 Webhook 或自写接入端时读
[最佳实践](./best-practices.md) 与 [协议适配器](../api/advanced/protocol_adapter.md)。

---

## 按主题查

### 指南

| 文档 | 内容 |
| ---- | ---- |
| [安装](./installation.md) | 环境、依赖、目录结构、升级 |
| [开始使用](./getting-started.md) | 启动成功、事件来源、Web 后台 |
| [对接 IM 平台](./connect-im.md) | 反向 WS、多平台接入、富媒体与群管 |
| [配置系统](./configuration.md) | 两份 yaml、插件开关、安全清单 |
| [编写插件](./writing-plugins.md) | 手把手做出签到类完整插件 |
| [多轮会话](./session.md) | `wait_for` / `create_session` |
| [最佳实践](./best-practices.md) | 不依赖 IM 的三类用法 + 写插件规范 |

### API（写插件时查）

| 文档 | 内容 |
| ---- | ---- |
| [PluginContext (ctx)](../api/basic/ctx.md) | 插件可用的全部能力清单 |
| [Event 事件对象](../api/basic/event.md) | 消息字段、富媒体、传播控制、权限 |
| [服务注册表](../api/basic/services.md) | 官方能力怎么取、DI 怎么用 |
| [协议适配器](../api/advanced/protocol_adapter.md) | 自写接入端的契约与示例 |
| [Framework 核心](../api/basic/framework.md) | 底层容器 `fw` 与生命周期 |
| [扩展点 Hook](../api/advanced/hooks.md) | 几乎每个运行环节都能挂逻辑 |

### 进阶

| 文档 | 内容 |
| ---- | ---- |
| [架构详解](../advanced/architecture.md) | 分层、启动时序、消息流转 |
| [插件加载机制](../advanced/loader.md) | 多文件导入、热重载、踩坑 |
| [数据库](../advanced/database.md) | 建表、CRUD、SQLite/MySQL |
| [定时任务](../advanced/scheduler.md) | cron / interval / date |
| [权限系统](../advanced/permission.md) | 节点、组、继承、审计 |
| [部署上线](../advanced/deployment.md) | systemd / Docker / 反代 / 安全 |

---

## 目录速查

| 路径 | 作用 |
| ---- | ---- |
| `framework/` | 平台内核：加载、路由、事件、上下文、权限、数据库（无具体 IM 实现） |
| `core_plugins/` | 官方插件：`onebot_adapter` / `telegram` / `discord` / `qq_official` / `ws_client` / `http_inject` / `http_api` / `webui` / `session` / `scheduler` / `image_renderer` / `html_assembler` |
| `core_plugins.yaml` | 官方插件开关与配置（启动自动同步） |
| `plugins/` | 用户插件，每个一个子目录，入口 `main.py` |
| `data/` | 日志、数据库、`plugins_dat/` 插件私有数据（长期保留） |
| `tests/` | 自测脚本 |

## 贯穿全局的约定

- **协议无关**：优先 `ctx.send_msg` / `ctx.actions`，少依赖平台特有字段
- **同步 / 异步双份**：普通 `def` 用同步方法；`async def` 用 `a` 前缀异步方法（推荐）
- **代码与数据分离**：代码在 `plugins/<名>/`（更新会覆盖），数据写 `ctx.get_data_dir()`
- **能力来自服务**：官方能力经服务注册表取用（`api_caller`、`scheduler`、`session_manager`…），平台内核不直接 import 插件
