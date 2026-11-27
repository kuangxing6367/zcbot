# 第 4 轮审计：ctx_base

范围：`framework/ctx/base.py` 第 1~130 行（材料 `excerpt_04_ctx_base.md`）。按简报允许，仅用 Grep 核对了 `framework/messaging/protocol.py`、`core_plugins/onebot_adapter/onebot_api.py` 等调用点，未 Read 其它文件。

## 发现

### D-4-1 有来源上下文时适配器查询失败会 fail-open 串线到全局 OneBot/api_caller
- 位置：`framework/ctx/base.py:88-113`
- 触发：多接入端并存；事件带 `source`（如 `tg_bot`），但 `services.adapter_for_source(source)` 抛异常（例如适配器的 `get_connected_bots()` 抛错，同一兜底模式见 `framework/messaging/protocol.py:519-520`）或返回 `None`（`protocol.py:513`、`protocol.py:533`）。这里把异常吞成 `adapter=None` 后继续走到 `services.get('onebot_api')` / `services.get('api_caller')` 并直接返回。
- 影响：本应发往当前来源的动作被发到全局 OneBot 或“最后注册”的 `api_caller`（`protocol.py:346` 为覆盖式注册；Grep 显示 multiple 接入端均注册 `api_caller`）。消息/禁言/删消息发错端；调用方拿到的是“看起来正常”的代理对象，只有 DEBUG 日志、无告警，除非重建来源上下文否则不会自愈。
- 证据：
  ```
  88  except Exception as _exc:
  89      logger.debug('[actions] 捕获异常后按降级路径继续: %s', _exc)
  ...
  106 api = services.get('onebot_api')
  107 if api is not None:
  108     return api
  109 caller = services.get('api_caller')
  ```
  Grep 证实 `adapter_for_source` 对未知/空 source 返回 `None`，`ServiceRegistry.register()` 直接覆盖同名服务。
- 建议：区分“无来源上下文”与“有来源但查不到适配器”：后者应抛 `RuntimeError` 或返回显式失败代理，禁止回落到全局 `onebot_api`/`api_caller`；若确需降级，至少打 WARNING 并在返回值上标注实际使用的 adapter_id。

### D-4-2 `ctx.onebot` 兼容别名并不绑定 OneBot，会随事件来源串到其它协议
- 位置：`framework/ctx/base.py:115-122`（配合 91-105 行）
- 触发：`ctx.onebot` 直接 `return self.actions`；在 Telegram/Discord 等事件上下文里，老插件按 `core_plugins/onebot_adapter/onebot_api.py:19-21` 的写法调用 `ctx.onebot.set_group_ban(...)` / `get_group_member_list(...)`。此时 `actions` 优先读 `current_source_var`（`framework/messaging/router_match.py:367/410` 设置），命中非 OneBot 适配器就返回 `ActionProxy(adapter)`。
- 影响：OneBot 专用动作被动态转发（`ActionProxy.__getattr__`，`protocol.py:316-321`）到其它适配器，通常得到 `{"status": "unsupported"}`（`protocol.py:290`）或 AttributeError；老插件静默失效/误发，无告警。
- 证据：
  ```
  118 兼容别名：等价于 ctx.actions（历史插件便捷面）。
  119 OneBot 11 API 封装优先返回 services['onebot_api']；
  ...
  122 return self.actions
  ```
  文档承诺与实现不一致：119 行说优先 OneBot，实际 122 行把来源路由规则完全继承下来。
- 建议：`onebot` 单独实现为 `services.get('onebot_api')`，缺失时明确抛错，不要复用受 `current_source_var` 影响的 `actions`。

### D-4-3 `_PluginLogger` 先 f-string 再传 `*args`，带百分号的消息触发 logging 格式化异常并丢日志
- 位置：`framework/ctx/base.py:43-59`
- 触发：插件按标准 logging 习惯传参，且消息里含字面 `%`，例如 `ctx.logger.info('进度 100%: %s', 5)`。f-string 先把 `msg` 拼进字符串，再把 `args` 交给 `logger.info`，`LogRecord.getMessage()` 仍会做 `%` 格式化。
- 影响：`logging` 抛 `ValueError: unsupported format character ':'`，该条日志被丢弃，只在 stderr 打印 `--- Logging error ---`；同时 f-string 提前展开，失去延迟格式化，插件热路径有额外开销。
- 证据：实跑纯标准库复现该方法（`log.info(f'[demo] 进度 100%: %s', 5)`）输出：
  ```
  ValueError: unsupported format character ':' (0x3a) at index 14
  Message: '[demo] 进度 100%: %s'
  Arguments: (5,)
  ```
  对应代码：`logger.info(f"[{self._name}] {msg}", *args, **kwargs)`（44 行；47/50/53/56/59 行同构）。
- 建议：改用 `logging.LoggerAdapter(logger, {'plugin': self._name})`，或在适配器内先 `rendered = msg % args`（无 args 时直接用 msg），再以 `logger.info("[%s] %s", self._name, rendered)` 记录，避免把 `*args` 再传给 logger。

### D-4-4 模块级共享线程池无生命周期管理，4 个 worker 可被单个插件占满且拖住退出
- 位置：`framework/ctx/base.py:23-27`、`framework/ctx/base.py:302-316`
- 触发：任何插件调用 `ctx.run_async()` 提交阻塞任务（文档示例是图片渲染）；4 个长任务/死循环即可占满全局池。全仓 Grep 只找到该线程池的定义与 `submit`，没有任何 `shutdown` / `cancel_futures`。
- 影响：所有插件共用这 4 个线程，一个插件的慢任务会饿死其它插件的异步任务；任务异常只留在 Future 里，调用方不取就静默失败；ThreadPoolExecutor 的 atexit join 会让卡住的任务阻塞进程退出，无超时、无告警、无法在运行期回收。
- 证据：
  ```
  24 _async_executor = ThreadPoolExecutor(
  25     max_workers=4,
  26     thread_name_prefix='async_plugin'
  ...
  316 return _async_executor.submit(func, *args, **kwargs)
  ```
  Grep `_async_executor` 全仓仅 2 处命中（定义 24 行、submit 316 行）。
- 建议：把 executor 挂到 framework 生命周期，在 shutdown 时 `shutdown(wait=False, cancel_futures=True)`；按插件隔离或限制队列/超时；在提交处包一层异常记录（或强制调用方注册 done callback）。

### D-4-5 `_PluginLogger` 自称“标准 logger 接口”但缺少常用方法
- 位置：`framework/ctx/base.py:38-59`
- 触发：插件按标准 logger 调用 `ctx.logger.critical(...)`、`ctx.logger.log(level, msg)`、`ctx.logger.isEnabledFor(...)` 或 `ctx.logger.setLevel(...)`。
- 影响：直接 `AttributeError` 中断插件逻辑。Grep 当前仓库未发现 `ctx.logger.*` 调用点，因此影响面主要是外部/历史插件与文档承诺不一致。
- 证据：类内只定义 `info/warning/error/debug/warn/exception` 六个方法（43-59 行），而 39 行注释写“标准 logger 接口”。
- 建议：继承或包装 `logging.LoggerAdapter`（或补齐 `log/critical/isEnabledFor/setLevel/...`），保持真正的标准接口。

## ruff 结果

```
$ ruff check --isolated --select F821 .
All checks passed!

$ ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .
All checks passed!
```

## 本轮结论

发现 5 个问题：D-4-1 来源适配器查询失败 fail-open 串线、D-4-2 `ctx.onebot` 别名串协议、D-4-3 日志 `%` 格式化丢日志、D-4-4 全局线程池无生命周期/饥饿、D-4-5 日志适配器接口不完整；ruff 两道门禁均通过。
