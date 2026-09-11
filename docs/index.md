---
layout: home

hero:
  name: ZCBOT
  text: 微内核式通用事件服务宿主
  tagline: 内核只做三件事——加载插件、路由事件、暴露扩展点契约；事件从哪来、业务做什么，全部由你插拔
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
  - title: 微内核，不是又一个「框架」
    details: 内核只负责加载插件、路由事件、暴露扩展点（hook）契约；IM 接入、Web 后台、会话、定时、权限……全是挂在内核上的扩展，可增可减。
    link: /advanced/architecture
  - title: 不限 IM，任意事件源
    details: 内置 OneBot 11 与 HTTP Webhook（http_inject）接入，纯定时任务也能跑；换一个 ProtocolAdapter 即可接 Telegram / Discord / MQTT 或任意系统，业务插件零改动。
    link: /api/advanced/protocol_adapter
  - title: 扩展点，处处可插
    details: 12 个标准扩展点覆盖启动/关闭、Web 请求、事件分发、命令执行、协议动作、出站文本；ctx.hook() 一行挂上去，做审计、限流或中间件。
    link: /api/advanced/hooks
  - title: 自带治理与持久化
    details: LuckPerms 风格权限、接口令牌、审计日志、多用户 Web 后台，SQLite / MySQL 双方言持久化，多用户与多场景开箱可用。
    link: /advanced/permission
---

## 它是什么

ZCBOT 是一个**微内核式的通用事件服务宿主**：内核极小（加载、路由、扩展点契约），
其余能力——接入平台、Web 后台、会话、定时、权限、数据库——全部是**可插拔的扩展**。

它不绑任何平台：框架内核**不含任何 IM 协议实现**，OneBot 11 只是 `onebot_adapter` 这个官方插件。
关掉它，ZCBOT 照样能作为**纯定时服务**或 **HTTP Webhook 接收器**运行。

## 谁适合用（使用人群）

| 你是 | ZCBOT 给你什么 |
| --- | --- |
| 做 IM 机器人 / 群管工具 | 白拿接入、权限、多轮会话、定时、Web 后台，只写 `register(ctx)` 里的业务 |
| 要接非 IM 事件源（Webhook / 定时 / MQTT / 任意系统） | 用内置 `http_inject` / `scheduler`，或自写 `ProtocolAdapter`；复用同一套插件、权限与后台 |
| 想把一批脚本 / 运维任务收成「事件 → 处理 → 响应」服务 | 内核自带调度、持久化、鉴权、可插拔前端，不必自己搭壳 |
| 想给现成系统加后台 / 权限 / 审计 | 微内核 + 扩展点切面，以最小侵入挂载 |

## 能用来做什么（使用范围）

- **接入层**：OneBot 11（默认）/ HTTP Webhook 注入 / 纯定时 / 自写 `ProtocolAdapter`（Telegram、Discord、MQTT…）
- **业务层**：命令、事件订阅、多轮会话、定时任务、任意 Python 逻辑
- **治理层**：LuckPerms 风格权限引擎、接口令牌、审计日志、多用户 Web 管理后台
- **数据层**：SQLite / MySQL 双方言持久化，自动建表与 schema 迁移
- **表现层**：可被插件接管 / 扩展的 Web 后台、仪表盘卡片、CLI 终端、扩展点切面

## 参考示例（不止 IM）

下面四个例子用的是**同一个插件模型**（`register(ctx)`），换接入端不改业务代码。

### 1) IM 机器人（OneBot 11，默认接入端）

```python
# plugins/greeter/main.py —— 群里发 /hello 就回一句
def register(ctx):
    ctx.command("/hello", on_hello, description="打个招呼")

def on_hello(event, match):
    ctx.send_msg(group_id=event.group_id, user_id=None, message="Hello, World!")
```

### 2) 纯定时任务服务（连 IM 都不开）

```python
# plugins/report/main.py —— 每天 8:00 跑一次，完全不需要任何接入端
def register(ctx):
    ctx.task("0 8 * * *", daily_report, description="每日报表")

def daily_report():
    n = ctx.db_query("SELECT count(*) AS c FROM users")[0]["c"]
    ctx.log(f"当前用户数: {n}")
```

### 3) HTTP Webhook 事件源（外部系统把事件 POST 进来）

在 `core_plugins.yaml` 开启官方插件 `http_inject` 后，任何系统都能推事件进来：

```bash
curl -X POST http://127.0.0.1:8901/hook \
     -H 'Content-Type: application/json' \
     -d '{"type":"message","user_id":10001,"message":"/hello"}'
```

事件被归一化后进入同一个内核，上面第 1 个例子的命令照常触发——**不依赖任何 IM**。
另见[对接 IM / 事件源](./guide/getting-started.md)。

### 4) 换一个接入端，或挂一个切面

```python
# 接任意平台：实现 ProtocolAdapter 子类即可（见「协议适配器」）
# 挂切面：一行挂到任意扩展点，做审计 / 限流 / 中间件
def register(ctx):
    ctx.hook("action.after", audit)

def audit(action, params, bot, result):
    ctx.log(f"[audit] {action} -> {result.get('status')}")
```

## 三分钟跑起来

```bash
git clone https://github.com/kuangxing6367/zcbot.git
cd zcbot
python main.py
```

启动后打开 `http://127.0.0.1:8080` 进入 Web 管理后台。**不需要 IM 接入端也能跑**：
在 `core_plugins.yaml` 里开启 `http_inject`，用一条 `curl` 就能注入事件（见上方示例 3）。

详见[安装](./guide/installation.md)与[开始使用](./guide/getting-started.md)；
想理解「内核 + 扩展点」的设计，看[架构总览](./advanced/architecture.md)与[扩展点（Hook 系统）](./api/advanced/hooks.md)。
