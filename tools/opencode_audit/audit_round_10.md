# 第 10 轮审计：eventbuf

范围：`framework/core/event_buffer.py` 第 1~130 行（材料 `excerpt_10_eventbuf.md`）；另按简报允许用 Grep 核对了同文件 `pop_batch` / `write_batch` 的调用点（未 Read 任何其它文件）。

## 发现

### D-10-1 `_compact()` 压缩失败后文件句柄保持关闭，L2 永久静默失效
- 位置：`framework/core/event_buffer.py:105-110`（失效后的守卫在 `:117`、`:119`、`:123-124`）
- 触发：`_compact()` 先 `self._fh.close()`，再 `os.replace(tmp, self._path)` 并重新 `open()`。只要 `os.replace` 或重开失败（Windows 上目标文件被其它句柄占用、目录权限变化、tmp 被外部移走、磁盘异常等），`except` 只打一条 warning 就返回，既不重开句柄也不把 `_fh` 置回 `None`。此后 `write()` / `pop_batch()` 的唯一守卫是 `if self._fh is None`，而 `_fh` 是一个**已关闭的文件对象**，守卫失效。
- 影响：所有后续 L2 写入抛 `ValueError: I/O operation on closed file.`，被 `write()` 的 `except` 吞掉并返回 `False`；读侧同样在 `seek()` 处失败并被 `pop_batch` 的 `except` 吞掉返回 `[]`。进程运行期间 L2 不可自愈——没有任何告警或状态位暴露这个致命状态（`stats()` 只会看到写入失败后的 L3/L4 接管），表现为"日志里只有一条无害的压缩失败 warning，之后 L2 无声死亡"。若 `os.replace` 已成功但重开失败，磁盘文件已压缩而内存 `_read_offset` 未复位，还存在偏移错位的二次风险。
- 证据：摘录关键行
  ```python
  105             self._fh.close()
  106             os.replace(tmp, self._path)
  107             self._fh = open(self._path, 'a+', encoding='utf-8', newline='')
  108             self._read_offset = 0
  109         except Exception as e:
  110             logger.warning(f"L2 文件缓冲压缩失败（忽略，继续顺序读）: {e}")
  ```
  写侧守卫 `:117` `if self._fh is None:` 只判 `None`、不判 `closed`。实跑（纯内存模拟该状态迁移，未读写产品文件）结果：
  ```
  compact except -> OSError os.replace failed
  write() guard fh is None: False | fh.closed: True
  write() swallows -> ValueError I/O operation on closed file.
  ```
- 建议：把 `close()` 推迟到 `os.replace` 成功之后（先复制 tmp，替换时短暂持锁）；在 `except` 中重试以 `'a+'` 重开原路径，或显式 `self._fh = None` 让上层走既有的"L2 不可用"回落路径；守卫改为 `if self._fh is None or self._fh.closed`，并把该状态计入 `stats()` / 告警，禁止只留 debug/warning 后继续服务。

### D-10-2 `_file_size()` 异常即返回 0，被消费端当成"已读完"而 `truncate(0)` 清空，静默丢事件
- 位置：`framework/core/event_buffer.py:85-91`（消费端 Grep 核对：`:179-183`）
- 触发：`_file_size()` 在 `self._fh.flush()` 或 `os.path.getsize(self._path)` 抛异常时（文件被外部移走/轮转、目录权限异常、句柄已关闭、磁盘瞬时错误）返回 `0`。`pop_batch()` 随即判断 `if self._read_offset >= self._file_size()`；由于 `_read_offset` 最小就是 `0`，`>= 0` 恒成立（重启后遗留事件尚未消费时 `_read_offset == 0` 尤其典型，此时 `_rows = _count_lines() > 0` 明明有待消费事件）。
- 影响：直接执行 `self._fh.truncate(0)` + `seek(0)` + `_read_offset = 0`，把尚未消费的 L2 积压全部抹掉，`_rows` 之后才按已取条目递减，计数与实际内容脱节。整个过程只有一条 `logger.debug`，无告警、不可自愈，事件永久丢失且 `stats()` 不体现丢弃。属于典型的" innocuous 调用 fail-open"：一次失败的 `getsize` 就破坏了共享的持久化缓冲状态。
- 证据：摘录关键行
  ```python
  87             self._fh.flush()
  88             return os.path.getsize(self._path)
  89         except Exception as _exc:
  90             logger.debug('事件缓冲文件大小获取失败，按 0 处理: %s', _exc)
  91             return 0
  ```
  消费端（Grep 实查）：
  ```
  Line 179:                 if self._read_offset >= self._file_size():
  Line 181:                     self._fh.truncate(0)
  Line 182:                     self._fh.seek(0)
  Line 183:                     self._read_offset = 0
  ```
- 建议：`_file_size()` 失败时不要返回会改变控制流的 `0`，应返回 `None`/抛给调用方并跳过本轮 truncate 判定（fail-close，保留积压）；把 `logger.debug` 提升为 `warning` 并计入失败计数；truncate 前增加 `_rows == 0`（或确认无未读行）的双重门禁，避免"统计失败"被解释成"消费完成"。

## ruff 结果

仓库根目录实跑两道门禁，输出原样：

```
$ ruff check --isolated --select F821 .
All checks passed!

$ ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .
All checks passed!
```

## 本轮结论

发现 2 个问题：压缩失败导致 L2 句柄永久关闭、文件大小查询失败触发清空式丢事件；ruff 两道门禁均通过。
