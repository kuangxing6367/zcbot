<div align="center">

# ZCBOT

**通用化 IM 平台 —— 协议无关的事件驱动机器人框架**

内核极简 · 扩展点契约 · 官方插件集 · 业务写在自己的插件里

<br>

[![PyPI](https://img.shields.io/badge/PyPI-zcbot-3775A9?logo=pypi&logoColor=white)](https://pypi.org/project/zcbot/)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-green)](#开源协议)
[![Release](https://img.shields.io/badge/release-v1.21.0-orange)](https://gitee.com/liquanfeng123/zcbot/releases)

<br>

[Gitee（国内）](https://gitee.com/liquanfeng123/zcbot) ｜
[GitHub（国际）](https://github.com/kuangxing6367/zcbot) ｜
[PyPI](https://pypi.org/project/zcbot/) ｜
[更新日志](https://gitee.com/liquanfeng123/zcbot/blob/main/CHANGELOG.md) ｜
[LLM 索引](https://gitee.com/liquanfeng123/zcbot/blob/main/LLM.md)

</div>

---

ZCBOT 是一个**不绑定任何聊天协议**的机器人框架。内核只负责事件分发、插件加载、权限与持久化；协议接入端、Web 后台、多轮会话、定时任务、LLM 能力全部是可选插件。换一个接入端就能接入另一种 IM，也可以不接任何平台，只跑定时任务或接收 HTTP 事件。

它把"能跑"做成"能长期稳定地跑"——单实例锁、崩溃守卫、安全模式、平台化依赖诊断、Web 后台安全提醒，都是为生产部署兜底，而不是只给你一套脚手架。

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
11. 🔒 **单实例锁 + 安全模式** —— 防止重复运行；持久配置错误时挂起「假启动」，让 `Type=simple` 守护进程停止无脑拉起，不再空吃冷启动税（崩溃计数文件 `error.log*` 清零后自愈）。
12. 🩺 **平台化依赖诊断** —— 插件加载失败自动归类（缺依赖 / 版本不兼容 / 系统运行库缺失），给出中文提示与 `pip install` 命令，并附原始报错，不再是一串英文堆栈。
13. 🔑 **Web 后台安全提醒** —— 用默认密码（`admin` / `admin123`）登录后，后台顶部提示「建议修改密码」并可一键跳转（建议，非强制）。
14. **LLM 子系统（可选插件）** —— 提供商与工具双总线、工具调用闭环、会话窗口、多模态与 MCP；内置本地知识库（混合检索 RAG，向量 + 全文，单 SQLite 文件）与调用链追踪面板。不启用时内核毫无感知。
15. **可观测性** —— `/healthz` 存活、`/readyz` 就绪（带逐项检查明细）、`/metrics`（Prometheus 文本格式）；插件元数据可用 `requires` 声明加载依赖，配置改完即时告诉你哪些要重启。

## 启动入口与目录约定

- **唯一入口 `startup.py`**：`python startup.py [config.yaml]`、`python startup.py code`、
  `python startup.py attach`、`python startup.py --iipc` 等全部从这里进入；
  `main.py` 只保留兼容转派，存量 systemd / NSSM / 旧脚本不用改。启动期细节统一在
  `framework/bootstrap/`，状态路径统一在 `framework/paths.py`。
- **状态统一在 `data/`**：`config.yaml`、`core_plugins.yaml`、`plugins/`、`plugins_dat/`、
  `logs/` 全部落 `<ZCBOT_HOME>/data/`；旧根目录布局由路径解析层兼容回退。
- **自动迁移（默认开启）**：内核 `framework/migration/` 在启动期先检测当前内核版本，
  再探测当前目录**可能**是哪一代版本留下的布局；判定为旧根目录布局时自动把上述
  状态资产收敛到 `data/`，已迁移过则零写盘。方向是**向后兼容**：新内核接住旧版目录，
  绝不覆盖已有文件、绝不改写配置内容、失败不阻断启动。
  关闭：在 `config.yaml` 的 `framework` 段写 `auto_migrate: false`；临时覆盖用
  `ZCBOT_AUTO_MIGRATE=0`。体检 / 预演：`python -m framework.migration --inspect` / `--dry-run`
  （旧脚本 `tools/migrate_state_to_data.py` 保留为兼容入口）。
- **代码与状态分离**：`framework/` 与 `core_plugins/`（或未来的
  `framework/core/plugins/`）是随包分发的内核代码，不写用户数据；
  `ZCBOT_HOME` 决定可写状态根目录，`ZCBOT_CONFIG_PATH` / `ZCBOT_CORE_PLUGINS_YAML`
  可单独覆盖，`ZCBOT_STATE_LAYOUT=data` 可强制新布局。

## 快速上手

### 前置：一定要用虚拟环境

框架会在运行期**自己调 `pip` 装插件依赖**（`sys.executable -m pip`）。不隔离环境会有三类问题：

- 现代发行版（Debian 12 / Ubuntu 23+ / 各类容器镜像）系统 Python 禁止全局 pip，
  直接报 `error: externally-managed-environment`，启动时所有自动补装全部失败；
- 装到系统 Python 里，插件之间版本冲突无法回退，也污染系统包管理器；
- 升级 / 回滚框架时无法整体丢弃旧依赖。

```bash
python3 -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows
.venv\Scripts\activate
```

之后所有 `pip` / `python` 都在这个环境里，**不要**用 `sudo pip`。

### 方式一 · pip 安装（推荐）

```bash
pip install zcbot
zcbot
```

首次启动会在 **`~/.zcbot/data`** 下自动生成配置 / 官方插件清单 / 用户插件 / 日志（可用环境变量 `ZCBOT_HOME` 指定其它根目录）。
看到 `框架启动完成，等待消息...` 即启动成功。pip 安装态自动启用新布局；源码 checkout 的
旧根目录布局会在启动时自动迁移（默认开启，可在配置里关闭），迁移前后都能正常启动。

升级：`pip install -U zcbot`。

### 方式二 · 源码运行

```bash
git clone https://gitee.com/liquanfeng123/zcbot.git   # 或 GitHub 镜像
cd zcbot
python3 -m venv .venv && source .venv/bin/activate    # Windows: py -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
python startup.py
```

唯一启动入口是 `startup.py`；`main.py` 只为存量 systemd / NSSM / 旧脚本保留兼容转派，行为一致。
配置与状态默认落在 `data/` 下，插件代码在 `core_plugins/`（内核自带）与 `data/plugins/`（用户插件）；
源码 checkout 若仍是旧根目录布局，启动时会自动迁移到 `data/`（默认开启，无需手工执行命令；
`framework.auto_migrate: false` 可关闭）。`requirements.txt` 只装内核必需依赖；
**官方插件专属依赖在启用该插件后自动补装到当前环境**——`webui` 要 `flask`/`flask-cors`/`waitress`，
`image_renderer` 要 `Pillow`，`llm_load` 要 `sqlite-vec`，`aiwriter` 要 `textual`。所以上一步的 venv 不是可选项。

国内网络建议始终带镜像源参数；插件自动补装也内置了清华→阿里→豆瓣→官方 的回退列表。

> **官方插件默认关闭，但 `echo` / `help` / `scheduler` / `webui` 默认开启。** 需要哪种能力就启用哪个：把 `core_plugins.yaml` 里对应块的 `enabled`
> 改成 `true`，或运行 `python tools/scan_core_plugins.py --enable <插件名>`，重启生效。
> `webui` 默认监听 `127.0.0.1:8080`（默认账号 `admin` / `admin123`，**登录后后台会自动建议改密**，一键跳转设置）；
> `core_plugins.yaml` 只负责官方插件的 `enabled` 开关与插件私有键；端口、监听地址、Token、AppSecret
> 等运行参数一律写在 `config.yaml` 的对应段（如 `web` / `onebot` / `telegram`），且 `config.yaml` 优先。
> 启动时会自动把 `core_plugins.yaml` 中与 `config.yaml` 重叠的键收敛掉，两处都写不会生效两次。
> 要收发聊天消息需启用某个接入端（如 `onebot_adapter`），凭证写在 `config.yaml` 对应段。

更多方式（Docker、反向代理、HTTPS、纯 API 部署）见
[安装](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E6%8C%87%E5%8D%97/%E5%AE%89%E8%A3%85.md) ·
[部署](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/%E8%BF%9B%E9%98%B6/%E9%83%A8%E7%BD%B2%E4%B8%8A%E7%BA%BF.md)。

## 支持的接入端

| 接入端 | 说明 |
| --- | --- |
| OneBot 11 | 反向 WebSocket（`onebot_adapter`），另有 Rust 加速实现（`rust_accel`，二选一） |
| 正向 WS | `ws_client`，框架主动连协议端 |
| QQ 官方 | `qq_official`，QQ 开放平台机器人 |
| Telegram / Discord | `telegram` / `discord`，Bot API / Gateway |
| KOOK / Matrix | `kook` / `matrix`，Gateway WS / Client-Server API v3 |
| HTTP 注入 / HTTP API | `http_inject` 事件注入、`http_api` 对外 REST，可不接任何 IM |

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

**MIT OR Apache-2.0**，任选其一适用（双授权，与 Rust 生态惯例一致）。

| 文件 | 内容 |
|------|------|
| [`LICENSE`](LICENSE) | MIT 全文（最常见的选用入口） |
| [`LICENSE-APACHE`](LICENSE-APACHE) | Apache License 2.0 全文 |
| [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) | WebUI 打包的第三方组件（element-plus / vue / vue-router，均为 MIT）版权声明 |

版权人：`zgric`。

选 Apache-2.0 的话额外获得明确的专利授权条款；只想要最简单条款就用 MIT。

> WebUI 产物里的 `THIRD-PARTY-NOTICES.txt` 是上述第三方声明的纯文本副本 ——
> 构建时的压缩与混淆会把源码里的版权注释删干净，MIT 要求副本保留声明，
> 所以它必须随产物一起分发，CI 有门禁校验。

> 本项目以 AI 生成为主、人工辅助完成。用着顺手的话，给个 Star 吧 ⭐
