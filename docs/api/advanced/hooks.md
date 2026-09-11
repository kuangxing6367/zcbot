# 扩展点（Hook 系统）

> **本篇面向**：所有想"往框架里插入自己逻辑"的开发者（角色 B / C）。
> 这是微内核对外最核心的契约——内核只做最小必要的事，其余行为都通过扩展点开放。

## 一句话理解

ZCBOT 的内核（Framework）只负责：**加载插件、路由事件、提供公共服务**（数据库 / 权限 / 服务注册 / 事件总线）。
它不规定"消息来了该干什么、Web 请求该怎么拦、动作发出前要不要审计"——这些全部留给**扩展点（hook point）**。

一个扩展点就是内核运行流程上的一个"插槽"。插件用 `ctx.hook(point, handler)` 往插槽里插自己的函数，
内核跑到那个环节就会按优先级依次调用你插进去的所有 handler。这套机制让你能在**几乎每一个运行环节**挂载行为，
而无需修改框架源码、无需继承任何基类。

```
            ┌─────────────────────────────────────────────┐
            │            内核（Framework 微内核）           │
            │  加载 ─ 路由 ─ 服务注册 ─ 事件总线 ─ 数据库   │
            │                                             │
            │   lifecycle.startup ──► 插件启动预热          │
            │        │                                      │
            │   http.before_request ──► 请求拦截/鉴权增强   │
            │        │                                      │
            │   event.before_dispatch ──► 事件过滤          │
            │        │                                      │
            │   command.before / after ──► 命令切面         │
            │        │                                      │
            │   action.before / after ──► 协议动作切面       │
            │        │                                      │
            │   message.before/after_send ──► 出站文本切面   │
            │        │                                      │
            │   lifecycle.shutdown ──► 资源释放             │
            └─────────────────────────────────────────────┘
              每一格都是一个扩展点，插件用 ctx.hook() 挂接
```

## 标准扩展点一览

| 扩展点 | 触发时机 | handler 参数 | 可短路？ | 运行上下文 |
| ------ | -------- | ------------ | -------- | ---------- |
| `lifecycle.startup` | 全部插件加载注册完成后、终端启动前 | — | 否 | 事件循环（async） |
| `lifecycle.shutdown` | 框架停止最开始 | — | 否 | 事件循环（async） |
| `http.before_request` | 每个 HTTP 请求处理前 | `request` | 是（返回 Flask Response 即短路） | Web 线程（sync） |
| `http.after_request` | 每个 HTTP 请求返回前 | `request, response` | 否（须返回 response） | Web 线程（sync） |
| `event.before_dispatch` | 事件进入内核、分发前 | `event(dict), bot_name` | 是（返回 `False` 丢弃事件） | 事件循环（async） |
| `event.after_dispatch` | 事件分发处理完毕后（含提前返回） | `event(dict), bot_name` | 否 | 事件循环（async） |
| `command.before` | 命令命中、执行 handler 前 | `{'plugin','handler','command_id','pattern','event','match'}` | 是（返回 `False` 跳过该命令） | 事件循环（async） |
| `command.after` | 命令 handler 执行后 | 同上 + `result` | 否（通知） | 事件循环（async） |
| `message.before_send` | 框架主动发文本前（如权限不足提示、关键词回复） | `{'text','group_id','user_id','source'}` | 是（返回 `False` 取消发送） | 事件循环（async） |
| `message.after_send` | 框架主动发文本后 | 同上 + `result` | 否 | 事件循环（async） |
| `action.before` | 任意协议动作调用前（send_msg / 禁言 / 查询…） | `action(str), params(dict), bot` | 否（通知） | 视调用方（async/sync 皆可） |
| `action.after` | 任意协议动作调用后 | `action, params, bot, result` | 否（通知） | 视调用方（async/sync 皆可） |

> `action.before/after` 是**覆盖面最广**的扩展点：只要插件/内核通过 `ctx.aapi()` / `ctx.api()` / `onebot.*`
> 发出的动作都会经过它，因此可以做统一审计、限流、改写、统计，而无需逐个命令去加代码。

## 注册与注销

```python
__plugin_meta__ = {"name": "MyExt", "version": "1.0.0", "priority": 50}

def register(ctx):
    # handler 可以是普通函数，也可以是 async def
    ctx.hook('action.after', on_action, priority=10)
    ctx.hook('event.before_dispatch', on_event, priority=50)
    ctx.hook('http.after_request', add_header)

async def on_action(action, params, bot, result):
    ctx.log(f"动作 {action} -> {result.get('status')}")

def on_event(event, bot_name):
    # 返回 False 直接丢弃该事件（例如黑名单来源）
    if event.get('user_id') in BLOCKLIST:
        return False

def add_header(request, response):
    response.headers['X-Powered-By'] = 'ZCBOT'
    return response
```

要点：

- **优先级**：`priority` 越小越先执行（默认 50）。多个插件挂同一扩展点时，按优先级串行调用。
- **同名去重**：内核以 `插件名:扩展点` 作为 handler 的唯一名；插件热重载时重复 `ctx.hook` 同一扩展点会自动覆盖，不会越挂越多。
- **注销**：`ctx.unhook('action.after')` 移除本插件在该扩展点的全部 handler；插件被卸载/禁用时框架也会自动清理。
- **自定义扩展点**：点位只是字符串，你可以注册任意自定义点位（如 `'myext.on_tick'`），并在自己的代码里用
  `ctx._framework.hooks.trigger_async('myext.on_tick', ...)` 触发，实现插件内部的发布/订阅。

## 同步 vs 异步 handler

| 扩展点运行上下文 | 推荐写法 | 说明 |
| ---------------- | -------- | ---- |
| 事件循环内（lifecycle / event / command / message / action 经 `aapi`） | `async def` 直接 `await` | 与内核同循环，零线程切换 |
| Web 线程内（http.*） | 普通函数 | 若写成 `async def`，内核会把它交给事件循环 **fire-and-forget**（不阻塞请求线程） |

> 在 `http.before/after_request` 里要做异步 DB 操作？用 `ctx.call_async(coro)` 或 `ctx.db_query_async(...)`，
> 不要自己 `await` 阻塞请求线程。

## 短路语义

只有以下扩展点支持"返回值改变流程"，其余均为**通知（不短路）**：

- `http.before_request`：返回带 `status_code` 的对象（如 `jsonify(...)` / `redirect(...)` / `abort(...)` 的响应）即短路，内核直接返回它，不再走后续路由。
- `event.before_dispatch`：任一 handler 返回 `False` → 事件被丢弃，不再路由 / 广播。
- `command.before`：任一 handler 返回 `False` → 跳过当前命令执行，继续尝试其它匹配（命中关键词等）。
- `message.before_send`：任一 handler 返回 `False` → 取消本次框架文本发送。
- `action.before`：**不短路**（动作已不可避免，仅作通知 / 审计）。

## 示例

### 基础 1：审计每一次出站动作

```python
def register(ctx):
    ctx.hook('action.after', audit)

def audit(action, params, bot, result):
    # action 覆盖 send_msg / set_group_ban / get_group_member_list 等一切动作
    ctx.log(f"[审计] {action} bot={bot} status={result.get('status')}")
```

### 基础 2：丢弃来自黑名单来源的事件

```python
BLOCK = {123456}

def register(ctx):
    ctx.hook('event.before_dispatch', drop_blocked)

def drop_blocked(event, bot_name):
    if event.get('user_id') in BLOCK:
        return False   # 丢弃，内核不再路由
```

### 进阶 1：Web 响应加统一头（中间件式）

```python
def register(ctx):
    ctx.hook('http.after_request', add_cors_debug)

def add_cors_debug(request, response):
    response.headers['X-ZCBOT'] = '1'
    return response
```

### 进阶 2：命令执行切面（计时 / 审计）

```python
import time

def register(ctx):
    ctx.hook('command.before', cmd_enter)
    ctx.hook('command.after', cmd_exit)

_t = {}

def cmd_enter(info):
    _t[info['command_id']] = time.time()

def cmd_exit(info):
    cost = time.time() - _t.pop(info['command_id'], time.time())
    ctx.log(f"[命令耗时] {info['handler']} 用时 {cost:.3f}s")
```

### 进阶 3：启动预热（缓存 / 建连接）

```python
def register(ctx):
    ctx.hook('lifecycle.startup', warm_up)

async def warm_up():
    # 进程启动时一次性加载，避免在消息热路径里反复查库
    rows = await ctx.db_query_async("SELECT word FROM sensitive_words")
    ctx._cache = {r['word'] for r in rows}
    ctx.log(f"敏感词缓存已预热：{len(ctx._cache)} 条")
```

### 进阶 4：自定义扩展点（插件内部总线）

```python
# 在你的插件里定义并触发自己的点位
async def do_work(ctx):
    await ctx._framework.hooks.trigger_async('myext.on_job', job_id=1)

def register(ctx):
    ctx.hook('myext.on_job', handle_job)

async def handle_job(**kw):
    ctx.log(f"收到自定义事件: {kw}")
```

## 性能与注意

- **零开销空载**：某扩展点没有任何 handler 时，内核触发只是遍历空列表，几乎不可测量；你挂了 handler 才会有成本。
- **不要在 sync hook 里做阻塞 IO**：`http.*` 跑在 Web 请求线程，阻塞会拖慢所有请求；需要 DB 请走 `ctx.db_query_async` / `ctx.call_async`。
- **保持幂等**：`command.before` / `event.before_dispatch` 在高频消息路径上，逻辑要轻；重活交给 `*.after` 或后台任务。
- **异常隔离**：任一 handler 抛异常，内核会记日志并跳过它，不会连累其它 handler 或主流程。

## 与既有扩展机制的关系

| 需求 | 用什么 |
| ---- | ------ |
| 插入 HTTP REST 路由（复用框架鉴权） | `ctx.register_api(path, handler, ...)`（见 [Framework](./../basic/framework.md) 或 Web 章节） |
| 订阅/发布业务事件 | `ctx.on / ctx.emit / ctx.aemit`（事件总线） |
| 接管原始消息（命令匹配前） | `ctx.on_raw_message(handler)` |
| 在运行环节插行为（本文） | `ctx.hook(point, handler)`（扩展点） |

扩展点是它们之间最"底层、最广"的一层：事件总线偏业务解耦，`register_api` 偏对外 HTTP，而
`hook` 直接挂在内核的每个运行环节上，适合做横切关注点（审计、限流、过滤、中间件、生命周期管理）。
