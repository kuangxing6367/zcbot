<div align="center">

# ZCBOT

**通用化 IM 平台 —— 协议无关的事件驱动机器人框架**

内核极简 · 扩展点契约 · 官方插件集 · 业务写在自己的插件里

<br>

[![PyPI](https://img.shields.io/badge/PyPI-zcbot-3775A9?logo=pypi&logoColor=white)](https://pypi.org/project/zcbot/)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-green)](#开源协议)
[![Release](https://img.shields.io/badge/release-v1.8.8-76bad9)](https://gitee.com/liquanfeng123/zcbot/releases)

<br>

[Gitee（国内）](https://gitee.com/liquanfeng123/zcbot) ｜
[GitHub（国际）](https://github.com/kuangxing6367/zcbot) ｜
[PyPI](https://pypi.org/project/zcbot/) ｜
[更新日志](https://gitee.com/liquanfeng123/zcbot/blob/main/CHANGELOG.md) ｜
[LLM 索引](https://gitee.com/liquanfeng123/zcbot/blob/main/LLM.md)

</div>

---

ZCBOT 是一个**不绑定任何聊天协议**的机器人框架。内核只负责事件分发、插件加载、权限与持久化；协议接入端、Web 后台、多轮会话、定时任务、LLM 能力全部是可选插件。换一个接入端就能接入另一种 IM，也可以不接任何平台，只跑定时任务或接收 HTTP 事件。

## 核心特性

1. 🧩 **协议无关内核** —— 内核不含任何协议实现，OneBot 11 只是默认接入端之一。
2. 🔌 **可插拔接入端** —— 内置 OneBot 11、QQ 官方、Telegram、Discord、出站 WS、HTTP 注入 / HTTP API。
3. 🪝 **扩展点（Hook）系统** —— 可挂到启动 / 关闭、Web 请求、事件分发、命令执行、协议动作等几乎每一个运行环节。
4. 🔥 **插件热加载** —— 放进 `plugins/` 即生效；用装饰器声明命令、权限、定时任务。
5. 🖥️ **Web 管理后台** —— Vue 3 + Element Plus；插件、用户、权限、日志、数据库一站管理。
6. 🔐 **权限与令牌** —— 节点式权限引擎 + 接口令牌（API Key），细粒度控制到命令级。
7. ⏱️ **多轮会话 / 定时任务** —— 会话状态与调度器均为官方插件，按需启用。
8. 🗄️ **双方言数据库** —— SQLite 零配置开箱即用，大环境一键切 MySQL。
9. 🛡️ **安全加固** —— 蜜罐诱饵、JS 人机验证、认证静默丢弃、可调安全等级（`off`~`paranoid`）。
10. ⚡ **双核心架构** —— 可选 core / host 双进程，插件崩溃不拖垮核心（实验特性，默认关闭）。

## 快速上手

### 方式一 · pip 安装（推荐）

```bash
pip install zcbot
zcbot
```

首次启动会在 **`~/.zcbot`** 自动生成配置与数据（可用环境变量 `ZCBOT_HOME` 指定其它目录）。
看到 `框架启动完成，等待消息...` 即启动成功。

升级：`pip install -U zcbot`。

### 方式二 · 源码运行

```bash
git clone https://gitee.com/liquanfeng123/zcbot.git   # 或 GitHub 镜像
cd zcbot
pip install -r requirements.txt
python main.py
```

配置与数据落在仓库目录下（行为同旧版）。依赖缺了启动时会自动补装。

> **官方插件默认全部关闭。** 需要哪种能力就启用哪个：把 `core_plugins.yaml` 里对应块的 `enabled`
> 改成 `true`，或运行 `python tools/scan_core_plugins.py --enable <插件名>`，重启生效。
> 例如进 Web 后台要启用 `webui`（`127.0.0.1:8080`，默认账号 `admin` / `admin123`，**登录后立即改密**）；
> 要收发聊天消息要启用某个接入端（如 `onebot_adapter`）。

更多方式（Docker、反向代理、HTTPS、纯 API 部署）见
[安装](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/installation.md) ·
[部署](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/deployment.md)。

## 支持的接入端

| 接入端 | 说明 |
| --- | --- |
| OneBot 11 | 反向 WebSocket，默认接入端（QQ 生态） |
| QQ 官方 | QQ 开放平台机器人 |
| Telegram | Bot API |
| Discord | Bot |
| 出站 WS / HTTP 注入 / HTTP API | 自定义协议或外部系统对接 |

> 不接任何平台也可以：内核可直接跑定时任务、或接收 HTTP 事件。

## 写一个插件

`plugins/hello/main.py`：

```python
__plugin_meta__ = {"name": "Hello", "version": "1.0.0",
                   "author": "你", "desc": "打招呼", "priority": 50}

def register(ctx):
    ctx.command("/hello", handle_hello, description="打个招呼")

async def handle_hello(event, match):
    await ctx.asend_msg(user_id=event.user_id,
                        group_id=event.group_id if event.is_group else None,
                        message="Hello, World!")
```

放进 `plugins/` 后点「重载」即可。权限用 `require_perm="myplugin.ban"` 声明式拦截；
想插到几乎每个运行环节就用 `ctx.hook(...)`（见
[扩展点](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/hooks.md)）。
完整教程见 [编写插件](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/writing-plugins.md)。

## 文档

文档随仓库分发，Gitee 与 GitHub 同步（下方链接指向 Gitee，GitHub 上把域名替换即可）。

- **指南**：[安装](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/installation.md) · [开始使用](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/getting-started.md) · [配置](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/configuration.md) · [对接 IM](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/connect-im.md) · [编写插件](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/writing-plugins.md) · [多轮会话](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/session.md) · [接入 LLM](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/llm-chat.md) · [最佳实践](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/best-practices.md)
- **API**：[ctx](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/ctx.md) · [Event](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/event.md) · [扩展点 Hook](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/hooks.md) · [协议适配器](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/protocol_adapter.md) · [Framework](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/framework.md) · [服务注册表](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/services.md) · [插件装饰器](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/plugin-decorators.md)
- **进阶**：[内核设计哲学](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/core-philosophy.md) · [架构详解](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/architecture.md) · [加载机制](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/loader.md) · [数据库](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/database.md) · [定时任务](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/scheduler.md) · [权限](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/permission.md) · [接口令牌](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/api-tokens.md) · [部署](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/deployment.md) · [双核心](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/dual-core.md) · [项目结构](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/project-structure.md) · [扩展清单](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/plugins-catalog.md)
- **LLM / Agent**：[LLM.md](https://gitee.com/liquanfeng123/zcbot/blob/main/LLM.md) · [框架结构](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/llm-framework.md) · [插件开发](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/llm-plugins.md) · [调试排错](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/llm-debugging.md)

## 反馈交流

- 交流群（QQ）：**1060129201**
- 问题反馈：[Gitee Issues](https://gitee.com/liquanfeng123/zcbot/issues) ｜ [GitHub Issues](https://github.com/kuangxing6367/zcbot/issues)
- 官方扩展仓库：[zcbot_plugins](https://gitee.com/liquanfeng123/zcbot_plugins)

## 开源协议

MIT OR Apache-2.0，任选其一适用。

> 本项目以 AI 生成为主、人工辅助完成。用着顺手的话，给个 Star 吧 ⭐
