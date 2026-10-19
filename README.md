<div align="center">

# ZCBOT

**通用化 IM 平台 —— 协议无关的事件驱动机器人框架**

内核极简 · 扩展点契约 · 官方插件集 · 业务写在自己的插件里

<br>

[![PyPI](https://img.shields.io/badge/PyPI-zcbot-3775A9?logo=pypi&logoColor=white)](https://pypi.org/project/zcbot/)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-green)](#开源协议)
[![Release](https://img.shields.io/badge/release-v1.8.9--beta.1-orange)](https://gitee.com/liquanfeng123/zcbot/releases)

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
[安装](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8C%87%E5%8D%97/%E5%AE%89%E8%A3%85.md) ·
[部署](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E8%BF%9B%E9%98%B6/%E9%83%A8%E7%BD%B2%E4%B8%8A%E7%BA%BF.md)。

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
[扩展点](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8E%A5%E5%8F%A3/%E9%AB%98%E7%BA%A7/%E6%89%A9%E5%B1%95%E7%82%B9Hook.md)）。
完整教程见 [编写插件](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8C%87%E5%8D%97/%E7%BC%96%E5%86%99%E6%8F%92%E4%BB%B6.md)。

## 文档

文档随仓库分发，Gitee 与 GitHub 同步（下方链接指向 Gitee，GitHub 上把域名替换即可）。

- **指南**：[安装](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8C%87%E5%8D%97/%E5%AE%89%E8%A3%85.md) · [开始使用](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8C%87%E5%8D%97/%E5%BF%AB%E9%80%9F%E5%BC%80%E5%A7%8B.md) · [配置](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8C%87%E5%8D%97/%E9%85%8D%E7%BD%AE%E7%B3%BB%E7%BB%9F.md) · [对接 IM](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8C%87%E5%8D%97/%E5%AF%B9%E6%8E%A5IM%E5%B9%B3%E5%8F%B0.md) · [编写插件](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8C%87%E5%8D%97/%E7%BC%96%E5%86%99%E6%8F%92%E4%BB%B6.md) · [多轮会话](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8C%87%E5%8D%97/%E5%A4%9A%E8%BD%AE%E4%BC%9A%E8%AF%9D.md) · [接入 LLM](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8C%87%E5%8D%97/%E6%8E%A5%E5%85%A5%E5%A4%A7%E6%A8%A1%E5%9E%8B.md) · [最佳实践](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8C%87%E5%8D%97/%E6%9C%80%E4%BD%B3%E5%AE%9E%E8%B7%B5.md)
- **API**：[ctx](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8E%A5%E5%8F%A3/%E5%9F%BA%E7%A1%80/%E6%8F%92%E4%BB%B6%E4%B8%8A%E4%B8%8B%E6%96%87.md) · [Event](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8E%A5%E5%8F%A3/%E5%9F%BA%E7%A1%80/%E4%BA%8B%E4%BB%B6%E5%AF%B9%E8%B1%A1.md) · [扩展点 Hook](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8E%A5%E5%8F%A3/%E9%AB%98%E7%BA%A7/%E6%89%A9%E5%B1%95%E7%82%B9Hook.md) · [协议适配器](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8E%A5%E5%8F%A3/%E9%AB%98%E7%BA%A7/%E5%8D%8F%E8%AE%AE%E9%80%82%E9%85%8D%E5%99%A8.md) · [Framework](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8E%A5%E5%8F%A3/%E5%9F%BA%E7%A1%80/%E6%A0%B8%E5%BF%83%E6%A1%86%E6%9E%B6.md) · [服务注册表](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8E%A5%E5%8F%A3/%E5%9F%BA%E7%A1%80/%E6%9C%8D%E5%8A%A1%E6%B3%A8%E5%86%8C%E8%A1%A8.md) · [插件装饰器](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8E%A5%E5%8F%A3/%E9%AB%98%E7%BA%A7/%E6%8F%92%E4%BB%B6%E8%A3%85%E9%A5%B0%E5%99%A8.md)
- **进阶**：[内核设计哲学](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E8%BF%9B%E9%98%B6/%E8%AE%BE%E8%AE%A1%E5%93%B2%E5%AD%A6.md) · [架构详解](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E8%BF%9B%E9%98%B6/%E6%9E%B6%E6%9E%84%E6%80%BB%E8%A7%88.md) · [加载机制](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E8%BF%9B%E9%98%B6/%E6%8F%92%E4%BB%B6%E5%8A%A0%E8%BD%BD%E6%9C%BA%E5%88%B6.md) · [数据库](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E8%BF%9B%E9%98%B6/%E6%95%B0%E6%8D%AE%E5%BA%93.md) · [定时任务](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E8%BF%9B%E9%98%B6/%E5%AE%9A%E6%97%B6%E4%BB%BB%E5%8A%A1.md) · [权限](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E8%BF%9B%E9%98%B6/%E6%9D%83%E9%99%90%E7%B3%BB%E7%BB%9F.md) · [接口令牌](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E8%BF%9B%E9%98%B6/%E6%8E%A5%E5%8F%A3%E4%BB%A4%E7%89%8C.md) · [部署](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E8%BF%9B%E9%98%B6/%E9%83%A8%E7%BD%B2%E4%B8%8A%E7%BA%BF.md) · [双核心](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E8%BF%9B%E9%98%B6/%E5%8F%8C%E6%A0%B8%E5%BF%83%E6%9E%B6%E6%9E%84.md) · [项目结构](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E8%BF%9B%E9%98%B6/%E9%A1%B9%E7%9B%AE%E7%BB%93%E6%9E%84.md) · [扩展清单](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E8%BF%9B%E9%98%B6/%E6%89%A9%E5%B1%95%E6%B8%85%E5%8D%95.md)
- **LLM / Agent**：[LLM.md](https://gitee.com/liquanfeng123/zcbot/blob/main/LLM.md) · [框架结构](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E9%80%9F%E6%9F%A5/%E6%A1%86%E6%9E%B6%E7%BB%93%E6%9E%84.md) · [插件开发](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E9%80%9F%E6%9F%A5/%E6%8F%92%E4%BB%B6%E5%BC%80%E5%8F%91.md) · [调试排错](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E9%80%9F%E6%9F%A5/%E8%B0%83%E8%AF%95%E6%8E%92%E9%94%99.md)

## 反馈交流

- 交流群（QQ）：**1060129201**
- 问题反馈：[Gitee Issues](https://gitee.com/liquanfeng123/zcbot/issues) ｜ [GitHub Issues](https://github.com/kuangxing6367/zcbot/issues)
- 官方扩展仓库：[zcbot_plugins](https://gitee.com/liquanfeng123/zcbot_plugins)

## 开源协议

MIT OR Apache-2.0，任选其一适用。

> 本项目以 AI 生成为主、人工辅助完成。用着顺手的话，给个 Star 吧 ⭐
