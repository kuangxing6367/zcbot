# ZCBOT

基于 Python 的 OneBot v11 协议 QQ 机器人框架，支持插件化扩展。

**当前版本：v0.0.1-beta.0（公测版）**

项目地址：https://github.com/kuangxing6367/zcbot

> 本项目代码由 AI 完成为主、人工辅助完成。如有 bug 或建议，欢迎加 QQ 群 **1060129201** 反馈。

## 特性

- 支持 OneBot v11 协议（反向 WebSocket 连接）
- **全异步架构**：消息处理、API 调用、定时任务均不阻塞事件循环；插件 handler 支持 `async def`（旧同步插件自动兼容）
- 插件化架构，热加载 / 热卸载，支持动态注册指令
- 默认 SQLite 零配置开箱即用，可选 MySQL（自动翻译 MySQL 方言 SQL）
- 内置统一 Web 管理面板（侧边导航 + 深色主题，覆盖仪表盘、插件、命令、用户、群组、任务、日志、设置）
- 完善的权限体系（超级管理员、群管理、普通用户），登录防爆破
- 插件依赖自动安装（基于全局环境，版本冲突自动跳过），支持手动创建插件虚拟环境（存放于插件数据目录）
- 日志系统支持多级过滤、关键词搜索与 SSE 实时推送
- 数据库自动重连：MySQL 连接断开（wait_timeout/服务重启/网络中断）后自动 ping 保活并重连，不再卡死
- **MySQL 连接池**（DBUtils PooledDB）：连接数有上限且空闲回收（`pool_size`/`min_cached`/`max_cached` 可配），坏连接自动丢弃重建，池满时阻塞等待，杜绝连接无限增长
- **系统级关键词自动回复**（动态命令）：无需写插件即可配置 关键词→自动回复，支持 完全相等/前缀/包含/正则 四种匹配方式，Web 管理面板可视化增删改启停，插件未命中时兜底触发
- **双请求防破解认证系统**：蜜罐探针 + nonce 挑战机制，保护管理后台免受暴力破解

## 快速开始

### 环境要求

- Python 3.10+
- OneBot v11 兼容客户端（如 Lagrange、NapCat、go-cqhttp 等）

### 安装

```bash
# 克隆仓库
git clone https://github.com/kuangxing6367/zcbot.git
cd zcbot

# 安装依赖
pip install -r requirements.txt
```

> 框架启动时会自动检测并安装缺失依赖（走清华源 + 自动回退），首次部署无需手动处理。

### 配置

编辑 `config.yaml`（首次启动会自动生成默认配置）：

```yaml
database:
  type: sqlite                    # sqlite（默认）或 mysql
  path: data/zcbot.db

onebot:
  listen_host: 0.0.0.0
  listen_port: 6830
  access_token: "your-token"

web:
  host: 127.0.0.1
  port: 8080

plugin:
  dir: plugins                    # 插件代码目录
```

### 启动

```bash
python main.py
```

Web 管理面板访问 `http://localhost:8080`，默认账号 `admin`，默认密码 `admin123`。

> 安全提示：首次部署请立即修改默认密码，并在 `config.yaml` 中设置 `onebot.access_token`（留空则任何客户端都能接入）。Web 面板如需公网访问，请改为监听 `0.0.0.0` 并注意防护。

## 插件仓库

框架内置插件位于 `plugins/` 目录，开箱即用。此外还有独立的插件仓库，提供更多功能插件：

**插件仓库地址**：`zgric_onebot11_plugins/plugins`

使用时将插件仓库目录配置到 `config.yaml`：

```yaml
plugin:
  dir: ../zgric_onebot11_plugins/plugins   # 指向插件仓库
```

或直接将需要的插件目录复制到框架的 `plugins/` 下。

### 内置插件一览

| 插件 | 说明 |
| ---- | ---- |
| **echo** | 原样返回用户文本消息，无参数时返回 PONG |
| **help** | 查询所有已注册命令，生成图片帮助菜单 |
| **image_renderer** | 通用图片渲染引擎，Rust + pyo3 原生扩展优先，支持信息卡片和文字转图片 |
| **restart_manager** | 框架重启管理，支持通过指令重启 |
| **runtime_status** | 系统运行状态监控，仪表盘卡片展示 CPU/内存/磁盘 |

### 插件仓库扩展插件

| 插件 | 说明 |
| ---- | ---- |
| **file** | 文件处理插件 |
| **llm_chat** | LLM 对话插件，支持多模型切换、函数调用、人格预设、对话统计 |
| **llm_plugin_gen** | AI 驱动的插件开发助手，通过 LLM 编写和管理插件文件 |
| **qqadmin** | QQ 群管理插件，提供群成员管理、禁言、踢人等功能 |

## 原生扩展（image_renderer）

图片渲染插件内置 Rust + pyo3 原生渲染引擎（`zcbot_render`），**按架构强制绑定**：

| 平台 | 产物路径 |
| ---- | ---- |
| Windows x86_64 | `plugins/image_renderer/native/bin/win64/zcbot_render.pyd` |
| Linux x86_64 | `plugins/image_renderer/native/bin/linux-x86_64/zcbot_render.so` |
| Linux aarch64 | `plugins/image_renderer/native/bin/linux-aarch64/zcbot_render.so` |

仅支持以上架构；在其他架构运行或找不到对应二进制时，插件**自动回退 PIL（Pillow）渲染**，功能一致。原生扩展为 abi3 稳定 ABI，兼容 Python 3.9+。

## 插件开发

插件存放在独立目录中，每个插件为一个包，入口文件为 `main.py`：

```python
__plugin_meta__ = {
    "name": "示例插件",
    "version": "1.0.0",
    "author": "your-name",
    "desc": "插件描述",
    "priority": 50,
}

def register(ctx):
    """注册插件指令（ctx 为本次注册上下文，仅此处作为参数传入）"""
    ctx.command("/hello", handle_hello, description="打招呼")

def handle_hello(event, match):
    # 注意：handler 内的 ctx 是框架注入到插件模块级的全局变量
    #（register 被调用前由框架设置 module.ctx），可直接调用 ctx.api/send_msg 等
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message="你好！",
    )
```

除命令匹配外，插件还可通过 `ctx.on()` 订阅框架事件。**无文本的消息**（如纯分享卡片、纯图片）不参与命令匹配，框架会将其广播为 `message.share` / `message.media` 等事件，插件订阅即可处理：

```python
def register(ctx):
    ctx.on("message.share", on_share)  # 订阅分享卡片事件

def on_share(event):
    info = event.share  # {'url': ..., 'title': ..., 'desc': ...}
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=f"收到分享：{info.get('title')}",
    )
```

完整插件开发指南已拆分为多个独立文档，从 [docs/INDEX.md](./docs/INDEX.md) 入口开始阅读。

## 文档

### 插件开发系列

| 文档 | 内容 |
| ---- | ---- |
| [文档索引](./docs/INDEX.md) | 文档总览与推荐阅读路径 |
| [快速入门](./docs/getting-started.md) | 最小可运行插件、异步支持、API 速查表 |
| [插件目录结构](./docs/plugin-structure.md) | 代码/数据分离约定、元信息、生命周期 |
| [API 参考](./docs/api-reference.md) | `ctx` 全部方法、`Event` 对象、OneBot 11 API |
| [配置系统](./docs/configuration.md) | `plugin.yaml`、`_conf_schema.json`、依赖声明 |
| [最佳实践](./docs/best-practices.md) | 错误处理、资源清理、性能与安全 |
| [示例合集](./docs/examples.md) | 签到插件完整实现 |

### 其他

| 文档 | 内容 |
| ---- | ---- |
| [Web API 接口文档](./docs/API.md) | Web UI 后端 HTTP API 完整定义 |
| [更新日志](./CHANGELOG.md) | 版本变更记录 |

## 目录结构

```
zcbot/
├── main.py                  # 启动入口
├── config.yaml              # 配置文件（首次启动自动生成）
├── requirements.txt         # 核心依赖
├── VERSION                  # 版本号
├── framework/               # 框架核心
│   ├── core.py              # 框架引擎（异步模型）
│   ├── ctx.py               # 插件上下文 (PluginContext)
│   ├── event.py             # 事件对象
│   ├── loader.py            # 插件加载器
│   ├── router.py            # 消息路由（内存路由表）
│   ├── scheduler.py         # 定时任务调度器
│   ├── event_bus.py          # 事件总线
│   ├── apis.py               # Web API 服务端
│   ├── onebot_api.py         # OneBot 11 标准 API 封装
│   ├── websocket_handler.py  # WebSocket 服务端
│   ├── db.py                 # 数据库适配层
│   ├── config.py             # 配置加载
│   ├── log_broker.py         # 日志代理
│   └── dual_auth.py          # 双请求防破解认证
├── plugins/                 # 插件代码目录
├── data/                    # 运行时数据
│   ├── logs/                # 日志文件
│   ├── plugins_dat/         # 插件数据/配置（不被覆盖）
│   └── zcbot.db             # SQLite 数据库（默认）
├── sql/                     # 数据库初始化脚本
├── web/                     # Web 管理面板前端
└── docs/                    # 文档
```

## 开源协议

本项目采用 **MIT + Apache 2.0 双开源协议**，你可任选其一适用，详见 [LICENSE](./LICENSE) 文件。

## 免责声明

本框架仅提供插件运行环境，所有第三方插件由开发者独立维护。框架作者不对任何第三方插件的安全性、稳定性、合法性负责。使用第三方插件前请自行审查代码，风险自负。
