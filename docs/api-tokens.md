# 接口令牌（API Key）与两类 HTTP 接口

ZCBOT 有**两类** HTTP 接口，别混淆端口：

| 接口 | 由谁提供 | 默认地址 | 认证 | 用途 |
| ---- | -------- | -------- | ---- | ---- |
| **后台 REST 接口**（`/api/**`，含权限/扩展/配置管理） | 官方扩展 `webui` | `127.0.0.1:8080` | 登录会话 token 或 API Key | 给管理后台与受信脚本用 |
| **独立对外 API**（`/send`、`/db/query` 等） | 官方扩展 `http_api`（默认关闭） | `127.0.0.1:1145` | 单一共享 token（`?token=`） | 给外部系统集成用 |

> 两类接口都依赖对应扩展：`webui` / `http_api` 未启用时端口不监听（官方扩展默认全部关闭）。

## API Key（接口令牌）

原先只有随登录轮换、会过期的会话 token；**API Key** 独立有效、不随登录轮换，专供脚本长期调用：

- `secrets.token_hex(32)`（64 字符），存 `api_tokens` 表，可设绝对过期或永不过期，可即时吊销（软删除）；
- 创建后**仅明文返回一次**；仅 `super` 角色可创建/吊销；
- 调用时带请求头 `Authorization: Bearer <token>`；`_verify_token` 同时兼容会话 token 与 API Key，**既有接口无需改动**即可用 API Key 调。

```bash
# 后台 REST 接口（webui，默认 8080）
curl -H "Authorization: Bearer <你的API_KEY>" \
     http://127.0.0.1:8080/api/perm/groups
```

后台在「接口令牌」页（`/apikeys`）创建/吊销；端点：

| 端点 | 作用 |
| ---- | ---- |
| `GET /api/apikeys` | 列出令牌 |
| `POST /api/apikeys` | 创建令牌（仅明文返回一次） |
| `POST /api/apikeys/<id>/revoke` | 吊销（软删除） |

## 安全注意

独立 `http_api` 扩展若开启 `allow_db: true`，其 `db/query`、`db/execute` 可执行**任意 SQL（含写库/删表）**，无表级粒度。默认关闭；确有需要时再开启，并务必：

1. 固定 `token`；
2. 仅绑定内网 `host`；
3. 尽量用只读账号或反向代理限权。

## Web 后台管理员分级（v1.7.3 安全收敛）

除登录管理员（`admin` 角色）外，以下高危操作**仅 `super` 角色**可执行：

- 插件上传 / pip 依赖安装 / 隔离 venv 创建 / 从 GitHub 更新插件 / 市场安装插件 / 框架在线更新；

另外：

- 数据库网关中 `admin_users`、`api_tokens` 两张敏感表对普通管理员隐藏（列表不可见、结构/数据 403）；
- 文件浏览禁止普通管理员下载 `.db/.sqlite` 等数据库文件；
- 登录失败统一提示「用户名或密码错误」（不区分账号是否存在/是否禁用，防用户名枚举）。

首次创建的默认账号为 `super`，可到「管理员管理」页添加受限的普通 `admin`。
