# 第 14 轮审计：configapi

范围：`framework/api/config.py:340~469`（GET/PUT `/api/config/yaml` 及 `_yaml_sections`、`_merged_core_sections`；按简报允许，用 Grep 核对了同文件的 `_mask_sensitive`/`_strip_masks`、`framework/api/app_helpers.py` 的读写助手、`framework/config.py` 的默认段与 `config.yaml` 的键名，未 Read 其它文件）

## 发现

### D-14-1 脱敏占位符只过滤顶层，嵌套凭据会被原样写回成 `***`
- 位置：`framework/api/config.py:438,453~465`；helper：`framework/api/config.py:51~53`（`_strip_masks`）、`28~44`（`_mask_sensitive` 递归）
- 触发：GET 整份配置 → `_mask_sensitive(doc)` 递归把 `onebot.instances[].access_token` 等嵌套敏感键打成 `'***'`；前端整表单回传 PUT `/api/config/yaml/onebot`；`values = _strip_masks(raw_values)` 只比较顶层值 `v != '***'`，嵌套字典/列表里的 `'***'` 被保留，随后 `merged.update(values)` 并 `_update_yaml_section` 落盘。
- 影响：嵌套凭据被持久化为字面量 `***`，对应实例/通道重启后认证失败或外连中断；无告警，只能等用户发现功能异常；需手工改 yaml 才能恢复。
- 证据：
  - `51: def _strip_masks(values: dict) -> dict:` / `53:     return {k: v for k, v in values.items() if v != _MASK}`（单层）。
  - `35:             if is_secret and not isinstance(v, (dict, list, tuple)):` / `38:                 out[k] = _mask_sensitive(v)`（GET 侧递归打码，两边不对称）。
  - `framework/config.py:132~135` 的默认模板给出 `instances` 列表内 `access_token` 的嵌套形态；`463: merged = dict(_read_yaml_section(section))` / `464: merged.update(values)` / `465: if not _update_yaml_section(section, merged):` 直接写回。
- 建议：把 `_strip_masks` 改成与 `_mask_sensitive` 对称的递归实现，按同一套 `_SENSITIVE_KEY` 判定逐层剔除占位符；或让前端只提交 diff，服务端对未提交字段保留原值，禁止把 `'***'` 当有效值写入。

### D-14-2 同一配置段的 PUT 是非原子 read-modify-write，且写助手整文件重写
- 位置：`framework/api/config.py:463~465`；`framework/api/app_helpers.py:220~221,245,254~258`
- 触发：两个超管会话/自动化脚本并发 PUT 同一段的不同字段；或并发 PUT 不同段。两个请求各自 `_read_yaml_section` 拿到同一份旧内容，各自 `merged.update(values)` 后写盘；`_update_yaml_section` 又是「读整文件 → 替换该段 → 写整文件」，后写者把先写者的改动整段/整文件覆盖。
- 影响：先提交的字段静默丢失，GET 回读不到；无冲突检测、无告警；进程重启后配置回退到旧值，属于典型 lost update。
- 证据：`463: merged = dict(_read_yaml_section(section))` → `464: merged.update(values)` → `465: if not _update_yaml_section(section, merged):` 之间没有锁；grep `framework/api/app_helpers.py` 得 `220: with open(path, 'r'...)`, `221: lines = f.readlines()`, `254: new_lines = ...`, `257: with open(path, 'w'...)`, `258: f.writelines(lines)`；该文件未检索到 `Lock`/`threading` 等同步原语。
- 建议：用进程内 `threading.RLock` 包住「读-改-写」全过程；跨进程部署改用文件锁或原子 patch（按字段更新并在写后回读校验），冲突时返回明确错误而不是静默覆盖。

### D-14-3 敏感键匹配过宽，非凭据的数值参数被打码后无法在面板修改
- 位置：`framework/api/config.py:438,453~461`；判定规则 `19~24,33~36`；过滤 `51~53`
- 触发：`console.token_bits`（`config.yaml:481`，值 16384）、`security.fake_token_len` / `real_token_len`（`config.yaml:313~314`）键名含 `token`，但 `_SAFE_KEY_HINT` 只含 `length/timeout/ttl/...`，不含 `bits`/`len`；GET 时 `_mask_sensitive` 把它们显示成 `'***'`；PUT 回传后 `_strip_masks` 把这些 `'***'` 丢掉，字段不进入 `values`，既不写盘也不 hotapply。
- 影响：这些普通数值参数在面板里看不到真值、也改不了；用户以为保存成功，实际未变更，且没有任何提示；与代码注释 `22: # token_timeout / secret_length 之类是普通数值，不能当凭据打码` 的意图相违背。
- 证据：`19: _SENSITIVE_KEY = re.compile(`（含 token）、`24:     r'(timeout|expire|ttl|interval|count|limit|enabled|length|size|level)', re.IGNORECASE)`、`35~36: ... out[k] = ... _MASK`、`53: ... if v != _MASK`；`config.yaml:481: token_bits: 16384`、`313/314: fake_token_len/real_token_len`。
- 建议：在 `_SAFE_KEY_HINT` 补 `bits|len|_len|_bits` 等后缀，或改为按值类型判定（int/float/bool 不打码）；同时对白名单字段做类型校验，避免占位符被误判为「无字段需要更新」。

### D-14-4 插件段发现异常被 debug 级吞掉，GET 与 PUT 的可用段视图可能不一致
- 位置：`framework/api/config.py:382~388,399~417`
- 触发：`list_core_plugins_state()` 抛异常（例如 `core_plugins.yaml` 损坏、读取失败）。GET 侧 `_merged_core_sections` 捕获后直接返回 `doc`（若异常发生在遍历中，则只合并了部分插件段）；PUT 侧 `_yaml_sections` 捕获后只返回固定段，插件段被判为「非法配置段」。
- 影响：同一时刻 GET 展示的段与 PUT 允许的段不一致——用户看到配置项却无法保存，或看到部分段；只留 `logger.debug`，无告警。与 `_yaml_sections` 文档 `38~45` 行「不放行这些段就改不了」的承诺正好相反。
- 证据：`383: from framework.config import list_core_plugins_state` / `385~387: except Exception as _exc: logger.debug(...); extra = set()`；`400: from framework.config import get_config, list_core_plugins_state` / `402: for item in list_core_plugins_state():` / `415~416: except Exception as e: logger.debug(f"补充官方插件配置段失败（不影响读取）: {e}")`。
- 建议：一次请求内共用同一份插件段快照（或在 `framework.config` 层缓存），异常时给前端明确的「配置源不可用/段不完整」标记并把日志升到 warning，避免静默降级成「无此段」。

## ruff 结果

在仓库根目录执行：

```
ruff check --isolated --select F821 .
All checks passed!

ruff check --select UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,UP006,UP045,RUF100 .
All checks passed!
```

## 本轮结论

发现 4 个问题：嵌套占位符回写、配置段非原子读改写、非凭据字段被误打码、插件段发现失败静默降级。
