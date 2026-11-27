# 第 13 轮审计：usersgroups

范围：`framework/api/users_groups.py` 第 1~130 行（`tools/opencode_audit/excerpt_13_usersgroups.md`）

## 发现

### D-13-1 `set_user_role` 对非字符串 role 直接 `.strip()`，且在 try 之外，必然抛未捕获 500
- 位置：`framework/api/users_groups.py:124`
- 触发：`PUT /api/users/123/role` 请求体为 `{"role": 1}`、`{"role": null}`、`{"role": []}` 等合法 JSON 但值非字符串时，`data.get('role', '')` 返回非 str，`.strip()` 抛 `AttributeError`；该行位于第 134 行 `try` 之前，异常不会被本函数的 `except` 捕获。
- 影响：Flask 返回默认的 HTML 500（不是本模块约定的 `{'code':..., 'msg':...}` JSON），既没有 `logger.error` 也没有审计日志；接口契约和错误监控都被破坏。状态未被改动，可自愈但无告警。
- 证据：
  ```
  123       data = request.get_json(silent=True) or {}
  124       role = data.get('role', '').strip()
  126       if role not in ('super', ''):
  129       try:
  134           db.execute("UPDATE users SET role = %s WHERE user_id = %s", (role, user_id))
  ```
- 建议：先做类型校验（`isinstance(role, str)`），非字符串直接返回 400；把参数解析一并放进 try，或统一在入口用 schema 校验请求体，保证所有错误路径都返回 JSON。

### D-13-2 分页参数 `int()` 解析在 try 之外，畸形查询串直接打穿接口
- 位置：`framework/api/users_groups.py:24-26`
- 触发：`GET /api/users?page=abc`、`?size=` 或 `?size=1.5` 时，`int()` 在第 29 行 `try` 之前抛 `ValueError`，未被捕获；`page=0`、`size=-1` 虽然进入 try，但会产生负的 LIMIT/OFFSET，落到 SQL 异常后统一报 500。
- 影响：任意用户（包括普通已登录用户）用一个查询参数即可让端点返回 HTML 500 而非 JSON 错误，日志里没有任何本模块的 `list_users 内部错误` 记录；配合前端分页/搜索框的异常输入很容易触发。
- 证据：
  ```
  24       page = int(request.args.get('page', 1))
  25       size = min(int(request.args.get('size', 50)), 200)
  26       offset = (page - 1) * size
  29       try:
  60           logger.error(f"list_users 内部错误: {e}")
  ```
- 建议：把解析放入 try，失败返回 `400` + JSON 错误；同时限制下界（`page >= 1`、`1 <= size <= 200`），避免负偏移和负 LIMIT。

### D-13-3 管理端点用 `<int:user_id>`，与 `VARCHAR(64)`／命名空间身份不兼容，用户建得出来却改不了删不掉
- 位置：`framework/api/users_groups.py:86`、`:105`、`:118`（Grep 另见 `:143` 黑名单端点同型）
- 触发：`users.user_id` 在 MySQL 建表与 SQLite schema 中均为 `VARCHAR(64)`（`sql/init.sql:18`、`framework/database/schema.py:154`）；开启 `multi_platform.namespace` 后，`framework/messaging/adapter_registry.py:121` 把身份改写为 `f"{uuid_val}@{raw_id}"`，例如 `<uuid>@10001`；部分适配器本身也产生非纯数字 ID（Matrix 为 `@user:server`）。`list_users` 会把这些 ID 原样返回给前端，但 `PUT/DELETE/role/blacklist` 的 URL 规则是 `<int:user_id>`，带 `@`/`:`/字母的 ID 匹配不到路由，直接 404。`create_user`（第 69 行）又允许写入任意字符串 user_id，于是能建出永远无法编辑/删除的行。
- 影响：多平台模式下，事件自动落库的用户全部无法通过 WebUI 改名、删除、设角色、拉黑；404 由路由层产生，没有错误日志，运维只能看到“点了没反应”；数据库中积累无法清理的僵尸用户。
- 证据：
  ```
  69       user_id = str(data.get('user_id') or '').strip()
  86   @app.route('/api/users/<int:user_id>', methods=['PUT'])
  105   @app.route('/api/users/<int:user_id>', methods=['DELETE'])
  118   @app.route('/api/users/<int:user_id>/role', methods=['PUT'])
  ```
  Grep 核对：`schema.py:154 user_id VARCHAR(64) NOT NULL`、`sql/init.sql:18 user_id VARCHAR(64)`、`adapter_registry.py:121 return f"{uuid_val}@{raw_id}"`、`stats_writer.py:195 nuid = self._namespace_id(user_id, adapter_key)`。
- 建议：改用 `<path:user_id>`/`<string:user_id>` 并做长度与字符白名单校验，或为字符串 ID 增加独立端点；同时让前端按实际 user_id 拼 URL；补一条“命名空间 ID 用户可编辑/删除”的回归测试。

### D-13-4 角色/黑名单写库后不失效共享权限缓存，撤权最长 60 秒不生效
- 位置：`framework/api/users_groups.py:134`、`:152`（Grep）、`:111`
- 触发：调用 `PUT /api/users/<id>/role` 把用户从 `super` 降为普通用户，或 `POST .../blacklist` 拉黑用户，或删除用户；本文件全文 Grep 不到任何 `invalidate`/缓存刷新调用。而 `framework/messaging/event.py:28` 的 `_ROLE_CACHE_TTL = 60.0`、`:315-327` 的 `_user_role_cache` 会缓存 `(role, is_blacklist)`，`framework/perm/__init__.py:52` 的 `_PERM_CACHE_TTL = 60.0`、`:372-373` 的 `_perm_cache` 也会缓存权限；两条缓存都要等 TTL 过期才重读 DB。
- 影响：这是安全相关的状态不一致——被撤销的超管权限在最长 60 秒内仍然有效，被拉黑用户仍可执行命令，删除后旧身份仍保留角色；无告警，只能靠 TTL 自愈。对比 `framework/perm/groups.py:186/197/219` 在改权限时都会调用 `invalidate_user`，这里明显漏了一环。
- 证据：
  ```
  134           db.execute("UPDATE users SET role = %s WHERE user_id = %s", (role, user_id))
  136           audit_log(admin['id'], admin['username'], 'set_user_role', 'user', str(user_id),
  140           logger.error(f"set_user_role 内部错误: {e}")
  ```
  Grep 核对：`event.py:40 def invalidate_user_role_cache(user_id: int = None)`、`perm/__init__.py:246 def invalidate_user(user_id=None)` 均存在但本文件未调用；`router.py:577 def invalidate_permissions(self, user_id=None)` 是既有统一入口。
- 建议：写库成功后调用按用户粒度的失效（如 `perm.invalidate_user(user_id)` + `event.invalidate_user_role_cache(user_id)`，必要时清群成员缓存），不要照搬会全清全局缓存的 `invalidate_permissions()`（参见第 11 轮 D-11-2）；失效失败要记 warning。

### D-13-5 `DELETE` 只删 `users` 行，留下 `group_members` 孤立数据与群角色残留
- 位置：`framework/api/users_groups.py:111`
- 触发：`DELETE /api/users/<id>` 只执行 `DELETE FROM users WHERE user_id = %s`。Grep 确认 `group_members` 没有任何外键/级联（`framework/database/schema.py` 与 `sql/init.sql` 均无 `FOREIGN KEY`/`CASCADE`），全仓 `DELETE FROM group_members` 只出现在 `dispatch.py:762` 的退群同步中，删除用户不会触发。而 `framework/messaging/event.py:347` 的群内角色判定仍从 `group_members` 读取 `role`，其 `_group_role_cache` 同样有 60 秒 TTL。
- 影响：用户被删除后，其群成员行与群级角色继续存在，权限判定在缓存有效期内仍按旧角色放行；用户下次发言时 `stats_writer` 会重建 `users` 行但库中仍残留历史成员记录。审计日志显示“用户已删除”，实际共享状态只删了一半，无告警。
- 证据：
  ```
  110           db.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
  112           audit_log(admin['id'], admin['username'], 'delete_user', 'user', str(user_id))
  113           return jsonify({'code': 0, 'msg': '用户已删除'})
  ```
  Grep 核对：`stats_writer.py:281 "INSERT INTO group_members (group_id, user_id, card, role, title, "`、`event.py:347 "SELECT role FROM group_members WHERE group_id=%s AND user_id=%s"`；`dispatch.py:762 "DELETE FROM group_members WHERE group_id = %s AND user_id = %s"` 是唯一其他清理点。
- 建议：在同一事务里删除 `group_members` 等所有以 user_id 为键的关联数据（或改为软删除并统一过滤），并调用按用户粒度的缓存失效；补“删除用户后群成员记录与权限同步消失”的回归测试。

## ruff 结果

在仓库根目录 `C:\zc-repos\zcbot` 执行：

```
$ ruff check --isolated --select F821 .
All checks passed!

$ ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .
All checks passed!
```

两道门禁均通过，本轮范围无风格/未定义名问题。

## 本轮结论

发现 5 个问题：非字符串 role 导致未捕获 500、分页参数未校验导致未捕获 500、int 路由与字符串/命名空间用户 ID 不兼容、角色/黑名单变更未失效共享缓存、删除用户遗留群成员数据。
