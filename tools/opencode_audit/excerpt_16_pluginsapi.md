# 第 16 轮侦察材料：framework/api/plugins.py 第 1~130 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
   1 """
   2 插件管理（生命周期 + 依赖/隔离环境）：列表 / 上传 / 重载 / 启停 / 删除 / 依赖 / venv
   3 
   4 市场与更新见 plugin_market.py，配置/README/schema/依赖图见 plugin_meta.py。
   5 register 同时编排三个子模块的路由注册（webapp 仅调用 plugins.register）。
   6 """
   7 import io
   8 import logging
   9 import os
  10 import re
  11 import shutil
  12 import time
  13 import zipfile
  14 
  15 from flask import jsonify, request
  16 
  17 from framework.security.archive import SafeArchiveError, safe_extract_zip, validate_zip
  18 
  19 logger = logging.getLogger('zcbot')
  20 
  21 
  22 def register(ctx):
  23     app = ctx.app
  24     framework = ctx.framework
  25     db = ctx.db
  26     plugins_dir = ctx.plugins_dir
  27     require_auth = ctx.require_auth
  28     require_super = ctx.require_super
  29     audit_log = ctx.audit_log
  30     _data_dir = ctx._data_dir
  31     _read_market_sources_custom = ctx._read_market_sources_custom
  32     _save_market_sources_custom = ctx._save_market_sources_custom
  33     _DEFAULT_MARKET = ctx._DEFAULT_MARKET
  34     _MIRROR_MARKETS = ctx._MIRROR_MARKETS
  35     _github_url_candidates = ctx._github_url_candidates
  36     _fetch_market_source = ctx._fetch_market_source
  37     _market_installed_set = ctx._market_installed_set
  38     _download_and_extract_plugin = ctx._download_and_extract_plugin
  39 
  40     # ---- 编排：注册其余插件域路由 ----
  41     from framework.api import plugin_market, plugin_meta
  42     plugin_market.register(ctx)
  43     plugin_meta.register(ctx)
  44 
  45     # ---- 插件管理 ----
  46 
  47     @app.route('/api/plugins', methods=['GET'])
  48     @require_auth
  49     def list_plugins():
  50         """获取插件列表（含命令/任务计数）"""
  51         try:
  52             try:
  53                 rows = db.query(
  54                     "SELECT id, plugin_name, version, author, description, "
  55                     "COALESCE(priority_override, priority) AS priority, "
  56                     "priority AS priority_declared, priority_override, "
  57                     "status, is_active, has_register, loaded_at, created_at "
  58                     "FROM plugins ORDER BY priority ASC, created_at ASC"
  59                 )
  60             except Exception:
  61                 # 迁移前的老库：还没有 priority_override 列，按声明值展示
  62                 rows = db.query(
  63                     "SELECT id, plugin_name, version, author, description, priority, "
  64                     "priority AS priority_declared, NULL AS priority_override, "
  65                     "status, is_active, has_register, loaded_at, created_at "
  66                     "FROM plugins ORDER BY priority ASC, created_at ASC"
  67                 )
  68             loaded = framework.plugin_loader.get_loaded_plugins()
  69             for r in rows:
  70                 r['priority_source'] = (
  71                     'override' if r.get('priority_override') is not None else 'declared')
  72                 r['is_loaded'] = r['plugin_name'] in loaded
  73                 # 核心插件/服务型插件不参与心跳，status 会一直停在初始 stopped；
  74                 # 只要实际已加载就按运行中显示（error/oom 等异常状态仍原样保留）
  75                 if r['is_loaded'] and r.get('status') in (None, '', 'stopped'):
  76                     r['status'] = 'running'
  77                 r['dir_missing'] = not os.path.isfile(
  78                     os.path.join(framework.plugin_loader.plugins_dir, r['plugin_name'], 'main.py')
  79                 )
  80                 r['has_readme'] = os.path.isfile(
  81                     os.path.join(framework.plugin_loader.plugins_dat_dir, r['plugin_name'], 'README.md')
  82                 )
  83                 yaml_data = framework.plugin_loader.read_plugin_yaml(r['plugin_name'])
  84                 r['has_yaml'] = bool(yaml_data)
  85                 r['has_github'] = bool(yaml_data.get('github', {}).get('repo'))
  86                 r['github_repo'] = yaml_data.get('github', {}).get('repo', '')
  87                 r['config_items'] = yaml_data.get('config', [])
  88                 schema = framework.plugin_loader.read_config_schema(r['plugin_name'])
  89                 r['has_schema'] = bool(schema)
  90                 dep_info = framework.plugin_loader.get_dep_status(r['plugin_name'])
  91                 r['has_missing_deps'] = dep_info['has_missing']
  92                 r['missing_deps'] = dep_info['missing']
  93                 r['has_conflict'] = dep_info['has_conflict']
  94                 r['conflicts'] = dep_info['conflicts']
  95                 try:
  96                     cmd_cnt = db.query_one(
  97                         "SELECT COUNT(*) as cnt FROM commands WHERE plugin_name = %s AND is_active = 1",
  98                         (r['plugin_name'],)
  99                     )
 100                     r['command_count'] = cmd_cnt['cnt'] if cmd_cnt else 0
 101                 except Exception as _exc:
 102                     logger.debug('[list_plugins] 捕获异常后按降级路径继续: %s', _exc)
 103                     r['command_count'] = 0
 104                 try:
 105                     task_cnt = db.query_one(
 106                         "SELECT COUNT(*) as cnt FROM tasks WHERE plugin_name = %s AND is_active = 1",
 107                         (r['plugin_name'],)
 108                     )
 109                     r['task_count'] = task_cnt['cnt'] if task_cnt else 0
 110                 except Exception as _exc:
 111                     logger.debug('[list_plugins] 捕获异常后按降级路径继续: %s', _exc)
 112                     r['task_count'] = 0
 113                 return jsonify({'code': 0, 'data': rows})
 114         except Exception as e:
 115             logger.error(f"获取插件列表失败: {e}")
 116             return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500
 117 
 118     @app.route('/api/plugins/<plugin_name>/status', methods=['GET'])
 119     @require_auth
 120     def plugin_status(plugin_name):
 121         """单个插件的运行状态（WebUI 页面加载时会无条件探测）。
 122 
 123         旧版本没有这个路由：请求落到蜜罐 `/api/<path>` 兜底 → 被判「未知路径探测」
 124         → 拉黑来源 IP + tarpit 600ms。表现就是正常用户打开后台，被自己的服务器
 125         封一小时，页面随机转圈/白屏。这里给出稳定契约，前端不必再猜。
 126 
 127         未安装的插件返回 installed=false（仍然 200），前端据此隐藏状态灯即可。
 128         """
 129         try:
 130             row = db.query_one(
```
