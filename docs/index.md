---
layout: home

hero:
  name: ZCBOT
  text: 事件驱动的插件化服务宿主
  tagline: 把「事件进来 → 插件处理 → 给出响应」的骨架做好，你只写业务
  actions:
    - theme: brand
      text: 快速开始
      link: /guide/installation
    - theme: alt
      text: 它是什么
      link: /guide/
    - theme: alt
      text: GitHub
      link: https://github.com/kuangxing6367/zcbot

features:
  - title: 插件化内核
    details: 内核只负责加载插件、路由事件、提供公共能力；连 IM、开后台、管会话、跑定时都是可插拔的官方插件。
    link: /advanced/architecture
  - title: 协议中立
    details: 换一个 ProtocolAdapter，就能接入 HTTP Webhook、定时事件、Telegram / Discord 或任意其它平台，业务插件零改动。
    link: /api/protocol_adapter
  - title: 权限与治理
    details: 内置 LuckPerms 风格权限引擎、接口令牌、审计日志、Web 管理后台，多用户场景开箱可用。
    link: /advanced/permission
  - title: 持久化与会话
    details: SQLite / MySQL 双方言持久化，多轮会话、定时任务、文件与日志管理全部内置。
    link: /advanced/database
---

## 它不绑任何平台

ZCBOT 起步于 OneBot 11 接入端，但插件化、权限、持久化、Web 后台这些骨架从一开始就是通用的。
现在框架内核**不包含任何 IM 协议实现**——OneBot 11 只是 `onebot_adapter` 这个官方插件，
关掉它，框架照样能作为纯定时服务或 HTTP Webhook 接收器运行。

```python
# 一个最小插件：plugins/hello/main.py
__plugin_meta__ = {
    "name": "Hello",
    "version": "1.0.0",
    "author": "你的名字",
    "desc": "一个简单的 Hello 插件",
    "priority": 50,
}

def register(ctx):
    ctx.command("/hello", handle_hello, description="打个招呼")

def handle_hello(event, match):
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message="Hello, World!"
    )
```

## 三分钟跑起来

```bash
git clone https://github.com/kuangxing6367/zcbot.git
cd zcbot
python main.py
```

启动后打开 `http://127.0.0.1:8080` 进入 Web 管理后台。不需要 IM 接入端也能跑：
在 `core_plugins.yaml` 里开 `http_inject`，用一条 `curl` 就能注入事件。

详见[安装](./guide/installation.md)与[开始使用](./guide/getting-started.md)。
