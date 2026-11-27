# 第 6 轮审计：dispatch

范围：`framework/core/dispatch.py:490-619`（摘录），并以 Grep/小范围行号读取核对 `_dispatch_raw_message_handlers` 后续实现（608-670 行片段）及调用点；未读取任何超过 150 行的完整文件。

## 发现

### D-6-1 `sender` 为显式 `None` 时消息分发崩溃，单条事件被丢弃
- 位置：`framework/core/dispatch.py:532,548,553`
- 触发：上游适配器/协议投递的消息事件中 `sender` 键存在但值为 `None`（或非 dict）。`event.get('sender', {})` 仅在键缺失时回退空 dict，键存在且为 `None` 时返回 `None`。
- 影响：群消息在 `sender.get('group_name')` 处抛 `AttributeError`；私聊消息跳过该分支后在 `self.stats_writer.register_user(...)` 内部 `dict(sender)` 抛 `TypeError`（`framework/core/stats_writer.py:74`）。异常从 `_process_event` 抛出：worker 模式下被 `framework/core/dispatch.py:391-392` 捕获并记录 `事件处理异常`，worker 存活但该条消息不会路由、不会响应；同步回退模式（worker 未启动/停机收尾，`dispatch.py:296-298`）下异常直接向上冒泡。无针对该畸形输入的专门告警，表现为消息“丢失”。
- 证据：
  ```python
  # framework/core/dispatch.py:532,548,553
  sender = event.get('sender', {})
  ...
  if message_type == 'group' and not sender.get('group_name'):
      gname = await self._resolve_group_name(bot_name, group_id, adapter_key)
      if gname:
          sender = {**sender, 'group_name': gname}
          event['sender'] = sender
  self.stats_writer.register_user(
      user_id, sender, message_type, group_id, adapter_key=adapter_key)
  ```
  ```python
  # framework/core/stats_writer.py:73-74
  self._reg_queue.put_nowait(
      (user_id, dict(sender), message_type, group_id, adapter_key))
  ```
  Grep 验证：仓库中 `event.get('sender', {})` 仅此一处（`framework/core/dispatch.py:532`），无 `event.get('sender') or {}` 防护模式。
- 建议：将 sender 规范化为 dict，例如 `sender = event.get('sender') or {}`，并在后续使用前加 `if not isinstance(sender, dict): sender = {}` 防御非映射类型；`stats_writer.register_user` 也可对非 dict 入参做同样收敛，避免其他调用点触发同类批量失败。

### D-6-2 原始消息工作流解析异常时 fail-open，绕过插件过滤
- 位置：`framework/core/dispatch.py:624-633`（按文件实际行号；摘录对应 `_dispatch_raw_message_handlers` 内工作流解析块）
- 触发：`workflow_store` 存在但 `resolve()` 抛出任意异常（配置损坏、存储异常、resolve 实现 bug 等）。
- 影响：代码记录 warning 后将 `_wf_plan` 保持为 `None`，随后 `if _wf_plan is not None and not _wf_plan.allows(...)` 不生效，所有原始消息处理器被“全部放行”。若工作流用于限制某些插件对特定用户/群的可见性或权限边界，该异常会造成策略绕过。系统有日志，但行为是静默放宽策略，属于 fail-open。
- 证据：
  ```python
  # framework/core/dispatch.py:624-633
  _wf_store = getattr(self, 'workflow_store', None)
  _wf_plan = None
  if _wf_store is not None:
      try:
          from framework.workflow import resolve as _wf_resolve
          _wf_plan = _wf_resolve(
              _wf_store, data.get('user_id'), data.get('group_id'))
      except Exception as _wf_exc:
          logger.warning('原始消息工作流解析失败（按全部放行继续）: %s', _wf_exc)
  ...
  if _wf_plan is not None and not _wf_plan.allows(item['plugin_name']):
      continue
  ```
- 建议：明确 fail-open/fail-closed 策略。若工作流是安全/权限边界，解析失败应 fail-closed（跳过受工作流管控的 raw handler，或至少跳过非默认允许项）并发出高优先级告警；若确认为可用性优先，也应将该策略写入配置/文档，并区分“无工作流配置”与“工作流解析错误”两种 None 状态，避免异常被当作正常无配置处理。

## ruff 结果

```
ruff check --isolated --select F821 .
All checks passed!
```

```
ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .
All checks passed!
```

## 本轮结论

发现 2 个问题：1 个 sender 显式 None 导致单事件分发崩溃，1 个原始消息工作流解析异常 fail-open 绕过过滤。
