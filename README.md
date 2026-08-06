# ZCBOT

基于 Python 的 OneBot v11 协议 QQ 机器人框架，支持插件化扩展。

项目地址：https://github.com/kuangxing6367/zcbot

## 特性

- 支持 OneBot v11 协议（反向 WebSocket 连接）
- **全异步架构**：消息处理、API 调用、定时任务均不阻塞事件循环；插件 handler 支持 `async def`（旧同步插件自动兼容）
- 插件化架构，热加载 / 热卸载，支持动态注册指令
- 默认 SQLite 零配置开箱即用，可选 MySQL（自动翻译 MySQL 方言 SQL）
- 内置统一 Web 管理面板（参考 AstrBot 设计：侧边导航 + 深色主题，覆盖仪表盘、插件、命令、用户、群组、任务、日志、设置）
- 完善的权限体系（超级管理员、群管理、普通用户），登录防爆破
- 插件依赖自动安装（基于全局环境，版本冲突自动跳过），支持手动创建插件虚拟环境（存放于插件数据目录）
- 日志系统支持多级过滤、关键词搜索与 SSE 实时推送
- 数据库自动重连：MySQL 连接断开（wait_timeout/服务重启/网络中断）后自动 ping 保活并重连，不再卡死

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
  dir: "../zcbot_plugins"
```

### 启动

```bash
python main.py
```

Web 管理面板访问 `http://localhost:8080`（配置见 `web.port`），默认账号 `admin`，默认密码 `admin123`。

> ⚠️ 安全提示：首次部署请立即修改默认密码，并在 `config.yaml` 中设置 `onebot.access_token`（留空则任何客户端都能接入）。Web 面板如需公网访问，请改为监听 `0.0.0.0` 并注意防护。

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

## 开源协议

本项目采用 **MIT 许可证**（同时兼容 Apache 2.0 许可证），详见 [LICENSE](./LICENSE) 文件。

## 免责声明

本框架仅提供插件运行环境，所有第三方插件由开发者独立维护。框架作者不对任何第三方插件的安全性、稳定性、合法性负责。使用第三方插件前请自行审查代码，风险自负。
