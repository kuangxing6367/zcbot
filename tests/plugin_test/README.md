# plugin_test —— 插件测试夹具

写插件测试不用再从头搭环境。这里给好了三件东西：一个**录制型 ctx**、一个**临时插件模块工厂**、一个**最小消息事件**。
不启动 Framework、不连数据库、不占端口、不发网络请求，一条用例毫秒级。

```bash
py -3 -m pytest tests/plugin_test -q        # 只跑这里
py -3 -m pytest tests -q                    # 全量也会带上这里
```

## 怎么开始

新建 `tests/plugin_test/test_我的插件.py`，照 `test_echo_plugin.py` 抄一段：

```python
def test_my_plugin_declares_command(make_plugin, rec_ctx):
    mod = make_plugin("""
from framework.plugin import command, flush

@command('/hello', priority=20, description='打招呼')
def hello(event, match):
    return 'hi ' + str(event.user_id)

def register(ctx):
    flush(__name__, ctx)
""")
    mod.register(rec_ctx)

    (_, kw), = rec_ctx.of('command')
    assert kw['pattern'] == '/hello'
    assert kw['priority'] == 20
```

## 三个 fixture

| fixture | 作用 | 备注 |
|---|---|---|
| `rec_ctx` | 录制型 ctx，把插件注册了什么原样记下来 | 用 `rec_ctx.of('command')` 取调用、`rec_ctx.handlers('on')` 取处理函数 |
| `make_plugin(source, name=...)` | 把插件源码写成临时模块并导入 | 默认模块名 `plugin_test_demo_plugin`；同一条用例里造多个模块要传不同 `name` |
| `msg_event(text, group_id=...)` | 构造一条最小消息事件 | `event.replies` 记下 `event.reply()` 的调用 |

`rec_ctx` 用 `__getattr__` 兜底：ctx 以后新增登记方法，夹具不用跟着改。所以**不要**在夹具里逐个写死 `ctx.command` 之类的方法。

## 四条硬约束

1. **`flush(__name__, ctx)` 的模块名必须对得上**。`flush` 按模块名匹配登记项，手工改过 `__module__` 或用 `exec` 灌源码却没注册进 `sys.modules`，登记就会被漏掉——表现是「插件加载了但命令不存在」，很难查。
2. **不要在这里起真 Framework**。要验数据库迁移、适配器注册、HTTP 路由，写 `tests/` 根下的专项用例；这里只验插件自己。
3. **`tests/plugin_test` 会被全量收集**，所以这里只能放**稳定通过**的用例。验尸性、一次性的脚本别放进来。
4. **登记表和 `sys.modules` 由 `make_plugin` 自动清理**。自己 `import` 的临时模块要手动清，否则下一个用例会 import 到旧模块、或下一次 `flush` 重放旧登记。

## 想看真实 ctx 长什么样

- 装饰器登记实现：`framework/plugin.py`（`_record` / `_apply` / `flush` / `has_pending`）
- ctx 各登记方法的语义：`framework/ctx/base.py` 的 `PluginContext`，具体方法分散在
  `framework/ctx/events.py`（`command` / `on` / `hook`）、`messaging.py`、`webui.py`、`db.py`
- 真实框架怎么加载插件：`framework/loader/lifecycle.py` 的 `load_plugin`
