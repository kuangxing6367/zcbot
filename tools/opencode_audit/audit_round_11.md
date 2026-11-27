# 第 11 轮审计：router

范围：`framework/messaging/router.py` 第 1~130 行（见 `tools/opencode_audit/excerpt_11_router.md`）；另按简报允许用 Grep 核对了 `_perm_key`、`_resolve_permissions_sync`、`invalidate_permissions` 及相关缓存调用点，未 Read 其它文件。

## 发现

### D-11-1 缓存 TTL 承诺的“最多一个 TTL 生效”不成立，实际受下层 60s 缓存拖累
- 位置：`framework/messaging/router.py:88-90`（类 docstring），配合 `framework/messaging/router.py:537-543`
- 触发：外部失效入口未接入时（如绕过 Web API 直接改权限表/另一进程写入），权限变更发生；5s 后本缓存条目过期，下一次 `_resolve_permissions_sync()` 重新取值时，仍从下层缓存读旧值。
- 影响：权限变更最长约 65s（下层 60s + 本层 5s）才生效，而不是注释承诺的 5s；无告警，只能靠下层 TTL 自然过期自愈。对封禁/提权类操作属于安全窗口被放大。
- 证据：摘录中的承诺：`88       - TTL 用 ``time.monotonic()``，不受系统时钟回拨影响；默认与路由表刷新` / `89         间隔同量级，即便外部未接失效入口，权限变更最多一个 TTL 生效。`；Grep 核对下层 TTL：`framework/perm/__init__.py:52: _PERM_CACHE_TTL = 60.0`、`framework/messaging/event.py:28: _ROLE_CACHE_TTL = 60.0`；本缓存 miss 后的取值路径：`framework/messaging/router.py:537: role = ev.role`、`framework/messaging/router.py:543: pset = ev._perm_set()`，两者分别命中上述 60s 缓存；`framework/perm/__init__.py:333: def resolve(db, user_id, context=None, role=None, use_cache=True)` 默认吃缓存，router.py 内未出现 `use_cache=False`。
- 建议：在 router 缓存 miss 的重建路径上显式绕过下层缓存（如 `perm.resolve(..., use_cache=False)` 并跳过 `_user_role_cache`），或把三层 TTL 对齐并修正 docstring；同时把“外部写入”场景纳入失效通道（DB 变更通知/短 TTL 兜底）。

### D-11-2 传入具体 user_id 的失效调用会清掉全局共享缓存
- 位置：`framework/messaging/router.py:93-95`（docstring 所述统一失效入口），实现在 `framework/messaging/router.py:577-592`
- 触发：调用 `MessageRouter.invalidate_permissions(user_id)`（例如 `tests/test_perf_regressions.py:173` 的 `router.invalidate_permissions(1001)`），本意只失效单个用户。
- 影响：一个用户改权限会顺带清空 `perm._perm_cache`（全体用户）和 `_group_role_cache`（全体群角色），破坏全局共享状态；所有在线用户下一次消息都要重新解析权限，形成 DB 查询风暴/延迟尖峰。可自愈但无告警，且 `user_id` 参数形同虚设。
- 证据：docstring `93       - 失效由 ``MessageRouter.invalidate_permissions()`` 统一触发（清本缓存` / `94         + 清底层 Event / perm 缓存）…`；Grep 核对实现：`577:     def invalidate_permissions(self, user_id=None):`、`583:         self._perm_cache.invalidate(user_id)`（本缓存确实按用户清）、`586:             _event_mod.invalidate_user_role_cache(user_id)`、`587:             _event_mod.invalidate_group_role_cache()`（无 group_id/user_id 限定）、`592:             _perm_mod.invalidate_all()`（无条件全清）。全文件仅 `126: if user_id is None:`，失效方法内没有按 user_id 分支。
- 建议：按参数粒度分流：`user_id is None` 时保留全清；传入具体用户时改为 `perm.invalidate_user(user_id)`、`event.invalidate_group_role_cache(user_id=user_id)`，仅保留本缓存的按用户失效；异常仍按现有 debug 日志处理即可，但应补一条可观测指标。

## ruff 结果

在仓库根目录执行，第一道门禁（`ruff check --isolated --select F821 .`）：

```
All checks passed!
```

第二道门禁（`ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .`）：

```
All checks passed!
```

## 本轮结论

发现 2 个问题。
