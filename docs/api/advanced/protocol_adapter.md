# 协议适配器（写自己的接入端）

> 这是让 ZCBOT **不局限于单一平台** 的关键文档。框架核心不绑定任何具体 IM 协议（`framework/`
> 内没有任何 OneBot 实现）；你写一个 `ProtocolAdapter`，就能把宿主接到任意"事件源"上
> ——另一个 IM、HTTP Webhook、定时器、MQTT、消息队列……业务插件完全感知不到接入端换了。

---

## 一、核心概念

### 1.1 事件从哪里来？

ZCBOT 的事件流是：

```
外部事件源 ──► ProtocolAdapter（转换）──► framework.dispatch_event(event) ──► 命令路由 / 事件总线 ──► 插件
```

- **Adapter 只做一件事**：把"外部原始事件"翻译成框架统一的内部事件 dict，交给 `dispatch_event`。
- 内部事件 dict 是**接入端无关的**——这正是业务插件能跨平台复用的原因。
- 反向（发消息）有两条路：通用动作调用 `call_api`，以及协议中立的发文本 `send_text`（见 2.2）。

### 1.2 内部事件 dict 长什么样？

`framework/core.py` 的 `dispatch_event` 按 `type` 分流，消息类事件至少需要这些字段：

```python
event = {
    "type": "message",            # message / notice / request / meta_event
    "message_type": "private",    # private / group
    "bot_name": "default",        # 区分多接入端/多账号
    "user_id": 12345,             # 发送者 ID（任意字符串/数字都行）
    "group_id": None,             # 群场景才有
    "message": "你好",             # 文本内容（_extract_text 会解析富媒体）
    "sender": {"user_id": 12345, "nickname": "某人"},
}
```

只要 `type == "message"` 且带 `message` / `user_id`，框架就会走命令路由——
`/echo 你好`、`require_perm`、审计等全部自动生效。

---

## 二、ProtocolAdapter 抽象契约

`framework/protocol.py` 定义了抽象基类。

### 2.1 必须实现的 5 个抽象方法

```python
from framework.protocol import ProtocolAdapter

class MyAdapter(ProtocolAdapter):
    async def handle_event(self, raw_event: dict, bot_name: str) -> dict | None:
        """把原始协议事件转成内部事件 dict；返回 None 表示丢弃"""

    async def call_api(self, action: str, bot: str = None, **params) -> dict:
        """调用下游协议 API 并返回结果（没有 IM 时可直接返回 {}）"""

    def get_connected_bots(self) -> list:
        """返回当前已连接的来源标识列表（用于主动发消息时选目标）"""

    def start(self):
        """启动适配器（监听端口 / 建立连接 / 起线程）"""

    async def stop(self):
        """停止适配器、释放连接"""
```

### 2.2 基类白送的 3 个方法（一般不用自己写）

| 方法 | 默认行为 | 什么时候要覆写 |
| ---- | -------- | -------------- |
| `await acall(action, bot=None, **params)` | 直接 `await self.call_api(...)` | 基本不用 |
| `call(action, bot=None, **params)`（同步） | 把 `call_api` 协程桥接到框架主事件循环执行，供 Web / 线程上下文使用 | 基本不用 |
| `await send_text(text, *, user_id=None, group_id=None, source=None)` | 返回 `{"status":"unsupported"}`，**不抛异常** | **想让框架/插件能主动发文本时覆写** |

要点：

- 只要实现了 `call_api`，把自己 `register('api_caller', self)` 后，插件里的
  `ctx.api()` / `ctx.aapi()` 就**立刻可用**——`call/acall` 由基类转发，无需重复样板。
- **框架自身的自动回复**（关键词回复、各类"权限不足"提示等）统一走
  `Framework.reply_text(event, text)`，它会调用当前接入端覆写后的 `send_text`。
  如果你不覆写 `send_text`，这些自动回复在你的接入端上不会发出（基类返回 unsupported）。
- 纯采集 / 纯定时、不需要对外发消息的接入端：`call_api` 返回 `{}`、不覆写
  `send_text` 即可，不会报错。

一个最小的 `send_text` 实现（把"发文本"翻译成本协议动作）：

```python
async def send_text(self, text, *, user_id=None, group_id=None, source=None):
    if group_id:                       # 群
        return await self.call_api("send_group_msg", group_id=group_id, message=text)
    return await self.call_api("send_private_msg", user_id=user_id, message=text)
```

> 对照实现可看 `core_plugins/onebot_adapter/main.py` 的 `OneBotAdapter.send_text`。

---

## 三、完整工作示例：HTTP Webhook adapter

下面是一个**可运行的完整 adapter**：接收外部 HTTP POST，转成内部消息事件，
业务插件照常响应。它证明了"没有 IM 平台，宿主也能跑完整流程"。

### 3.1 目录结构

```
core_plugins/http_webhook/
├── __init__.py
└── main.py
```

> 放进 `core_plugins/` 就是官方插件（在 `core_plugins.yaml` 里用开关控制）；
> 放进 `plugins/` 就是用户插件。两者加载机制一致。

### 3.2 main.py

```python
# core_plugins/http_webhook/main.py
"""
HTTP Webhook 接入端：接收外部 POST → 转内部事件 → 交给框架路由。
演示「换 adapter 不换插件」：业务插件不需要知道消息来自 HTTP 还是 IM 平台。
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from framework.protocol import ProtocolAdapter

__plugin_meta__ = {
    "name": "http_webhook",
    "version": "1.0.0",
    "author": "you",
    "desc": "HTTP Webhook 接入端示例（非 OneBot）",
    "priority": 10,
    "official": True,
    # 双进程归属：协议/Web 基础设施标 "core"（仅核心进程与单进程加载）；
    # 业务能力插件省略或标 "host"。单进程部署时该字段不影响加载。
    "process": "core",
}


class WebhookHandler(BaseHTTPRequestHandler):
    """把 HTTP POST 负载转成内部事件，投递到框架"""
    framework = None
    route = "/hook"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False, "error": "invalid json"})
            return

        # 把 HTTP 负载翻译成框架内部消息事件
        event = {
            "type": "message",
            "message_type": data.get("message_type", "private"),
            "bot_name": "webhook",
            "user_id": data.get("user_id", 0),
            "group_id": data.get("group_id"),
            "message": data.get("text", ""),
            "sender": {"user_id": data.get("user_id", 0),
                       "nickname": data.get("nickname", "webhook")},
        }

        # 投递给框架（run_coroutine_threadsafe：从 HTTP 线程跨到事件循环）
        fut = asyncio.run_coroutine_threadsafe(
            self.framework.dispatch_event(event), self.framework.loop)
        try:
            fut.result(timeout=10)
        except Exception:
            pass

        self._json(200, {"ok": True})

    def do_GET(self):
        self._json(200, {"ok": True, "hint": "POST /hook 即可注入事件"})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 不刷默认访问日志
        pass


class HttpWebhookAdapter(ProtocolAdapter):
    """实现 ProtocolAdapter 契约的最小接入端"""

    def __init__(self, framework):
        self.framework = framework
        self._server = None

    def start(self):
        cfg = self.framework.config.get("http_webhook", {})
        host = cfg.get("host", "127.0.0.1")
        port = cfg.get("port", 8901)
        WebhookHandler.framework = self.framework
        self._server = HTTPServer((host, port), WebhookHandler)
        threading.Thread(target=self._server.serve_forever,
                         daemon=True, name="http-webhook").start()

    async def stop(self):
        if self._server:
            self._server.shutdown()

    async def handle_event(self, raw_event, bot_name):
        # 已在 WebhookHandler 转好，这里原样透传
        return raw_event

    async def call_api(self, action, bot=None, **params):
        # 没有下游 IM 可发，返回空即可；也可改成转发到第三方通知渠道
        return {}

    # 想让框架的自动回复/主动发文本在本接入端生效，就覆写 send_text；
    # 不覆写则沿用基类的 unsupported（不会报错）。
    # async def send_text(self, text, *, user_id=None, group_id=None, source=None):
    #     ...

    def get_connected_bots(self):
        return ["webhook"]


_adapter = None


def register(ctx):
    """作为接入端注册进服务注册表并启动"""
    global _adapter
    fw = ctx._framework
    if fw.config.get("http_webhook", {}).get("enabled") is False:
        ctx.log("http_webhook 已禁用")
        return

    _adapter = HttpWebhookAdapter(fw)
    fw.services.register("protocol_adapter", _adapter)
    # 基类已提供 call/acall，注册自身为 api_caller 后 ctx.api()/aapi() 即可用
    fw.services.register("api_caller", _adapter)
    _adapter.start()
    ctx.log("HTTP Webhook adapter 已启动 :8901")


def unregister():
    global _adapter
    if _adapter:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_adapter.stop())
        _adapter = None
```

### 3.3 启用

```yaml
# core_plugins.yaml（官方插件开关）
http_webhook: true
```
```yaml
# config.yaml（插件自身配置）
http_webhook:
  host: 127.0.0.1
  port: 8901
  enabled: true
```

> 注意：如果同时开 OneBot 和 Webhook，两者都会注册 `protocol_adapter`，后者会覆盖前者
> （`ServiceRegistry.register` 会警告并覆盖）。多数场景下**只开一个接入端**即可。

### 3.4 测试

```bash
curl -X POST http://127.0.0.1:8901/hook \
     -H "Content-Type: application/json" \
     -d '{"user_id": 10001, "text": "/echo 来自 webhook"}'
```

如果已有 `echo` 插件，它会回"来自 webhook"。业务插件与接入端彻底解耦。

---

## 四、接入端的三种典型形态

| 形态 | 事件源 | 说明 |
| ---- | ------ | ---- |
| **实时推送**（OneBot/Telegram） | 长连接 / WebSocket 收到消息 | 最常用 |
| **Webhook 接收**（HTTP/Web 回调） | 外部 POST | 见上文示例 |
| **主动轮询 / 定时**（cron/MQTT） | 定时器 / 订阅 | 见[最佳实践](../../guide/best-practices.md)场景一 |

统一入口都是 `framework.dispatch_event(event)`。**形态不同，插件零改动。**

---

## 五、反向发消息

- 事件对象上 `event.bot_name` 标记来源接入端/账号；
- `ctx.send_msg / asend_msg`、`ctx.api / aapi` 已封装，不传 `bot` 时自动跟随当前来源
  （来源由 `framework/runtime.py` 的 `current_source_var` 在 handler 执行期间注入）；
- 框架自动回复走 `Framework.reply_text(event, text)` → 接入端 `send_text`；
- 主动发起（定时任务）时用 `get_connected_bots()` 选目标，或显式传 `bot=<来源名>`；
- 没有 IM 时，`call_api` 可被插件转发到任何"通知渠道"（日志、邮件、钉钉机器人……）。

---

## 六、服务注册表（DI）

`framework/protocol.py` 的 `ServiceRegistry` 是核心与插件的解耦点：

```python
fw.services.register("protocol_adapter", adapter)   # 接入端本体
fw.services.register("api_caller", adapter)          # 通用动作调用入口（ctx.api/aapi）
fw.services.register("onebot_api", onebot_wrapper)   # 某协议的专用面向对象封装（可选）
```

- 核心框架不直接 import 接入端代码，只通过 `services.get(name)` 取服务；
- 插件通过 `ctx.api()` / `ctx.onebot` 等调用，**完全兼容旧代码**；
- `ctx.onebot` 优先返回 `services['onebot_api']`；若当前接入端没注册专用封装，
  框架用协议无关的 `ActionProxy` 兜底——把任意属性访问机械转发成一次 `api_caller` 调用，
  因此 `ctx.onebot.<动作名>()` 在非 OneBot 接入端上也不会崩；
- 接入端可以整体被替换，业务插件无需改动。

## 七、官方 OneBot 实现参考

OneBot 协议的全部实现都在 `core_plugins/onebot_adapter/` 内，框架核心不含 OneBot 代码：

| 文件 | 内容 |
| ---- | ---- |
| `main.py` | 连接管理（`BotConnection`/`ApiCaller`）、反向 WS 服务端、`OneBotAdapter`、事件归一化 `normalize_event` |
| `onebot_api.py` | 38 个标准动作的面向对象封装 `OneBotAPI`（扩展动作动态转发），即 `ctx.onebot` |

启动后注册四个服务：

| 服务名 | 内容 |
|--------|------|
| `protocol_adapter` | `OneBotAdapter` 实例 |
| `api_caller` | API 调用器：`.call(action, **params)` / `.acall(action, **params)` |
| `onebot_api` | 面向对象封装 `OneBotAPI`（即 `ctx.onebot`） |
| `ws_server` | 反向 WebSocket 服务端实例 |

它的 `normalize_event(raw_event, bot_name)` 就是 `handle_event` 的一个实现范例，
写新接入端时可对照参考字段映射。

---

## 八、双进程下的进程归属（`__plugin_meta__["process"]`）

启用 `dual_process`（核心进程 / 宿主进程分离）时，框架**按插件 meta 自动分派**加载，
不再维护硬编码名单：

| `process` 值 | 加载位置 | 典型插件 |
| ------------ | -------- | -------- |
| `"core"` | 单进程 + **核心进程**；宿主进程排除 | 协议接入、Web/API（onebot_adapter、http_inject、http_api、webui） |
| 省略 / `"host"` | 单进程 + **宿主进程**；纯核心进程排除 | 会话、调度器等业务侧能力 |

- 单进程（默认 `standard`）部署时**全部加载**，该字段无影响；
- 仍可用 `config.yaml → dual_process.core_plugins: [名字...]` 显式指定核心侧名单覆盖 meta；
- 判断逻辑见 `framework/core.py` 的 `_read_plugin_process_tag`（静态 AST 解析，不执行插件）。

---

> 想在接入端之外插入自己的行为？见 [扩展点（Hook 系统）](./hooks.md)：在启动/关闭、Web 请求、事件分发、命令执行、协议动作、出站文本等几乎每个运行环节挂接逻辑。
