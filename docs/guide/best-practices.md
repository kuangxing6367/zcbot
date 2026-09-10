# 官方最佳实践

> 定位：ZCBOT 是一个**事件驱动的插件化服务宿主**，OneBot（QQ）只是默认接入端。
> **本篇面向**：角色 B/C，尤其是想把它用于非 QQ 场景（纯定时 / HTTP Webhook / 内部后台 / 其它 IM）的开发者。
> 本页讲的是**把它当通用服务宿主用**的正确姿势——不局限于 QQ。

---

## 一、先想清楚：ZCBOT 不是"QQ 机器人框架"

很多人误以为 ZCBOT 只能做 QQ 机器人。其实它的骨架是通用的：

- **事件驱动**：任何"事件进来 → 插件响应"的应用都能跑
- **插件化**：功能按插件加载，可插拔、可热重载
- **接入端抽象**：`ProtocolAdapter` 决定"事件从哪来"，与业务插件解耦
- **内置**：权限引擎、双方言数据库、Web 后台、会话、调度器，全是现成的

**它被"OneBot 机器人"这个第一印象困住了。** 事实是：接入端可以换，插件可以换，
但权限、后台、持久化、会话这些骨架能力始终不变。

### 换"接入端 + 插件"的迁移成本（从低到高）

| 你要做什么 | 迁移成本 | 说明 |
| ---------- | -------- | ---- |
| QQ 机器人 | 零 | 现状，默认接入端就是 OneBot |
| 其他 IM（Telegram / Discord / 微信） | 写一个 adapter | 契约见 [协议适配器](../api/protocol_adapter.md) |
| 带权限后台的内部工具 | 换插件 | 权限 + WebUI + DB 现成 |
| 定时任务 / 监控 / 告警 | 换插件 | 调度器 + 事件总线 + 通知渠道现成 |
| 业务系统（审批 / 工单） | 换插件 | 权限 + 会话 + 审计现成 |
| CMS / headless CMS | 换插件 + 前台 adapter | 权限、后台、持久化现成 |
| 数据平台控制面 | 换插件 | HTTP API（`db/query` 等）+ 权限现成 |

> 核心结论：**权限、后台、持久化不绑定任何 IM**。你只需要一个能产生事件的
> `ProtocolAdapter`，以及一组业务插件。

---

## 二、场景一：没有 QQ，纯定时驱动的应用

QQ 机器人依赖 OneBot 客户端实时推消息。但如果你要的是"每天定时干活"，**根本不需要任何 IM 接入端**。

`core_plugins/scheduler` 已经内置 APScheduler。把 `core_plugins.onebot_adapter` 关掉，只留调度器，就变成一个纯定时服务宿主：

```yaml
# core_plugins.yaml（官方插件配置中心）
core_plugins:
  onebot_adapter:
    enabled: false        # 不加载任何 IM 长连接
  webui:
    enabled: true         # 后台 + REST 仍可用
  session:
    enabled: true         # 会话（可选）
  scheduler:
    enabled: true         # 定时任务（必须）
```

写一个业务插件，用 `ctx.task()` 注册定时任务：

```python
# plugins/daily_report/main.py
import datetime

__plugin_meta__ = {
    "name": "日报生成",
    "version": "1.0.0",
    "author": "you",
    "desc": "每天 08:30 生成日报并写入数据库/推送到你的通知渠道",
}

def register(ctx):
    # cron：每天 08:30 执行
    ctx.task("30 8 * * *", daily_report, description="生成每日报表")
    # cron：每 5 分钟采集一次
    ctx.task("*/5 * * * *", collect_metrics, description="采集指标")

def daily_report():
    # 定时任务 handler 没有 event 参数
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    rows = ctx.db_query(
        "SELECT COUNT(*) AS n, SUM(amount) AS total FROM orders "
        "WHERE DATE(created_at) = %s", (yesterday,)
    )
    # 写入一张报表表，Web 后台能看
    ctx.db_execute(
        "INSERT INTO daily_reports (report_date, order_count, total_amount) "
        "VALUES (%s, %s, %s) ON CONFLICT(report_date) DO UPDATE SET "
        "order_count=excluded.order_count, total_amount=excluded.total_amount",
        (yesterday, rows[0]['n'], rows[0]['total'] or 0),
    )
    # 也通过事件总线广播，其他插件可订阅
    ctx.emit("report.generated", {"date": yesterday})

def collect_metrics():
    import psutil
    mem = psutil.virtual_memory().percent
    ctx.db_execute(
        "INSERT INTO metrics (ts, mem_pct) VALUES (%s, %s)",
        (int(__import__('time').time()), mem),
    )
```

> 提示：定时任务 handler 签名不带 `event`（`ctx.task` 见 [API 参考](../api/ctx.md)）。
> 需要发消息时才手动从 `get_connected_bots()` 选一个接入端。

**要点**：定时任务的"事件源"就是时间。没有 OneBot，`scheduler` 就是你的接入端。

---

## 三、场景二：HTTP Webhook 接入（外部程序推事件进来）

很多系统之间靠 HTTP Webhook 互通（GitHub、支付回调、CI 结果……）。写一个
`ProtocolAdapter`，接收外部 POST，转成内部事件，业务插件照常处理。

> 内置 `http_inject` 已实现等价能力（默认 `127.0.0.1:8901/hook`，在 `core_plugins.yaml` 开启即用）。下面手写一个最小 adapter，帮助你理解契约、自定义路径与鉴权；完整版见 [协议适配器](../api/protocol_adapter.md)。

```python
# core_plugins/http_webhook/main.py
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from framework.protocol import ProtocolAdapter

class WebhookHandler(BaseHTTPRequestHandler):
    framework = None
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        try:
            data = json.loads(body.decode('utf-8'))
        except Exception:
            self.send_response(400); self.end_headers(); return
        # 关键：把 HTTP 负载转成框架内部事件，交给 dispatch_event
        event = {
            "type": "message",
            "message_type": "private",
            "bot_name": "webhook",
            "user_id": data.get("user_id", 0),
            "message": data.get("text", ""),
            "sender": {"user_id": data.get("user_id", 0), "nickname": "webhook"},
        }
        import asyncio
        asyncio.run_coroutine_threadsafe(
            self.framework.dispatch_event(event), self.framework.loop).result()
        self.send_response(200); self.end_headers()
        self.wfile.write(b'{"ok":true}')
    def log_message(self, *a): pass

class HttpWebhookAdapter(ProtocolAdapter):
    def __init__(self, framework):
        self.framework = framework
        self._server = None
    def start(self):
        WebhookHandler.framework = self.framework
        self._server = HTTPServer(("127.0.0.1", 8901), WebhookHandler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
    async def stop(self):
        if self._server: self._server.shutdown()
    async def handle_event(self, raw_event, bot_name): return raw_event
    async def call_api(self, action, bot=None, **params): return {}
    def get_connected_bots(self): return ["webhook"]

def register(ctx):
    fw = ctx._framework
    if fw.config.get('http_webhook', {}).get('enabled') is False:
        return
    adapter = HttpWebhookAdapter(fw)
    fw.services.register('protocol_adapter', adapter)
    fw.services.register('api_caller', adapter)
    adapter.start()
    ctx.log("HTTP Webhook adapter 已启动 :8901")

def unregister():
    ...
```

> 提示：`call/acall` 由基类从 `call_api` 自动派生，因此把自己注册成 `api_caller` 后 `ctx.api()` 立即可用；需要框架自动回复或主动发文本时再覆写 `send_text`（详见[协议适配器](../api/protocol_adapter.md) 2.2）。

之后任何插件都能 `@ctx.command("/hello")` 响应 webhook 传来的 `text`，命令路由、权限、审计照常工作——**业务插件根本不知道消息来自 HTTP 还是 QQ**。

> 提示：这就是"换 adapter 换插件"最直观的体现：业务插件零改动，接入端从 QQ 换成 HTTP。

---

## 四、场景三：带权限的业务后台

权限引擎 + WebUI + 数据库都是框架内置的。做一个"内部审批系统"：

1. 用 `ctx.create_table()` 声明业务表（如 `leave_requests`）
2. 用 `require_perm` 控制谁能操作（如 `approval.submit` / `approval.approve`）
3. 用 `ctx.override_webui()` 或插件 WebUI 提供管理界面
4. 用 `ctx.audit_log()` 记录每次审批

```python
@ctx.command("请假", require_perm="approval.submit", help="提交请假申请")
def leave(ev, match):
    days = match.group(1)
    ctx.db_execute(
        "INSERT INTO leave_requests (user_id, days, status, created_at) "
        "VALUES (%s, %s, 'pending', %s)",
        (ev.user_id, days, int(__import__('time').time())),
    )
    ctx.audit_log(action="leave.submit", target_type="user",
                  target_name=str(ev.user_id), detail={"days": days})
    ctx.asend_msg(...)  # 或任何通知渠道
```

权限、审计、后台全是框架给的，你只写业务。

---

## 五、通用插件编写规范（适用于所有接入端）

### 1. 代码与数据分离

```python
# 错误：把配置写到 plugins/my_plugin/config.json —— GitHub 更新会覆盖！
# 正确：用 ctx.get_data_dir()（= data/plugins_dat/my_plugin/）
import os, json

def load_config(ctx):
    p = os.path.join(ctx.get_data_dir(), "config.json")
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    return {}
```

### 2. 不要写死"发送到群/QQ"

让"发消息"抽象化：优先用 `ctx.send_msg(...)` 这类已封装的方法，它在没有 IM 时
可被 adapter 或插件转发到任意通知渠道（Webhook、日志、邮件插件……）。不要在业务
逻辑里直接依赖 `event.group_id` 这种 QQ 特有字段，除非你确认只做 QQ。

### 3. 异常处理

```python
def handle(event, match):
    try:
        result = call_external_api()
    except requests.Timeout:
        ctx.asend_msg(...); return
    except Exception as e:
        ctx.logger.exception(f"处理失败: {e}")   # 一定要记录，别裸 except 吞掉
        ctx.asend_msg(...); return
```

### 4. 耗时操作别阻塞事件循环

同步 HTTP / 文件 IO 放线程，或 handler 写成 `async def` 直接 `await`：

```python
async def handle(event, match):
    result = await asyncio.to_thread(expensive_operation, arg)  # 或直接用 aiohttp
    ctx.asend_msg(...)
```

### 5. 声明业务表

插件自建的业务表写进 `plugin.yaml` 的 `managed_tables`，这样在后台彻底删除插件时
框架会自动 DROP，避免数据库残留：

```yaml
# plugin.yaml
managed_tables:
  - leave_requests
  - daily_reports
```

### 6. 卸载时清理资源

```python
def on_unload(ctx):
    if hasattr(ctx, '_http_session'):
        ctx._http_session.close()
```

---

## 六、提交前自查清单

写完后对照逐条过一遍，能避开大多数"上线才发现"的坑：

- [ ] **函数内给模块级变量重新赋值有没有 `global`？**
  `cache = {}` 这类重绑定会变成局部变量 → `UnboundLocalError`（被 `try/except` 吞掉就成"神秘失效"）。只改内容（`.clear()/.append()/d[k]=v`）不用 `global`。
- [ ] **`except` 是不是裸捕获？**
  至少加一行 `ctx.logger.exception(...)`；禁止 `except:` 后什么都不做。
- [ ] **核心逻辑抽成纯函数并自测？**
  见下方"如何写可测试的插件"。
- [ ] **数据库有没有防注入？**
  一律 `%s` 占位符 + 参数元组，禁止 f-string 拼 SQL。
- [ ] **耗时操作有没有阻塞事件循环？**
- [ ] **业务表写进 `managed_tables` 了吗？**
- [ ] **有没有写死 QQ 特有字段/硬编码发送目标？**（见"通用插件编写规范"第 2 条）

---

## 七、如何写可测试的插件

> 现在框架有服务注册表（DI），但 `ctx` 仍是 `register(ctx)` 注入模块级全局。
> 用"纯函数 + 假 ctx"即可让核心逻辑脱离宿主单独测试。

### 1. 逻辑写成纯函数（不碰 ctx）

```python
def calc_level(exp: int) -> dict:
    level = 1
    for need in (100, 300, 600, 1000):
        if exp >= need: level += 1
        else: break
    return {"level": level, "exp": exp}

def handle_exp(event, match):
    result = calc_level(int(match.group(1)))   # 纯函数，好测
    ctx.asend_msg(...)
```

### 2. 给纯函数写测试（不用启动宿主）

```python
# tests/test_my_plugin.py —— 放项目根 tests/
from plugins.my_plugin.main import calc_level

def test_calc_level():
    assert calc_level(0)["level"] == 1
    assert calc_level(150)["level"] == 2
    assert calc_level(9999)["level"] == 5

if __name__ == "__main__":
    test_calc_level(); print("全部通过")
```

### 3. 用假 ctx（Fake）模拟框架

```python
class FakeCtx:
    def __init__(self):
        self.sent = []
    async def asend_msg(self, **kw):
        self.sent.append(kw)
    def get_data_dir(self):
        return "test_data/"

fake = FakeCtx()
handle_exp(FakeEvent(...), FakeMatch("150"))
assert fake.sent[0]["message"] == "你 2 级"
```

---

## 八、进一步

- 写自己的接入端 → [协议适配器](../api/protocol_adapter.md)
- 完整插件开发 → [编写插件](./writing-plugins.md)
- 权限引擎 → [权限系统](../advanced/permission.md)
