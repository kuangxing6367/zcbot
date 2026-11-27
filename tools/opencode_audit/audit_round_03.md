# 第 3 轮审计：db_api

范围：framework/database/db.py 第 1~130 行（线索来自 `tools/opencode_audit/excerpt_03_db_api.md`；另用 Grep 核对了调用点，未 Read 任何超过 150 行的文件）

## 发现

### D-3-1 `_is_readonly_error` 用错误文本子串判定，把普通 SQL 失败误升级为全局“只读降级”状态
- 位置：`framework/database/db.py:71-81`；调用点 `framework/database/db.py:317-328`（`_note_write_failure`）
- 触发：只需写异常的消息文本里出现 `readonly` / `read-only`，哪怕不是只读挂载。例如 MySQL `Incorrect datetime value: 'readonly' for column created_at`、`Unknown column 'readonly' in 'field list'`；经 `/api/db_gateway` 执行含这类值的写 SQL 即可到达（`framework/api/db_gateway.py:148` → `db.execute` → `_write` → `_note_write_failure`）。SQLite 侧的 `no such column: readonly` 同理。
- 影响：`self._persist_degraded` 被置为 True，并打出“数据库进入只读状态（...）：本次运行的所有写入都不会落盘”的 **error**，把普通 SQL/数据错误伪装成存储层故障。该标记是实例级共享状态，且告警被有意压成“只打一次”（`db.py:318-321`），之后再发生真实只读也不会补报；只有下一次任意写成功才由 `_note_write_ok`（`db.py:331-334`）清除，若失败SQL反复执行则一直保持误报状态。补充核对：全仓 Grep `persist_degraded` 只命中 db.py 自身与测试，未接入 `/readyz`（`db_degraded` 是另一个字段），因此当前后果是错误告警+状态污染，不是就绪误报。
- 证据：
  ```
  80:    s = str(err).lower()
  81:    return 'readonly' in s or 'read-only' in s
  323:        if not _is_readonly_error(err) or self._persist_degraded:
  325:        self._persist_degraded = True
  ```
  实跑验证（原样复刻 80-81 行逻辑）：
  `[(True, 'attempt to write a readonly database'), (True, 'Incorrect datetime value: "readonly" for column created_at'), (True, 'Unknown column "readonly" in field list'), (False, 'database is locked'), (False, 'UNIQUE constraint failed: t.k')]`
- 建议：优先按可靠信号判定（SQLite 取 `err.sqlite_errorname == 'SQLITE_READONLY'`；MySQL 按 SQLSTATE/错误码，如 1290/1036；`recovery.NON_CORRUPT_ERRNOS` 已有同类做法），文本兜底要匹配完整固定短语（如 `attempt to write a readonly database` / `--read-only option`），不要对用户回显值做子串匹配；同时避免把偶然错误永久写进降级标记。

### D-3-2 `_emergency_recover` 退出失败时吞掉退出异常并返回 True，与自身注释的 fail-open 承诺相反
- 位置：`framework/database/db.py:116-127`
- 触发：`recovery.perform_exit(report)` 抛异常（崩溃日志链路失败、退出被拦截/测试替身）或直接正常返回。
- 影响：函数只记一条 error 就 `return True`，调用方拿到的是“已按恢复流程处理”的信号；注释明确写着“不能吞掉原异常，调用方仍要看到最初的损坏”，实现却没有重新抛出原异常或退出异常。当前 4 个调用点在调用后都跟了裸 `raise`（`db.py:237/261/302/743`），所以**目前**损坏异常仍会外抛、进程仍会结束；但不会走 `perform_exit` 的 crash log 计数/安全模式路径（告警链断裂），且这个返回值契约会诱导后续任何按 `if _emergency_recover(...): return ...` 写的调用点静默吞掉损坏异常，继续带病运行。属于已埋好的 fail-open 陷阱，不是当前调用点上的直接数据损失。
- 证据：
  ```
  119:        # 或退出被拦截，不能吞掉原异常，调用方仍要看到最初的损坏。
  121:    try:
  122:        recovery.perform_exit(report)
  123:    except Exception as exit_exc:
  126:        logger.error('退出动作失败（%s: %s）', type(exit_exc).__name__, exit_exc)
  127:    return True
  ```
  Grep 复核：`^\s*raise` 在 db.py 命中 237/261/302/743 四行，均为上述调用点之后的裸 `raise`。
- 建议：退出失败/退出被拦截时不要返回 True；应 `raise ... from exit_exc` 把退出失败和原始损坏一起抛出，或至少返回 False 并显式区分 `recovery_done` 与 `exit_done`；不要依赖“每个调用方都会自己 bare raise”这一隐含约定。

## ruff 结果

在仓库根目录执行，两道门禁原样输出：

```
$ ruff check --isolated --select F821 .
All checks passed!

$ ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .
All checks passed!
```

## 本轮结论

发现 2 个问题（1 个当前可触发的全局状态误报，1 个已埋下的 fail-open 返回值陷阱）。
