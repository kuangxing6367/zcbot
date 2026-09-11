# ZCBOT 开发文档

> **ZCBOT 是一个事件驱动的插件化服务宿主**，内置依赖注入（服务注册表）、LuckPerms 风格权限引擎、
> 双方言持久化、Web 管理后台与协议无关的接入端抽象。
>
> **OneBot 11只是默认接入端之一，不是身份。** 换一个 `ProtocolAdapter`，即可接入
> HTTP Webhook、定时事件、Telegram / Discord 等其它 IM，或任何"事件 → 插件 → 响应"的服务。

本页是文档总入口。**先对号入座找到你的角色，按推荐路径读即可，不必从头读到尾。**

---

## 一、我是谁？我该先读哪篇？

### 角色 A：我想搭一个能用的机器人 / 服务（使用者，不一定写代码）

> 目标：把宿主跑起来、接入平台（或不连）、会装插件、会改配置、会用后台。

1. [安装](./installation.md) —— 环境、依赖、目录结构
2. [开始使用](./getting-started.md) —— 启动、接入平台、第一次对话、Web 后台
3. [配置系统](./configuration.md) —— `config.yaml` 与官方插件配置中心 `core_plugins.yaml`
4. [部署上线](../advanced/deployment.md) —— 要放到服务器长期跑时再看

### 角色 B：我要写业务插件（Python 插件开发者，最常见）

> 目标：掌握 `register(ctx)`、命令、事件、数据库、会话、定时、权限、配置、Web 扩展。

1. [编写插件](./writing-plugins.md) —— **新手必读**，从零写一个完整插件
2. [多轮会话](./session.md) —— 一问一答 / 连续多轮交互
3. 写插件时随时查 API：[PluginContext (ctx)](../api/basic/ctx.md)、[Event 事件对象](../api/basic/event.md)
4. 插件要拆多个文件：[插件加载与模块机制](../advanced/loader.md)（**多文件插件必读**，避免导入踩坑）
5. 需要时按主题查：[数据库](../advanced/database.md)、[定时任务](../advanced/scheduler.md)、
   [权限系统](../advanced/permission.md)、[服务注册表（DI）](../api/basic/services.md)

### 角色 C：我要接非 IM 的事件源 / 写自己的接入端（高级开发者）

> 目标：把 ZCBOT 当通用宿主，接 HTTP Webhook、纯定时、其它 IM，或理解内核分层。

1. [官方最佳实践](./best-practices.md) —— **先读这篇**：纯定时、HTTP Webhook、带权限业务后台的完整范式
2. [协议适配器 ProtocolAdapter](../api/advanced/protocol_adapter.md) —— 写自己接入端的契约与完整示例
3. [服务注册表（DI）](../api/basic/services.md) —— 内核与插件如何通过服务解耦
4. [架构详解](../advanced/architecture.md) —— 分层、启动时序、消息流转、事件总线
5. [Framework 核心对象](../api/basic/framework.md) —— 底层容器与高级用法

---

## 二、按主题查（全量目录）

### 指南 Guide（上手与教程）

| 文档 | 内容 | 主要受众 |
| ---- | ---- | -------- |
| [安装](./installation.md) | 环境要求、依赖安装、目录结构、升级 | A |
| [开始使用](./getting-started.md) | 启动、接入平台（可选）、第一次对话、Web 面板 | A |
| [对接 IM 平台](./connect-im.md) | 启用 onebot_adapter、NapCat/Lagrange 反向 WS、富媒体与群管 API | A |
| [配置系统](./configuration.md) | `config.yaml`、`core_plugins.yaml`、插件配置 schema | A/B |
| [编写插件](./writing-plugins.md) | 从零写完整插件的手把手教程 | B |
| [多轮会话](./session.md) | `wait_for` / `create_session` 多轮交互 | B |
| [官方最佳实践](./best-practices.md) | 当通用宿主用：定时/Webhook/业务后台、通用插件规范与自查清单 | B/C |

### API 参考（写插件时查）

| 文档 | 内容 | 主要受众 |
| ---- | ---- | -------- |
| [PluginContext (ctx)](../api/basic/ctx.md) | 插件可用能力的完整清单 | B |
| [Event 事件对象](../api/basic/event.md) | 消息字段、富媒体段、传播控制、权限 | B |
| [服务注册表 Services](../api/basic/services.md) | 官方插件能力如何取用、DI 机制 | B/C |
| [协议适配器 ProtocolAdapter](../api/advanced/protocol_adapter.md) | 接入端契约、ActionProxy、内置 http_inject 示例 | C |
| [Framework 核心对象](../api/basic/framework.md) | 底层容器、生命周期与高级用法 | C |

### 进阶 Advanced（深入机制）

| 文档 | 内容 | 主要受众 |
| ---- | ---- | -------- |
| [架构详解](../advanced/architecture.md) | 分层、启动时序、消息流转、事件总线 | C |
| [插件加载与模块机制](../advanced/loader.md) | 合成包、相对/短名导入、热重载原理、`__pycache__` 策略 | B/C |
| [数据库](../advanced/database.md) | 建表、CRUD、事务、SQLite/MySQL 方言适配 | B |
| [定时任务](../advanced/scheduler.md) | cron/interval/date 任务与底层 APScheduler | B |
| [权限系统](../advanced/permission.md) | 节点、权限组、继承、上下文、晋升轨道、审计 | B |
| [部署上线](../advanced/deployment.md) | systemd / Docker / 反向代理 / 安全清单 | A |

---

## 三、推荐学习路径

1. 按「安装 → 开始使用」把宿主跑通（连不接入平台 都行）；
2. 跟「编写插件」做出第一个能响应命令的插件；
3. 插件要拆多文件时，先读「插件加载与模块机制」；
4. 需要存数据、定时、权限、多轮交互时，查对应进阶文档；
5. 想脱离 IM 平台 当通用宿主，读「最佳实践」+「协议适配器」；
6. 日常开发随时查 `ctx` 与 `Event` 两份 API 参考。

---

## 四、目录约定速查

| 路径 | 作用 |
|------|------|
| `framework/` | **极简内核**：加载器、路由、事件、上下文、协议抽象、权限、数据库（不含任何 OneBot 实现） |
| `core_plugins/` | 官方插件：`onebot_adapter` / `http_inject` / `http_api` / `webui` / `session` / `scheduler` |
| `core_plugins.yaml` | **官方插件配置中心**：开关与配置集中于此，启动自动扫描同步并合并进主配置 |
| `plugins/` | 用户插件，每个一个子目录，入口为 `main.py` |
| `data/` | 运行数据：日志、SQLite、`plugins_dat/` 插件私有数据 |
| `tests/` | 自测脚本（如 `python tests/test_plugin_imports.py`） |

## 五、几个贯穿全局的设计约定

- **协议无关**：业务插件优先用 `ctx.send_msg / ctx.api` 这类中立接口，不直接依赖 平台特有字段，换接入端时零改动。
- **同步/异步双份 API**：普通函数 handler 用同步方法，`async def` handler 用带 `a` 前缀的异步方法（推荐异步，避免阻塞事件循环）。
- **代码与数据分离**：插件代码在 `plugins/<名>/`（更新会覆盖），运行期数据一律写 `ctx.get_data_dir()`（=`data/plugins_dat/<名>/`，长期保留）。
- **能力来自服务**：内核不直接 import 插件，官方能力通过服务注册表获取（如 `api_caller`、`scheduler`、`session_manager`、`web_server`）。
