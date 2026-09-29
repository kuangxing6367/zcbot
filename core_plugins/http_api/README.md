# HTTP API 插件

HTTP 控制面插件（官方插件）。在独立端口提供一组 HTTP 接口，用于外部系统查询状态、
发送消息、管理群成员、执行数据库查询等。

## 元信息

- 优先级：未显式声明（默认 0 级别加载顺序）
- 进程归属：core
- 默认开关：禁用（`http_api.enabled` 必须为 `true` 才开启）

## 接入方式

基于 `ThreadingHTTPServer` 提供 HTTP 服务，缺省端口 `1145`。
所有请求通过 `?token=` 鉴权（token 来源于配置）；`allow_db` 控制是否开放数据库接口。

## 接口一览

GET：

- `/status` 框架运行状态
- `/plugins` 已加载插件列表
- `/users` 用户列表
- `/groups` 群组列表
- `/help` 接口说明

POST：

- `/sendmsg` 发送消息
- `/send_private_msg` 私聊消息
- `/send_group_msg` 群消息
- `/kick` 移出群成员
- `/ban` 禁言
- `/unban` 解除禁言
- `/broadcast` 广播
- `/reload` 重载插件
- `/db/query` 数据库查询（需 `allow_db`）
- `/db/execute` 数据库执行（需 `allow_db`）

## 可配置项（config.yaml 段 `http_api`）

| 键 | 说明 | 默认值 |
|---|---|---|
| `enabled` | 是否启用 | `false` |
| `host` | 监听地址 | `0.0.0.0` |
| `port` | 监听端口 | `1145` |
| `token` | 访问令牌（`?token=`） | 空 |
| `allow_db` | 是否开放 `/db/*` 接口 | `false` |

## 安全说明

`/db/execute` 具备直接写库能力，请仅在可信网络下启用 `allow_db`，并务必设置强 `token`。
默认 `allow_db=false`，数据库接口不对外开放。
