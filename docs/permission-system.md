# 权限系统（LuckPerms 风格）

> 框架 v1.2.0-beta.1 起，在原有「单一 `role` 字符串」身份轴之外，平行提供一套完整的**权限节点**模型（对齐 Minecraft LuckPerms v5）。两者**并存**：旧命令可继续用 `require_level`，新功能推荐 `require_perm` 权限节点。

对应代码：`framework/perm.py`（纯 Python、无第三方依赖）；建表见 `sql/init.sql` / `sql/init_mysql55.sql` / `framework/db.py::_auto_create_tables` 与 `_migrate_*` 迁移。

## 1. 核心概念

| 概念 | 说明 |
| ---- | ---- |
| **节点 node** | `plugin.action.sub` 形式的权限字符串，三态：授予 / 显式否决 / 未定义 |
| **组 group** | 一组节点的集合，带 `weight`（权重决定优先级与 primary group），可设前缀/默认组 |
| **继承** | 组通过持有 `group.xxx` 节点继承另一组 —— 继承与权限统一用节点表达，天然支持多级 |
| **上下文 context** | 节点可限定只在特定环境生效：`group=<群号>`、`bot=<OneBot实例名>`、`msgtype=group|private`；context 为空 = 全局生效 |
| **临时** | 节点可带 `expire_at`（unix 时间戳），到期自动失效；框架每小时清理一次过期节点 |
| **否决** | `value=0` 的同名节点优先于 `value=1`（显式禁止 > 允许） |
| **通配符** | `a.b.*` 匹配 `a.b.c` 任意子级；`*` 匹配一切节点 |

判定顺序（组内）：① 用户直接节点 → ② 所属组（按 weight 降序）→ ③ 同来源内按精确度（精确 > 段级通配 > 全局 `*`）→ ④ 同精确度下**否决优先**。

## 2. 内置角色组（运行时虚拟注入，不入库）

框架原有 `super / owner / admin / member` 四层身份映射为内置继承链：

```
__member(w0) ← __admin(w20) ← __owner(w30) ← __super(w100)
```

每个内置组自带 `zcbot.role.{member|admin|owner|super}` 节点，因此：

```
require_level='admin'  ≡  检查节点 zcbot.role.admin
```

语义与 `Event.is_admin` 完全一致；`Event.role`（单一字符串，供旧代码）无需改动。内置组**不可删除/改名**，它们的 `super > blacklist > owner/admin > member` 判定顺序保持框架既有行为（超管即使被拉黑仍可执行）。

## 3. 在插件里做权限控制

### A. 命令级声明（推荐，框架自动拦截）

```python
# 与 require_level 双轨并存：两者任一满足即放行
@ctx.command("ban", require_perm="myplugin.ban", require_level="admin")
def ban(ev):
    ...
```

- `require_perm` 会持久化到 `commands` 表，可被 Web「命令管理」覆盖
- 缺省值时读库里的配置

### B. 事件内判断（handler 最常用）

| 方法 | 返回 | 说明 |
| ---- | ---- | ---- |
| `ev.has_perm(node)` | `bool` | 是否拥有节点（未定义按拒绝） |
| `ev.check_perm(node)` | `True/False/None` | 三态：授予 / 显式否决 / 未定义 |
| `ev.perms` | `PermissionSet` | 完整权限快照（`.groups` / `.nodes` / `.primary_group`） |
| `ev.perm_groups` | `list` | 生效的权限组（含继承展开，按 weight 降序） |
| `ev.primary_group` | `str` | 权重最高的非内置权限组 |

事件上下文（`bot` / `group` / `msgtype`）自动构造，无需手动传。

### C. 任意位置用 ctx 判断（定时任务等非事件场景）

```python
ctx.has_perm(user_id, "myplugin.ban",
             context={"group": "123456", "msgtype": "group"}, role=ev_role)
ctx.check_perm(user_id, "myplugin.ban", ...)    # 三态
ctx.user_groups(user_id, context=..., role=...)  # 生效组列表
```

### D. 底层模块

`framework.perm` 提供 `resolve / has_perm / check_perm / user_groups / create_group / set_group_node / add_user_group / promote / demote / audit / cleanup_expired`，可组合实现自定义逻辑。

### E. 缓存

解析结果 60s TTL 缓存，组快照 10s；Web 后台改动后自动 `invalidate_all()`，无需手动处理。

## 4. Web 后台管理

侧边栏「权限管理」（`/permissions`）5 个标签页：

| 标签页 | 功能 |
| ---- | ---- |
| 权限组 | 创建/编辑/删除组：权重、前缀、默认组；查看组节点 |
| 用户 | 给用户直接授予/撤销节点、加入/移出组（支持上下文 + 过期时间） |
| 轨道 | 配置 Tracks（如 `default,vip,admin`），一键晋升/降级 |
| 校验器 | 输入 QQ + 上下文，实时看生效组与节点解析 |
| 审计 | 权限变更审计日志（`perm_audit` 表） |

## 5. 节点命名约定

| 前缀 | 含义 | 示例 |
| ---- | ---- | ---- |
| `插件名.动作.子项` | 插件自定义权限 | `myplugin.ban`、`myplugin.config.set` |
| `zcbot.role.*` | 框架内置身份 | `zcbot.role.admin` |
| `zcbot.wf.*` | 工作流权限（下游项目约定） | `zcbot.wf.mc_command` |

下游默认组带 `zcbot.wf.*` 通配 → 新工作流默认全员可触发；要收紧：在后台移除通配、按 `zcbot.wf.<id>` 设显式节点。

## 6. REST API

权限与接口令牌的完整端点/字段见 [Web API 接口文档](./API.md) 的《权限系统 API》《接口令牌（API Key）API》两章。核心端点：

| 方法 & 路径 | 说明 |
| ---- | ---- |
| `GET /api/perm/builtins` | 内置角色组定义 |
| `GET/POST /api/perm/groups` | 列出 / 创建权限组 |
| `PUT/DELETE /api/perm/groups/<name>` | 更新 / 删除权限组 |
| `GET/POST/DELETE /api/perm/groups/<name>/nodes` | 组节点读写 |
| `GET /api/perm/users/<id>` | 用户权限快照 |
| `POST/DELETE /api/perm/users/<id>/nodes` | 用户节点读写 |
| `POST/DELETE /api/perm/users/<id>/groups` | 用户组加入/移出 |
| `POST /api/perm/users/<id>/track` | 沿轨道晋升/降级 |
| `GET/POST/DELETE /api/perm/tracks` | 轨道管理 |
| `POST /api/perm/check` | 校验用户+上下文节点 |
| `GET /api/perm/audit` | 审计日志 |
| `POST /api/perm/cleanup` | 手动清理过期节点 |
| `GET/POST /api/apikeys` | 接口令牌列表 / 创建（token 仅明文返回一次） |
| `POST /api/apikeys/<id>/revoke` | 吊销接口令牌 |

> 认证：除 `POST /api/login` 等公开端点，均需 `Authorization: Bearer <token>`（登录会话 token 或接口令牌均可）；权限管理相关端点要求 `super` 角色。
