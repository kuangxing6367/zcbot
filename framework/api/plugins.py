# -*- coding: utf-8 -*-
"""
插件管理：列表 / 上传 / 重载 / 启停 / 删除 / 依赖 / 隔离环境 / 配置 / 更新 / 市场 / 依赖图
"""
import io
import json
import logging
import os
import re
import shutil
import time
import zipfile

import requests
import yaml
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

    @app.route('/api/plugins/<plugin_name>/check_update', methods=['GET'])
    @require_auth
    def check_plugin_update(plugin_name):
        """检查插件是否有 GitHub 更新（基于版本号对比）"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            yaml_data = framework.plugin_loader.read_plugin_yaml(plugin_name)
            github = yaml_data.get('github', {})
            repo = github.get('repo', '')
            branch = github.get('branch', 'main')

            current_version = yaml_data.get('version', 'unknown')

            latest_version = None
            latest_sha = ''
            commit_msg = ''
            commit_date = ''
            author = ''
            has_update = False

            reg = _load_market_registry()
            reg_entry = next((p for p in reg.get('plugins', []) if p['name'] == plugin_name), None)
            if reg_entry and reg_entry.get('version'):
                latest_version = reg_entry['version']
                has_update = _version_gt(latest_version, current_version)
            else:
                if not repo:
                    return jsonify({'code': 400, 'msg': '该插件未在官方市场注册，且 plugin.yaml 缺少 github.repo'}), 400
                if repo.startswith('https://github.com/'):
                    repo = repo.replace('https://github.com/', '').rstrip('/')
                elif repo.startswith('http://github.com/'):
                    repo = repo.replace('http://github.com/', '').rstrip('/')
                api_url = f"https://api.github.com/repos/{repo}/commits/{branch}"
                headers = {'Accept': 'application/vnd.github.v3+json'}
                last_err = ''
                for cand in _github_url_candidates(api_url):
                    try:
                        resp = requests.get(cand, headers=headers, timeout=15)
                        if resp.status_code == 200:
                            try:
                                data = resp.json()
                            except ValueError:
                                last_err = f'非 JSON 响应: {cand}'
                                continue
                            latest_sha = data.get('sha', '')[:7]
                            commit_msg = data.get('commit', {}).get('message', '').split('\n')[0]
                            commit_date = data.get('commit', {}).get('author', {}).get('date', '')
                            author = data.get('commit', {}).get('author', {}).get('name', '')
                            has_update = True
                            break
                        last_err = f'HTTP {resp.status_code}'
                    except Exception as e:
                        last_err = str(e)
                if not latest_sha:
                    return jsonify({'code': 500, 'msg': f'GitHub API 获取失败: {last_err}'}), 500

            return jsonify({
                'code': 0,
                'data': {
                    'plugin_name': plugin_name,
                    'repo': repo,
                    'branch': branch,
                    'current_version': current_version,
                    'latest_version': latest_version or 'unknown',
                    'latest_commit': latest_sha,
                    'commit_message': commit_msg,
                    'commit_date': commit_date,
                    'author': author,
                    'has_update': has_update,
                }
            })
        except requests.exceptions.Timeout:
            return jsonify({'code': 500, 'msg': 'GitHub API 请求超时'}), 500
        except Exception as e:
            logger.error(f"检查更新失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    def _version_gt(a, b):
        """语义化版本比较：a > b 返回 True（无法解析时按字符串比较）"""

        def _parse(v):
            v = str(v).lstrip('vV^~>=< ').strip()
            parts = re.split(r'[.\-+]', v)
            out = []
            for p in parts:
                m = re.match(r'(\d+)', p)
                out.append(int(m.group(1)) if m else 0)
            return out

        try:
            pa, pb = _parse(a), _parse(b)
            n = max(len(pa), len(pb))
            pa += [0] * (n - len(pa))
            pb += [0] * (n - len(pb))
            return pa > pb
        except Exception:
            return str(a) != str(b)

    def _load_market_registry() -> dict:
        """加载官方插件市场 registry（默认源 + 镜像源 + 自定义源兜底），返回 {'plugins': [...]}"""
        sources = [_DEFAULT_MARKET] + _MIRROR_MARKETS + _read_market_sources_custom()
        last_err = ''
        for src in sources:
            try:
                plugins = _fetch_market_source(src)
                if plugins:
                    return {'plugins': plugins}
            except Exception as e:
                last_err = str(e)
        logger.warning(f"加载官方市场 registry 失败: {last_err}")
        return {'plugins': []}

    def _persist_plugin_github_meta(plugin_name: str, repo: str, branch: str, sub_path: str) -> None:
        """安装/更新第三方源插件后，把 github 源信息写回 plugin.yaml 的 github 段"""
        try:
            dat_yaml = os.path.join(framework.plugin_loader.plugins_dat_dir, plugin_name, 'plugin.yaml')
            data = {}
            if os.path.isfile(dat_yaml):
                with open(dat_yaml, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                data = {}
            gh = data.get('github') or {}
            if not isinstance(gh, dict):
                gh = {}
            gh.setdefault('repo', repo)
            gh.setdefault('branch', branch or 'main')
            gh.setdefault('path', sub_path or '/')
            data['github'] = gh
            os.makedirs(os.path.dirname(dat_yaml), exist_ok=True)
            with open(dat_yaml, 'w', encoding='utf-8') as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        except Exception as e:
            logger.warning(f"写回插件 github 元信息失败 {plugin_name}: {e}")

    @app.route('/api/plugins/<plugin_name>/update', methods=['POST'])
    @require_auth
    def update_plugin_from_github(plugin_name):
        """从 GitHub 更新插件代码"""
        admin = request.admin

        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            yaml_data = framework.plugin_loader.read_plugin_yaml(plugin_name)
            github = yaml_data.get('github', {})
            repo = github.get('repo', '')
            branch = github.get('branch', 'main')
            sub_path = github.get('path', '/')

            if not repo:
                return jsonify({'code': 400, 'msg': '该插件未配置 GitHub 更新源'}), 400

            if repo.startswith('https://github.com/'):
                repo = repo.replace('https://github.com/', '').rstrip('/')
            elif repo.startswith('http://github.com/'):
                repo = repo.replace('http://github.com/', '').rstrip('/')

            target_dir = os.path.join(plugins_dir, plugin_name)

            framework.plugin_loader.unload_plugin(plugin_name)

            backup_dir = None
            if os.path.isdir(target_dir):
                backup_dir = target_dir + f'.bak.{int(time.time())}'
                shutil.move(target_dir, backup_dir)

            try:
                ok_dl, dl_msg = _download_and_extract_plugin(repo, branch, sub_path, target_dir)
                if not ok_dl:
                    raise RuntimeError(f'更新下载失败: {dl_msg}')
            except Exception as e:
                if backup_dir and os.path.isdir(backup_dir):
                    shutil.rmtree(target_dir, ignore_errors=True)
                    shutil.move(backup_dir, target_dir)
                raise e

            framework.plugin_loader.split_installed_files(plugin_name)

            if framework.plugin_loader.load_plugin(plugin_name):
                framework.plugin_loader.register_commands(plugin_name)
                if backup_dir and os.path.isdir(backup_dir):
                    shutil.rmtree(backup_dir, ignore_errors=True)
                    logger.info(f"[{plugin_name}] 已清理更新前的代码备份: {backup_dir}")
                audit_log(admin['id'], admin['username'], 'update_plugin_github',
                          'plugin', plugin_name, {'repo': repo, 'branch': branch}, 'success')
                return jsonify({'code': 0, 'msg': f'插件 [{plugin_name}] 已从 GitHub 更新并重新加载'})
            else:
                if backup_dir and os.path.isdir(backup_dir):
                    shutil.rmtree(target_dir, ignore_errors=True)
                    shutil.move(backup_dir, target_dir)
                    logger.warning(f"[{plugin_name}] 新代码加载失败，已恢复旧代码备份")
                audit_log(admin['id'], admin['username'], 'update_plugin_github',
                          'plugin', plugin_name, {'repo': repo, 'branch': branch}, 'failure', '加载失败')
                return jsonify({'code': 500, 'msg': f'代码已下载但加载失败，请检查 main.py'}), 500

        except zipfile.BadZipFile:
            return jsonify({'code': 400, 'msg': '下载的 ZIP 文件无效'}), 400
        except Exception as e:
            logger.error(f"GitHub 更新插件失败: {e}", exc_info=True)
            audit_log(admin['id'], admin['username'], 'update_plugin_github',
                      'plugin', plugin_name, None, 'failure', str(e))
            return jsonify({'code': 500, 'msg': f'更新失败: {e}'}), 500

    # ---- 插件市场（Registry JSON）----

    _MARKET_CACHE_FILE = 'plugin_market_cache.json'
    _MARKET_CACHE_TTL = 300  # 秒

    @app.route('/api/plugins/market', methods=['GET'])
    @require_auth
    def plugin_market():
        """获取在线插件市场列表（默认源 + 自定义源，带缓存）"""
        force = request.args.get('force_refresh', 'false').lower() == 'true'
        cache_path = os.path.join(_data_dir(), _MARKET_CACHE_FILE)

        if not force and os.path.isfile(cache_path):
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    cache = json.load(f)
                if time.time() - cache.get('ts', 0) < _MARKET_CACHE_TTL:
                    return jsonify({'code': 0, 'data': cache['data']})
            except Exception:
                pass

        sources = [_DEFAULT_MARKET] + _read_market_sources_custom()
        all_plugins, errors = [], []
        for src in sources:
            try:
                if src.get('url') == _DEFAULT_MARKET['url']:
                    ok_src = False
                    for cand in _github_url_candidates(_DEFAULT_MARKET['url']):
                        try:
                            all_plugins.extend(_fetch_market_source(
                                {'name': src.get('name', '默认源'), 'url': cand}))
                            ok_src = True
                            break
                        except Exception:
                            continue
                    if not ok_src:
                        errors.append(f"{src.get('name', src.get('url', ''))}: 所有源均获取失败")
                else:
                    src_url = src.get('url', '')
                    cands = _github_url_candidates(src_url) if 'github' in src_url else [src_url]
                    ok_src = False
                    for cand in cands:
                        try:
                            all_plugins.extend(_fetch_market_source(
                                {'name': src.get('name', '自定义源'), 'url': cand}))
                            ok_src = True
                            break
                        except Exception:
                            continue
                    if not ok_src:
                        errors.append(f"{src.get('name', src_url)}: 所有源均获取失败")
            except Exception as e:
                errors.append(f"{src.get('name', src.get('url', ''))}: {str(e)[:120]}")

        installed = _market_installed_set()
        for p in all_plugins:
            p['installed'] = p.get('name') in installed

        result = {'plugins': all_plugins, 'errors': errors, 'sources': sources}
        try:
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump({'ts': time.time(), 'data': result}, f, ensure_ascii=False)
        except Exception:
            pass
        if not all_plugins and os.path.isfile(cache_path):
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    cache = json.load(f)
                if cache.get('data', {}).get('plugins'):
                    cache_data = cache['data']
                    cache_data['errors'] = (cache_data.get('errors') or []) + ['网络获取失败，展示缓存数据（可能不是最新）']
                    return jsonify({'code': 0, 'data': cache_data, 'cached': True})
            except Exception:
                pass
        return jsonify({'code': 0, 'data': result})

    @app.route('/api/plugins/market/sources', methods=['GET'])
    @require_auth
    def plugin_market_sources_get():
        """获取插件源列表（默认源 + 自定义源）"""
        return jsonify({'code': 0, 'data': {
            'default': _DEFAULT_MARKET,
            'custom': _read_market_sources_custom(),
        }})

    @app.route('/api/plugins/market/sources', methods=['POST'])
    @require_super
    def plugin_market_sources_save():
        """保存自定义插件源列表"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        sources = data.get('sources', [])
        if not isinstance(sources, list):
            return jsonify({'code': 400, 'msg': 'sources 必须是数组'}), 400
        cleaned = []
        for s in sources:
            if not isinstance(s, dict):
                continue
            name = str(s.get('name') or '').strip()
            url = str(s.get('url') or '').strip()
            if name and url.startswith(('http://', 'https://')):
                cleaned.append({'name': name, 'url': url})
        _save_market_sources_custom(cleaned)
        audit_log(admin['id'], admin['username'], 'update_market_sources', 'plugin', 'market', {'sources': cleaned})
        return jsonify({'code': 0, 'msg': f'已保存 {len(cleaned)} 个自定义插件源'})

    @app.route('/api/plugins/market/install', methods=['POST'])
    @require_auth
    def plugin_market_install():
        """从市场安装插件（下载 ZIP 到插件目录并加载）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        plugin_name = str(data.get('name') or '').strip()
        repo = str(data.get('repo') or '').strip()
        branch = str(data.get('branch') or 'main').strip() or 'main'
        sub_path = str(data.get('sub_path') or '/')

        if not plugin_name or not re.match(r'^[\w\-]+$', plugin_name):
            return jsonify({'code': 400, 'msg': '非法的插件名'}), 400
        if not repo:
            return jsonify({'code': 400, 'msg': '缺少仓库地址（repo）'}), 400

        try:
            target_dir = os.path.join(plugins_dir, plugin_name)
            backup_dir = None
            if os.path.isdir(target_dir) and os.listdir(target_dir):
                framework.plugin_loader.unload_plugin(plugin_name)
                backup_dir = target_dir + f'.bak.{int(time.time())}'
                shutil.move(target_dir, backup_dir)

            ok, msg = _download_and_extract_plugin(repo, branch, sub_path, target_dir)
            if not ok:
                if backup_dir and os.path.isdir(backup_dir):
                    shutil.rmtree(target_dir, ignore_errors=True)
                    shutil.move(backup_dir, target_dir)
                    logger.warning(f"[{plugin_name}] 安装下载失败，已恢复旧代码")
                return jsonify({'code': 500, 'msg': f'下载失败: {msg}'}), 500

            framework.plugin_loader.split_installed_files(plugin_name)

            if framework.plugin_loader.load_plugin(plugin_name):
                framework.plugin_loader.register_commands(plugin_name)
                _persist_plugin_github_meta(plugin_name, repo, branch, sub_path)
                if backup_dir and os.path.isdir(backup_dir):
                    shutil.rmtree(backup_dir, ignore_errors=True)
                    logger.info(f"[{plugin_name}] 已清理安装前的代码备份: {backup_dir}")
                audit_log(admin['id'], admin['username'], 'install_plugin_market',
                          'plugin', plugin_name, {'repo': repo, 'branch': branch, 'sub_path': sub_path})
                return jsonify({'code': 0, 'msg': f'插件 [{plugin_name}] 安装成功并已加载'})
            if backup_dir and os.path.isdir(backup_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
                shutil.move(backup_dir, target_dir)
                logger.warning(f"[{plugin_name}] 新代码加载失败，已恢复旧代码备份")
            audit_log(admin['id'], admin['username'], 'install_plugin_market',
                      'plugin', plugin_name, {'repo': repo}, 'failure', '加载失败')
            return jsonify({'code': 500, 'msg': '代码已下载但加载失败，请检查 main.py'}), 500
        except Exception as e:
            logger.error(f"市场安装插件失败: {e}", exc_info=True)
            return jsonify({'code': 500, 'msg': f'安装失败: {e}'}), 500

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
