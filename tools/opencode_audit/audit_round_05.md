# 第 5 轮审计：ctx_events

范围：tools/opencode_audit/excerpt_05_ctx_events.md（即 framework/ctx/events.py 第 1~130 行：PluginEventsMixin 的 `_journal` / `command` / `register_api`，以及 `call_async` 的文档头；`call_async` 函数体不在摘录内，未纳入结论）

## 发现

### D-05-1 `command()` 对 `handler.__name__` 的无条件访问会让可调用对象在注册期崩溃
- 位置：`framework/ctx/events.py:46-47, 68, 73`（摘录行 46-47、68、73）
- 触发：插件传入一个**可调用但没有 `__name__` 属性**的 handler，例如 `functools.partial(...)`、定义了 `__call__` 的类实例、或装饰后返回可调用对象的包装器。`callable(handler)` 为 True，可以通过第 46 行的校验；但第 68/73 行直接取 `handler.__name__`，抛 `AttributeError`。
- 影响：注册在构造 dict 字面量时即抛异常，`_commands.append(...)` 与 `_journal('command', ...)` 都没有执行——命令完全没被登记。若加载器吞掉异常，插件已是「加载成功但命令缺失」的不一致状态且无命令级告警；若不吞，则整个插件加载中断。值得注意的是第 47 行的报错分支自己就写了 `handler.__name__ if hasattr(handler, '__name__') else handler`，说明作者已意识到该属性可能不存在，但后续两处未沿用该防护。
- 证据：
  ```python
  if not callable(handler):
      raise TypeError(f"handler '{handler.__name__ if hasattr(handler, '__name__') else handler}' 不可调用")
  ...
  self._commands.append({
      ...
      'handler': handler,
      'handler_name': handler.__name__,
  ...
  self._journal('command', f"{pattern} -> {handler.__name__}")
  ```
- 建议：与第 47 行保持一致，注册前先归一化名称，例如 `handler_name = getattr(handler, '__name__', type(handler).__name__)`（partial 可再取 `handler.func.__name__`），后续统一使用该局部变量；或显式拒绝无 `__name__` 的 handler 并给出可操作的 TypeError 文案。不要在通过 `callable()` 后就假定 `__name__` 存在。

### D-05-2 `methods` 传入单个字符串时被拆成逐字符方法名，路由实际不可用
- 位置：`framework/ctx/events.py:93-95`（摘录行 93-95）
- 触发：`register_api('/api/x', fn, methods='POST')`（或 `'GET'`）。`methods` 无类型标注、代码也不做类型检查，直接 `[m.upper() for m in methods]`，字符串被迭代为字符，得到 `['P','O','S','T']`。这是非常自然的调用写法（文档里的默认值就是 `['GET']`，调用方很容易省去方括号）。
- 影响：本地/远程注册器收到的都是非法 HTTP 方法列表；真实 POST/GET 请求不会命中该路由（典型表现为 405/404），插件 API 看似注册成功却完全不可达。第 120/122 行的成功日志里会打出 `('P','O','S','T')`，但混在启动日志中很容易被忽略，属于静默失败；函数仍返回 True，调用方无法据返回值发现问题。
- 证据：
  ```python
  if methods is None:
      methods = ['GET']
  methods = [m.upper() for m in methods]
  self._journal('api', f"{','.join(methods)} {path}")
  ```
- 建议：先做归一化与校验：`if isinstance(methods, str): methods = [methods]`；再统一 `str(m).strip().upper()`，并对空列表、非标准方法名（可对照允许集合）直接 `raise ValueError`，让错误在注册期显式暴露，而不是生成一个永远匹配不上的路由。

### D-05-3 远程路由注册的 `except TypeError` 全捕获兜底会掩盖真实错误并可能重复注册
- 位置：`framework/ctx/events.py:100-109`（摘录行 100-109）
- 触发：双进程宿主模式下 `self._framework._remote_routes` 存在。第 102 行的 `rr.register_route(path, methods, handler, auth, plugin=...)` 抛出的 **任何** TypeError 都会进入第 104 行的兜底——既包括「老注册器不收 `plugin`」这一预期情况，也包括注册器内部因参数/处理器契约错误而抛出的 TypeError。随后第 106 行不带 `plugin` 再调一次。
- 影响：
  1. 若第一次调用在抛 TypeError 前已产生部分副作用（写入路由表、建立远程通道），第二次调用会留下**重复/半初始化路由**，污染的是全局路由表，单看日志却是「已注册远程 API 路由」的成功输出，无告警、不能自愈。
  2. 若第一次 TypeError 是真实错误（如 handler 签名不符）而第二次调用恰好不再触发，错误被静默吞掉，插件以为路由已挂载。
  3. 若兜底调用本身再次抛 TypeError（例如新注册器把 `plugin` 设为必填），第 106 行不在任何 try 内，原始异常线索丢失且异常直接冒泡到插件加载流程。
- 证据：
  ```python
  try:
      rr.register_route(path, methods, handler, auth, plugin=self._plugin_name)
  except TypeError:
      # 老版本注册器不收 plugin：退回三参调用，保证插件不因框架升级失配
      rr.register_route(path, methods, handler, auth)
  ```
- 建议：用 `inspect.signature(rr.register_route)`（或一次性能力探测）判断注册器是否接受 `plugin`，据此只调用正确形态一次；若仍要保留兼容兜底，至少把 try 缩到只包围参数形态探测、记录原始 TypeError 到日志、并确认 `register_route` 是幂等/无部分副作用的。另注：注释写「退回三参调用」，实际代码传了 `path, methods, handler, auth` 四个位置参数，建议一并修正注释。

## ruff 结果

在仓库根目录 `C:\zc-repos\zcbot` 执行，两道门禁实际输出：

```
> ruff check --isolated --select F821 .
All checks passed!

> ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .
All checks passed!
```

说明：本轮范围内未发现未定义名或所选风格规则的问题；上述发现均为语义/健壮性缺陷，不属于这两道门禁的覆盖范围。

## 本轮结论

发现 3 个问题（`handler.__name__` 无条件访问、`methods` 字符串被拆成字符、远程路由 `TypeError` 兜底重试），均已给出位置、触发条件与修改建议；未修改任何产品代码、未打 tag、未发版。
