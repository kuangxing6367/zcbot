# 权限系统

ZCBOT 内置 LuckPerms 风格的权限系统，支持节点、组、继承、上下文。

## 基本概念

| 概念 | 说明 |
|------|------|
| 权限节点 | 如 `sign_in.use`、`admin.ban` |
| 权限组 | 一组权限节点的集合 |
| 继承 | 子组继承父组的权限 |
| 上下文 | 限定权限生效范围（如某个群） |

## 身份等级

```
super（超管）> owner（群主）> admin（管理员）> member（成员）> blacklist（黑名单）
```

## 在插件中使用

### 检查权限

```python
async def handle_ban(event, match):
    if not ctx.has_perm(event.user_id, 'admin.ban'):
        await ctx.asend_msg(..., message="权限不足")
        return
    # 执行禁言操作...
```

### 三态检查

```python
result = ctx.check_perm(user_id, 'myplugin.use')
# True  = 授予
# False = 显式否决
# None  = 未定义（按拒绝处理）
```

### 命令权限要求

```python
ctx.command(
    "/ban",
    handle_ban,
    require_admin=True,        # 需要管理员权限
    # require_superuser=True,  # 需要超管权限
    # require_perm="admin.ban",  # 需要权限节点
)
```

## Web 管理

在 Web 面板「权限管理」页面可以：
- 创建/删除权限组
- 为用户分配权限组
- 设置权限节点
- 配置继承关系
