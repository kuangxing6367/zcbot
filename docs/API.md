# ZCBOT Web API 接口定义文档

本文档定义 ZCBOT 框架 Web UI 后端的所有 HTTP API 接口，供前端开发、第三方集成和二次开发参考。

## 目录

- [通用约定](#通用约定)
- [认证机制](#认证机制)
- [认证接口](#认证接口)
  - [POST /api/login](#post-apilogin)
  - [POST /api/logout](#post-apilogout)
  - [GET /api/me](#get-apime)
  - [POST /api/change_password](#post-apichange_password)
- [仪表盘](#仪表盘)
  - [GET /api/dashboard](#get-apidashboard)
  - [GET /api/dashboard/cards](#get-apidashboardcards)
- [插件管理](#插件管理)
  - [GET /api/plugins](#get-apiplugins)
  - [POST /api/plugins/upload](#post-apipluginsupload)
  - [POST /api/plugins/{name}/reload](#post-apipluginsnamereload)
  - [POST /api/plugins/{name}/toggle](#post-apipluginsnametoggle)
  - [DELETE /api/plugins/{name}](#delete-apipluginsname)
  - [POST /api/plugins/{name}/install_deps](#post-apipluginsnameinstall_deps)
  - [POST /api/plugins/{name}/create_isolated_env](#post-apipluginsnamecreate_isolated_env)
  - [GET /api/plugins/venv_usage](#get-apipluginsvenv_usage)
  - [DELETE /api/plugins/{name}/isolated_env](#delete-apipluginsnameisolated_env)
  - [GET /api/plugins/{name}/readme](#get-apipluginsnamereadme)
  - [GET /api/plugins/{name}/config](#get-apipluginsnameconfig)
  - [GET /api/plugins/{name}/file/{filename}](#get-apipluginsnamefilefilename)
  - [GET /api/plugins/{name}/check_update](#get-apipluginsnamecheck_update)
  - [POST /api/plugins/{name}/update](#post-apipluginsnameupdate)
- [插件配置](#插件配置)
  - [GET /api/plugins/{name}/config_schema](#get-apipluginsnameconfig_schema)
  - [PUT /api/plugins/{name}/config_schema](#put-apipluginsnameconfig_schema)
- [群级插件开关](#群级插件开关)
  - [GET /api/plugins/group-settings](#get-apipluginsgroup-settings)
  - [POST /api/plugins/{name}/group/{group_id}/toggle](#post-apipluginsnamegroupgroup_idtoggle)
- [命令管理](#命令管理)
  - [GET /api/commands](#get-apicommands)
  - [GET /api/commands/dynamic](#get-apicommandsdynamic)
  - [GET /api/plugins/{name}/commands](#get-apipluginsnamecommands)
  - [PUT /api/commands/{cmd_id}/alias](#put-apicommandscmd_idalias)
  - [POST /api/commands/{cmd_id}/toggle](#post-apicommandscmd_idtoggle)
- [用户管理](#用户管理)
  - [GET /api/users](#get-apiusers)
  - [PUT /api/users/{user_id}/role](#put-apiusersuser_idrole)
  - [POST /api/users/{user_id}/blacklist](#post-apiusersuser_idblacklist)
- [群组管理](#群组管理)
  - [GET /api/groups](#get-apigroups)
  - [POST /api/groups/{group_id}/blacklist](#post-apigroupsgroup_idblacklist)
- [定时任务](#定时任务)
  - [GET /api/tasks](#get-apitasks)
  - [POST /api/tasks](#post-apitasks)
  - [DELETE /api/tasks/{task_id}](#delete-apitaskstask_id)
  - [POST /api/tasks/{task_id}/toggle](#post-apitaskstask_idtoggle)
  - [POST /api/tasks/{task_id}/trigger](#post-apitaskstask_idtrigger)
- [审计日志](#审计日志)
  - [GET /api/audit_logs](#get-apiaudit_logs)
- [运行日志](#运行日志)
  - [GET /api/runtime_logs](#get-apiruntime_logs)
  - [GET /api/runtime_logs/stats](#get-apiruntime_logsstats)
  - [POST /api/runtime_logs/clear](#post-apiruntime_logsclear)
  - [GET /api/logs/sse](#get-apilogssse)
- [系统配置](#系统配置)
  - [GET /api/config](#get-apiconfig)
  - [PUT /api/config/{key}](#put-apiconfigkey)
- [管理员管理](#管理员管理)
  - [GET /api/admins](#get-apiadmins)
  - [POST /api/admins](#post-apiadmins)
  - [DELETE /api/admins/{admin_id}](#delete-apiadminsadmin_id)
- [框架操作](#框架操作)
  - [POST /api/restart](#post-apirestart)
- [插件 WebUI](#插件-webui)
  - [GET /api/plugin_webuis](#get-apiplugin_webuis)
  - [GET /api/plugin_webui/{name}](#get-apiplugin_webuiname)
  - [GET /api/plugin_webui/{name}/assets/{filename}](#get-apiplugin_webuinameassetsfilename)

---

## 通用约定

### Base URL

```
http://<host>:8080
```

默认端口 `8080`，可在 `config.yaml` → `web.port` 修改。

### 请求格式

- `GET` 请求参数通过 Query String 传递
- `POST`/`PUT`/`DELETE` 请求体使用 JSON 格式，需带 `Content-Type: application/json` 头
- 文件上传使用 `multipart/form-data`

### 响应格式

所有接口统一返回 JSON 格式：

```json
{
  "code": 0,
  "msg": "成功",
  "data": {}
}
```

| 字段   | 类型    | 说明                                            |
| ------ | ------- | ----------------------------------------------- |
| `code` | int     | `0` 表示成功，非 `0` 表示失败                   |
| `msg`  | string  | 提示消息                                        |
| `data` | any     | 返回数据（可选，部分接口无此字段）              |
| `total`| int     | 分页接口的总记录数（仅分页接口返回）            |
| `page` | int     | 当前页码（仅分页接口返回）                      |
| `size` | int     | 每页条数（仅分页接口返回）                      |

### HTTP 状态码

| 状态码 | 含义                     |
| ------ | ------------------------ |
| 200    | 成功                     |
| 400    | 请求参数错误             |
| 401    | 未认证或 token 失效      |
| 403    | 权限不足                 |
| 404    | 资源不存在               |
| 409    | 资源冲突（如用户名重复） |
| 500    | 服务器内部错误           |

---

## 认证机制

ZCBOT 使用 **Bearer Token** 认证机制，token 为 2048 位随机 hex 字符串。

### 请求头

除 `/api/login` 外，所有接口都需要在请求头携带认证 token：

```
Authorization: Bearer <token>
```

### Token 生成规则

- 通过 `secrets.token_hex(1024)` 生成 2048 字符的随机 hex 字符串
- 存储在 `admin_users` 表的 `token` 字段，签发时间存储在 `token_created_at` 字段
- 默认有效期 24 小时（86400 秒），可在 `config.yaml` → `web.token_timeout` 修改

### Token 失效处理

- 服务端返回 `401` 状态码时，前端应清除本地 token 并跳转登录页
- Token 过期、被登出、被其他设备登录挤下线都会导致 401

### 前端示例

```javascript
// 从 localStorage 读取 token
const token = localStorage.getItem('zcbot_token');

// 发起认证请求
fetch('/api/plugins', {
    headers: { 'Authorization': 'Bearer ' + token }
})
.then(r => {
    if (r.status === 401) {
        localStorage.removeItem('zcbot_token');
        location.href = '/login.html';
        return;
    }
    return r.json();
});
```

---

## 认证接口

### POST /api/login

管理员登录，获取认证 token。

**请求体**：

```json
{
  "username": "admin",
  "password": "admin"
}
```

**响应**：

```json
{
  "code": 0,
  "msg": "登录成功",
  "data": {
    "token": "a1b2c3...(2048 字符)",
    "username": "admin",
    "role": "super"
  }
}
```

**错误**：

| 状态码 | code | 说明               |
| ------ | ---- | ------------------ |
| 400    | 400  | 用户名密码为空     |
| 401    | 401  | 用户名或密码错误   |
| 403    | 403  | 账号已禁用         |

**默认账号**：`admin` / `admin`（首次启动自动创建，建议立即修改密码）

---

### POST /api/logout

退出登录，清除当前 token。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "msg": "已退出"
}
```

---

### GET /api/me

获取当前登录管理员信息。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "data": {
    "id": 1,
    "username": "admin",
    "role": "super"
  }
}
```

---

### POST /api/change_password

修改当前管理员密码。

**权限**：需要认证

**请求体**：

```json
{
  "old_password": "admin",
  "new_password": "newpass123"
}
```

**校验规则**：
- 新密码至少 6 位

**响应**：

```json
{
  "code": 0,
  "msg": "密码已修改"
}
```

---

## 仪表盘

### GET /api/dashboard

获取仪表盘统计数据。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "data": {
    "plugins_active": 5,
    "plugins_total": 8,
    "commands_total": 25,
    "dynamic_commands": 3,
    "users_total": 1024,
    "groups_active": 12,
    "tasks_active": 4,
    "bots": ["bot_1"],
    "ws_port": 6830,
    "framework_name": "ZCBOT",
    "framework_version": "0.1.0-beta.0",
    "github_repo": "https://github.com/kuangxing6367/zcbot"
  }
}
```

---

### GET /api/dashboard/cards

获取仪表盘插件卡片数据（由插件通过 `ctx.dashboard_card()` 注册）。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "data": [
    {
      "plugin_name": "runtime_status",
      "title": "CPU 使用率",
      "value": "23%",
      "label": "当前 CPU",
      "icon": "chart",
      "color": "#34c759",
      "priority": 10
    }
  ]
}
```

---

## 插件管理

### GET /api/plugins

获取所有插件列表，含命令/任务计数、依赖状态、GitHub 更新源等运行时信息。

**权限**：需要认证

**响应字段说明**：

| 字段               | 类型    | 说明                              |
| ------------------ | ------- | --------------------------------- |
| `plugin_name`      | string  | 插件名（目录名）                  |
| `version`          | string  | 版本号                            |
| `author`           | string  | 作者                              |
| `description`      | string  | 描述                              |
| `priority`         | int     | 加载优先级                        |
| `status`           | string  | 运行状态                          |
| `is_active`        | int     | 是否启用（0/1）                   |
| `is_loaded`        | bool    | 当前是否已加载                    |
| `command_count`    | int     | 启用命令数                        |
| `task_count`       | int     | 启用任务数                        |
| `has_readme`       | bool    | 是否有 README.md                  |
| `has_yaml`         | bool    | 是否有 plugin.yaml                |
| `has_github`       | bool    | 是否配置 GitHub 源                |
| `github_repo`      | string  | GitHub 仓库地址                   |
| `has_schema`       | bool    | 是否有 _conf_schema.json          |
| `has_missing_deps` | bool    | 是否缺失依赖                      |
| `missing_deps`     | array   | 缺失的依赖列表                    |
| `has_conflict`     | bool    | 是否有依赖冲突                    |
| `conflicts`        | array   | 冲突的依赖列表                    |
| `config_items`     | array   | plugin.yaml 中定义的配置项        |

**响应示例**：

```json
{
  "code": 0,
  "data": [
    {
      "id": 1,
      "plugin_name": "echo",
      "version": "1.0.0",
      "author": "ZGRIC",
      "description": "原样返回用户文本消息",
      "priority": 50,
      "status": "loaded",
      "is_active": 1,
      "is_loaded": true,
      "has_register": 1,
      "loaded_at": "2026-07-29 10:00:00",
      "created_at": "2026-07-29 10:00:00",
      "has_readme": false,
      "has_yaml": false,
      "has_github": false,
      "github_repo": "",
      "config_items": [],
      "has_schema": false,
      "has_missing_deps": false,
      "missing_deps": [],
      "has_conflict": false,
      "conflicts": [],
      "command_count": 1,
      "task_count": 0
    }
  ]
}
```

---

### POST /api/plugins/upload

上传 ZIP 格式的插件包进行安装。

**权限**：需要认证

**请求格式**：`multipart/form-data`

| 字段   | 类型 | 必填 | 说明                  |
| ------ | ---- | ---- | --------------------- |
| `file` | File | 是   | .zip 格式的插件包     |

**校验规则**：
- 文件必须以 `.zip` 结尾
- ZIP 包必须包含 `main.py`
- 插件名只能包含字母、数字、下划线、横杠
- 路径穿越攻击会被拦截

**响应**：

```json
{
  "code": 0,
  "msg": "插件 [my_plugin] 上传成功"
}
```

---

### POST /api/plugins/{name}/reload

重新加载指定插件。

**权限**：需要认证

**路径参数**：

| 参数  | 类型   | 说明     |
| ----- | ------ | -------- |
| `name`| string | 插件名   |

**响应**：

```json
{
  "code": 0,
  "msg": "插件 [my_plugin] 已重新加载"
}
```

---

### POST /api/plugins/{name}/toggle

启用或禁用插件。

**权限**：需要认证

**请求体**：

```json
{
  "is_active": true
}
```

**响应**：

```json
{
  "code": 0,
  "msg": "插件 [my_plugin] 已启用"
}
```

---

### DELETE /api/plugins/{name}

卸载并删除插件（同时清理 plugins/ 和 plugins_dat/ 中的文件，并 gc.collect()）。

**权限**：需要认证

**查询参数**：

| 参数       | 类型 | 说明                          |
| ---------- | ---- | ----------------------------- |
| `keep_data`| bool | 是否保留用户数据（默认 false）|

**响应**：

```json
{
  "code": 0,
  "msg": "插件 [my_plugin] 已卸载"
}
```

---

### POST /api/plugins/{name}/install_deps

为插件安装依赖（走清华源 + 自动回退）。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "msg": "依赖安装完成",
  "data": {
    "success": true,
    "mirror": "https://pypi.tuna.tsinghua.edu.cn/simple"
  }
}
```

---

### POST /api/plugins/{name}/create_isolated_env

为插件创建独立虚拟环境（用于解决依赖冲突）。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "msg": "独立环境已创建",
  "data": {
    "venv_path": "plugins/my_plugin/.venv"
  }
}
```

---

### GET /api/plugins/venv_usage

获取所有插件的虚拟环境磁盘占用情况。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "data": [
    {
      "plugin_name": "conflict_plugin",
      "venv_path": "plugins/conflict_plugin/.venv",
      "size_mb": 45.32,
      "size_human": "45.3 MB"
    }
  ]
}
```

---

### DELETE /api/plugins/{name}/isolated_env

删除插件的独立虚拟环境（释放磁盘空间）。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "msg": "独立环境已删除，释放 45.3 MB 空间"
}
```

---

### GET /api/plugins/{name}/readme

获取插件 README.md 内容。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "data": {
    "content": "# My Plugin\n\n插件说明..."
  }
}
```

---

### GET /api/plugins/{name}/config

获取插件的配置文件列表（plugins_dat/<name>/ 下的文件）。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "data": {
    "plugin_name": "my_plugin",
    "files": ["_conf_schema.json", "plugin.yaml", "data.json"]
  }
}
```

---

### GET /api/plugins/{name}/file/{filename}

获取插件数据目录中指定文件的内容。

**权限**：需要认证

**路径参数**：

| 参数       | 类型   | 说明                  |
| ---------- | ------ | --------------------- |
| `name`     | string | 插件名                |
| `filename` | string | 文件名（不含路径）    |

**响应**：直接返回文件内容（`text/plain`）

---

### GET /api/plugins/{name}/check_update

检查插件是否有 GitHub 新版本。

**权限**：需要认证

**前提条件**：插件的 `plugin.yaml` 配置了 `github.repo`

**响应**：

```json
{
  "code": 0,
  "data": {
    "has_update": true,
    "current_version": "1.0.0",
    "latest_version": "1.1.0",
    "latest_commit": "abc123...",
    "update_time": "2026-07-28 10:00:00"
  }
}
```

---

### POST /api/plugins/{name}/update

从 GitHub 拉取最新代码更新插件（仅覆盖 `plugins/` 目录，保留 `plugins_dat/`）。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "msg": "插件 [my_plugin] 已更新到 1.1.0"
}
```

---

## 插件配置

### GET /api/plugins/{name}/config_schema

获取插件配置 schema 和当前配置值。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "data": {
    "plugin_name": "my_plugin",
    "schema": {
      "api_key": {
        "description": "API 密钥",
        "type": "string",
        "default": "",
        "hint": "申请地址：https://example.com"
      },
      "timeout": {
        "description": "请求超时",
        "type": "number",
        "default": 10
      }
    },
    "values": {
      "api_key": "sk-xxxxx",
      "timeout": 15
    },
    "has_schema": true
  }
}
```

---

### PUT /api/plugins/{name}/config_schema

更新插件配置值（用户在 Web UI 修改配置项后调用）。

**权限**：需要认证

**请求体**：

```json
{
  "values": {
    "api_key": "sk-new-key",
    "timeout": 20
  }
}
```

**响应**：

```json
{
  "code": 0,
  "msg": "配置已保存"
}
```

> 配置保存后，插件下次读取 `ctx.get_config()` 时生效。如需立即生效，请配合 `/api/plugins/{name}/reload` 重新加载插件。

---

## 群级插件开关

### GET /api/plugins/group-settings

获取所有群级插件开关设置。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "data": [
    {
      "plugin_name": "ipquery",
      "group_id": 123456,
      "enabled": false,
      "updated_at": "2026-07-29 10:00:00"
    }
  ]
}
```

---

### POST /api/plugins/{name}/group/{group_id}/toggle

启用/禁用插件在指定群的状态。

**权限**：需要认证

**路径参数**：

| 参数        | 类型 | 说明 |
| ----------- | ---- | ---- |
| `name`      | str  | 插件名 |
| `group_id`  | int  | 群号 |

**请求体**：

```json
{
  "enabled": true
}
```

**响应**：

```json
{
  "code": 0,
  "msg": "插件 [ipquery] 在群 123456 已启用"
}
```

---

## 命令管理

### GET /api/commands

获取所有静态命令列表（含别名、描述、启停状态、权限要求）。

**权限**：需要认证

**响应字段**：

| 字段              | 类型   | 说明                                |
| ----------------- | ------ | ----------------------------------- |
| `plugin_name`     | string | 所属插件                            |
| `pattern`         | string | 命令匹配模式                        |
| `alias`           | string | 别名（逗号分隔）                    |
| `description`     | string | 命令描述                            |
| `priority`        | int    | 优先级                              |
| `handler`         | string | 处理函数名                          |
| `is_dynamic`      | int    | 是否动态命令（0=静态，1=动态）      |
| `is_active`       | int    | 是否启用                            |
| `hit_count`       | int    | 命中次数                            |
| `require_level`   | string | 权限要求（super/admin/空）          |

**响应示例**：

```json
{
  "code": 0,
  "data": [
    {
      "id": 1,
      "plugin_name": "echo",
      "pattern": "/echo",
      "alias": null,
      "description": "原样返回输入文本",
      "priority": 50,
      "handler": "handle_echo",
      "is_dynamic": 0,
      "is_active": 1,
      "hit_count": 42,
      "require_level": "",
      "created_at": "2026-07-29 10:00:00"
    }
  ]
}
```

---

### GET /api/commands/dynamic

获取动态命令列表（插件通过 `ctx.command(dynamic=True)` 注册，只读展示）。

**权限**：需要认证

**响应**：同 [/api/commands](#get-apicommands)，但仅返回 `is_dynamic = 1` 的记录。

---

### GET /api/plugins/{name}/commands

获取指定插件的所有命令（静态 + 动态）。

**权限**：需要认证

**响应**：同 [/api/commands](#get-apicommands)，但仅返回该插件的命令。

---

### PUT /api/commands/{cmd_id}/alias

修改命令的别名和描述（用户自定义，重启后保留）。

**权限**：需要认证

**路径参数**：

| 参数      | 类型 | 说明    |
| --------- | ---- | ------- |
| `cmd_id`  | int  | 命令 ID |

**请求体**：

```json
{
  "alias": "/h,/?",
  "description": "查看帮助信息"
}
```

**响应**：

```json
{
  "code": 0,
  "msg": "命令别名已更新"
}
```

---

### POST /api/commands/{cmd_id}/toggle

启用或禁用命令。

**权限**：需要认证

**请求体**：

```json
{
  "is_active": false
}
```

**响应**：

```json
{
  "code": 0,
  "msg": "命令已禁用"
}
```

---

## 用户管理

### GET /api/users

获取用户列表（分页 + 关键词搜索）。

**权限**：需要认证

**查询参数**：

| 参数       | 类型 | 默认 | 说明                          |
| ---------- | ---- | ---- | ----------------------------- |
| `page`     | int  | 1    | 页码                          |
| `size`     | int  | 50   | 每页条数（最大 200）          |
| `keyword`  | str  | -    | 搜索关键词（昵称/QQ/备注）    |

**响应**：

```json
{
  "code": 0,
  "data": [
    {
      "id": 1,
      "user_id": 123456,
      "nickname": "用户昵称",
      "is_friend": 1,
      "is_blacklist": 0,
      "remark": "",
      "role": "",
      "first_seen_at": "2026-07-29 10:00:00",
      "last_active_at": "2026-07-29 12:00:00"
    }
  ],
  "total": 1024,
  "page": 1,
  "size": 50
}
```

---

### PUT /api/users/{user_id}/role

设置用户角色（设为/取消超级管理员）。

**权限**：需要超级管理员

**请求体**：

```json
{
  "role": "super"
}
```

| 值      | 说明                  |
| ------- | --------------------- |
| `super` | 设为超级管理员        |
| `""`    | 取消超级管理员身份    |

**响应**：

```json
{
  "code": 0,
  "msg": "用户 [昵称] 角色已设为 超级管理员"
}
```

---

### POST /api/users/{user_id}/blacklist

拉黑或取消拉黑用户。

**权限**：需要认证

**请求体**：

```json
{
  "is_blacklist": true
}
```

**响应**：

```json
{
  "code": 0,
  "msg": "已拉黑"
}
```

---

## 群组管理

### GET /api/groups

获取所有群列表。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "data": [
    {
      "id": 1,
      "group_id": 123456,
      "group_name": "测试群",
      "member_count": 50,
      "max_member_count": 200,
      "is_active": 1,
      "is_blacklist": 0,
      "join_at": "2026-07-29 10:00:00"
    }
  ]
}
```

---

### POST /api/groups/{group_id}/blacklist

拉黑或取消拉黑群。

**权限**：需要认证

**请求体**：

```json
{
  "is_blacklist": true
}
```

**响应**：

```json
{
  "code": 0,
  "msg": "已拉黑"
}
```

---

## 定时任务

### GET /api/tasks

获取所有定时任务列表（含插件注册的和 Web UI 创建的）。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "data": [
    {
      "id": 1,
      "plugin_name": "sign_in",
      "cron_expression": "1 0 * * *",
      "handler": "reset_daily_signin",
      "description": "每日重置签到",
      "is_active": 1,
      "last_run_at": "2026-07-29 00:01:00",
      "next_run_at": "2026-07-30 00:01:00",
      "created_at": "2026-07-29 10:00:00"
    }
  ]
}
```

---

### POST /api/tasks

创建自定义定时任务。

**权限**：需要认证

**请求体**：

```json
{
  "cron_expression": "0 8 * * *",
  "description": "每日早安",
  "handler": "custom_task"
}
```

**校验规则**：
- `cron_expression` 必须为 5 段格式（分 时 日 月 周）
- `description` 不能为空

**响应**：

```json
{
  "code": 0,
  "msg": "任务已创建",
  "data": {
    "id": 10
  }
}
```

---

### DELETE /api/tasks/{task_id}

删除定时任务。

**权限**：需要认证

**说明**：仅可删除 Web UI 创建的任务（`plugin_name = '__web__'`）；插件注册的任务需卸载插件。

**响应**：

```json
{
  "code": 0,
  "msg": "任务已删除"
}
```

---

### POST /api/tasks/{task_id}/toggle

启用或禁用定时任务。

**权限**：需要认证

**请求体**：

```json
{
  "is_active": false
}
```

**响应**：

```json
{
  "code": 0,
  "msg": "任务已禁用"
}
```

---

### POST /api/tasks/{task_id}/trigger

手动触发一次定时任务执行（不影响下次计划执行时间）。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "msg": "任务已触发"
}
```

---

## 审计日志

### GET /api/audit_logs

获取审计日志（分页）。

**权限**：需要认证

**查询参数**：

| 参数   | 类型 | 默认 | 说明                |
| ------ | ---- | ---- | ------------------- |
| `page` | int  | 1    | 页码                |
| `size` | int  | 50   | 每页条数（最大 200）|

**响应**：

```json
{
  "code": 0,
  "data": [
    {
      "id": 1,
      "admin_id": 1,
      "admin_name": "admin",
      "action": "login",
      "target_type": null,
      "target_name": null,
      "detail": null,
      "ip_address": "127.0.0.1",
      "result": "success",
      "error_message": null,
      "created_at": "2026-07-29 10:00:00"
    }
  ],
  "total": 100,
  "page": 1,
  "size": 50
}
```

---

## 运行日志

### GET /api/runtime_logs

获取运行日志（分页 + 多维过滤）。

**权限**：需要认证

**查询参数**：

| 参数        | 类型 | 默认   | 说明                                                  |
| ----------- | ---- | ------ | ----------------------------------------------------- |
| `page`      | int  | 1      | 页码                                                  |
| `size`      | int  | 100    | 每页条数                                              |
| `level`     | str  | -      | 日志级别过滤（DEBUG/INFO/WARNING/ERROR）              |
| `category`  | str  | -      | 日志分类（framework/plugin/websocket/onebot/scheduler）|
| `keyword`   | str  | -      | 关键词搜索                                            |

**响应**：

```json
{
  "code": 0,
  "data": [
    {
      "seq": 1,
      "timestamp": "2026-07-29 10:00:00",
      "level": "INFO",
      "category": "framework",
      "logger_name": "zcbot",
      "message": "框架启动中...",
      "plugin_name": null
    }
  ],
  "total": 1000
}
```

---

### GET /api/runtime_logs/stats

获取运行日志统计信息。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "data": {
    "total": 1000,
    "by_level": {
      "DEBUG": 100,
      "INFO": 800,
      "WARNING": 50,
      "ERROR": 50
    },
    "by_category": {
      "framework": 500,
      "plugin": 300,
      "websocket": 100,
      "onebot": 50,
      "scheduler": 50
    }
  }
}
```

---

### POST /api/runtime_logs/clear

清空所有运行日志。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "msg": "已清空"
}
```

---

### GET /api/logs/sse

SSE（Server-Sent Events）实时日志推送端点。

**权限**：需要认证

**响应格式**：`text/event-stream`

**事件格式**：

```
id: 1
data: {"seq":1,"timestamp":"2026-07-29 10:00:00","level":"INFO","category":"framework","message":"..."}

id: 2
data: {"seq":2,"timestamp":"2026-07-29 10:00:01","level":"INFO","category":"framework","message":"..."}
```

**前端使用示例**：

```javascript
const token = localStorage.getItem('zcbot_token');
const eventSource = new EventSource('/api/logs/sse');
// 注意：EventSource 不支持自定义请求头，需通过 query 参数传递 token
// 或使用 fetch + ReadableStream 替代方案

// 推荐方案：fetch + ReadableStream
const res = await fetch('/api/logs/sse', {
    headers: { 'Authorization': 'Bearer ' + token }
});
const reader = res.body.getReader();
const decoder = new TextDecoder();
let buffer = '';
while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n\n');
    buffer = lines.pop();
    for (const line of lines) {
        if (line.startsWith('data: ')) {
            const log = JSON.parse(line.slice(6));
            console.log(log);
        }
    }
}
```

**说明**：
- 连接建立时先推送最近 50 条历史日志
- 之后持续推送新日志
- 心跳间隔 30 秒（无日志时保持连接）

---

## 系统配置

### GET /api/config

获取系统配置项列表。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "data": [
    {
      "config_key": "show_cpu",
      "config_value": "true",
      "description": "仪表盘显示 CPU 使用率",
      "updated_by": "admin"
    },
    {
      "config_key": "show_disk",
      "config_value": "true",
      "description": "仪表盘显示磁盘使用率",
      "updated_by": "admin"
    },
    {
      "config_key": "status_interval",
      "config_value": "30",
      "description": "系统状态刷新间隔（秒）",
      "updated_by": "admin"
    },
    {
      "config_key": "pip_mirror",
      "config_value": "\"tsinghua\"",
      "description": "pip 安装镜像源",
      "updated_by": "admin"
    }
  ]
}
```

---

### PUT /api/config/{key}

更新系统配置项。

**权限**：需要超级管理员

**路径参数**：

| 参数  | 类型   | 说明         |
| ----- | ------ | ------------ |
| `key` | string | 配置项键名   |

**请求体**：

```json
{
  "value": "aliyun"
}
```

**响应**：

```json
{
  "code": 0,
  "msg": "配置已更新"
}
```

---

## 管理员管理

### GET /api/admins

获取管理员账号列表。

**权限**：需要超级管理员

**响应**：

```json
{
  "code": 0,
  "data": [
    {
      "id": 1,
      "username": "admin",
      "role": "super",
      "is_active": 1,
      "last_login_at": "2026-07-29 10:00:00",
      "last_login_ip": "127.0.0.1",
      "created_at": "2026-07-29 09:00:00"
    }
  ]
}
```

---

### POST /api/admins

添加新的管理员账号。

**权限**：需要超级管理员

**请求体**：

```json
{
  "username": "manager1",
  "password": "mypassword",
  "role": "admin"
}
```

**校验规则**：
- 用户名和密码不能为空
- 密码至少 6 位
- `role` 只能是 `super` 或 `admin`
- 用户名不能重复

**响应**：

```json
{
  "code": 0,
  "msg": "管理员 [manager1] 已添加"
}
```

---

### DELETE /api/admins/{admin_id}

删除管理员账号。

**权限**：需要超级管理员

**说明**：不能删除自己。

**响应**：

```json
{
  "code": 0,
  "msg": "已删除"
}
```

---

## 框架操作

### POST /api/restart

重启框架（通过 `os.execv` 原地替换进程）。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "msg": "框架正在重启..."
}
```

**说明**：
- 接口立即返回响应，1 秒后异步执行重启
- 重启过程中 WebSocket 连接会断开，OneBot 客户端会自动重连
- 前端应在收到响应后显示「重启中」状态，并定时检测服务是否恢复

---

## 插件 WebUI

### GET /api/plugin_webuis

获取所有已注册的插件 WebUI 列表。

**权限**：需要认证

**响应**：

```json
{
  "code": 0,
  "data": [
    {
      "plugin_name": "my_plugin",
      "title": "我的插件面板",
      "entry": "index.html",
      "icon": "settings",
      "order": 50
    }
  ]
}
```

---

### GET /api/plugin_webui/{name}

获取插件 WebUI 入口页面（HTML）。

**权限**：需要认证

**查询参数**：

| 参数    | 类型 | 默认         | 说明         |
| ------- | ---- | ------------ | ------------ |
| `entry` | str  | `index.html` | 入口文件名   |

**响应**：HTML 文件内容

---

### GET /api/plugin_webui/{name}/assets/{filename}

获取插件 WebUI 的静态资源文件（JS/CSS/图片等）。

**权限**：需要认证

**路径参数**：

| 参数       | 类型   | 说明                          |
| ---------- | ------ | ----------------------------- |
| `name`     | string | 插件名                        |
| `filename` | string | 文件路径（支持子目录）        |

**响应**：静态文件内容（自动设置 Content-Type）

---

## 错误处理

### 统一错误响应

所有错误响应都遵循统一格式：

```json
{
  "code": <错误码>,
  "msg": "<错误描述>"
}
```

### 常见错误码

| code | HTTP 状态码 | 说明                          |
| ---- | ----------- | ----------------------------- |
| 400  | 400         | 请求参数错误                  |
| 401  | 401         | 未认证或 token 已失效         |
| 403  | 403         | 权限不足                      |
| 404  | 404         | 资源不存在                    |
| 409  | 409         | 资源冲突                      |
| 500  | 500         | 服务器内部错误                |

### 401 处理流程

1. 请求未携带 `Authorization` 头 → 返回 401 `未提供认证令牌`
2. Token 长度不为 2048 → 返回 401 `令牌无效或已过期`
3. Token 在数据库中不存在 → 返回 401 `令牌无效或已过期`
4. Token 已过期（超过 `token_timeout`）→ 返回 401 `令牌无效或已过期`
5. 账号被禁用（`is_active = 0`）→ 返回 401 `令牌无效或已过期`

**前端处理**：收到 401 后应清除 localStorage 中的 `zcbot_token` 并跳转到登录页。

---

## 速率限制

当前版本未实现速率限制，建议在生产环境中通过反向代理（如 Nginx）配置限流。

---

## 版本兼容性

- 所有 API 路由保持向后兼容，不会删除现有路由
- 新增字段不会破坏现有前端
- 字段类型变更会通过新增字段实现，旧字段保留过渡期

---

## 相关文档

- [插件开发文档](./INDEX.md) - 插件开发系列文档索引
- [README](../README.md) - 项目总览
- [OneBot 11 标准](https://github.com/botuniverse/onebot-11) - OneBot 协议规范
