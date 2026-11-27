# 第 12 轮审计：protocol

范围：framework/messaging/protocol.py 第 1~130 行（线索文件 `tools/opencode_audit/excerpt_12_protocol.md`）；仅用 Grep 核对了 `contract.py` / `segments.py` 的相关调用点，未读取其它文件全文。

## 发现

### D-12-1 `ProtocolAdapter.supports()` 吞掉全部异常并静默返回 False，能力查询故障被伪装成「不支持」
- 位置：`framework/messaging/protocol.py:114-120`
- 触发：插件/第三方适配器按文档调用 `adapter.supports('voice', 'in')` 或 `adapter.supports('image')` 来决定是否发送附件；只要覆写的 `capabilities()` 抛异常（例如动态能力探测失败、返回了会抛错的 Iterable），`try` 块整体失败，进入 `except Exception`。`as_capabilities()` 自身对 None/dict/list/Capabilities 都有兜底（`contract.py:209-220`），`Capabilities.supports()` 也不主动抛异常（`contract.py:180-185`），因此能走到这里的异常基本都是适配器自身的真实故障，不是正常的「能力不支持」。
- 影响：能力查询被降级为 `False`，包括 `'text'` 也会被判为不支持；据此判断的插件会静默去掉图片/语音/按钮，甚至完全不发送消息，功能悄然缺失。只有一条 DEBUG 日志（默认日志级别下不可见），没有 WARN/告警；偶发错误下次调用可自愈，持续性故障会一直沉默，直到有人去看契约自检结果。与同一模块的契约自检行为不一致：`validate_adapter()` 对 `capabilities()` 抛异常会显式记入问题清单（`contract.py:394-399`：`issues.append(f'{name}.capabilities() 抛异常: {e}')`）。另外 Grep 显示仓库内产品代码没有直接调用 `adapter.supports()`（仅 `protocol.py:117` 自身和 `tests/test_message_contract.py:421`），受影响的主要是文档所面向的插件/第三方接入端，但那正是该 API 的目标用户。
- 证据：摘录中关键代码——
  ```python
  116         try:
  117             return as_capabilities(self.capabilities()).supports(cap, direction)
  118         except Exception as _exc:
  119             logger.debug('[supports] 捕获异常后按降级路径继续: %s', _exc)
  120             return False
  ```
  Grep 复核：`contract.py:394-399` 将 `capabilities()` 抛异常列为契约问题；`contract.py:180-185,209-220` 表明正常输入下 `as_capabilities(...).supports(...)` 不会抛异常；`framework/messaging/protocol.py:114-120` 是本轮唯一吞异常点。
- 建议：不要把适配器故障伪装成能力不支持。可缩小捕获范围并改为记录带 `adapter_id` 的 WARNING（或按需上报），让调用方区分「明确不支持」与「查询失败」；或让异常向上冒泡，由插件决定是否降级。至少把日志级别从 DEBUG 提到 WARNING，并与 `validate_adapter()` 的问题上报口径保持一致。只给建议，未改动代码。

## ruff 结果

在仓库根目录 `C:\zc-repos\zcbot` 实跑两道门禁，实际输出：

```
> ruff check --isolated --select F821 .
All checks passed!

> ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .
All checks passed!
```

## 本轮结论

发现 1 个问题（`supports()` 异常吞噬导致能力查询静默降级）。
