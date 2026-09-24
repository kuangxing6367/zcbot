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
    details: 插件热加载、Web 管理后台、多用户权限、SQLite / MySQL、定时任务——启动就有，你只写业务命令。
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

## 你遇到的场景，多半是这几种

想给群里加一个签到命令，或者让服务器每天定时跑一段脚本，再或者外部系统来了一条 Webhook 通知——事件进来，你处理，结果发回去。ZCBOT 干的就是这件事：聊天消息、定时到点、外部 HTTP，统一变成事件，交给你的 Python 插件处理。

三句话说清它是什么：

- **外面的事件进来，插件处理**——事件路由、权限、数据库、热重载都是现成的，你只写 `register(ctx)` 里的业务逻辑。
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

## 最短的一条线

第一次来，或者装到一半卡住——先把系统跑起来：

```bash
git clone https://github.com/kuangxing6367/zcbot.git
cd zcbot
python main.py
```

没装依赖也能跑：启动时会自动检测并补装缺失包。

1. 打开 `http://127.0.0.1:8080`，用 `admin / admin123` 登录后台（**请立刻改密**）。
2. 按需接一个聊天平台，见 [对接 IM](./guide/connect-im.md)。
3. 向机器人发 `/echo 你好`，能原样回复就通了。
4. 跟着 [编写插件](./guide/writing-plugins.md) 写你的第一条命令。

## 该从哪一篇读起

| 你的情况 | 从这里开始 |
| -------- | ---------- |
| 想先把系统跑起来、连上聊天软件 | [安装](./guide/installation.md) → [开始使用](./guide/getting-started.md) → [对接 IM](./guide/connect-im.md) |
| 要写业务功能（命令、签到、群管…） | [编写插件](./guide/writing-plugins.md) |
| 想接 Webhook、定时任务，或其它 IM | [最佳实践](./guide/best-practices.md) |
| 想改配置、上服务器部署 | [配置系统](./guide/configuration.md) · [部署](./advanced/deployment.md) |

完整目录见 [文档总入口](./guide/)。

## 拿它能做什么

- **QQ / TG / Discord 机器人**：命令菜单、AI 对话、自动回复（群管等动作各端能力不同，见 [对接 IM](./guide/connect-im.md)）
- **定时自动化**：日报、健康检查、到点推送（不需要任何聊天平台）
- **事件驱动服务**：GitHub / 支付回调 → 插件处理 → 回调或通知
- **带权限的内部工具**：多用户后台、审计日志、接口令牌

## 设计上你会在意的几件事

- **协议无关**：业务优先走 `ctx.actions` 这类中立接口，换平台时事件与插件接口一致（协议专有动作需按端适配）
- **代码与数据分离**：插件目录更新会覆盖，运行数据在 `data/`，升级不丢
- **权限、后台、持久化内置**：不必再找一套管理系统

想深入结构，看 [架构详解](./advanced/architecture.md) 与 [扩展点](./api/advanced/hooks.md)。
