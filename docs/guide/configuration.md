# 配置系统

ZCBOT 使用 `config.yaml` 作为全局配置，首次启动自动生成。
也可以在启动时传入自定义配置路径：`python main.py /path/to/config.yaml`。

## 官方插件开关

```yaml
core_plugins:
  onebot_adapter: true    # OneBot 11 协议适配器（连 QQ 必备）
  webui: true             # Web 管理后台
  session: true           # 多轮会话管理器（ctx.wait_for/create_session）
  scheduler: true         # 定时任务调度器（ctx.task）
  http_api: false         # 独立 HTTP API（默认关闭）
```

设为 `false` 禁用对应官方插件，**重启后生效**。关闭某个服务后，
依赖它的 `ctx` 能力（如会话、定时任务、API 调用）会不可用。

## 协议适配（OneBot 11）

```yaml
onebot:
  listen_port: 6830       # WebSocket 服务端监听端口（等 OneBot 客户端反向连入）
  access_token: ""        # 接入令牌，公网部署必须设置非空强随机值
  enabled: true           # 是否启用
```

ZCBOT 作为 WebSocket **服务端**，由 NapCat / Lagrange 等 OneBot 实现反向连接，
配置方式见 [开始使用](./getting-started.md#连接-qq)。

## Web 管理后台

```yaml
web:
  host: 127.0.0.1         # 监听地址；0.0.0.0 = 监听所有网卡（公网可访问，务必配合令牌/白名单）
  port: 8080              # 监听端口
  session_timeout: 3600   # 登录会话超时时长（秒）
  enabled: true           # 是否启用
```

## 独立 HTTP API

```yaml
http_api:
  host: 127.0.0.1         # 仅本机访问更安全
  port: 1145              # HTTP API 端口
  token: ""               # 调用令牌；留空则首次自动生成
```

需要先把 `core_plugins.http_api` 置为 `true`。

## 数据库

```yaml
# SQLite（默认，零配置，数据落在单文件）
database:
  type: sqlite
  path: data/zcbot.db

# MySQL（高并发/多群推荐）
database:
  type: mysql
  host: 127.0.0.1
  port: 3306
  user: root
  password: ""
  database: zcbot
```

插件 SQL 统一写 `%s` 占位、`AUTOINCREMENT` 自增，框架自动适配方言，
详见 [数据库](../advanced/database.md)。

## 插件相关

```yaml
plugin:
  dir: plugins                        # 用户插件代码目录
  heartbeat_interval: 60              # 心跳间隔（秒）：检测文件变化并重跑 register
  auto_install_deps_on_startup: true  # 启动/加载时自动安装插件缺失依赖
  max_memory_mb: 64                   # 单插件估算内存上限（MB），连续超限自动卸载
```

:::warning 心跳 ≠ 完全重载
心跳只对改动过的插件重新执行 `register(ctx)`，不会重新 import 代码。
修改了函数体逻辑后，请在 Web 面板点「重载」走 unload + load，
原理见 [插件加载与模块机制](../advanced/loader.md)。
:::

## 日志

```yaml
log:
  level: INFO                 # DEBUG / INFO / WARNING / ERROR
  file: data/logs/zcbot.log   # 日志文件
  log_raw_message: true       # 记录收到的原始消息
  log_sent_message: true      # 记录发出的消息
```

插件内用 `ctx.log()` 或标准 `logging` 输出，会自动带插件名前缀。

## 系统状态展示

```yaml
system:
  show_cpu: true             # 面板展示 CPU 状态
  show_disk: true            # 面板展示磁盘状态
  status_interval: 30        # 状态采样间隔（秒）
```

## GitHub 加速

```yaml
github_proxy: ""             # 插件更新走的 GitHub 代理前缀，留空直连
```

从 GitHub 拉取插件更新较慢时，可填镜像代理地址。

## 安全配置

```yaml
security:
  fake_token_len: 8          # 蜜罐探针（假令牌）长度
  real_token_len: 8192       # 真实认证 Token 长度
  nonce_len: 16              # 防重放 Nonce 长度
  nonce_expiry: 60           # Nonce 有效期（秒）
  fake_response_msg: "🎣 你上钩了！但这里只是蜜罐，请去 GitHub 点个 Star。"
  blacklist_enabled: true    # 启用黑名单
  whitelist_ips:             # 白名单 IP（仅这些 IP 可访问管理接口）
    - "127.0.0.1"
```

:::warning 公网部署清单
1) `onebot.access_token` 设为强随机值；2) `web.host` 保持 `127.0.0.1`
或放在反代/防火墙之后；3) 首次登录后立刻修改默认管理员密码；
4) 按需配置 `security.whitelist_ips`。
:::

## 插件元信息（plugin.yaml）

放在插件目录（安装时会迁移到 `data/plugins_dat/<插件名>/`），
声明元信息、依赖与更新源；其中版本/作者等字段会覆盖 `__plugin_meta__`：

```yaml
name: 我的插件
version: 1.0.0
author: 你的名字
description: 插件描述
priority: 50

dependencies:           # Python 依赖（也可用 requirements.txt，二者会合并）
  python:
    - requests>=2.28
    - Pillow>=10.0

github:                 # 在线更新源
  repo: owner/repo
  branch: main
  sub_path: plugins/my_plugin
```

## 插件 Web 配置（_conf_schema.json）

让插件在 Web 面板可视化配置，值存入数据库，代码用 `ctx.get_config` 读取：

```json
{
  "api_key": {"type": "string", "default": "", "description": "API 密钥", "hint": "在第三方平台获取"},
  "timeout": {"type": "number", "default": 30, "description": "请求超时（秒）"},
  "enabled": {"type": "bool", "default": true, "description": "是否启用"},
  "mode": {
    "type": "select", "default": "auto", "description": "工作模式",
    "options": [
      {"label": "自动", "value": "auto"},
      {"label": "手动", "value": "manual"}
    ]
  }
}
```

```python
api_key = ctx.get_config("api_key", default="")
all_cfg = ctx.get_all_config()
```

## 代码目录与数据目录

| 目录 | 内容 | 是否随插件更新覆盖 |
|------|------|--------------------|
| `plugins/<名>/` | 插件代码 | 是 |
| `data/plugins_dat/<名>/` | 插件配置、缓存、用户数据（`ctx.get_data_dir()`） | 否，长期保留 |

运行期要持久化的文件一律写到数据目录，避免更新插件时丢失。
