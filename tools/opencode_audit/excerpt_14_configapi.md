# 第 14 轮侦察材料：framework/api/config.py 第 340~469 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
 340                 },
 341                 'process_memory_mb': round(proc.memory_info().rss / 1024 / 1024, 1),
 342                 'threads': proc.num_threads(),
 343                 'uptime_seconds': uptime,
 344                 'python_version': sys.version.split()[0],
 345                 'db_type': db_type,
 346                 'ws': {'connected': len(bots), 'bots': bots},
 347                 'health': health,
 348                 'event_buffer': buffer_stats,
 349                 'requests': req_counts,
 350                 'outbound': outbound,
 351                 'scheduler': scheduler_stats,
 352                 'event_loop': loop_health,
 353                 'db_degraded': bool(getattr(framework, 'db_degraded', False)),
 354                 'db_schema_incomplete': list(
 355                     getattr(framework, 'schema_incomplete', None) or []),
 356             }})
 357         except Exception as e:
 358             logger.error(f"获取运行状态失败: {e}")
 359             return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500
 360 
 361     # ---- 系统配置（config.yaml 分组读写）----
 362 
 363     # 与 config.yaml 默认模板的顶层段保持一致（第 7 轮补齐此前面板改不了的段）；
 364     # 官方插件私有段由 _yaml_sections() 从 list_core_plugins_state() 动态补充。
 365     _YAML_SECTIONS = (
 366         'database', 'onebot', 'web', 'ssl', 'plugin', 'log', 'system',
 367         'security', 'framework', 'observability', 'reliability', 'outbound',
 368         'scheduler', 'startup', 'memory', 'dependencies',
 369         'dual_process', 'language', 'terminal', 'console', 'event_queue',
 370         'multi_platform', 'github_proxy', 'http_api', 'http_inject',
 371     )
 372 
 373     def _yaml_sections():
 374         """面板允许读写的 config.yaml 段 = 固定部署段 + 各官方插件的配置段。
 375 
 376         官方插件的配置块写在 core_plugins.yaml，启动时由 load_config 合并进
 377         主配置的同名段，且 config.yaml 的值优先（见 framework/config.py）。
 378         如果这里不放行这些段，就会出现「配置项存在、面板改不了」——
 379         用户只能去手改 yaml，而面板看起来一切正常。
 380         """
 381         base = tuple(_YAML_SECTIONS)
 382         try:
 383             from framework.config import list_core_plugins_state
 384             extra = {it['section'] for it in list_core_plugins_state()}
 385         except Exception as _exc:
 386             logger.debug('[_yaml_sections] 捕获异常后按降级路径继续: %s', _exc)
 387             extra = set()
 388         return base + tuple(sorted(extra - set(base)))
 389 
 390     def _merged_core_sections(doc):
 391         """把官方插件段的生效值补进 config.yaml 的 doc。
 392 
 393         config.yaml 里通常没有 image_renderer 这类段（它们来自
 394         core_plugins.yaml）。面板看不到这些段就等于改不了，所以用合并后的
 395         内存配置补齐；enabled 不补——它的唯一权威是 core_plugins.yaml，
 396         混进 config.yaml 只会变成改一处不生效的另一处。
 397         写回时这些键会落到 config.yaml，从此它以覆盖方身份生效。
 398         """
 399         try:
 400             from framework.config import get_config, list_core_plugins_state
 401             full = get_config() or {}
 402             for item in list_core_plugins_state():
 403                 sec = item['section']
 404                 blk = full.get(sec)
 405                 if not isinstance(blk, dict):
 406                     continue
 407                 vals = {k: v for k, v in blk.items() if k != 'enabled'}
 408                 if not vals:
 409                     continue
 410                 cur = doc.get(sec)
 411                 merged = dict(vals)
 412                 if isinstance(cur, dict):
 413                     merged.update(cur)      # config.yaml 已有值优先
 414                 doc[sec] = merged
 415         except Exception as e:
 416             logger.debug(f"补充官方插件配置段失败（不影响读取）: {e}")
 417         return doc
 418 
 419     @app.route('/api/config/yaml', methods=['GET'])
 420     @require_auth
 421     def get_yaml_config():
 422         """读取 config.yaml（凭据一律脱敏，超管也一样）"""
 423         path = _yaml_config_path()
 424         if not os.path.isfile(path):
 425             return jsonify({'code': 404, 'msg': 'config.yaml 不存在'}), 404
 426         try:
 427             with open(path, 'r', encoding='utf-8') as f:
 428                 doc = yaml.safe_load(f) or {}
 429             doc = _merged_core_sections(doc)
 430             # 落盘是密文：超管看明文需要先解密；普通管理员随后仍会被脱敏。
 431             try:
 432                 from framework import secretbox
 433                 secretbox.decrypt_config_secrets(doc)
 434             except Exception as exc:
 435                 logger.warning(f"配置页凭据解密失败（按原值显示）: {exc}")
 436             # 一律脱敏（含超管），见 docs/速查/安全加固.md「配置读取口径」
 437             masked = True
 438             doc = _mask_sensitive(doc)
 439             return jsonify({'code': 0, 'data': doc, 'masked': masked})
 440         except Exception as e:
 441             logger.error(f"get_yaml_config 内部错误: {e}")
 442             return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500
 443 
 444     @app.route('/api/config/yaml/<section>', methods=['PUT'])
 445     @require_super
 446     def update_yaml_config(section):
 447         """分组更新 config.yaml（合并更新，保留未提交字段）"""
 448         admin = request.admin
 449         allowed = _yaml_sections()
 450         if section not in allowed:
 451             return jsonify({'code': 400, 'msg': f'非法配置段，可选: {", ".join(allowed)}'}), 400
 452 
 453         data = request.get_json(silent=True) or {}
 454         raw_values = data.get('data')
 455         if not isinstance(raw_values, dict) or not raw_values:
 456             return jsonify({'code': 400, 'msg': 'data 必须是非空对象'}), 400
 457         values = _strip_masks(raw_values)
 458         if not values:
 459             # 前端把脱敏页整表单回传时，不能把 '***' 写成真配置
 460             return jsonify({'code': 0, 'msg': '无字段需要更新（脱敏占位符已忽略）',
 461                             'data': {'notes': [], 'restart_required': False}})
 462 
 463         merged = dict(_read_yaml_section(section))
 464         merged.update(values)
 465         if not _update_yaml_section(section, merged):
 466             return jsonify({'code': 500, 'msg': '写入 config.yaml 失败'}), 500
 467 
 468         # 写盘成功后再应用到运行中的框架（内存配置 + 运行时副作用），并回报哪些字段即时生效
 469         from framework import hotapply
```
