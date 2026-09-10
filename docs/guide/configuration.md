# 配置系统

> **本篇面向**：角色 A/B（使用者与插件开发者）。ZCBOT 有两份配置，分工如下：
> - **`config.yaml`**：框架**全局**配置（数据库、日志、安全、插件目录等），首次启动自动生成；
> - **`core_plugins.yaml`**：**官方插件配置中心**，集中管理 `core_plugins/` 下每个官方插件的开关与配置。
>
> 也可以在启动时传入自定义的全局配置路径：`python main.py /path/to/config.yaml`。

## 官方插件配置中心 core_plugins.yaml（重点）

官方插件（`core_plugins/` 目录）的开关与配置**统一写在根目录 `core_plugins.yaml`**，不再散落在 `config.yaml`。

启动时框架会自动完成同步（见 `framework/config.py` 的 `_autoload_core_plugins`）：

1. 扫描 `core_plugins/` 目录，得到"已安装"的官方插件；
2. 为**已安装但 yaml 里缺失**的插件补上默认配置块；
3. **删除** yaml 里已经不再安装的插件块（即卸载插件后配置块自动消失）；
4. 有变化就**自动回写** `core_plugins.yaml`（无需手动维护）；
5. 把每个插件的 `enabled` 合并进主配置的 `core_plugins` 段，把插件配置块合并进对应主配置段
   （`onebot_adapter → onebot`、`webui → web`，其余插件段名与插件名相同）。

因此**代码里既有的 `fw.config.get('onebot')`、`fw.config.get('web')` 等读法保持不变**，对插件透明。

一份开箱即用的 `core_plugins.yaml` 形如：

```yaml
core_plugins:
  onebot_adapter:          # 合并后主配置段名为 onebot
    enabled: true
    listen_host: 0.0.0.0
    listen_port: 6830      # 反向 WebSocket 服务端端口（等 OneBot 客户端连入）
    access_token: ''       # 接入令牌，公网必须设非空强随机值
  webui:                   # 合并后主配置段名为 web
    enabled: true
    host: 127.0.0.1
    port: 8080             # Web 管理后台端口
  session:
    enabled: true          # 多轮会话（ctx.wait_for/create_session）
  scheduler:
    enabled: true          # 定时任务（ctx.task）
  http_inject:             # HTTP 事件注入接入端（默认关闭）
    enabled: false
    host: 127.0.0.1
    port: 8901
    path: /hook
    token: ''
  http_api:                # 独立对外 HTTP API（默认关闭）
    enabled: false
    host: 127.0.0.1
    port: 1145
    token: ''
    allow_db: false
```

把某个插件的 `enabled` 改为 `false` 即禁用，**重启后生效**。关闭某个服务后，依赖它的能力
（如会话、定时、API 调用）会不可用。`http_api`、`http_inject` 涉及开放端口，**默认关闭**，需显式开启。

> 提示：旧版本把开关写在 `config.yaml → core_plugins` 的写法仍能被识别（向后兼容，首次合并时迁移），
> 但新配置请统一写到 `core_plugins.yaml`。

## 协议接入（OneBot 11，来自 onebot_adapter）

`onebot_adapter` 的配置块在 `core_plugins.yaml`，合并后主配置段为 `onebot`：

```yaml
# core_plugins.yaml
core_plugins:
  onebot_adapter:
    enabled: true
    listen_host: 0.0.0.0
    listen_port: 6830   # WebSocket 服务端端口（等 OneBot 实现端反向连入）
    access_token: ""    # 接入令牌，公网部署必须设置非空强随机值
```

ZCBOT 作为 WebSocket **服务端**，由 NapCat / Lagrange 等 OneBot 实现端反向连接。
客户端怎么配、连接怎么验证、插件里怎么发图片 / @ / 做群管，见 [对接 IM 平台](./connect-im.md)。
不使用该平台 时可整体关闭该插件，改用 `http_inject` / `scheduler` 或自写接入端，
见 [最佳实践](./best-practices.md)。

## Web 管理后台（来自 webui）

```yaml
# core_plugins.yaml → core_plugins.webui（合并后主配置段为 web）
web:
  host: 127.0.0.1         # 监听地址；0.0.0.0 = 监听所有网卡（公网可访问，务必配合令牌/白名单）
  port: 8080              # 监听端口
  session_timeout: 3600   # 登录会话超时时长（秒），同时也是会话 Token 有效期
  enabled: true
```

后台 REST 接口（`/api/**`）与后台页面**共用这个端口**；另有独立的 `http_api` 插件（默认 1145、默认关闭）用于对外集成，两者区别见 README「接口令牌」一节。

## 独立对外 HTTP API（来自 http_api，默认关闭）

先在 `core_plugins.yaml` 把 `http_api.enabled` 置为 `true`：

```yaml
core_plugins:
  http_api:
    enabled: true
    host: 127.0.0.1       # 仅本机访问更安全
    port: 1145            # HTTP API 端口
    token: ""             # 调用令牌；留空则首次自动生成
    allow_db: false       # 是否开放数据库网关 db/query、db/execute（默认关闭）
```

> **权限说明**：HTTP API 用单一共享 token 认证（`?token=xxx`），`allow_db: true` 后
> `db/query` / `db/execute` 可执行**任意 SQL（含写库/删表）**，没有表级或角色级粒度。默认关闭；
> 确有需要时再开启，并务必：① 固定 `token`；② 仅绑定内网 `host`；③ 尽量用只读账号/反向代理限权。

## 数据库（config.yaml）

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

## 插件相关（config.yaml）

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

## 日志（config.yaml）

```yaml
log:
  level: INFO                 # DEBUG / INFO / WARNING / ERROR
  file: data/logs/zcbot.log   # 日志文件
  log_raw_message: true       # 记录收到的原始消息
  log_sent_message: true      # 记录发出的消息
```

插件内用 `ctx.log()` 或标准 `logging` 输出，会自动带插件名前缀。

## 系统状态展示（config.yaml）

```yaml
system:
  show_cpu: true             # 面板展示 CPU 状态
  show_disk: true            # 面板展示磁盘状态
  status_interval: 30        # 状态采样间隔（秒）
```

## GitHub 加速（config.yaml）

```yaml
github_proxy: ""             # 插件更新走的 GitHub 代理前缀，留空直连（内置多镜像回退）
```

从 GitHub 拉取插件更新较慢时，可填镜像代理地址。

## 安全配置（config.yaml）

```yaml
security:
  fake_token_len: 8          # 蜜罐探针（假令牌）长度
  real_token_len: 8192       # 真实认证 Token 长度
  nonce_len: 16              # 防重放 Nonce 长度
  nonce_expiry: 60           # Nonce 有效期（秒）
  fake_response_msg: "你上钩了！但这里只是蜜罐，请去 GitHub 点个 Star。"
  blacklist_enabled: true    # 启用黑名单
  whitelist_ips:             # 白名单 IP（仅这些 IP 可访问管理接口）
    - "127.0.0.1"
```

:::warning 公网部署清单
1) `onebot_adapter.access_token` 设为强随机值；2) `webui.host` 保持 `127.0.0.1`
或放在反代/防火墙之后；3) 首次登录后立刻修改默认管理员密码；
4) 按需配置 `security.whitelist_ips`；5) 不需要对外的 `http_api/http_inject` 保持关闭。
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
