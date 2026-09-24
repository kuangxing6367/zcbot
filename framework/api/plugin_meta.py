# -*- coding: utf-8 -*-
"""
插件元数据：README / 配置文件读写 / config_schema / 命令 / 依赖图
"""
import json
import logging
import os
import re

from flask import jsonify, request

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    db = ctx.db
    plugins_dir = ctx.plugins_dir
    require_auth = ctx.require_auth
    require_super = ctx.require_super
    audit_log = ctx.audit_log
    _data_dir = ctx._data_dir
    _read_market_sources_custom = ctx._read_market_sources_custom
    _save_market_sources_custom = ctx._save_market_sources_custom
    _DEFAULT_MARKET = ctx._DEFAULT_MARKET
    _MIRROR_MARKETS = ctx._MIRROR_MARKETS
    _github_url_candidates = ctx._github_url_candidates
    _fetch_market_source = ctx._fetch_market_source
    _market_installed_set = ctx._market_installed_set
    _download_and_extract_plugin = ctx._download_and_extract_plugin
    @app.route('/api/plugins/<plugin_name>/readme', methods=['GET'])
    @require_auth
    def get_plugin_readme(plugin_name):
        """获取插件 README"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        readme_path = os.path.join(framework.plugin_loader.plugins_dat_dir, plugin_name, 'README.md')
        if not os.path.isfile(readme_path):
            return jsonify({'code': 404, 'msg': '该插件没有 README.md'}), 404

        try:
            with open(readme_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return jsonify({'code': 0, 'data': {'content': content, 'plugin_name': plugin_name}})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/config', methods=['GET'])
    @require_auth
    def get_plugin_config(plugin_name):
        """获取插件的 plugin.yaml 配置及配置文件列表"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            yaml_data = framework.plugin_loader.read_plugin_yaml(plugin_name)
            config_files = framework.plugin_loader.get_plugin_config_files(plugin_name)

            return jsonify({
                'code': 0,
                'data': {
                    'plugin_name': plugin_name,
                    'yaml': yaml_data,
                    'has_yaml': bool(yaml_data),
                    'github': yaml_data.get('github', {}),
                    'config_items': yaml_data.get('config', []),
                    'docs': yaml_data.get('docs', []),
                    'dependencies': yaml_data.get('dependencies', {}),
                    'config_files': config_files,
                    'dep_status': framework.plugin_loader.get_missing_deps(plugin_name),
                }
            })
        except Exception as e:
            logger.error(f"获取插件配置失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/file/<filename>', methods=['GET'])
    @require_auth
    def get_plugin_file(plugin_name, filename):
        """读取插件目录下的指定文件内容"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400
        if '..' in filename or '/' in filename or '\\' in filename:
            return jsonify({'code': 400, 'msg': '非法文件名'}), 400

        try:
            content = framework.plugin_loader.read_plugin_file(plugin_name, filename)
            return jsonify({
                'code': 0,
                'data': {
                    'plugin_name': plugin_name,
                    'filename': filename,
                    'content': content,
                }
            })
        except FileNotFoundError:
            return jsonify({'code': 404, 'msg': '文件不存在'}), 404
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 插件配置读写 API ----

    @app.route('/api/plugins/<plugin_name>/config_schema', methods=['GET'])
    @require_auth
    def get_plugin_config_schema(plugin_name):
        """获取插件的配置 schema 和当前配置值"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            schema = framework.plugin_loader.read_config_schema(plugin_name)
            config_rows = db.query(
                "SELECT config_key, config_value FROM plugin_configs WHERE plugin_name = %s",
                (plugin_name,)
            )
            config_values = {}
            for r in config_rows:
                try:
                    config_values[r['config_key']] = json.loads(r['config_value'])
                except (json.JSONDecodeError, TypeError):
                    config_values[r['config_key']] = r['config_value']

            return jsonify({
                'code': 0,
                'data': {
                    'plugin_name': plugin_name,
                    'schema': schema,
                    'values': config_values,
                    'has_schema': bool(schema),
                }
            })
        except Exception as e:
            logger.error(f"获取插件配置 schema 失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/config_schema', methods=['PUT'])
    @require_auth
    def update_plugin_config(plugin_name):
        """更新插件配置值（用户在 Web UI 修改配置项）"""
        admin = request.admin
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        data = request.get_json(silent=True) or {}
        if not data:
            return jsonify({'code': 400, 'msg': '缺少配置数据'}), 400

        try:
            schema = framework.plugin_loader.read_config_schema(plugin_name)

            updated_keys = []
            for key, value in data.items():
                if schema and key not in schema:
                    continue

                if schema:
                    spec = schema.get(key, {})
                    val_type = spec.get('type', 'string')
                    if val_type == 'int':
                        try:
                            value = int(value)
                        except (ValueError, TypeError):
                            value = spec.get('default', 0)
                    elif val_type == 'float':
                        try:
                            value = float(value)
                        except (ValueError, TypeError):
                            value = spec.get('default', 0.0)
                    elif val_type == 'bool' or val_type == 'boolean':
                        if isinstance(value, str):
                            value = value.lower() in ('true', '1', 'yes', 'on')
                        else:
                            value = bool(value)

                config_value = json.dumps(value, ensure_ascii=False)
                existing = db.query_one(
                    "SELECT id FROM plugin_configs WHERE plugin_name = %s AND config_key = %s",
                    (plugin_name, key)
                )
                if existing:
                    db.execute(
                        "UPDATE plugin_configs SET config_value = %s WHERE plugin_name = %s AND config_key = %s",
                        (config_value, plugin_name, key)
                    )
                else:
                    db.execute(
                        "INSERT INTO plugin_configs (plugin_name, config_key, config_value) VALUES (%s, %s, %s)",
                        (plugin_name, key, config_value)
                    )
                updated_keys.append(key)

            audit_log(admin['id'], admin['username'], 'update_plugin_config',
                      'plugin', plugin_name, {'keys': updated_keys})
            return jsonify({'code': 0, 'msg': f'已更新 {len(updated_keys)} 项配置'})
        except Exception as e:
            logger.error(f"更新插件配置失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 插件命令查看（插件配置弹窗展示触发命令）----

    @app.route('/api/plugins/<plugin_name>/commands', methods=['GET'])
    @require_auth
    def get_plugin_commands(plugin_name):
        """获取指定插件注册的所有命令"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            static_cmds = db.query(
                "SELECT id, pattern, alias, description, priority, handler, "
                "is_active, hit_count, require_level, created_at FROM commands "
                "WHERE plugin_name = %s AND is_dynamic = 0 "
                "ORDER BY priority ASC, created_at ASC",
                (plugin_name,)
            )
            dynamic_cmds = db.query(
                "SELECT id, pattern, alias, description, priority, handler, "
                "is_active, hit_count, created_at FROM commands "
                "WHERE plugin_name = %s AND is_dynamic = 1 "
                "ORDER BY priority ASC, created_at ASC",
                (plugin_name,)
            )
            tasks = db.query(
                "SELECT id, cron_expression, handler, description, is_active, "
                "last_run_at, run_count, last_status FROM tasks "
                "WHERE plugin_name = %s ORDER BY id ASC",
                (plugin_name,)
            )

            return jsonify({
                'code': 0,
                'data': {
                    'plugin_name': plugin_name,
                    'static_commands': static_cmds,
                    'dynamic_commands': dynamic_cmds,
                    'tasks': tasks,
                }
            })
        except Exception as e:
            logger.error(f"获取插件命令失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 插件依赖图 ----

    def _parse_version_spec(dep: str):
        """解析依赖版本说明符，返回 (包名, 运算符, 版本号)"""
        dep = dep.strip()
        m = re.match(r'^([a-zA-Z_][a-zA-Z0-9_.-]*)', dep)
        if not m:
            return (dep, None, None)
        pkg = m.group(1)
        spec_part = dep[len(pkg):].strip()
        if not spec_part:
            return (pkg, None, None)
        m2 = re.match(r'^([><=!~]+)\s*([a-zA-Z0-9.*_+-]+)', spec_part)
        if m2:
            return (pkg, m2.group(1), m2.group(2))
        return (pkg, None, None)

    @app.route('/api/plugins/deps/graph', methods=['GET'])
    @require_auth
    def plugin_deps_graph():
        """获取插件依赖关系图数据（节点 + 边）"""
        try:
            plugin_rows = db.query("SELECT plugin_name, version FROM plugins ORDER BY plugin_name")
            plugins = {r['plugin_name']: r for r in plugin_rows}

            nodes = []
            edges = []
            installed = set(plugins.keys())
            pkg_to_plugins = {}

            for pname in installed:
                yaml_data = framework.plugin_loader.read_plugin_yaml(pname)
                deps = yaml_data.get('dependencies', {}).get('python', []) if yaml_data else []
                dep_info = framework.plugin_loader.get_dep_status(pname)
                missing = set(dep_info.get('missing', []))
                conflicts = dep_info.get('conflicts', [])

                parsed_deps = []
                for dep in deps:
                    pkg_name, op, ver = _parse_version_spec(dep)
                    if pkg_name:
                        parsed_deps.append({
                            'raw': dep,
                            'pkg_name': pkg_name,
                            'operator': op,
                            'version': ver,
                            'missing': dep in missing,
                            'conflict': any(c['name'] == pkg_name for c in conflicts),
                        })
                        pkg_to_plugins.setdefault(pkg_name, []).append(pname)

                nodes.append({
                    'id': pname,
                    'type': 'plugin',
                    'deps': parsed_deps,
                    'dep_count': len(parsed_deps),
                    'missing_count': len([d for d in parsed_deps if d['missing']]),
                    'version': plugins[pname].get('version', '?'),
                })

            for pkg_name, plugin_list in pkg_to_plugins.items():
                if len(plugin_list) >= 2:
                    for i in range(len(plugin_list)):
                        for j in range(i + 1, len(plugin_list)):
                            edges.append({
                                'source': plugin_list[i],
                                'target': plugin_list[j],
                                'label': pkg_name,
                                'shared': True,
                            })

            return jsonify({'code': 0, 'data': {
                'nodes': nodes,
                'edges': edges,
                'total_plugins': len(nodes),
                'total_edges': len(edges),
            }})
        except Exception as e:
            logger.error(f"获取依赖图数据失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

