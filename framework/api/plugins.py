# -*- coding: utf-8 -*-
"""
插件管理（生命周期 + 依赖/隔离环境）：列表 / 上传 / 重载 / 启停 / 删除 / 依赖 / venv

市场与更新见 plugin_market.py，配置/README/schema/依赖图见 plugin_meta.py。
register 同时编排三个子模块的路由注册（webapp 仅调用 plugins.register）。
"""
import io
import logging
import os
import re
import shutil
import time
import zipfile

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

    # ---- 编排：注册其余插件域路由 ----
    from framework.api import plugin_market, plugin_meta
    plugin_market.register(ctx)
    plugin_meta.register(ctx)

    # ---- 插件管理 ----

    @app.route('/api/plugins', methods=['GET'])
    @require_auth
    def list_plugins():
        """获取插件列表（含命令/任务计数）"""
        try:
            rows = db.query(
                "SELECT id, plugin_name, version, author, description, priority, "
                "status, is_active, has_register, loaded_at, created_at "
                "FROM plugins ORDER BY priority ASC, created_at ASC"
            )
            loaded = framework.plugin_loader.get_loaded_plugins()
            for r in rows:
                r['is_loaded'] = r['plugin_name'] in loaded
                r['dir_missing'] = not os.path.isfile(
                    os.path.join(framework.plugin_loader.plugins_dir, r['plugin_name'], 'main.py')
                )
                r['has_readme'] = os.path.isfile(
                    os.path.join(framework.plugin_loader.plugins_dat_dir, r['plugin_name'], 'README.md')
                )
                yaml_data = framework.plugin_loader.read_plugin_yaml(r['plugin_name'])
                r['has_yaml'] = bool(yaml_data)
                r['has_github'] = bool(yaml_data.get('github', {}).get('repo'))
                r['github_repo'] = yaml_data.get('github', {}).get('repo', '')
                r['config_items'] = yaml_data.get('config', [])
                schema = framework.plugin_loader.read_config_schema(r['plugin_name'])
                r['has_schema'] = bool(schema)
                dep_info = framework.plugin_loader.get_dep_status(r['plugin_name'])
                r['has_missing_deps'] = dep_info['has_missing']
                r['missing_deps'] = dep_info['missing']
                r['has_conflict'] = dep_info['has_conflict']
                r['conflicts'] = dep_info['conflicts']
                try:
                    cmd_cnt = db.query_one(
                        "SELECT COUNT(*) as cnt FROM commands WHERE plugin_name = %s AND is_active = 1",
                        (r['plugin_name'],)
                    )
                    r['command_count'] = cmd_cnt['cnt'] if cmd_cnt else 0
                except Exception:
                    r['command_count'] = 0
                try:
                    task_cnt = db.query_one(
                        "SELECT COUNT(*) as cnt FROM tasks WHERE plugin_name = %s AND is_active = 1",
                        (r['plugin_name'],)
                    )
                    r['task_count'] = task_cnt['cnt'] if task_cnt else 0
                except Exception:
                    r['task_count'] = 0
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            logger.error(f"获取插件列表失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/upload', methods=['POST'])
    @require_auth
    def upload_plugin():
        """上传 ZIP 插件包"""
        admin = request.admin

        if 'file' not in request.files:
            return jsonify({'code': 400, 'msg': '未选择文件'}), 400

        file = request.files['file']
        if not file.filename or not file.filename.lower().endswith('.zip'):
            return jsonify({'code': 400, 'msg': '仅支持 .zip 文件'}), 400

        filename = os.path.basename(file.filename)
        plugin_name = filename[:-4]  # 去掉 .zip

        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '插件名只能包含字母、数字、下划线、横杠'}), 400

        target_dir = os.path.join(plugins_dir, plugin_name)

        try:
            file_stream = io.BytesIO(file.read())

            with zipfile.ZipFile(file_stream, 'r') as zf:
                names = zf.namelist()
                has_main = any(n.endswith('main.py') for n in names)
                if not has_main:
                    return jsonify({'code': 400, 'msg': 'ZIP 包中未找到 main.py'}), 400

                for name in names:
                    if '..' in name or name.startswith('/'):
                        return jsonify({'code': 400, 'msg': f'非法路径: {name}'}), 400

                for name in names:
                    try:
                        info = zf.getinfo(name)
                        if (info.external_attr >> 16) & 0xF000 == 0xA000:
                            return jsonify(
                                {'code': 400, 'msg': f'拒绝解压符号链接: {name}'}
                            ), 400
                    except KeyError:
                        pass

                backup_dir = None
                if os.path.isdir(target_dir):
                    for old in os.listdir(plugins_dir):
                        if old.startswith(plugin_name + '.bak.'):
                            old_path = os.path.join(plugins_dir, old)
                            try:
                                shutil.rmtree(old_path, ignore_errors=True)
                                logger.debug(f"已清理旧备份: {old}")
                            except Exception:
                                pass
                    backup_dir = target_dir + f'.bak.{int(time.time())}'
                    shutil.move(target_dir, backup_dir)

                os.makedirs(target_dir, exist_ok=True)

                zf.extractall(target_dir)

                entries = os.listdir(target_dir)
                if len(entries) == 1 and os.path.isdir(os.path.join(target_dir, entries[0])):
                    inner = os.path.join(target_dir, entries[0])
                    for item in os.listdir(inner):
                        shutil.move(os.path.join(inner, item), os.path.join(target_dir, item))
                    os.rmdir(inner)

            file_stream.close()

            framework.plugin_loader.split_installed_files(plugin_name)

            load_ok = framework.plugin_loader.load_plugin(plugin_name)
            if backup_dir and os.path.isdir(backup_dir):
                try:
                    shutil.rmtree(backup_dir, ignore_errors=True)
                except Exception:
                    pass
            if load_ok:
                framework.plugin_loader.register_commands(plugin_name)
                dep_info = framework.plugin_loader.get_missing_deps(plugin_name)
                msg = f'插件 [{plugin_name}] 上传并加载成功'
                if dep_info['has_missing']:
                    msg += f'，但缺少依赖: {", ".join(dep_info["missing"])}'
                audit_log(admin['id'], admin['username'], 'upload_plugin',
                          'plugin', plugin_name, {'filename': filename}, 'success')
                return jsonify({'code': 0, 'msg': msg})
            else:
                dep_info = framework.plugin_loader.get_missing_deps(plugin_name)
                if dep_info['has_missing']:
                    err_msg = f'插件 [{plugin_name}] 缺少依赖: {", ".join(dep_info["missing"])}'
                else:
                    err_msg = f'插件 [{plugin_name}] 加载失败，请检查 main.py'
                audit_log(admin['id'], admin['username'], 'upload_plugin',
                          'plugin', plugin_name, {'filename': filename}, 'failure', err_msg)
                return jsonify({'code': 500, 'msg': err_msg}), 500

        except zipfile.BadZipFile:
            return jsonify({'code': 400, 'msg': '无效的 ZIP 文件'}), 400
        except Exception as e:
            logger.error(f"上传插件失败: {e}", exc_info=True)
            audit_log(admin['id'], admin['username'], 'upload_plugin',
                      'plugin', plugin_name, None, 'failure', str(e))
            return jsonify({'code': 500, 'msg': f'上传失败: {e}'}), 500

    @app.route('/api/plugins/<plugin_name>/reload', methods=['POST'])
    @require_auth
    def reload_plugin(plugin_name):
        """重新加载插件"""
        admin = request.admin

        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            framework.plugin_loader.unload_plugin(plugin_name)
            if framework.plugin_loader.load_plugin(plugin_name):
                framework.plugin_loader.register_commands(plugin_name)
                framework.router._invalidate_cache()
                audit_log(admin['id'], admin['username'], 'reload_plugin', 'plugin', plugin_name)
                return jsonify({'code': 0, 'msg': f'插件 [{plugin_name}] 已重新加载'})
            else:
                return jsonify({'code': 500, 'msg': '加载失败'}), 500
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/toggle', methods=['POST'])
    @require_auth
    def toggle_plugin(plugin_name):
        """启用/禁用插件"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        is_active = 1 if data.get('is_active') else 0

        try:
            db.execute(
                "UPDATE plugins SET is_active = %s WHERE plugin_name = %s",
                (is_active, plugin_name)
            )
            action = 'enable_plugin' if is_active else 'disable_plugin'
            audit_log(admin['id'], admin['username'], action, 'plugin', plugin_name)

            if not is_active:
                framework.plugin_loader.unload_plugin(plugin_name)
            else:
                framework.plugin_loader.load_plugin(plugin_name)
                framework.plugin_loader.register_commands(plugin_name)

            framework.router._invalidate_cache()

            return jsonify({'code': 0, 'msg': f'插件已{"启用" if is_active else "禁用"}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>', methods=['DELETE'])
    @require_auth
    def delete_plugin(plugin_name):
        """
        删除插件
        默认保留插件数据（plugins_dat/<插件名>/ 下的配置文件等）；
        请求体携带 {"delete_data": true} 时一并删除插件数据目录及 plugin.yaml 中
        声明的 managed_tables 业务表。
        """
        admin = request.admin

        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        body = request.get_json(silent=True) or {}
        delete_data = bool(body.get('delete_data'))

        target_dir = os.path.join(plugins_dir, plugin_name)
        dat_dir = os.path.join(framework.plugin_loader.plugins_dat_dir, plugin_name)
        venv_dir = os.path.join(dat_dir, '.venv')
        try:
            framework.plugin_loader.unload_plugin(plugin_name)
            if os.path.isdir(target_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
            if os.path.isdir(venv_dir):
                shutil.rmtree(venv_dir, ignore_errors=True)
            if delete_data and os.path.isdir(dat_dir):
                shutil.rmtree(dat_dir, ignore_errors=True)
            db.execute("DELETE FROM plugins WHERE plugin_name = %s", (plugin_name,))
            db.execute("DELETE FROM commands WHERE plugin_name = %s", (plugin_name,))
            db.execute("DELETE FROM tasks WHERE plugin_name = %s", (plugin_name,))
            db.execute("DELETE FROM plugin_configs WHERE plugin_name = %s", (plugin_name,))

            dropped_tables = []
            if delete_data:
                yaml_data = framework.plugin_loader.read_plugin_yaml(plugin_name)
                managed_tables = yaml_data.get('managed_tables', []) if isinstance(yaml_data, dict) else []
                if isinstance(managed_tables, str):
                    managed_tables = [managed_tables]
                for table_name in managed_tables:
                    if not isinstance(table_name, str):
                        continue
                    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', table_name):
                        logger.warning(f"[{plugin_name}] 跳过非法表名: {table_name}")
                        continue
                    try:
                        db.execute(f"DROP TABLE IF EXISTS {table_name}")
                        dropped_tables.append(table_name)
                        logger.info(f"[{plugin_name}] 已删除插件业务表: {table_name}")
                    except Exception as e:
                        logger.warning(f"[{plugin_name}] 删除表 {table_name} 失败: {e}")

            audit_log(admin['id'], admin['username'], 'delete_plugin', 'plugin', plugin_name,
                      {'delete_data': delete_data, 'dropped_tables': dropped_tables}, 'success')
            msg = f'插件 [{plugin_name}] 已删除'
            if not delete_data and os.path.isdir(dat_dir) and os.listdir(dat_dir):
                msg += '（配置文件已保留在 plugins_dat，可手动清理）'
            if dropped_tables:
                msg += f'（已清理业务表: {", ".join(dropped_tables)}）'
            return jsonify({'code': 0, 'msg': msg})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/install_deps', methods=['POST'])
    @require_auth
    def install_plugin_deps(plugin_name):
        """一键安装插件缺失的 Python 依赖"""
        admin = request.admin
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            result = framework.plugin_loader.install_missing_deps(plugin_name)
            if result['success']:
                if result['installed']:
                    audit_log(admin['id'], admin['username'], 'install_deps',
                              'plugin', plugin_name, {'installed': result['installed']}, 'success')
                    return jsonify({
                        'code': 0,
                        'msg': f"已安装依赖: {', '.join(result['installed'])}"
                    })
                else:
                    return jsonify({'code': 0, 'msg': '所有依赖已满足'})
            else:
                audit_log(admin['id'], admin['username'], 'install_deps',
                          'plugin', plugin_name,
                          {'installed': result['installed'], 'failed': result['failed']}, 'failure')
                msg = ''
                if result['installed']:
                    msg += f"已安装: {', '.join(result['installed'])}；"
                msg += f"安装失败: {', '.join(result['failed'])}"
                return jsonify({'code': 500, 'msg': msg}), 500
        except Exception as e:
            logger.error(f"安装依赖失败 [{plugin_name}]: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/create_isolated_env', methods=['POST'])
    @require_auth
    def create_plugin_isolated_env(plugin_name):
        """为插件创建隔离虚拟环境（解决版本冲突）"""
        admin = request.admin
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            result = framework.plugin_loader.create_isolated_env(plugin_name)
            if result['success']:
                audit_log(admin['id'], admin['username'], 'create_isolated_env',
                          'plugin', plugin_name,
                          {'venv_path': result.get('venv_path', ''),
                           'installed': result.get('installed', [])}, 'success')
                msg = f"隔离环境已创建"
                if result.get('installed'):
                    msg += f"，已安装依赖: {', '.join(result['installed'])}"
                return jsonify({'code': 0, 'msg': msg, 'data': result})
            else:
                audit_log(admin['id'], admin['username'], 'create_isolated_env',
                          'plugin', plugin_name,
                          {'error': result.get('error', '')}, 'failure')
                return jsonify({'code': 500, 'msg': f"创建隔离环境失败: {result.get('error', '')}"}), 500
        except Exception as e:
            logger.error(f"创建隔离环境失败 [{plugin_name}]: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/venv_usage', methods=['GET'])
    @require_auth
    def venv_usage():
        """获取所有插件的 .venv 隔离环境磁盘占用情况"""
        try:
            data = framework.plugin_loader.scan_venv_usage()
            return jsonify({'code': 0, 'data': data})
        except Exception as e:
            logger.error(f"扫描 venv 占用失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/isolated_env', methods=['DELETE'])
    @require_auth
    def delete_plugin_isolated_env(plugin_name):
        """删除插件的隔离虚拟环境（清理磁盘空间）"""
        admin = request.admin
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            loaded = framework.plugin_loader.get_loaded_plugins()
            if plugin_name in loaded:
                return jsonify({
                    'code': 400,
                    'msg': f'插件 [{plugin_name}] 正在运行，请先卸载再清理隔离环境'
                }), 400

            result = framework.plugin_loader.remove_isolated_env(plugin_name)
            if result['success']:
                audit_log(admin['id'], admin['username'], 'delete_isolated_env',
                          'plugin', plugin_name, {}, 'success')
                return jsonify({'code': 0, 'msg': result.get('msg', '隔离环境已删除')})
            else:
                audit_log(admin['id'], admin['username'], 'delete_isolated_env',
                          'plugin', plugin_name,
                          {'error': result.get('error', '')}, 'failure')
                return jsonify({
                    'code': 500,
                    'msg': f"清理失败: {result.get('error', '未知错误')}"
                }), 500
        except Exception as e:
            logger.error(f"删除隔离环境失败 [{plugin_name}]: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

