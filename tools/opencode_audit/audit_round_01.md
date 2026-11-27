# 第 1 轮审计：hooks

范围：`framework/hooks.py` 第 1~150 行（摘录 `tools/opencode_audit/excerpt_01_hooks.md`），并用 Grep 核对了同文件的 `_fit_args` / `register` / `trigger_async` / `trigger_sync` 调用实现及相关调用点（`framework/core/dispatch.py`、`framework/api/webapp.py`）。

## 发现

### D-1-1 短路点上允许静默跳过 async handler，HTTP 守卫可 fail-open
- 位置：`framework/hooks.py:147-150`（`_ALLOW_SKIP_ASYNC` 含 `HTTP_BEFORE_REQUEST` / `HTTP_AFTER_REQUEST`），实际跳过逻辑 `framework/hooks.py:399-405`
- 触发：用 `async def` 在 `http.before_request` 注册鉴权/拦截 handler；当内核 loop 未运行（启动前、关闭中、loop 异常终止）时走 `elif point not in _ALLOW_SKIP_ASYNC` 的分支之外——既不记 warning 也不写入 results；即使 loop 正常，`run_coroutine_threadsafe` 返回的 Future 被丢弃、结果固定 `append(None)`（`:400-401`），守卫的返回值永远不参与请求判定。
- 影响：请求被无条件放行，且没有任何告警；这是典型的 Guard fail-open，不自愈（除非 loop 恢复且把守卫改成同步并真正返回结果），运维无法从日志发现守卫从未执行。
- 证据：摘录第 146-150 行原文「这些扩展点在无可用事件循环时，async handler 允许被跳过（避免阻塞）」并列出两个 HTTP 点位；Grep 到 `:402 elif point not in _ALLOW_SKIP_ASYNC:`、`:403-404 logger.warning(...)`（只对不在集合中的点位告警）。实跑验证：注册 async 守卫后 `trigger_sync('http.before_request', ...)` 返回 `[]`（无结果、无 warning 输出）。
- 建议：对带短路语义的点位不允许静默跳过——无 loop 时至少 warning 并把结果置为“拒绝/未完成”而非放行；有 loop 时保存 Future、等待/回调处理结果并 retrieved exception；或在注册阶段禁止 async handler 用于 `*_before_*` 短路点，要求同步实现。

### D-1-2 短路点上 handler 异常被转成 False，与“主动否决”不可区分
- 位置：`framework/hooks.py:337-343`（`trigger_async`）、`framework/hooks.py:411-415`（`trigger_sync`）
- 触发：任一短路点（如 `event.before_dispatch`）的 handler 抛出任意异常（插件 bug、脏数据），catch 块执行 `results.append(False)` 后按短路语义提前 `return results`。
- 影响：调用方把 False 当成明确的一票否决——`framework/core/dispatch.py:285-293` 会把事件丢弃并打“被扩展点拦截”warning；后续 handler 全部不再执行。消息凭空消失且日志归因错误（报错是 ERROR，看到的却是“拦截”）。HTTP 侧更糟：`http.before_request` 收到的 False 不会被 `framework/api/webapp.py:569-571` 当成 Response，请求继续走，守卫崩溃等于放行。
- 证据：Grep 到 `:337 except Exception as e:`、`:338 logger.error(...)`、`:343 results.append(False)` 与 `:411/:412/:415` 的同步对应实现。实跑验证：注册一个抛 `RuntimeError('boom')` 的 handler 加一个后置 handler，`trigger_sync(EVENT_BEFORE_DISPATCH, ...)` 返回 `[False]` 且后置 handler 未运行；同样注册到 `HTTP_BEFORE_REQUEST` 返回 `[False]`、后置未运行。
- 建议：异常不要伪装成 False；用独立的错误哨兵/异常对象或 `raise` 交由调用方决定。安全类 before 点应“出错即拒绝”，通知类点可记录错误后继续；同时保证日志与最终行为一致。

### D-1-3 参数适配把多余位置参数直接截断，keyword-only 参数必然报错并导致事件被丢
- 位置：`framework/hooks.py:40-70`（`_arity_of`）、`framework/hooks.py:167-179`（`_fit_args`，尤其 `:175-176` 截断与 `:177-178` 过滤）
- 触发：按模块文档（摘录第 51-52 行）为 `event.before_dispatch` 写 `def f(event, *, bot_name)` 这类“位置参数 + 关键字专有参数”签名；`framework/core/dispatch.py:285` 固定以 `(event, bot_name)` 两个位置实参触发。`_arity_of` 把 `bot_name` 计入 `allowed` 但 `max_pos` 只有 1，`_fit_args` 先截断到 1 个位置参数，又不把被截掉的值回填到 keyword-only 参数。
- 影响：该 handler 对每条消息都抛 `TypeError`，再叠加 D-1-2 被转成 False，事件被整条丢弃；日志只有“处理器异常/被拦截”，插件作者很难定位是参数适配造成的。属于稳定复现、不可自愈的功能静默失效。
- 证据：摘录第 66-75 行显示 KEYWORD_ONLY 只加入 `allowed`；Grep 到 `:175 if max_pos is not None and len(args) > max_pos:`、`:176 args = args[:max_pos]`。实跑验证：`register(EVENT_BEFORE_DISPATCH, 'kw', lambda event, *, bot_name: ...)` 后 `trigger_async(..., event, 'bot-a')` 输出 `TypeError: kw() missing 1 required keyword-only argument: 'bot_name'`，结果 `[False]`。
- 建议：`_fit_args` 在截断前把多余位置实参按签名顺序映射到 KEYWORD_ONLY 名字（含无默认值者）；或在 `register` 时校验 handler 签名与点位的调用契约并给出明确告警，禁止静默丢弃实参。

### D-1-4 `_SHORT_CIRCUIT` 声明 Response 短路，实现却只认 `is False`
- 位置：`framework/hooks.py:138-144`（摘录第 144 行注释「返回 Flask Response 即短路」），判定处 `framework/hooks.py:335` 与 `framework/hooks.py:409`
- 触发：在 `http.before_request` 注册一个返回 Flask `Response`（或 `jsonify(...)` 等带 `status_code` 的对象）的 handler。
- 影响：请求最终确实被 `framework/api/webapp.py:570-571` 拦下，但 registry 不会提前终止链——后面的 handler（统计、写库、发消息等副作用）全部照常执行完。守卫以为自己“短路”了，实际只是调用方顺带返回了第一个 Response；与模块文档和常量表承诺的语义不符，拒绝请求仍付出完整处理代价并可能产生不该有的副作用。
- 证据：摘录第 139 行 `HTTP_BEFORE_REQUEST: True, # 返回 Flask Response 即短路`；Grep 到 `:335 if short_circuit and value is False:`、`:409` 同样判断。实跑验证：注册先返回带 `status_code` 的对象、后注册第二个 handler，`trigger_sync` 返回 `[<R 403>, 'after']` 且后置 handler 执行（`LATER_RAN ['ran']`）。
- 建议：让短路判定理解各点位的返回值契约（HTTP 点识别 Response/status_code 并立即终止链），或把该点位从 `_SHORT_CIRCUIT` 移除并明确标注“由调用方短路”，避免表与实现互相矛盾。

### D-1-5 混合 serial/parallel 时 `trigger_async` 破坏跨段位优先级保证
- 位置：`framework/hooks.py:98-105`（摘录中 HookPriority「跨段位的先后是有保证的」），分组执行 `framework/hooks.py:322-323`、`:326`、`:357`
- 触发：同一扩展点上既有 `serial=True` 的 LATE handler，又有 `serial=False` 的 GUARD/EARLY handler；`trigger_async` 先按快照顺序跑完整个串行组，再 `gather` 整个并行组，优先级只在组内生效。
- 影响：LATE（收尾/观察）先于 GUARD 执行，看到的是尚未被拦截/改写的事件，且可能已经产生副作用；GUARD 即使随后返回 False，按模块文档第 29 行并行 handler 不参与短路，事件仍已被“收尾”处理。段位约定（EARLY→GUARD→NORMAL→LATE）在真实混合配置下不成立，顺序随 `serial` 声明漂移。
- 证据：摘录第 100-105 行的段位语义说明；Grep 到 `:322 serial_entries = [... if s]`、`:323 parallel_entries = [... if not s]`、`:326 for ... in serial_entries`、`:357 await asyncio.gather(...)`。实跑验证：LATE(serial=True) 与 GUARD(serial=False) 注册后执行顺序为 `['LATE(serial)', 'GUARD(parallel)']`。
- 建议：按优先级切分执行阶段，在每个优先级位置先跑该段位的串行 handler、再并发该段位并行 handler 并设同步屏障；或在 `register` 时禁止 GUARD/EARLY 段位声明 `serial=False`，并在文档中明确剩余的顺序限制。

## ruff 结果

在仓库根目录执行：

```
ruff check --isolated --select F821 .
All checks passed!
```

```
ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .
All checks passed!
```

两道门禁均通过，本轮范围无未定义名/风格问题。

## 本轮结论

发现 5 个问题。
