# 第 2 轮审计：db_conn

范围：`framework/database/db_conn.py` 第 1~130 行（线索：`excerpt_02_db_conn.md`）；另用 Grep 核对调用/初始化点 `framework/database/db.py:138-141,674,729,736`、`framework/database/storage.py:31`、`framework/console_server.py:523`、`framework/bootstrap/console_attach.py:41`。未 Read 任何摘录以外的文件。

## 发现

### D-2-1 `database.path` 未做类型校验，显式 null 会让 `os.path.isabs` 裸崩
- 位置：`framework/database/db_conn.py:54,59`（`.get(key, default)` 取配置后立即交给 `os.path.isabs`）
- 触发：config.yaml 中显式写 `database: {path: null}`（或把 `path` 写成 list/dict/数字等非路径类型）时，`self.config.get('path', 'data/zcbot.db')` 返回的是那个非法值而不是默认值。`dict.get` 的默认值只在**键缺失**时生效。Grep 核对：`db.py:674` / `storage.py:31` 用 `cfg.setdefault('path', 'data/zcbot.db')`，同样兜不住显式 null；而仓库其它读同一配置的地方（`console_server.py:523`、`console_attach.py:41`）用的是 `...get('path') or 'data/zcbot.db'`，会把 null 当默认处理——口径不一致。
- 影响：启动阶段 `os.path.isabs(None)` 直接抛 `TypeError: expected str, bytes or os.PathLike object, not NoneType`，初始化中断；异常栈指向 `db_conn.py:59` 而非配置文件，用户拿不到「database.path 写错了」这样的可操作提示，也没有 error 级日志。不会破坏全局共享状态，修正配置即可恢复（进程重启），但属于类型误用导致的启动失败与错误定位缺失。
- 证据：摘录中关键代码——
  ```python
     54         db_path = self.config.get('path', 'data/zcbot.db')
     59         if not os.path.isabs(db_path):
     61                 from framework.config import get_home
     62                 db_path = os.path.normpath(os.path.join(get_home(), db_path))
  ```
  同文件 `:102-120` 已为 MySQL 参数专门写了 `_normalize_mysql_params()`，把 dict/list 等错误变成「请检查 config.yaml 的 database 段」的 `ValueError`；SQLite 的 `path` 缺少同等保护。另注意 `:61-68` 的 `try` 把 import、`get_home()` 调用、`os.path.join`/`normpath` 全包在里面，非导入类异常只会留一条 debug 日志，随后在 `:66-68` 的降级分支里用同一个非法值重试并再次抛出，进一步掩盖根因。
- 建议：在 `_init_sqlite` 开头显式校验 `db_path` 为非空 `str`/`os.PathLike`，否则 `raise ValueError(f"database.path 应为非空路径字符串，实际是 {type(...).__name__}: {db_path!r}")`，与 MySQL guard 口径一致；同时把 `try` 收窄到只包 `from framework.config import get_home`，让 join/normpath 的错误按真实原因上报，而不是被记成「配置模块不可导入」。

## ruff 结果

在仓库根目录执行，两道门禁实际输出：

```
$ ruff check --isolated --select F821 .
All checks passed!
```

```
$ ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .
All checks passed!
```

## 本轮结论

发现 1 个问题（`database.path` 缺类型校验，`null` 导致裸崩且错误定位差）。
