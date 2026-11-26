# 底层破坏性测试 · 发现清单

目标不是「功能对不对」，而是找出**一句看似无害的调用就能把宿主搞死/搞残，且无告警、不可自愈**的底层缺陷。原型是 1.28.16 修掉的 `db.close()` 关掉全进程连接池。

## 阅读约定

测试文件里的失败用例统一用 `pytest.mark.xfail(strict=True)` 标记：

- 当前代码下它们是 XFAIL，**不把 CI 钉红**；
- 某天缺陷真被修好，它们会变成 XPASS，**strict 模式下反而让 CI 红**，逼着维护者回来删标记。

跑法：

```bash
python -m pytest tests/adversarial/ -q -rx
```

---

## A1 · 同插件同扩展点注册多个 handler，前一个被静默覆盖

- **严重级别**：高（功能静默丢失，无日志、无异常）
- **触发路径**：`ctx.hook('event.before_dispatch', h1)` + `ctx.hook('event.before_dispatch', h2)`
- **位置**：`framework/ctx/events.py:239`（以及 `:250` 的 unhook），`framework/hooks.py:199` register
- **现象**：注册名固定为 `f"{plugin_name}:{point}"`，不含 handler 标识。同插件同点位第二次注册直接覆盖第一次。实测只触发 `second`，`first` 连日志都没有。
- **影响面**：插件作者的自然写法「一个点位挂两个不同处理逻辑」直接失效；没有报错，排查极难。
- **最小复现**：`tests/adversarial/test_adv_hook_registry.py::test_two_handlers_same_point_same_plugin_both_fire`
- **建议**：注册名加入 handler 标识（如 `qualifiedname` + 序号），或在同一名字下改成多 handler 列表并保留去重（按 handler 身份而非点位）。

## A2 · HookRegistry 没有 owner 概念，可清空任意插件的处理器

- **严重级别**：中（误写插件名即连锁失效，无痕迹）
- **位置**：`framework/hooks.py:232` `clear_plugin(plugin_name)`
- **现象**：任何拿到 `framework.hooks` 的代码都能传任意插件名，清空对方全部扩展点。前缀相同（如 `foo` / `foo_bar`）还会连带误伤。
- **影响面**：同进程插件按设计互相信任，不要求安全隔离；但「传错名字」这种普通失误目前完全静默。
- **最小复现**：`tests/adversarial/test_adv_hook_registry.py::test_plugin_cannot_clear_another_plugins_hooks`
- **建议**：`clear_plugin` 增加调用方身份校验，或至少打一条 INFO 记录「清除了 N 个 handler，来源 XXX」。

## A3 · unregister 只按 name 匹配，无归属校验

- **严重级别**：中
- **位置**：`framework/hooks.py:223` `unregister(point, name)`
- **现象**：与 A2 同源——name 里虽然带了 `plugin:` 前缀，但不校验调用方。
- **最小复现**：`tests/adversarial/test_adv_hook_registry.py::test_unregister_is_scoped_to_owner`
- **建议**：同 A2。

## A4 · `trigger_sync` 完全忽略处理器返回值，短路语义失效

- **严重级别**：高（安全守卫被绕过，静默穿透）
- **位置**：`framework/hooks.py:334-380` `trigger_sync`（代码里 :341 的注释还专门写着「遇到首个 False 提前终止」，实现却没做）
- **现象**：`trigger_async` 会在 `False` 时短路并返回；`trigger_sync` 把每个 handler 的返回值直接丢掉，全部执行完且不返回任何结果。实测 `guard` 返回 `False` 后，`downstream` 仍然被调用。
- **影响面**：Web 请求钩子、线程内触发的扩展点走这条路径。挂在这些点位上的 Guard 插件等于摆设；`http.before_request` 这类「拒绝请求」的语义完全失效。
- **最小复现**：`tests/adversarial/test_adv_oc_hooks.py::test_adv_oc_trigger_sync_ignores_short_circuit`
- **发现来源**：opencode（`opencode/big-pickle`），主仓库实跑确认。
- **建议**：与 `trigger_async` 对齐——逐个执行、首个 `False` 立即返回；若扩展点不支持短路则显式声明。

## A5 · Guard 抛异常被吞后事件静默放行（fail-open）

- **严重级别**：高（守卫插件崩溃 = 没有守卫，且只有一条 ERROR 日志）
- **位置**：`framework/hooks.py:305-330` 的异常捕获；消费点 `framework/core/dispatch.py:94`、`:284`（`if False in (await hooks.trigger_async(...))`）
- **现象**：handler 抛异常时只 `logger.error` 并把结果丢弃（或记为 `None`）。调用方按约定用 `False in results` 判断要不要丢弃事件，于是「守卫崩溃」和「守卫放行」产生完全相同的返回值。实测事件确实继续下发到下游。
- **影响面**：任何以 `before_*` 做拦截的安全/权限/风控插件，一旦自身依赖缺失或写错代码，就会静默失效，而不是拦住事件。
- **最小复现**：`tests/adversarial/test_adv_oc_hooks.py::test_adv_oc_guard_exception_fails_open`
- **发现来源**：opencode（`opencode/big-pickle`），主仓库实跑确认。
- **建议**：短路型扩展点的 handler 异常应转换为否决（`False`）而非 `None`，或向上抛出让调用方 fail-closed；至少要在返回值里带上异常标记。

## B1 · 短命线程的 SQLite 连接在运行期永不回收

- **严重级别**：高（资源泄漏，长时间运行必然撞 fd 上限）
- **位置**：`framework/database/db_conn.py:386` `_close_tracked_conns`，唯一调用点在 `:523`（`shutdown()` 内）
- **现象**：每个线程首次查询都会新建 SQLite 连接并登记进 `_sqlite_conns`。线程结束后，这条连接既不会被自动关掉，运行期 `close()` 也只关「当前线程」那条。实测 12 个短命线程跑完，登记表从 1 涨到 13；再调 `close()` 仍是 12，只有 `shutdown()` 才回到 0。
- **影响面**：插件里用 `threading.Thread` 做一次性 DB 查询是常见写法，每跑一次泄漏一个文件句柄。Windows 上还会让库文件删不掉。全程零告警。
- **最小复现**：`tests/adversarial/test_adv_db_threads.py::test_dead_thread_sqlite_conns_are_reclaimed_at_runtime`
- **建议**：在每次登记新连接时顺带做一次 `dead_only=True` 的清扫；或由后台线程定期清扫。清扫本身很便宜（一次 `threading.enumerate()` + 线性扫描）。

## C1 · 插件可直接拿到 framework 全局引用，停机类接口无保护

- **严重级别**：设计取舍，仅记录
- **位置**：`framework/ctx/base.py:63` `self._framework = framework`
- **现象**：插件可调用 `ctx._framework.stop()`、`hooks.clear_plugin(...)`、`plugin_loader.unload_plugin(...)` 等宿主级接口。
- **影响面**：这些能力本身是插件生态需要的（插件要能管理别的插件），不要求沙箱；但与 A1~A3、B1 叠加后，「误调一行」的破坏半径很大。
- **建议**：至少对 `stop()` / `shutdown()` 加一个「仅框架自身可调用」的令牌校验，避免插件误调导致整进程退出。

## C2 · MySQL 下 `db.shutdown()` 从插件侧调用仍可永久关池

- **严重级别**：高（1.28.16 修复的残留面）
- **位置**：`framework/database/db_conn.py:510` `shutdown`
- **现象**：1.28.16 已经把 `close()` 改成安全语义，但 `shutdown()` 依然会 `self._pool.close()`。SQLite 下实测 shutdown 后查询会自动重连，所以自愈；MySQL 下池被关后不会自动重建。插件只要能拿到 `ctx._db` 就能调用它。
- **影响面**：比原来的 `close()` 隐蔽性低（方法名明确写了 shutdown），但依然没有「只有停机路径能调」的保护。
- **建议**：给 `shutdown()` 加调用来源校验，或让 MySQL 池在后续查询时惰性重建。

## C3 · 用户注册队列丢弃只在第 1、1001、2001… 条告警

- **严重级别**：低
- **位置**：`framework/core/stats_writer.py:75` `if self._dropped % 1000 == 1`
- **现象**：队列满时用户/群自动注册被静默丢弃，前 1000 条没有任何日志。
- **影响面**：突发高峰后，前 999 个新用户/新群不会进 `users` / `groups_info` 表，运维查不到原因。
- **建议**：改成指数退避告警（1、2、4、8…）或按时间间隔告警。

---

## 已验证「不是问题」的项（避免重复挖）

- **事件上下文 contextvar 泄漏**：`framework/core/dispatch.py:573` 在嵌套 `finally` 中正确 `reset`。
- **插件卸载残留**：`framework/loader/lifecycle.py:708` 起依次清理定时任务、事件订阅、原始消息处理器、hook、Web 路由，路径完整。
- **统计批量写库的坏行隔离**：`_execute_many_resilient` 逐行写入并统计 `bad`，不会因一行坏数据丢整批。
