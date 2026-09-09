# 权限系统

ZCBOT 内置一套 LuckPerms 风格的权限系统（`framework/perm.py`）：
**节点（node）+ 权限组（group）+ 继承（inherit）+ 上下文（context）+ 三态判定**，
同时保留一条独立的“身份轴”（群主/管理员/超管）。

## 两条互相平行的轴

| 轴 | 取值/形式 | 用途 |
|----|-----------|------|
| 身份轴 `event.role` | `super > owner > admin > member > blacklist` | 粗粒度判断是不是管理员/超管/黑名单 |
| 权限组轴 `event.has_perm(node)` | 节点字符串，如 `sign.admin`、`chat.*` | 细粒度、可配置、可继承的功能授权 |

命令上的 `require_admin` / `require_superuser` 走身份轴；`require_perm` 走权限组轴，
两者可叠加（同时满足）。

## 基本概念

| 概念 | 说明 |
|------|------|
| 权限节点 | 点分字符串，如 `sign_in.use`、`admin.ban`；支持 `chat.*`、`*` 通配 |
| 权限组 | 节点的集合，带 `weight`（权重，决定主组）与显示前后缀 |
| 继承 | 组可以继承其他组，权限随继承链展开（子组拥有父组全部节点） |
| 用户节点 | 直接授予/否决某个用户的节点，优先级高于组 |
| 上下文 | 节点可限定生效范围，如仅某个群、某种消息类型、某个 bot |
| 三态 | `True` 授予 / `False` 显式否决 / `None` 未定义（按拒绝处理） |

### 内置角色组

框架内置四个角色组，随 `event.role` 自动加入，自带对应节点：

| 内置组 | 权重 | 自带节点 | 继承 |
|--------|------|----------|------|
| `__member`（成员） | 0 | `zcbot.role.member` | — |
| `__admin`（群管理） | 20 | `zcbot.role.admin` | `__member` |
| `__owner`（群主） | 30 | `zcbot.role.owner` | `__admin` |
| `__super`（超管） | 100 | `zcbot.role.super` | `__owner` |

因此命令的 `require_level='admin'` 等价于检查节点 `zcbot.role.admin`。
此外每个用户默认属于 `default` 组，可把“所有人都能用”的公共节点放进该组。

## 在插件中使用

### 方式一：命令声明式

```python
ctx.command("/ban", handle_ban,
            require_admin=True,             # 身份轴：管理员及以上
            require_perm="admin.ban")        # 权限组轴：还需该节点
```

需要“满足 A 或 B”这类组合时，不要堆参数，在 handler 内自行判断。

### 方式二：handler 内判断

```python
async def handle_ban(event, match):
    # 事件上直接判（带当前群/bot 上下文，结果有缓存）
    if not event.has_perm("admin.ban"):
        await ctx.asend_msg(..., message="权限不足")
        return

    # 三态查询
    state = event.check_perm("admin.ban")   # True / False / None

    # 查看权限组信息
    event.perm_groups                       # 生效权限组列表
    event.primary_group                     # 权重最高的非内置组
```

### 方式三：脱离事件用 ctx 查询

```python
ctx.has_perm(user_id, "myplugin.use", context={"group": "123456"})
ctx.check_perm(user_id, "myplugin.use")     # 三态
ctx.user_groups(user_id)                    # 生效组列表
ctx.is_superuser(user_id)
ctx.get_user_role(group_id, user_id)
```

`context` 形如 `{'group': '群号', 'bot': '实例名', 'msgtype': 'group'}`；
`event.has_perm` 会自动从事件构造该上下文。

## 节点匹配与优先级

- 精确匹配优先于通配；`chat.*` 命中 `chat.ban`、`chat.image` 等；`*` 命中一切；
- **显式否决（False）优先于授予（True）**：父组授予 `chat.*` 但用户被显式
  否决 `chat.ban` 时，以否决为准；
- 用户节点优先于组节点，近的上下文优先于全局。

## 上下文限定示例

同一节点可以只在某个群授予：在 Web 面板为用户/组添加节点时选择上下文为指定群，
则该节点只在该群事件中生效，其他群按未定义处理。这让“某用户在 A 群是管理、
在 B 群是普通成员”成为可能。

## 晋升轨道（Track）

权限组可以编排成“轨道”，用户沿轨道 `promote`（晋升）/ `demote`（降级），
适合等级、活跃度体系（底层 API 位于 `framework/perm.py`，可在插件中调用）：

```python
from framework import perm
perm.save_track(db, "活跃度", ["newcomer", "regular", "vip"])
perm.promote(db, user_id, "活跃度", ctx_key="group", ctx_val="123456")
perm.demote(db, user_id, "活跃度")
```

## 时效性与清理

节点/组成员关系支持设置过期时间；框架内置定时任务每小时清理一次过期权限
（`perm.cleanup_expired(db)`），到期自动失效，无需手动维护。

## Web 管理

在管理后台「权限管理」页可可视化完成：

- 创建/编辑/删除权限组，设置权重、前后缀、继承关系；
- 为用户分配/移除权限组；
- 授予或否决具体节点（可限定群/bot 上下文、设置有效期）；
- 配置晋升轨道、查看审计记录。

所有权限变更都会写审计日志；解析结果带短 TTL 缓存，变更后会自动失效重建，
兼顾性能与实时性。
