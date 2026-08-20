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

## 提交前自查清单

写完后提交 / 发布前，对照下面逐条过一遍，能避免大多数"上线才发现"的坑：

- [ ] **函数内修改模块级变量有没有 `global`？**
  - 在 handler 里给模块级 dict/列表**重新赋值**（`cache = {}`）而不只是改内容（`cache.clear()`）时，必须声明 `global`。
  - 否则 Python 会把它当成局部变量，运行时抛 `UnboundLocalError`。如果这段代码外面套了 `try/except`，异常会被静默吞掉，变成"功能神秘失效"——本框架就踩过这个坑（Event.role 全员降级 member，见 [KNOWN_ISSUES.md](./KNOWN_ISSUES.md) S7）。
  - 规律：**只改内容的操作（`.clear()/.append()/dict[k]=v`）不用 global；重新绑定名字的赋值（`x = ...`）需要。**
- [ ] **`except` 是裸捕获吗？**
  - `except:` / `except Exception:` 会把真实错误吞掉。要么缩小异常类型（`except requests.Timeout:`），要么至少加一行 `ctx.logger.exception(...)` 记录现场。
  - 禁止"except 后什么都不做"——错误会变成无法定位的幽灵 bug。
- [ ] **测试覆盖了吗？**
  - 至少把纯逻辑抽出来用 `if __name__ == "__main__":` 手跑一遍；核心逻辑建议写 pytest（见下节"可测试的插件"）。
  - 上线前用 `/echo` 或造一条真实消息验证命令能命中、权限正确。
- [ ] **数据库操作有没有防注入？**
  - 一律用 `%s` 占位符 + 参数元组，禁止用 f-string 拼 SQL（见 [插件开发详解](./plugin-tutorial.md) 的数据库章节）。
- [ ] **耗时操作是否阻塞了事件循环？**
  - 同步 HTTP/文件 IO 放线程（`threading.Thread` 或 async handler 里直接 `await`），见上文"避免阻塞事件循环"。
- [ ] **声明的业务表有没有写进 `plugin.yaml` 的 `managed_tables`？**（见上文"声明业务表"）
- [ ] **卸载时资源清理了吗？**（见上文"资源清理"）

## 如何写可测试的插件

> 说明：ZCBOT 的 `ctx` 是框架在 `register(ctx)` 时注入模块级全局的（`module.ctx`），**目前框架本身不支持依赖注入（DI）**。但你可以用下面的思路把逻辑和框架解耦，让核心代码可以脱离机器人单独测试。

### 1. 把逻辑写成纯函数（不碰 ctx）

规则：**凡是"算一算"的逻辑，一律写成不依赖 ctx 的普通函数**，handler 只做"取参数 → 调函数 → 发结果"三件事：

```python
def calc_level(exp: int) -> dict:
    """纯逻辑：给经验值，算等级。不碰 ctx，可以单测。"""
    level = 1
    for need in (100, 300, 600, 1000):
        if exp >= need:
            level += 1
        else:
            break
    return {"level": level, "exp": exp}

def handle_exp(event, match):
    exp = int(match.group(1))
    result = calc_level(exp)          # 纯函数，好测
    ctx.send_msg(group_id=event.group_id, message=f"你 {result['level']} 级")
```

### 2. 给纯函数写测试（不用启动机器人）

把纯函数放在插件里，测试脚本可以独立运行：

```python
# tests/test_sign_in.py —— 不放插件目录，放项目根 tests/ 下
from plugins.my_plugin.main import calc_level

def test_calc_level():
    assert calc_level(0)["level"] == 1
    assert calc_level(150)["level"] == 2
    assert calc_level(9999)["level"] == 5

if __name__ == "__main__":
    test_calc_level()
    print("全部通过")
```

> 框架本身还没带 pytest 目录（见 [KNOWN_ISSUES.md](./KNOWN_ISSUES.md) P2），先这样手动跑即可。

### 3. 用假 ctx（Fake）模拟框架

handler 里实在绕不开的 ctx 调用，可以在测试里造一个"假 ctx"来收集调用、返回固定结果：

```python
class FakeCtx:
    def __init__(self):
        self.sent = []
        self._data = {}
    def send_msg(self, **kw):
        self.sent.append(kw)
    def get_data_dir(self):
        return "test_data/"

# 用法：把 handler 绑到假 ctx 上跑
fake = FakeCtx()
handle_exp(FakeEvent(group_id=1), FakeMatch("150"))
assert fake.sent[0]["message"] == "你 2 级"
```

这样即使框架不支持依赖注入，你也能把"纯逻辑 + 假 ctx"组合起来，在几秒内跑完核心测试，而不是每次改完都要重启机器人试。
