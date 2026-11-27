# 第 9 轮审计：runtime

范围：`framework/core/runtime.py` 第 1~130 行（`FrameworkRuntimeMixin._load_core_plugins`，线索见 `excerpt_09_runtime.md`）

## 发现

### D-9-1 核心插件加载失败后 sys.modules 残留半初始化模块，全程无回滚
- 位置：`framework/core/runtime.py:103-108`（写入点在 107；失败处理在 190-217）
- 触发：`main.py` 顶层 import 或模块级代码抛异常（缺依赖、初始化报错等）。107 行先把 module 放进全局 `sys.modules`，108 行才 `exec_module`；异常被 190 行 `except` 捕获后只做 `classify_load_error` / 日志 / 自愈重试（`classify_load_error` 见 191、重试见 522），没有从 `sys.modules` 撤掉该名字。
- 影响：全局 `sys.modules['core_plugin_<name>']` 长期指向只执行到一半的 module。其他插件按公开契约用 `sys.modules.get("core_plugin_html_assembler")` 取主模块（`core_plugins/html_assembler/README.md:59,98,126`），会静默拿到残缺对象；`importlib.import_module("core_plugin_<name>")` 同样直接命中而不再重新执行。自愈重试成功前（若不自愈则直到进程结束）跨插件调用会得到 AttributeError/半初始化状态，只有一条 error 日志、无告警；module 对象及其全局状态还被无谓持有。
- 证据：
  ```python
  103  spec = importlib.util.spec_from_file_location(
  104      f"core_plugin_{name}", main_file,
  105      submodule_search_locations=[plugin_dir])
  106  module = importlib.util.module_from_spec(spec)
  107  sys.modules[f"core_plugin_{name}"] = module
  108  spec.loader.exec_module(module)
  ```
  Grep 整个 `framework/core/runtime.py` 只有 107 这一处 `sys.modules`，`except`（190-217）内无 `pop`/`del`。对照：`framework/terminal/cmd_plugin.py:168` 重建前先 `sys.modules.pop(f"core_plugin_{name}", None)`；`framework/loader/lifecycle.py:336` 卸载时会 pop 插件模块。
- 建议：在 `exec_module` / `register` 的失败分支回滚 `sys.modules`（含该插件合成包的子模块名），或复用 `loader/lifecycle.py` 的清理逻辑；重建前先 pop 旧项，避免自愈重试与配置热切换（`framework/api/config.py:618-622`）之间暴露半初始化模块。只写建议，未改代码。

### D-9-2 `_register_journal` 只在加载后清空一次，运行期无界增长
- 位置：`framework/core/runtime.py:127-142`（写入方 `framework/ctx/events.py:14-19`、`framework/ctx/db.py:10-15`）
- 触发：131 行快照、142 行清空只在插件加载时执行一次。此后任何运行期 ctx 调用都会继续 append：`db_execute` / `db_execute_many` / `db_insert` / `create_table`（`db.py:31,36,41,50`），`command` / `register_api` / `task` / `on` / `on_raw_message` / `hook`（`events.py:73,96,156,163,184,257`）。Grep `_register_journal` 全仓库只有 `ctx/base.py:74` 初始化、`runtime.py:142` 与 `loader/base.py:346` 两处加载期清空，没有运行期清空。
- 影响：每个已加载插件的 ctx 持有随消息量/DB 操作数线性增长的列表（每项含最长 60 字符的 SQL 片段），长跑进程内存无界上涨、无阈值无告警；127-128 行注释所说"读完即清空，避免运行时调用混入"在加载之后并不成立。另外，清空放在 130-141 的 blackbox 记录之后且受 `if _journal:` 保护：若记录过程抛错（例如 `meta` 非 dict 时 138 行 `meta.get` 抛 AttributeError），142 行不执行，异常仅在 144 行 debug 级记录，加载期条目会永久混入并与运行期条目一起增长——异常吞噬导致状态不一致。
- 证据：
  ```python
  131  _journal = list(getattr(ctx, '_register_journal', []) or [])
  132  if _journal:
  ...
  142      ctx._register_journal = []
  144  except Exception as _exc:
  ```
  `framework/ctx/db.py:31`: `self._journal('db_execute', (sql or '')[:60])`，且 `events.py:14-19` / `db.py:10-15` 的 `_journal()` 是无条件 append。
- 建议：快照后立即清空（如移到 `list(...)` 之后或放进 `finally`），并在 ctx 层给 `_journal` 加"仅加载期记录"开关与条数上限，超限丢弃并告警；运行期不再写入该流水。只写建议，未改代码。

## ruff 结果

在仓库根目录执行，两道门禁实际输出：

```
$ ruff check --isolated --select F821 .
All checks passed!
$ ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .
All checks passed!
```

## 本轮结论

发现 2 个问题（sys.modules 无回滚、注册流水无界增长）；另检查了 `enabled` 的多种配置形态解析、`only` 幂等守卫、`scan_mode` 的 block/warn 分支与 `dual_process` 角色分派，未发现可确认的缺陷。
