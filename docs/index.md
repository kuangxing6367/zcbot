---
layout: home

hero:
  name: ZCBOT
  text: 事件驱动的 IM 平台
  tagline: 接上聊天软件、写几行 Python，就能跑机器人、定时任务和自动化——权限、后台、数据库都现成
  actions:
    - theme: brand
      text: 5 分钟跑起来
      link: /guide/installation
    - theme: alt
      text: ZCBOT 是什么？
      link: /guide/
    - theme: alt
      text: GitHub
      link: https://github.com/kuangxing6367/zcbot

features:
  - title: 起步就带全套
    details: 插件热加载、Web 管理后台、多用户权限、SQLite / MySQL、定时任务——启动就有，业务只写命令本身。
    link: /guide/getting-started
  - title: 一个平台，多种接入
    details: 内置 OneBot 11（QQ）、QQ 官方、Telegram、Discord、HTTP Webhook 与出站 WS；换接入端，走统一事件与插件接口（群管等协议专有动作各端能力不同）。
    link: /guide/connect-im
  - title: 改了就能重载
    details: 功能都在 plugins/ 里，一个文件夹一个插件；后台点「重载」立即生效，不影响在线。
    link: /guide/writing-plugins
  - title: 每个环节都能挂钩子
    details: 启动、收消息、执行命令、发协议动作……几乎每个环节都能挂自己的逻辑（审计、限流、过滤）。
    link: /api/advanced/hooks
---

## 它解决什么问题

聊天消息、定时到点、外部 HTTP 回调——ZCBOT 把这些统一变成**事件**，交给 Python 插件处理，结果发回去。事件路由、权限、数据库、热重载都是内置的，业务逻辑只写在 `register(ctx)` 里。

三个要点：

- **事件进来，插件处理**——路由、权限、数据库、热重载现成，只写业务逻辑本身。
- **不绑死任何平台**——OneBot 11 只是默认接入端之一，关掉它照样能当纯定时服务或 Webhook 接收器。
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
python main.py
```

没装依赖也能跑：启动时会自动检测并补装缺失包。

1. 打开 `http://127.0.0.1:8080`，用 `admin / admin123` 登录后台（**请立刻改密**）。
2. 按需接一个聊天平台，见 [对接 IM](./guide/connect-im.md)。
3. 向机器人发 `/echo 你好`，能原样回复就通了。
4. 跟着 [编写插件](./guide/writing-plugins.md) 写第一条命令。

## 文档导航

| 文档 | 内容 |
| ---- | ---- |
| [安装](./guide/installation.md) · [开始使用](./guide/getting-started.md) | 环境、依赖、目录；启动、第一次对话、Web 后台 |
| [对接 IM 平台](./guide/connect-im.md) | 反向 WS、多平台接入、富媒体与群管 |
| [编写插件](./guide/writing-plugins.md) · [多轮会话](./guide/session.md) | 从零跟做完整命令；一问一答连续对话 |
| [配置系统](./guide/configuration.md) · [部署上线](./advanced/deployment.md) | 两份 yaml、插件开关、安全清单；systemd / Docker / 反代 |
| [最佳实践](./guide/best-practices.md) | 纯定时、Webhook、带权限后台的完整范式 |
| [API 参考](./api/basic/ctx.md) · [进阶专题](./advanced/architecture.md) | ctx / Event / 服务注册表 / 扩展点；架构、加载、数据库、权限 |

完整目录见 [文档总入口](./guide/)。

## 典型用途

- **QQ / TG / Discord 机器人**：命令菜单、AI 对话、自动回复（群管等动作各端能力不同，见 [对接 IM](./guide/connect-im.md)）
- **定时自动化**：日报、健康检查、到点推送（不需要任何聊天平台）
- **事件驱动服务**：GitHub / 支付回调 → 插件处理 → 回调或通知
- **带权限的内部工具**：多用户后台、审计日志、接口令牌

## 设计要点

- **协议无关**：业务优先走 `ctx.actions` 这类中立接口，换平台时事件与插件接口一致（协议专有动作需按端适配）
- **代码与数据分离**：插件目录更新会覆盖，运行数据在 `data/`，升级不丢
- **权限、后台、持久化内置**：不必再找一套管理系统

想深入结构，看 [架构详解](./advanced/architecture.md) 与 [扩展点](./api/advanced/hooks.md)。
