---
layout: home

hero:
  name: ZCBOT
  text: 通用化 IM 平台
  tagline: 不绑定聊天协议——接平台、收 Webhook、跑定时任务都行，业务只写 Python 插件
  actions:
    - theme: brand
      text: 5 分钟跑起来
      link: /installation
    - theme: alt
      text: ZCBOT 是什么？
      link: /getting-started
    - theme: alt
      text: GitHub
      link: https://github.com/kuangxing6367/zcbot

features:
  - title: 一个平台，多种接入
    details: 接入端彼此并列——OneBot 11（QQ）、QQ 官方、Telegram、Discord、HTTP 注入、出站 WS；换接入端走统一事件与插件接口。
    link: /connect-im
  - title: 需要哪种能力就开哪个
    details: 官方扩展（接入端 / Web 后台 / 会话 / 定时 / 图片渲染…）默认全部关闭，在 core_plugins.yaml 里按需启用。
    link: /configuration
  - title: 改了就能重载
    details: 功能都在 plugins/ 里，一个文件夹一个插件；后台点「重载」立即生效，不影响在线。
    link: /writing-plugins
  - title: 每个环节都能挂钩子
    details: 启动、收消息、执行命令、发协议动作……几乎每个环节都能挂自己的逻辑（审计、限流、过滤）。
    link: /hooks
---

## 它解决什么问题

聊天消息、定时到点、外部 HTTP 回调——ZCBOT 把这些统一变成**事件**，交给 Python 插件处理，结果发回去。
事件路由、权限、数据库、热重载都是内置的，业务逻辑只写在 `register(ctx)` 里。

三个要点：

- **事件进来，插件处理**——路由、权限、数据库、热重载现成，只写业务逻辑本身。
- **不绑死任何平台**——OneBot 11 只是**可选接入端之一**，不装任何接入端照样能当纯定时服务或 Webhook 接收器。
- **改了就能重载**——功能都在 `plugins/` 里，一个文件夹一个插件，后台点「重载」立即生效。

```
聊天平台 / 定时 / Webhook
        │  事件进入
        ▼
   ZCBOT 内核（路由 · 权限 · 数据库）
        │  分发给插件
        ▼
  你的插件 register(ctx) → 回复 / 存库 / 调 API
```

接入端决定事件从哪来，插件决定收到后干什么，像搭积木一样组合。

## 快速上手

```bash
git clone https://github.com/kuangxing6367/zcbot.git
cd zcbot
pip install -r requirements.txt
python main.py
```

看到 `框架启动完成，等待消息...` 即启动成功。没装依赖也能跑：启动时会自动检测并补装缺失包。

> **官方扩展默认全部关闭**。要哪种能力就启用哪个扩展（`core_plugins.yaml` 改 `enabled: true`，
> 或 `python tools/scan_core_plugins.py --enable <名>`），重启生效。
> 进 Web 后台需启用 `webui`，收发聊天消息需启用某个接入端。

---

## 文档导航（全部平铺在 docs/ 一层）

### 指南

| 文档 | 内容 |
| ---- | ---- |
| [安装](installation.md) | 环境、依赖、目录结构、升级 |
| [开始使用](getting-started.md) | 启动、确定事件来源、进后台 |
| [配置系统](configuration.md) | 两份 yaml、插件开关、安全清单 |
| [对接 IM 平台](connect-im.md) | 反向 WS、多平台接入、富媒体与群管 |
| [编写插件](writing-plugins.md) | 手把手做出签到类完整插件 |
| [多轮会话](session.md) | `wait_for` / `create_session` |
| [接入大模型（LLM）](llm-chat.md) | 模型提供商总线、函数（工具）调用、Agent 循环 |
| [接入端契约与规范消息](adapter-contract.md) | 消息段跨协议统一、能力自述、通知事件命名 |
| [最佳实践](best-practices.md) | 不依赖 IM 的用法 + 写插件规范 |

### API（写插件时查）

| 你要做的事 | 打开这页 |
| ---- | ---- |
| 注册命令、发消息、查库、定时任务、WebUI、权限 | [PluginContext (ctx)](ctx.md) |
| 读消息字段、判断图片/@/回复、拦事件 | [Event 事件对象](event.md) |
| 改生命周期、拦 HTTP、审动作、自定义扩展点 | [扩展点（Hook 系统）](hooks.md) |
| 接 Telegram / Discord / MQTT / 自定义事件源 | [协议适配器 ProtocolAdapter](protocol_adapter.md) |
| 摸底层容器：生命周期、服务注册表、事件循环 | [Framework 核心](framework.md) |
| 取官方能力、查内置服务名 | [服务注册表（DI）](services.md) |
| 装饰器风格写插件 | [插件装饰器](plugin-decorators.md) |

### 进阶

| 文档 | 内容 |
| ---- | ---- |
| [内核设计哲学](core-philosophy.md) | 10 条设计信条 + 铁律（改框架前先读） |
| [架构详解](architecture.md) | 分层、启动时序、消息流转 |
| [插件加载机制](loader.md) | 多文件导入、热重载、踩坑 |
| [数据库](database.md) | 建表、CRUD、SQLite/MySQL |
| [定时任务](scheduler.md) | cron / interval / date |
| [权限系统](permission.md) | 节点、组、继承、审计 |
| [接口令牌（API Key）](api-tokens.md) | 两类 HTTP 接口、令牌创建与吊销 |
| [部署上线](deployment.md) | systemd / Docker / 反代 / 安全 |
| [双核心（core/host）](dual-core.md) | 双进程实验特性 |
| [项目结构](project-structure.md) | 目录、前端构建、建表 SQL |
| [扩展清单](plugins-catalog.md) | 官方扩展 / 内置用户扩展 / 官方扩展仓库 |

### 给 AI 编码助手（LLM）

| 文档 | 内容 |
| ---- | ---- |
| [LLM 索引](../LLM.md) | 按需加载入口，先读它 |
| [框架结构（LLM）](llm-framework.md) | 目录、事件流水线、扩展点、服务名 |
| [插件开发（LLM）](llm-plugins.md) | 最小模板、ctx 常用面、坑 |
| [调试排错（LLM）](llm-debugging.md) | 跑测试、排查报错 |

---

## 典型用途

- **QQ / TG / Discord 机器人**：命令菜单、AI 对话、自动回复
- **定时自动化**：日报、健康检查、到点推送（不需要任何聊天平台）
- **事件驱动服务**：GitHub / 支付回调 → 插件处理 → 回调或通知
- **带权限的内部工具**：多用户后台、审计日志、接口令牌

## 贯穿全局的约定

- **协议无关**：优先 `ctx.send_msg` / `ctx.actions`，少依赖平台特有字段
- **同步 / 异步双份**：普通 `def` 用同步方法；`async def` 用 `a` 前缀异步方法（推荐）
- **代码与数据分离**：代码在 `plugins/<名>/`（更新会覆盖），数据写 `ctx.get_data_dir()`
- **能力来自服务**：官方能力经服务注册表取用（`api_caller`、`scheduler`、`session_manager`…），平台内核不直接 import 插件
