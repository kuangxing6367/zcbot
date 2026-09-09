# 配置系统

ZCBOT 使用 `config.yaml` 作为配置文件，首次启动时自动生成。

## 官方插件开关

```yaml
core_plugins:
  onebot_adapter: true    # OneBot 11 协议适配器
  webui: true             # Web 管理后台
  session: true           # 多轮会话管理器
  scheduler: true         # 定时任务调度器
```

设为 `false` 可禁用对应插件，重启后生效。

## 协议适配

```yaml
onebot:
  listen_port: 6830       # WebSocket 监听端口
  access_token: ""        # 接入令牌（必须设置！）
  enabled: true           # 是否启用
```

## Web 管理后台

```yaml
web:
  host: 127.0.0.1         # 监听地址（0.0.0.0 = 公网可访问）
  port: 8080              # 监听端口
  session_timeout: 3600   # 登录会话超时（秒）
  enabled: true           # 是否启用
```

## 数据库

```yaml
# SQLite（默认，零配置）
database:
  type: sqlite
  path: data/zcbot.db

# MySQL
database:
  type: mysql
  host: 127.0.0.1
  port: 3306
  user: root
  password: ""
  database: zcbot
```

## 插件配置

```yaml
plugin:
  dir: plugins                    # 插件代码目录
  heartbeat_interval: 60          # 心跳间隔（秒）
  auto_install_deps_on_startup: true  # 自动安装缺失依赖
  max_memory_mb: 64               # 单插件内存上限
```

## 日志

```yaml
log:
  level: INFO                     # DEBUG / INFO / WARNING / ERROR
  file: data/logs/zcbot.log       # 日志文件路径
  log_raw_message: true           # 记录原始消息
  log_sent_message: true          # 记录发送消息
```

## 安全配置

```yaml
security:
  fake_token_len: 8               # 蜜罐探针长度
  real_token_len: 8192            # 真实认证 Token 长度
  nonce_len: 16                   # Nonce 长度
  nonce_expiry: 60                # Nonce 有效期（秒）
  blacklist_enabled: true         # 启用黑名单
  whitelist_ips:                  # 白名单 IP
    - "127.0.0.1"
```

## 插件配置（plugin.yaml）

每个插件可在目录下创建 `plugin.yaml` 声明元信息：

```yaml
name: 我的插件
version: 1.0.0
author: 你的名字
desc: 插件描述
priority: 50

# Python 依赖
dependencies:
  - requests>=2.28
  - Pillow>=10.0

# GitHub 更新源
github:
  repo: kuangxing6367/zcbot_plugins
  branch: main
  sub_path: plugins/my_plugin
```

## 插件配置 schema（_conf_schema.json）

让插件支持 Web UI 配置：

```json
{
  "api_key": {
    "type": "string",
    "default": "",
    "description": "API 密钥",
    "hint": "在第三方平台获取"
  },
  "timeout": {
    "type": "number",
    "default": 30,
    "description": "请求超时（秒）",
    "hint": "建议 10-60"
  },
  "mode": {
    "type": "select",
    "default": "auto",
    "description": "工作模式",
    "options": [
      {"label": "自动", "value": "auto"},
      {"label": "手动", "value": "manual"}
    ]
  }
}
```

插件通过 `ctx.get_config(key)` 读取配置值。
