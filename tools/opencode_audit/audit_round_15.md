# 第 15 轮审计：webapp

范围：`framework/api/webapp.py` 第 500~629 行（摘录 `tools/opencode_audit/excerpt_15_webapp.md`）；仅用 Grep 核对了 `framework/dual_auth.py`、`framework/api/auth.py`、`framework/hooks.py`、`framework/api/framework_ops.py` 的调用点。

## 发现

### D-15-1 采信 X-Forwarded-For 最左值且不做 IP 校验，可绕过全部基于 IP 的风控
- 位置：`framework/api/webapp.py:619-626`（相关：`framework/api/webapp.py:517`、`framework/api/webapp.py:637`、`framework/api/auth.py:265-269`、`framework/dual_auth.py:224,391`）
- 触发：客户端经受信反向代理访问时自行发送 `X-Forwarded-For: 127.0.0.1`（或任意伪造 IP）。代理会在其后追加真实来源 IP，但代码取 `parts[0]`，即攻击者自己控制的最左值；该值未经过 `ipaddress` 校验或规范化，直接作为 `get_client_ip()` 结果。
- 影响：`get_client_ip()` 的返回值被认证失败封禁（`_deny_auth` → `_record_auth_failure`）、`dual_auth.handle_request()`、`_detect_rapid_reload()`、登录失败/限流、`audit_log()` 共用。伪造 `127.0.0.1` 可命中 `dual_auth.whitelist_ips` 默认值，`is_blacklisted()` 与 `handle_request()` 均对白名单 IP 直接放行，软封禁/nonce 校验被绕过；轮换伪造 IP 可逃避连续认证失败阈值、登录速率与刷新阈值；伪造字符串还会写入 `_auth_failures`/`_refresh_windows`、审计日志乃至 `ip_blacklist` 表（污点数据持久化，不能自愈，仅有 `dual_auth` 写库失败时的 warning）。摘录中 595-597 行的"踩坑保佑"注释本身就在提醒这个坑，但实现仍然踩了。
- 证据：摘录对应代码（webapp.py:619-626）：
  ```python
  remote = request.remote_addr or 'unknown'
  if _trusted_proxies and remote in _trusted_proxies:
      xff = request.headers.get('X-Forwarded-For', '')
      if xff:
          parts = [p.strip() for p in xff.split(',') if p.strip()]
          if parts:
              return parts[0]
  return remote
  ```
  Grep 核对：`framework/dual_auth.py:90` 默认 `sec.get("whitelist_ips", ["127.0.0.1"])`；`:224` `if self.is_whitelisted(ip):`；`:391` `if self.is_whitelisted(ip):`（`handle_request` 开头直接放行）。
- 建议：从 XFF 右向左遍历，只跳过确认为受信代理的跳数，取第一个不可信跳作为客户端 IP；对结果做 `ipaddress.ip_address()` 合法性与地址族校验，异常/私网/回环值按配置拒绝并告警；不要采信 `parts[0]`。同时为白名单豁免增加来源校验，避免纯字符串相等即完全跳过认证。

### D-15-2 `http.after_request` 扩展点的返回值被静默丢弃，插件无法改写响应
- 位置：`framework/api/webapp.py:576-586`（尤其 583-585 行）
- 触发：任何插件在 `http.after_request` 钩子中返回修改后的 Flask Response（该扩展点的文档语义即"可读取/改写响应，必须返回响应对象"）。
- 影响：`trigger_sync()` 的返回列表被直接丢弃，函数固定 `return resp`，插件的改写永不生效且无任何告警；钩子内部异常也仅 `logger.warning` 后继续返回原响应，形成静默失败。不会自愈，只有改代码或改插件约定才能恢复。
- 证据：摘录代码（webapp.py:577-586）：
  ```python
  def _hook_after_request(resp):
      """请求后扩展点：可读取/改写响应，必须返回响应对象。"""
      ...
          hooks.trigger_sync('http.after_request', request, resp)
      except Exception as e:
          logger.warning(f"http.after_request 扩展点异常: {e}")
      return resp
  ```
  Grep 核对：`framework/hooks.py:392,417` 中 `trigger_sync` 构造 `results` 并 `return results`；`:139` 只有 `HTTP_BEFORE_REQUEST` 在 `_SHORT_CIRCUIT` 中，after 的结果确实回传给调用方，只是此处未使用。
- 建议：接收 `trigger_sync()` 的结果列表，按约定取最后一个非 `None` 的 Response 作为返回值（并校验其为有效 Flask 响应），再 `return`；若是钩子异常导致改写失效，应至少增加可观测指标或明确文档说明"只读不可改"，避免契约与行为不一致。

### D-15-3 两个 IP 状态字典只有"按最近时间清理"的条件，轮换 IP 可无限增长
- 位置：`framework/api/webapp.py:499-503`、`framework/api/webapp.py:550-553`
- 触发：攻击者每请求换一个新 IP（结合 D-15-1 只需换 XFF 首段即可），使 `_auth_failures` 超过 4096 或 `_refresh_windows` 超过 2048；清理条件分别是 `now - v[-1] >= _AUTH_FAIL_WINDOW` 和 `now - v[-1] >= 600`。
- 影响：所有新键的 `v[-1]` 都是刚写入的时间戳，清理时没有一个键满足 stale 条件，两个字典随请求数无上限增长；`_refresh_windows` 每个键还携带时间戳列表。属于内存泄漏/内存型 DoS：无告警，进程 OOM 才暴露；只有流量停止且后续仍有清理触发时才可能自愈。
- 证据：摘录代码（`webapp.py:499-503`、`550-553`）：
  ```python
  if len(_auth_failures) > 4096:
      stale = [k for k, v in _auth_failures.items()
               if not v or now - v[-1] >= _AUTH_FAIL_WINDOW]
      for k in stale: _auth_failures.pop(k, None)
  ...
  if len(_refresh_windows) > 2048:
      stale = [k for k, v in _refresh_windows.items() if not v or now - v[-1] >= 600]
  ```
- 建议：设置硬上限并配合 LRU/最久未用优先淘汰（而非仅按是否 stale 过滤），例如达到上限时按最旧 `v[-1]` 淘汰一批；同时限制单 IP 时间戳列表长度、使用 `time.monotonic()` 计算窗口，并暴露字典大小指标/告警。

## ruff 结果

在仓库根目录执行，两道门禁输出均为：

```
$ ruff check --isolated --select F821 .
All checks passed!

$ ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .
All checks passed!
```

## 本轮结论

发现 3 个问题：XFF 最左值未校验导致 IP 风控可被绕过、`http.after_request` 返回值被静默丢弃、两个 IP 状态字典可被轮换 IP 无限撑大。
