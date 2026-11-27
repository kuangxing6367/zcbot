# 第 16 轮审计：pluginsapi

范围：`framework/api/plugins.py` 第 1~130 行（侦察材料：`tools/opencode_audit/excerpt_16_pluginsapi.md`）

## 发现

### D-16-1 单个 plugin.yaml 结构异常会让整个 /api/plugins 列表接口返回 500
- 位置：`framework/api/plugins.py:83-87`
- 触发：插件的 `plugin.yaml` 是合法 YAML 但不是映射（例如顶层写成 `- name: xxx` 或单个字符串），或 `github:` 写成空值 / 非映射（例如 `github: https://example.com`）。`read_plugin_yaml()` 通过 Grep 确认其实现为 `return yaml.safe_load(f) or {}`（`framework/loader/base.py:223,232`），非映射的真值会被原样返回；随后 `yaml_data.get(...)` 或 `yaml_data.get('github', {}).get('repo')` 抛出 `AttributeError`，被外层 `except` 捕获后整接口返回 500。
- 影响：只要有一个插件的配置写法不合规，插件管理页的整个列表接口就不可用（不是只影响该插件），其他插件也无法展示；只能靠人工修正该 YAML 才能恢复，无专门告警，仅有顶层 `logger.error('获取插件列表失败: ...')`。同文件 `:422` 对同一类返回值已做 `isinstance(yaml_data, dict)` 保护，说明此处缺少校验是不一致的防御缺口。
- 证据（摘录 83-87 行）：
  ```python
  yaml_data = framework.plugin_loader.read_plugin_yaml(r['plugin_name'])
  r['has_yaml'] = bool(yaml_data)
  r['has_github'] = bool(yaml_data.get('github', {}).get('repo'))
  r['github_repo'] = yaml_data.get('github', {}).get('repo', '')
  r['config_items'] = yaml_data.get('config', [])
  ```
  对照（仅 Grep 核对，未 Read 大文件）：`framework/loader/base.py:223,232` 为 `return yaml.safe_load(f) or {}`，`framework/api/plugins.py:422` 为 `... if isinstance(yaml_data, dict) else []`。
- 建议：先做类型归一化 `if not isinstance(yaml_data, dict): yaml_data = {}`；再对嵌套字段用 `gh = yaml_data.get('github') or {}`，并同样校验 `isinstance(gh, dict)` 后再取 `repo`。更根本的是让 `read_plugin_yaml()` 对 `safe_load` 结果做统一校验，保证契约只返回 `dict`，并在解析失败时按告警级别上报（而非只 warning 后继续）。

### D-16-2 命令/任务计数查询失败被吞成 0，前端无法区分“真的为 0”和“查询失败”
- 位置：`framework/api/plugins.py:100-112`
- 触发：`commands` / `tasks` 表或其 `plugin_name`、`is_active` 列缺失（迁移未完成）、DB 连接异常或只读降级导致 `db.query_one()` 抛异常时，两个 `except` 都直接把计数写成 0。
- 影响：UI 显示“0 条命令 / 0 个任务”，与插件真实拥有命令/任务时不可区分，属于静默的数据不一致；异常仅写入 `logger.debug`，生产日志级别下不可见、无告警，接口仍返回 `code: 0`，不会自愈（只能等 DB/表结构恢复后下一次请求刷新）。按插件逐个追加两次查询也放大了故障期间的失败面。
- 证据（摘录 100-112 行节选）：
  ```python
  try:
      cmd_cnt = db.query_one(
          "SELECT COUNT(*) as cnt FROM commands WHERE plugin_name = %s AND is_active = 1",
          (r['plugin_name'],)
      )
      r['command_count'] = cmd_cnt['cnt'] if cmd_cnt else 0
  except Exception as _exc:
      logger.debug('[list_plugins] 捕获异常后按降级路径继续: %s', _exc)
      r['command_count'] = 0
  ```
  （`tasks` 分支 110-112 行结构完全相同。）
- 建议：区分“未知”与“零”：失败时置 `None`/增加 `count_error` 标志由前端显示“—”，而不是伪装成 0；日志提升到 warning 并带插件名，便于发现 schema/连接故障；如果架构要求必须降级，也应在响应中带明确的完整性提示。

## ruff 结果

在仓库根目录实跑两道门禁，实际输出关键行：

```
$ ruff check --isolated --select F821 .
All checks passed!

$ ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .
All checks passed!
```

## 本轮结论

发现 2 个问题：plugin.yaml 结构异常导致列表接口整体 500（D-16-1）；计数查询异常被静默降级为 0（D-16-2）。
