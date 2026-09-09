# ZCBOT 文档

ZCBOT 是一个基于 OneBot 11 协议、插件化的 QQ 机器人框架。

## 快速开始

- [安装](./installation.md) —— 环境要求、依赖安装、目录结构
- [开始使用](./getting-started.md) —— 启动、连接 QQ、第一次对话
- [编写插件](./writing-plugins.md) —— 从零写一个完整插件（**新手必读**）
- [多轮会话](./session.md) —— 一问一答 / 连续多轮交互
- [配置系统](./configuration.md) —— config.yaml 与插件配置

## API 参考

- [PluginContext (ctx)](../api/ctx.md) —— 插件可用能力的完整清单
- [Event 事件对象](../api/event.md) —— 消息字段、富媒体、传播控制、权限
- [Framework 核心对象](../api/framework.md) —— 底层容器与高级用法
- [ServiceRegistry 服务注册表](../api/services.md) —— 官方插件能力的取用
- [协议适配器](../api/protocol_adapter.md) —— OneBot 11 封装与自定义协议

## 进阶主题

- [架构详解](../advanced/architecture.md) —— 分层、启动时序、消息流转、事件总线
- [插件加载与模块机制](../advanced/loader.md) —— 相对导入/短名导入、热重载原理（**多文件插件必读**）
- [数据库](../advanced/database.md) —— 建表、CRUD、事务、SQLite/MySQL 适配
- [定时任务](../advanced/scheduler.md) —— cron 任务与底层 APScheduler
- [权限系统](../advanced/permission.md) —— 节点、权限组、继承、上下文、晋升轨道
- [部署上线](../advanced/deployment.md) —— systemd/Docker/反代/安全清单

## 学习路径建议

1. 按「安装 → 开始使用」跑通机器人；
2. 跟着「编写插件」做出第一个插件；
3. 插件要拆多个文件时，先读「插件加载与模块机制」，避免导入踩坑；
4. 需要存数据、定时、权限、多轮交互时，查对应进阶文档；
5. 写插件过程中随时查 `ctx` 与 `Event` 两份 API 参考。

## 目录约定速查

| 路径 | 作用 |
|------|------|
| `framework/` | 框架核心（加载器、路由、事件、上下文、数据库） |
| `core_plugins/` | 官方插件（协议适配、WebUI、会话、调度器），可在配置中开关 |
| `plugins/` | 用户插件，每个一个子目录，入口为 `main.py` |
| `data/` | 运行数据：日志、SQLite、`plugins_dat/` 插件私有数据 |
| `tests/` | 自测脚本（如 `python tests/test_plugin_imports.py`） |
