# 最佳实践

凌晨 8 点半，报表要自动生成并落库——此时你可能根本没接任何聊天软件。ZCBOT 的骨架是事件驱动的：调度器到点产生事件，插件响应，权限、Web 后台、数据库照常可用。这一页讲怎么用好这副骨架：纯定时、HTTP Webhook、带权限的内部工具，以及写插件时该守的规矩。

## 先建立正确预期

很多人以为它「只能做个 QQ 机器人」。其实骨架是通用的：

| 能力 | 说明 |
| ---- | ---- |
| 事件驱动 | 任何「事件进来 → 插件响应」的应用都能跑 |
| 插件化 | 功能按插件加载，可插拔、可热重载 |
| 接入端抽象 | `ProtocolAdapter` 决定事件从哪来，与业务解耦 |
| 自带基建 | 权限、双方言数据库、Web 后台、会话、调度器都是现成的 |

**接入端可以换，插件可以换，权限 / 后台 / 持久化 / 会话这些骨架不变。**

### 换场景要动多少（从易到难）

| 你想做什么 | 改动量 | 说明 |
| ---------- | ------ | ---- |
| 默认 QQ 机器人 | 零 | 开箱即是 |
| 换 Telegram / Discord / QQ 官方 | 开对应接入端 | 已内置，填凭证即可；不够再写 adapter |
| 定时任务 / 监控告警 | 换插件 | 调度器 + 事件总线现成 |
| 带权限的内部工具 | 换插件 | 权限 + 后台 + 数据库现成 |
| 审批 / 工单类业务 | 换插件 | 权限 + 会话 + 审计现成 |
| CMS / 数据平台控制面 | 换插件 + 接入端 | 后台与 HTTP API 现成 |

> 结论：权限、后台、持久化不绑任何 IM。你只需要一个能产生事件的接入端，加一组业务插件。

## 场景一：没有聊天平台，纯定时

「每天 8 点干活」根本不需要接入端。

`core_plugins.yaml` 示例：

```yaml
core_plugins:
  onebot_adapter:
    enabled: false        # 不加载任何 IM 长连接
  webui:
    enabled: true         # 后台照常
  session:
    enabled: true         # 可选
  scheduler:
    enabled: true         # 定时任务（必须）
```

插件里用 `ctx.task()` 注册：

```python
# plugins/daily_report/main.py
import datetime

__plugin_meta__ = {
    "name": "日报生成",
    "version": "1.0.0",
    "author": "you",
    "desc": "每天 08:30 生成日报",
}

def register(ctx):
    ctx.task("30 8 * * *", daily_report, description="生成每日报表")
    ctx.task("*/5 * * * *", collect_metrics, description="采集指标")

def daily_report():
    # 定时 handler 没有 event 参数
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    rows = ctx.db_query(
        "SELECT COUNT(*) AS n, SUM(amount) AS total FROM orders "
        "WHERE DATE(created_at) = %s", (yesterday,)
    )
    ctx.db_execute(
        "INSERT INTO daily_reports (report_date, order_count, total_amount) "
        "VALUES (%s, %s, %s) ON CONFLICT(report_date) DO UPDATE SET "
        "order_count=excluded.order_count, total_amount=excluded.total_amount",
        (yesterday, rows[0]['n'], rows[0]['total'] or 0),
    )
    ctx.emit("report.generated", {"date": yesterday})

def collect_metrics():
    import psutil
    mem = psutil.virtual_memory().percent
    ctx.db_execute(
        "INSERT INTO metrics (ts, mem_pct) VALUES (%s, %s)",
        (int(__import__('time').time()), mem),
    )
```

> 提示：定时任务签名不带 `event`（见 [ctx 参考](../api/basic/ctx.md)）。
> 真要发消息时，再从 `get_connected_bots()` 里选一个就绪接入端。

要点：定时任务的事件源就是时间。没有 OneBot 时，`scheduler` 就是你的事件源。

## 场景二：HTTP Webhook（外部系统推事件）

GitHub、支付回调、CI 结果……都可以 POST 进来。

**用内置 `http_inject`（推荐）**：`core_plugins.yaml` 打开后，默认收：

```bash
curl -X POST http://127.0.0.1:8901/hook \
     -H 'Content-Type: application/json' \
     -d '{"type":"message","user_id":10001,"message":"/hello"}'
```

事件归一化入核后，和 IM 来的消息走同一条插件管线。

**想自定义路径 / 鉴权时**，可手写最小接入端（理解契约用，完整版见 [协议适配器](../api/advanced/protocol_adapter.md)）：

```python
# core_plugins/http_webhook/main.py
import json, threading, asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
from framework.messaging.protocol import ProtocolAdapter

class WebhookHandler(BaseHTTPRequestHandler):
    framework = None
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        try:
            data = json.loads(body.decode('utf-8'))
        except Exception:
            self.send_response(400); self.end_headers(); return
        event = {
            "type": "message",
            "message_type": "private",
            "bot_name": "webhook",
            "user_id": data.get("user_id", 0),
            "message": data.get("text", ""),
            "sender": {"user_id": data.get("user_id", 0), "nickname": "webhook"},
        }
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

之后任意插件用 `ctx.command("/hello", ...)` 注册的命令都能响应——**业务根本不知道消息来自 HTTP 还是 IM。**

## 场景三：带权限的业务后台

权限引擎、WebUI、数据库都是内置的。做一个「内部审批」只要四步：

1. `ctx.create_table()` 声明业务表（如 `leave_requests`）
2. `require_perm` 控制谁能操作
3. `ctx.override_webui()` 或插件 WebUI 出界面
4. `ctx.audit_log()` 记每次操作

```python
def register(ctx):
    ctx.command("请假", leave, require_perm="approval.submit",
                description="提交请假申请")

async def leave(ev, match):
    days = match.group(1)
    ctx.db_execute(
        "INSERT INTO leave_requests (user_id, days, status, created_at) "
        "VALUES (%s, %s, 'pending', %s)",
        (ev.user_id, days, int(__import__('time').time())),
    )
    ctx.audit_log(action="leave.submit", target_type="user",
                  target_name=str(ev.user_id), detail={"days": days})
    await ctx.asend_msg(user_id=ev.user_id,
                        group_id=ev.group_id if ev.is_group else None,
                        message="请假申请已提交")
```

权限、审计、后台是平台给的，你只写业务。权限节点与身份轴的细节见 [权限系统](../advanced/permission.md)。

## 写插件的通用规范

### 1. 代码与数据分离

```python
# 错误：配置写在 plugins/my_plugin/config.json —— 更新插件会被覆盖！
# 正确：用 ctx.get_data_dir()（= data/plugins_dat/my_plugin/）
import os, json

def load_config(ctx):
    p = os.path.join(ctx.get_data_dir(), "config.json")
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    return {}
```

### 2. 不要写死「发到某群 / 某平台」

优先 `ctx.send_msg(...)`；没有 IM 时也能被转到 Webhook、日志、邮件等渠道。
除非你确定只服务单一平台，否则别在业务里强依赖 `event.group_id` 这类字段。

### 3. 异常要记日志

```python
def handle(event, match):
    try:
        result = call_external_api()
    except requests.Timeout:
        ctx.send_msg(...); return
    except Exception as e:
        ctx.logger.exception(f"处理失败: {e}")   # 别裸 except 吞掉
        ctx.send_msg(...); return
```

### 4. 耗时操作别堵事件循环

同步 HTTP / 文件 IO 放线程，或 handler 直接写 `async def`：

```python
async def handle(event, match):
    result = await asyncio.to_thread(expensive_operation, arg)
    await ctx.asend_msg(...)
```

### 5. 业务表写进 `managed_tables`

后台彻底删插件时框架会自动 DROP 这些表，避免残留：

```yaml
# plugin.yaml
managed_tables:
  - leave_requests
  - daily_reports
```

### 6. 卸载时清理资源

```python
def on_unload():
    if hasattr(ctx, '_http_session'):
        ctx._http_session.close()
```

## 提交前自查

- [ ] 函数里给模块级变量重赋值有没有 `global`？（只改内容 `.append` / `d[k]=v` 不用）
- [ ] `except` 有没有至少 `ctx.logger.exception(...)`？
- [ ] 核心逻辑是否抽成纯函数、能单测？
- [ ] SQL 是否一律 `%s` 占位 + 参数，没有 f-string 拼接？
- [ ] 耗时操作有没有阻塞事件循环？
- [ ] 业务表是否进了 `managed_tables`？
- [ ] 有没有写死平台字段 / 发送目标？（见规范第 2 条）

## 怎么测插件

`ctx` 虽是 `register(ctx)` 注入的全局，但核心逻辑写成纯函数后，用「假 ctx」就能单测。

### 1. 逻辑写成纯函数

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

### 2. 直接跑测试（不必启动整个框架）

```python
# tests/test_my_plugin.py
from plugins.my_plugin.main import calc_level

def test_calc_level():
    assert calc_level(0)["level"] == 1
    assert calc_level(150)["level"] == 2
    assert calc_level(9999)["level"] == 5

if __name__ == "__main__":
    test_calc_level(); print("全部通过")
```

### 3. 用 FakeCtx 模拟框架

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

## 继续深入

- 自写接入端 → [协议适配器](../api/advanced/protocol_adapter.md)
- 完整插件教程 → [编写插件](./writing-plugins.md)
- 权限机制 → [权限系统](../advanced/permission.md)
