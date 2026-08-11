# 最佳实践

## 代码与数据分离

```python
# 错误：把配置写到 plugins/my_plugin/config.json
# GitHub 更新时会覆盖用户配置！

# 正确：使用 plugins_dat/ 目录
import os, json

def load_config(ctx):
    data_dir = ctx.get_data_dir()  # 返回 plugins_dat/my_plugin/
    config_path = os.path.join(data_dir, "config.json")
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}
```

## 异常捕获

```python
def handle_xxx(event, match):
    try:
        result = call_external_api()
        ctx.send_msg(group_id=event.group_id, message=result)
    except requests.Timeout:
        ctx.send_msg(group_id=event.group_id, message="请求超时，请稍后重试")
    except Exception as e:
        ctx.logger.exception(f"处理失败: {e}")
        ctx.send_msg(group_id=event.group_id, message="内部错误，请联系管理员")
```

## 避免阻塞事件循环

耗时操作（HTTP 请求、文件 IO）建议放到独立线程：

```python
import threading

def handle_long_task(event, match):
    def worker():
        result = expensive_operation()
        ctx.send_msg(group_id=event.group_id, message=result)
    threading.Thread(target=worker, daemon=True).start()
```

## 权限校验

```python
def handle_admin_cmd(event, match):
    if not event.is_admin:
        return  # 静默忽略非管理员
    # 管理操作...
```

或使用 `require_admin` 参数：

```python
ctx.command("/admin_cmd", handle_admin_cmd, require_admin=True)
ctx.command("/super_cmd", handle_super_cmd, require_superuser=True)
```

## 资源清理

```python
def on_unload(ctx):
    # 关闭文件句柄、数据库连接、HTTP 会话等
    if hasattr(ctx, '_http_session'):
        ctx._http_session.close()
    ctx.logger.info("资源已清理")
```

## 声明业务表

如果插件通过 `ctx.db_execute("CREATE TABLE ...")` 创建了自己的业务表，应在 `plugin.yaml` 中通过 `managed_tables` 声明。这样用户在 Web UI 删除插件并勾选"删除数据"时，框架会自动 DROP 这些表，避免数据库残留：

```yaml
# plugin.yaml
managed_tables:
  - sign_in_records
  - user_scores
```

不声明的话，表会留在数据库中（热卸载和重载不影响业务表，只有彻底删除时才有机会清理）。

## 别名支持

```python
ctx.command("/help", handle_help, alias="/h,/?", description="查看帮助")
```
