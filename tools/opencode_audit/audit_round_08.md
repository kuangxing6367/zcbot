# 第 8 轮审计：lifecycle

范围：`framework/loader/lifecycle.py` 第 350~479 行（摘录）及 `load_plugin` 后续异常处理路径（通过定向切片/grep 核对）

## 发现

### D-08-1 加载失败/卸载后插件目录残留在 `sys.path`
- 位置：`framework/loader/lifecycle.py:386-387`；失败返回路径 `457-463,480-485,488-494,580-594,596-618`；`_purge_plugin_modules` `295-348`；仓库内无 `sys.path.remove/pop(plugin_path)`
- 触发：`load_plugin` 在 387 行将 `plugin_path` 插入 `sys.path` 后，若发生 spec 为空、缺少/不可调用 `register`、ImportError 终态失败、或其他异常终态失败，函数直接清理模块/路由并返回 `False`，但不移除 `sys.path` 条目。已卸载插件目录同样未见移除逻辑（`git grep "sys.path.remove" -- framework/loader/*.py` 无结果；仅 `lifecycle.py:387` 插入）。
- 影响：失效或已卸载插件目录持续留在 `sys.path` 前端，可能导致后续导入解析到陈旧/不应加载的插件文件，形成跨插件导入污染或隐蔽的模块遮蔽。问题不会自愈，无专门告警；仅进程重启可清除。
- 证据：
  ```python
  # framework/loader/lifecycle.py:386-387
  if plugin_path not in sys.path:
      sys.path.insert(0, plugin_path)
  ```
  ```python
  # framework/loader/lifecycle.py:580-583 (ImportError 终态，其他终态路径类似)
  # 回滚 sys.modules 中残留的合成包与子模块
  self._purge_plugin_modules(plugin_name, plugin_path)
  # 半初始化可能已登记 API 路由：失败终态一并回收，避免旧闭包驻留
  self._release_plugin_api_routes(plugin_name)
  ...
  return False
  ```
  `_purge_plugin_modules`（295-348）仅按 `__file__` 前缀从 `sys.modules` 清理，不处理 `sys.path`。
- 建议：在 `load_plugin` 所有终态失败返回前，若该 `plugin_path` 是本次加载插入且不应保留，则从 `sys.path` 移除（注意避免误删其他逻辑共享的同路径）；`unload_plugin`/禁用流程也应对称清理。可将“插入 sys.path + 失败回滚”封装为成对操作，确保模块清理与路径清理一致。

### D-08-2 ImportError 重试后的终态诊断使用过期依赖快照
- 位置：`framework/loader/lifecycle.py:396,546-579`
- 触发：首次加载遇到 `ImportError`（attempt==0）时，代码取 `dep_result2 = self.check_dependencies(...)` 并对缺失依赖执行 `pip_install_all` 后 `continue` 重试（558-561），但未更新外层 `dep_result`。若第二次仍为 `ImportError` 终态，错误日志使用 `dep_result.get('missing', [])` 生成 pip 建议和缺失清单（572-579）。
- 影响：终态错误提示可能显示已安装/已变化前的过期缺失依赖，误导排障人员执行错误的 pip 命令。不会自愈（仅影响该次日志），无额外告警。
- 证据：
  ```python
  # framework/loader/lifecycle.py:396
  dep_result = self.check_dependencies(plugin_name)
  ...
  # 546-561, attempt==0
  dep_result2 = self.check_dependencies(plugin_name)
  if dep_result2.get('missing'):
      pip_install_all(plugin_name, dep_result2['missing'])
  continue
  ...
  # 570-579, attempt==1 terminal
  logger.error(
      f"[{plugin_name}] 加载失败，依赖可能未正确安装\n"
      f"  请检查: {sys.executable} -m pip install "
      f"{' '.join(dep_result.get('missing', []))}\n"
      f"  或在 Web UI 插件管理页查看详情"
  )
  if dep_result.get('missing'):
      logger.error(f"[{plugin_name}] 依赖清单里缺失：{', '.join(dep_result['missing'])}")
  ```
- 建议：重试安装后将 `dep_result = dep_result2`（或在终态前重新 `check_dependencies`），并基于最新结果输出诊断；同时考虑记录 `pip_install_all` 的实际失败项，避免“已尝试安装但仍失败”与“未检测到缺失”混淆。

## ruff 结果

```text
$ ruff check --isolated --select F821 .
All checks passed!

$ ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .
All checks passed!
```

## 本轮结论

发现 2 个问题：1 个 `sys.path` 生命周期清理缺失，1 个 ImportError 重试终态诊断使用过期依赖快照。
