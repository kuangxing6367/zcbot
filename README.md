# ZCBOT

基于 Python 的 OneBot v11 协议 QQ 机器人框架，支持插件化扩展。

项目地址：https://github.com/kuangxing6367/zcbot

## 特性

- 支持 OneBot v11 协议（反向 WebSocket 连接）
- 插件化架构，热加载 / 热卸载，支持动态注册指令
- 默认 SQLite 零配置开箱即用，可选 MySQL
- 内置 Web UI 管理面板（用户管理、插件管理、指令管理、日志中心、定时任务）
- 完善的权限体系（超级管理员、群管理、普通用户）
- 插件依赖冲突检测与隔离虚拟环境
- LLM 对话集成
- 日志系统支持多级过滤与关键词搜索

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
  host: 0.0.0.0
  port: 8081

plugin:
  dir: "../zcbot_plugins"
```

### 启动

```bash
python main.py
```

Web 管理面板访问 `http://localhost:8081`，默认账号 `admin`，密码 `admin`。

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
    """注册插件指令"""
    ctx.command("/hello", handle_hello, description="打招呼")

def handle_hello(event, match):
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
