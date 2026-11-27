# 第 7 轮审计：stats

范围：`framework/core/stats_writer.py` 第 1~130 行（线索文件：`tools/opencode_audit/excerpt_07_stats.md`）；仅用 Grep 核对了 `_flush`/`stop`、`namespace_identity`、`register_user` 调用点等少量上下文。

## 发现

### D-7-1 取消后台任务时，已出队的整批统计数据被静默丢弃
- 位置：`framework/core/stats_writer.py:96-106`（配合 `framework/core/stats_writer.py:295-304`、`framework/core/base.py:876`）
- 触发：`_run()` 执行 `fns = self._drain_to_fns()` 之后、仍在 `await asyncio.gather(...)` 等待 DB 线程时发生任务取消。正常停机路径即可触发：shutdown 调用 `await self.stats_writer.stop()`，`stop()` 直接 `self._task.cancel()`。
- 影响：`_drain_to_fns()` 已把 `_cmd_hits`/`_kw_hits` 换成空 dict，并把注册项从 `_reg_queue` 取出；取消会连带取消 gather 中尚未完成的写库 future。随后 `stop()` 的 `_flush()` 只能处理"仍在内存中"的数据，无法恢复已交给 `fns` 的那一批：最多一个 flush 周期的命令/关键词命中与用户/群注册永久丢失。若部分闭包已完成，还会留下"部分写入"的不一致状态。无专门告警，不可自愈（源事件已被消费）。
- 证据：摘录中的关键代码：
  ```python
  96                 fns = self._drain_to_fns()
  97                 if not fns:
  98                     continue
  99                 results = await asyncio.gather(
 100                     *[self._run_in_db_thread(fn) for fn in fns],
 101                     return_exceptions=True)
 105             except asyncio.CancelledError:
 106                 raise
  ```
  停机的 `stop()` 只在取消后补 flush 内存残留：`298 self._task.cancel()` / `304 await asyncio.to_thread(self._flush)`。
- 建议：在 `except asyncio.CancelledError` 分支中对已生成但未完成的 `fns` 做兜底（例如用 `asyncio.shield` 保护在途批次，或把未完成的 hits 合并回内存 dict、items 回填队列后再 re-raise）；`stop()` 也可改为先触发一次正常 flush、等其结束再 cancel，避免"排空即丢失"。

### D-7-2 写库异常只记日志、不回滚数据，整批统计静默丢失
- 位置：`framework/core/stats_writer.py:99-109`
- 触发：DB 线程池中任一闭包抛异常（如 SQLite `database is locked`、MySQL 连接断开、单条 SQL 失败），`gather(return_exceptions=True)` 收集异常。
- 影响：这些 hits/items 已在 `_drain_to_fns()` 阶段从 `_cmd_hits`/`_kw_hits`/`_reg_queue` 移除，失败后既不回填也不重试；进程继续正常运行、后续周期照常 flush，丢失的数据没有持久标记或补偿。只有一行 `logger.error`，没有失败计数/告警，事后无法察觉命中计数与用户/群注册出现空洞，不可自愈。
- 证据：摘录中的关键代码：
  ```python
  96                 fns = self._drain_to_fns()
  99                 results = await asyncio.gather(..., return_exceptions=True)
 102                 for r in results:
 103                     if isinstance(r, Exception):
 104                         logger.error(f"统计批量写库异常: {r}")
 107             except Exception as e:
 108                 logger.error(f"统计批量写库异常: {e}")
  ```
- 建议：按失败项把 `hits`/`kwhits` 合并回当前聚合 dict、把注册项重新入队（或写死信/落盘重试），并增加累计失败计数与告警；不要让"从内存移除"等同于"写入成功"。

### D-7-3 adapter_key 未做入队前收敛，一条坏身份键可带崩整批注册
- 位置：`framework/core/stats_writer.py:48-49,73-74`
- 触发：`multi_platform.namespace` 开启时，`register_user` 只校验了 `user_id`/`group_id`（第 58-71 行），`adapter_key` 原样入队。若事件字段 `adapter` 被适配器/插件写成不可哈希对象（dict/list），或插件直接调用该公开方法时把元数据 dict 当作 `adapter_key` 传入，即会走到 DB 线程的身份命名空间逻辑。
- 影响：`_aggregate_registrations` 用该值调用 `_namespace_id`（`stats_writer.py:143-154`）→ `namespace_identity` → `adapter_registry.uuid_for()`（`adapter_registry.py:83-86` 中 `self._cache.get(adapter_key)`），对 dict/list 抛 `TypeError: unhashable type`。异常发生在聚合阶段，使同一批中的所有正常用户/群注册一起失败（外层 `gather` 只记 ERROR，见 D-7-2），无专门告警、不可自愈。这与第 55-63 行注释声明的"坏身份键必须在入队前挡掉，不能带进批量写库"不一致：只挡了两个身份键，漏了第三个。
- 证据：摘录中的入队代码：
  ```python
  73             self._reg_queue.put_nowait(
  74                 (user_id, dict(sender), message_type, group_id, adapter_key))
  ```
  Grep 核对：`stats_writer.py:195 users: nuid = self._namespace_id(user_id, adapter_key)`、`:199 ngid = self._namespace_id(group_id, adapter_key)`；`adapter_registry.py:85 return self._cache.get(adapter_key) or ''`。
- 建议：入队前把 `adapter_key` 与 `user_id`/`group_id` 同等对待：仅接受 `str`（非 str 归一为 `None` 或 `str(...)`）并限制长度；或在 `_namespace_id` 内捕获异常，只跳过坏键对应的行而不是让整批失败。

## ruff 结果

在仓库根目录执行，两道门禁实际输出：

```
$ ruff check --isolated --select F821 .
All checks passed!

$ ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .
All checks passed!
```

## 本轮结论

发现 3 个问题（取消在途批次丢数据、写库失败无补偿、adapter_key 未收敛可带崩整批）。
