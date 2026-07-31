"""
API 接口模块
提供：登录认证、仪表盘、插件管理（ZIP上传、GitHub更新）、命令管理、审计日志
基于 Flask，运行在独立线程中
"""
import io
import json
import logging
import os
import queue
import secrets
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta
from functools import wraps

import bcrypt
import requests
import yaml
from flask import Flask, request, jsonify, send_from_directory, Response

from framework.log_broker import log_broker

logger = logging.getLogger('zcbot')


def create_web_app(framework) -> Flask:
    """
    创建 Flask 应用并注册所有路由
    :param framework: Framework 实例
    """
    app = Flask(__name__, static_folder=None)
    web_cfg = framework.config.get('web', {})

    db = framework.db
    plugins_dir = framework.plugin_loader.plugins_dir

    # ---- 工具函数 ----

    def get_client_ip():
        return request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown')

    def audit_log(admin_id, admin_name, action, target_type=None, target_name=None,
                  detail=None, result='success', error_message=None):
        """记录审计日志"""
        try:
            db.execute(
                "INSERT INTO audit_logs (admin_id, admin_name, action, target_type, target_name, "
                "detail, ip_address, result, error_message) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (admin_id, admin_name, action, target_type, target_name,
                 json.dumps(detail, ensure_ascii=False) if detail else None,
                 get_client_ip(), result, error_message)
            )
        except Exception as e:
            logger.error(f"审计日志写入失败: {e}")

    def _extract_token(req):
        """从 Authorization: Bearer xxx 头提取 token"""
        auth = req.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            return auth[7:]
        return None

    def _verify_token(token):
        """验证 token，返回 admin 字典或 None"""
        if len(token) != 2048:
            return None
        row = db.query_one(
            "SELECT id, username, role, is_active, token_created_at FROM admin_users WHERE token = %s",
            (token,)
        )
        if not row or not row['is_active']:
            return None
        # 检查过期
        timeout = web_cfg.get('token_timeout', 86400)
        if row['token_created_at']:
            expiry = row['token_created_at'] + timedelta(seconds=timeout)
            if datetime.now() > expiry:
                return None
        return {'id': row['id'], 'username': row['username'], 'role': row['role']}

    def require_auth(fn):
        """登录验证装饰器（基于 token）"""
        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = _extract_token(request)
            if not token:
                return jsonify({'code': 401, 'msg': '未提供认证令牌'}), 401
            admin = _verify_token(token)
            if not admin:
                return jsonify({'code': 401, 'msg': '令牌无效或已过期'}), 401
            request.admin = admin  # 将 admin 信息附加到 request
            return fn(*args, **kwargs)
        return wrapper

    def require_super(fn):
        """超级管理员验证装饰器（基于 token）"""
        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = _extract_token(request)
            if not token:
                return jsonify({'code': 401, 'msg': '未提供认证令牌'}), 401
            admin = _verify_token(token)
            if not admin:
                return jsonify({'code': 401, 'msg': '令牌无效或已过期'}), 401
            if admin.get('role') != 'super':
                return jsonify({'code': 403, 'msg': '权限不足，需要超级管理员'}), 403
            request.admin = admin
            return fn(*args, **kwargs)
        return wrapper

    # ---- 认证接口 ----

    @app.route('/api/login', methods=['POST'])
    def login():
        """管理员登录"""
        data = request.get_json(silent=True) or {}
        username = data.get('username', '').strip()
        password = data.get('password', '')

        if not username or not password:
            return jsonify({'code': 400, 'msg': '用户名和密码不能为空'}), 400

        try:
            row = db.query_one(
                "SELECT id, username, password_hash, role, is_active FROM admin_users WHERE username = %s",
                (username,)
            )
        except Exception as e:
            logger.error(f"登录查询失败: {e}")
            return jsonify({'code': 500, 'msg': f'数据库错误: {e}'}), 500

        if not row:
            audit_log(None, username, 'login', result='failure', error_message='用户不存在')
            return jsonify({'code': 401, 'msg': '用户名或密码错误'}), 401

        if not row['is_active']:
            audit_log(row['id'], username, 'login', result='failure', error_message='账号已禁用')
            return jsonify({'code': 403, 'msg': '账号已禁用'}), 403

        # 验证密码
        try:
            stored_hash = row['password_hash']
            if isinstance(stored_hash, str):
                stored_hash = stored_hash.encode('utf-8')
            if isinstance(password, str):
                password = password.encode('utf-8')
            if not bcrypt.checkpw(password, stored_hash):
                audit_log(row['id'], username, 'login', result='failure', error_message='密码错误')
                return jsonify({'code': 401, 'msg': '用户名或密码错误'}), 401
        except Exception as e:
            logger.error(f"密码验证异常: {e}")
            return jsonify({'code': 500, 'msg': '密码验证失败'}), 500

        # 生成 2048 位随机 token
        token = secrets.token_hex(1024)  # 2048 字符
        db.execute(
            "UPDATE admin_users SET token = %s, token_created_at = NOW(), last_login_at = NOW(), last_login_ip = %s WHERE id = %s",
            (token, get_client_ip(), row['id'])
        )
        audit_log(row['id'], username, 'login', result='success')

        return jsonify({
            'code': 0,
            'msg': '登录成功',
            'data': {'token': token, 'username': username, 'role': row['role']}
        })

    @app.route('/api/logout', methods=['POST'])
    @require_auth
    def logout():
        """退出登录"""
        admin = request.admin
        try:
            db.execute(
                "UPDATE admin_users SET token = NULL, token_created_at = NULL WHERE id = %s",
                (admin['id'],)
            )
        except Exception as e:
            logger.error(f"清除 token 失败: {e}")
        audit_log(admin['id'], admin['username'], 'logout')
        return jsonify({'code': 0, 'msg': '已退出'})

    @app.route('/api/me', methods=['GET'])
    @require_auth
    def me():
        """获取当前登录信息"""
        return jsonify({'code': 0, 'data': request.admin})

    @app.route('/api/change_password', methods=['POST'])
    @require_auth
    def change_password():
        """修改密码"""
        data = request.get_json(silent=True) or {}
        old_pwd = data.get('old_password', '')
        new_pwd = data.get('new_password', '')

        if not old_pwd or not new_pwd:
            return jsonify({'code': 400, 'msg': '旧密码和新密码不能为空'}), 400
        if len(new_pwd) < 6:
            return jsonify({'code': 400, 'msg': '新密码至少6位'}), 400

        admin = request.admin
        row = db.query_one("SELECT password_hash FROM admin_users WHERE id = %s", (admin['id'],))

        try:
            stored = row['password_hash']
            if isinstance(stored, str):
                stored = stored.encode('utf-8')
            if not bcrypt.checkpw(old_pwd.encode('utf-8'), stored):
                return jsonify({'code': 401, 'msg': '旧密码错误'}), 401
        except Exception:
            return jsonify({'code': 500, 'msg': '密码验证失败'}), 500

        new_hash = bcrypt.hashpw(new_pwd.encode('utf-8'), bcrypt.gensalt(12)).decode('utf-8')
        db.execute("UPDATE admin_users SET password_hash = %s WHERE id = %s", (new_hash, admin['id']))
        audit_log(admin['id'], admin['username'], 'change_password', result='success')

        return jsonify({'code': 0, 'msg': '密码已修改'})

    # ---- 仪表盘 ----

    @app.route('/api/dashboard', methods=['GET'])
    @require_auth
    def dashboard():
        """仪表盘数据"""
        data = {}

        # 插件统计
        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM plugins WHERE is_active = 1")
            data['plugins_active'] = row['cnt'] if row else 0
            row = db.query_one("SELECT COUNT(*) as cnt FROM plugins")
            data['plugins_total'] = row['cnt'] if row else 0
        except Exception:
            data['plugins_active'] = 0
            data['plugins_total'] = 0

        # 命令统计
        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM commands")
            data['commands_total'] = row['cnt'] if row else 0
            row = db.query_one("SELECT COUNT(*) as cnt FROM dynamic_commands WHERE is_active = 1")
            data['dynamic_commands'] = row['cnt'] if row else 0
        except Exception:
            data['commands_total'] = 0
            data['dynamic_commands'] = 0

        # 用户/群统计
        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM users")
            data['users_total'] = row['cnt'] if row else 0
            row = db.query_one("SELECT COUNT(*) as cnt FROM groups_info WHERE is_active = 1")
            data['groups_active'] = row['cnt'] if row else 0
        except Exception:
            data['users_total'] = 0
            data['groups_active'] = 0

        # OneBot 连接状态
        data['bots'] = framework.ws_server.get_connected_bots()
        data['ws_port'] = framework.config.get('onebot', {}).get('listen_port', 6830)

        # 定时任务
        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM tasks WHERE is_active = 1")
            data['tasks_active'] = row['cnt'] if row else 0
        except Exception:
            data['tasks_active'] = 0

        # 框架信息
        data['framework_name'] = 'ZCBOT'
        data['framework_version'] = '1.0.0'
        data['github_repo'] = 'https://github.com/kuangxing6367/zcbot'

        return jsonify({'code': 0, 'data': data})

    @app.route('/api/dashboard/cards', methods=['GET'])
    @require_auth
    def dashboard_cards():
        """获取仪表盘插件卡片"""
        try:
            cards = framework.plugin_loader.get_dashboard_cards()
            return jsonify({'code': 0, 'data': cards})
        except Exception as e:
            logger.error(f"获取仪表盘卡片失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 群级插件开关 ----

    @app.route('/api/plugins/group-settings', methods=['GET'])
    @require_auth
    def list_group_plugin_settings():
        """获取所有群级插件开关设置"""
        try:
            rows = framework.plugin_loader.get_group_plugin_settings()
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            logger.error(f"获取群级插件设置失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/group/<int:group_id>/toggle', methods=['POST'])
    @require_auth
    def toggle_group_plugin(plugin_name, group_id):
        """启用/禁用插件在指定群的状态"""
        admin = request.admin
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        data = request.get_json(silent=True) or {}
        enabled = data.get('enabled', True)

        try:
            framework.plugin_loader.set_group_plugin_enabled(plugin_name, group_id, enabled)
            action = 'enable_group_plugin' if enabled else 'disable_group_plugin'
            audit_log(admin['id'], admin['username'], action, 'plugin', plugin_name,
                      {'group_id': group_id})
            return jsonify({
                'code': 0,
                'msg': f"插件 [{plugin_name}] 在群 {group_id} 已{'启用' if enabled else '禁用'}"
            })
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

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
            # 补充运行时状态
            loaded = framework.plugin_loader.get_loaded_plugins()
            for r in rows:
                r['is_loaded'] = r['plugin_name'] in loaded
                r['has_readme'] = os.path.isfile(
                    os.path.join(framework.plugin_loader.plugins_dat_dir, r['plugin_name'], 'README.md')
                )
                # 补充 plugin.yaml 信息
                yaml_data = framework.plugin_loader.read_plugin_yaml(r['plugin_name'])
                r['has_yaml'] = bool(yaml_data)
                r['has_github'] = bool(yaml_data.get('github', {}).get('repo'))
                r['github_repo'] = yaml_data.get('github', {}).get('repo', '')
                r['config_items'] = yaml_data.get('config', [])
                # 补充 _conf_schema.json 信息
                schema = framework.plugin_loader.read_config_schema(r['plugin_name'])
                r['has_schema'] = bool(schema)
                # 补充依赖检查信息
                dep_info = framework.plugin_loader.get_dep_status(r['plugin_name'])
                r['has_missing_deps'] = dep_info['has_missing']
                r['missing_deps'] = dep_info['missing']
                r['has_conflict'] = dep_info['has_conflict']
                r['conflicts'] = dep_info['conflicts']
                # 补充命令/任务计数
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

        # 安全检查文件名
        filename = os.path.basename(file.filename)
        plugin_name = filename[:-4]  # 去掉 .zip

        # 验证插件名合法性
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '插件名只能包含字母、数字、下划线、横杠'}), 400

        target_dir = os.path.join(plugins_dir, plugin_name)

        try:
            # 读取 ZIP 内容到内存
            file_stream = io.BytesIO(file.read())

            # 验证 ZIP 结构：必须包含 main.py
            with zipfile.ZipFile(file_stream, 'r') as zf:
                names = zf.namelist()
                # 检查是否有 main.py（可能在子目录中）
                has_main = any(n.endswith('main.py') for n in names)
                if not has_main:
                    return jsonify({'code': 400, 'msg': 'ZIP 包中未找到 main.py'}), 400

                # 检查路径穿越攻击
                for name in names:
                    if '..' in name or name.startswith('/'):
                        return jsonify({'code': 400, 'msg': f'非法路径: {name}'}), 400

                # 备份旧插件（如果存在）
                if os.path.isdir(target_dir):
                    backup_dir = target_dir + f'.bak.{int(time.time())}'
                    shutil.move(target_dir, backup_dir)

                os.makedirs(target_dir, exist_ok=True)

                # 解压到目标目录
                zf.extractall(target_dir)

                # 如果解压后多了一层目录，提上来
                entries = os.listdir(target_dir)
                if len(entries) == 1 and os.path.isdir(os.path.join(target_dir, entries[0])):
                    inner = os.path.join(target_dir, entries[0])
                    for item in os.listdir(inner):
                        shutil.move(os.path.join(inner, item), os.path.join(target_dir, item))
                    os.rmdir(inner)

            file_stream.close()

            # 分离配置文件到 plugins_dat（代码留 plugins，配置留 plugins_dat）
            framework.plugin_loader.split_installed_files(plugin_name)

            # 尝试加载插件
            if framework.plugin_loader.load_plugin(plugin_name):
                framework.plugin_loader.register_commands(plugin_name)
                dep_info = framework.plugin_loader.get_missing_deps(plugin_name)
                msg = f'插件 [{plugin_name}] 上传并加载成功'
                if dep_info['has_missing']:
                    msg += f'，但缺少依赖: {", ".join(dep_info["missing"])}'
                audit_log(admin['id'], admin['username'], 'upload_plugin',
                          'plugin', plugin_name, {'filename': filename}, 'success')
                return jsonify({'code': 0, 'msg': msg})
            else:
                # 检查是否因依赖缺失导致加载失败
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

        # 安全检查
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            # 先卸载
            framework.plugin_loader.unload_plugin(plugin_name)
            # 重新加载
            if framework.plugin_loader.load_plugin(plugin_name):
                framework.plugin_loader.register_commands(plugin_name)
                # 清空路由缓存
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

            # 清空路由缓存
            framework.router._invalidate_cache()

            return jsonify({'code': 0, 'msg': f'插件已{"启用" if is_active else "禁用"}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>', methods=['DELETE'])
    @require_auth
    def delete_plugin(plugin_name):
        """删除插件"""
        admin = request.admin

        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        target_dir = os.path.join(plugins_dir, plugin_name)
        dat_dir = os.path.join(framework.plugin_loader.plugins_dat_dir, plugin_name)
        try:
            # 卸载
            framework.plugin_loader.unload_plugin(plugin_name)
            # 删除代码目录
            if os.path.isdir(target_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
            # 删除数据/配置目录
            if os.path.isdir(dat_dir):
                shutil.rmtree(dat_dir, ignore_errors=True)
            # 删除数据库记录
            db.execute("DELETE FROM plugins WHERE plugin_name = %s", (plugin_name,))
            db.execute("DELETE FROM commands WHERE plugin_name = %s", (plugin_name,))
            db.execute("DELETE FROM tasks WHERE plugin_name = %s", (plugin_name,))
            db.execute("DELETE FROM plugin_configs WHERE plugin_name = %s", (plugin_name,))

            audit_log(admin['id'], admin['username'], 'delete_plugin', 'plugin', plugin_name)
            return jsonify({'code': 0, 'msg': f'插件 [{plugin_name}] 已删除'})
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
            # 先确保插件已卸载，避免删除正在使用的 venv
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
            # 读取 plugin.yaml
            yaml_data = framework.plugin_loader.read_plugin_yaml(plugin_name)
            # 获取配置文件列表
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
                    # 补充依赖安装状态
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
        # 安全检查
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
        """检查插件是否有 GitHub 更新"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            yaml_data = framework.plugin_loader.read_plugin_yaml(plugin_name)
            github = yaml_data.get('github', {})
            repo = github.get('repo', '')

            if not repo:
                return jsonify({'code': 400, 'msg': '该插件未配置 GitHub 更新源（plugin.yaml 中缺少 github.repo）'}), 400

            branch = github.get('branch', 'main')

            # 规范化 repo 地址
            if repo.startswith('https://github.com/'):
                repo = repo.replace('https://github.com/', '').rstrip('/')
            elif repo.startswith('http://github.com/'):
                repo = repo.replace('http://github.com/', '').rstrip('/')

            # 查询 GitHub API 获取最新 commit
            api_url = f"https://api.github.com/repos/{repo}/commits/{branch}"
            headers = {'Accept': 'application/vnd.github.v3+json'}
            resp = requests.get(api_url, headers=headers, timeout=15)

            if resp.status_code == 404:
                return jsonify({'code': 404, 'msg': f'GitHub 仓库或分支不存在: {repo}@{branch}'}), 404
            if resp.status_code != 200:
                return jsonify({'code': 500, 'msg': f'GitHub API 返回 {resp.status_code}'}), 500

            data = resp.json()
            latest_sha = data.get('sha', '')[:7]
            commit_msg = data.get('commit', {}).get('message', '').split('\n')[0]
            commit_date = data.get('commit', {}).get('author', {}).get('date', '')
            author = data.get('commit', {}).get('author', {}).get('name', '')

            # 当前版本
            current_version = yaml_data.get('version', 'unknown')

            return jsonify({
                'code': 0,
                'data': {
                    'plugin_name': plugin_name,
                    'repo': repo,
                    'branch': branch,
                    'current_version': current_version,
                    'latest_commit': latest_sha,
                    'commit_message': commit_msg,
                    'commit_date': commit_date,
                    'author': author,
                    'has_update': True,  # 简化：总是允许更新
                }
            })
        except requests.exceptions.Timeout:
            return jsonify({'code': 500, 'msg': 'GitHub API 请求超时'}), 500
        except Exception as e:
            logger.error(f"检查更新失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

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

            # 规范化 repo
            if repo.startswith('https://github.com/'):
                repo = repo.replace('https://github.com/', '').rstrip('/')
            elif repo.startswith('http://github.com/'):
                repo = repo.replace('http://github.com/', '').rstrip('/')

            # 下载 ZIP
            zip_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
            logger.info(f"正在从 GitHub 下载插件: {zip_url}")
            resp = requests.get(zip_url, timeout=60, stream=True)

            if resp.status_code == 404:
                return jsonify({'code': 404, 'msg': f'仓库或分支不存在: {repo}@{branch}'}), 404
            if resp.status_code != 200:
                return jsonify({'code': 500, 'msg': f'下载失败: HTTP {resp.status_code}'}), 500

            # 保存到临时文件
            tmp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
            for chunk in resp.iter_content(chunk_size=8192):
                tmp_zip.write(chunk)
            tmp_zip.close()

            target_dir = os.path.join(plugins_dir, plugin_name)

            # 卸载当前插件
            framework.plugin_loader.unload_plugin(plugin_name)

            # 备份旧代码
            if os.path.isdir(target_dir):
                backup_dir = target_dir + f'.bak.{int(time.time())}'
                shutil.move(target_dir, backup_dir)

            os.makedirs(target_dir, exist_ok=True)

            # 解压
            with zipfile.ZipFile(tmp_zip.name, 'r') as zf:
                # GitHub ZIP 内会有一层 repo-branch/ 目录
                names = zf.namelist()
                # 找到前缀目录名
                prefix = names[0].split('/')[0] if names else ''

                for name in names:
                    if name.endswith('/'):
                        continue
                    # 去掉前缀目录
                    if prefix and name.startswith(prefix + '/'):
                        rel_path = name[len(prefix) + 1:]
                    else:
                        rel_path = name

                    if not rel_path:
                        continue

                    # 如果配置了子目录，只提取该目录下的文件
                    if sub_path and sub_path != '/':
                        sp = sub_path.lstrip('/')
                        if not rel_path.startswith(sp + '/') and rel_path != sp:
                            continue
                        rel_path = rel_path[len(sp) + 1:] if rel_path.startswith(sp + '/') else rel_path

                    if not rel_path:
                        continue

                    # 安全检查
                    if '..' in rel_path or rel_path.startswith('/'):
                        continue

                    dest = os.path.join(target_dir, rel_path)
                    os.makedirs(os.path.dirname(dest), exist_ok=True) if os.path.dirname(dest) else None
                    with open(dest, 'wb') as f:
                        f.write(zf.read(name))

            # 清理临时文件
            os.unlink(tmp_zip.name)

            # 分离配置文件到 plugins_dat（保留用户已有配置不被覆盖）
            framework.plugin_loader.split_installed_files(plugin_name)

            # 重新加载插件
            if framework.plugin_loader.load_plugin(plugin_name):
                framework.plugin_loader.register_commands(plugin_name)
                audit_log(admin['id'], admin['username'], 'update_plugin_github',
                          'plugin', plugin_name, {'repo': repo, 'branch': branch}, 'success')
                return jsonify({'code': 0, 'msg': f'插件 [{plugin_name}] 已从 GitHub 更新并重新加载'})
            else:
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

    # ---- 命令管理 ----

    @app.route('/api/commands', methods=['GET'])
    @require_auth
    def list_commands():
        """获取静态命令列表（包含别名、描述、启停状态、权限要求）"""
        try:
            rows = db.query(
                "SELECT id, plugin_name, pattern, alias, description, priority, handler, "
                "is_dynamic, is_active, hit_count, require_level, created_at "
                "FROM commands ORDER BY plugin_name, priority ASC, created_at ASC"
            )
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/commands/dynamic', methods=['GET'])
    @require_auth
    def list_dynamic_commands():
        """
        获取动态命令列表（插件注册的 dynamic=True 命令，只读展示）
        动态命令由插件通过 ctx.command(dynamic=True) 注册，存储在 commands 表
        """
        try:
            rows = db.query(
                "SELECT id, plugin_name, pattern, alias, description, priority, handler, "
                "is_dynamic, is_active, hit_count, created_at "
                "FROM commands WHERE is_dynamic = 1 "
                "ORDER BY plugin_name, priority ASC, created_at ASC"
            )
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 插件配置读写 API（参考 AstrBot _conf_schema.json 配置系统）----

    @app.route('/api/plugins/<plugin_name>/config_schema', methods=['GET'])
    @require_auth
    def get_plugin_config_schema(plugin_name):
        """获取插件的配置 schema 和当前配置值"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            # 读取 _conf_schema.json
            schema = framework.plugin_loader.read_config_schema(plugin_name)
            # 读取当前配置值
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
            # 获取 schema 用于校验
            schema = framework.plugin_loader.read_config_schema(plugin_name)

            updated_keys = []
            for key, value in data.items():
                # 校验 key 是否在 schema 中
                if schema and key not in schema:
                    continue

                # 类型转换校验
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
                # UPSERT
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

    # ---- 静态命令管理（别名/启停）----

    @app.route('/api/commands/<int:cmd_id>/alias', methods=['PUT'])
    @require_auth
    def update_command_alias(cmd_id):
        """更新静态命令的别名/描述/权限（参考 AstrBot CommandConfig）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        alias = data.get('alias', '').strip()
        description = data.get('description', '').strip()
        require_level = data.get('require_level', '').strip()

        # 校验权限等级
        if require_level and require_level not in ('', 'admin', 'super'):
            return jsonify({'code': 400, 'msg': '无效的权限等级（允许: admin/super）'}), 400

        try:
            row = db.query_one("SELECT id, plugin_name, handler FROM commands WHERE id = %s", (cmd_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '命令不存在'}), 404

            db.execute(
                "UPDATE commands SET alias = %s, description = %s, require_level = %s WHERE id = %s",
                (alias if alias else None, description if description else None,
                 require_level, cmd_id)
            )
            audit_log(admin['id'], admin['username'], 'update_command_alias',
                      'command', str(cmd_id),
                      {'plugin': row['plugin_name'], 'handler': row['handler'],
                       'alias': alias, 'description': description, 'require_level': require_level})
            return jsonify({'code': 0, 'msg': '命令已更新'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/commands/<int:cmd_id>/toggle', methods=['POST'])
    @require_auth
    def toggle_static_command(cmd_id):
        """启用/禁用静态命令"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        is_active = 1 if data.get('is_active') else 0

        try:
            row = db.query_one("SELECT id, plugin_name, handler FROM commands WHERE id = %s", (cmd_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '命令不存在'}), 404

            db.execute("UPDATE commands SET is_active = %s WHERE id = %s", (is_active, cmd_id))
            action = 'enable' if is_active else 'disable'
            audit_log(admin['id'], admin['username'], f'{action}_static_command',
                      'command', str(cmd_id),
                      {'plugin': row['plugin_name'], 'handler': row['handler']})
            return jsonify({'code': 0, 'msg': f'已{"启用" if is_active else "禁用"}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 插件命令查看（插件配置弹窗展示触发命令）----

    @app.route('/api/plugins/<plugin_name>/commands', methods=['GET'])
    @require_auth
    def get_plugin_commands(plugin_name):
        """获取指定插件注册的所有命令（参考 AstrBot 插件详情组件展示）"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            # 静态命令（is_dynamic=0）
            static_cmds = db.query(
                "SELECT id, pattern, alias, description, priority, handler, "
                "is_active, hit_count, require_level, created_at FROM commands "
                "WHERE plugin_name = %s AND is_dynamic = 0 "
                "ORDER BY priority ASC, created_at ASC",
                (plugin_name,)
            )

            # 动态命令（is_dynamic=1，插件以 dynamic=True 注册）
            dynamic_cmds = db.query(
                "SELECT id, pattern, alias, description, priority, handler, "
                "is_active, hit_count, created_at FROM commands "
                "WHERE plugin_name = %s AND is_dynamic = 1 "
                "ORDER BY priority ASC, created_at ASC",
                (plugin_name,)
            )

            # 定时任务
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

    # ---- 用户/群管理 ----

    @app.route('/api/users', methods=['GET'])
    @require_auth
    def list_users():
        """获取用户列表（含角色信息）"""
        page = int(request.args.get('page', 1))
        size = min(int(request.args.get('size', 50)), 200)
        offset = (page - 1) * size
        keyword = request.args.get('keyword', '').strip()

        try:
            if keyword:
                rows = db.query(
                    "SELECT id, user_id, nickname, is_friend, is_blacklist, remark, role, "
                    "first_seen_at, last_active_at FROM users "
                    "WHERE nickname LIKE %s OR user_id LIKE %s OR remark LIKE %s "
                    "ORDER BY last_active_at DESC LIMIT %s OFFSET %s",
                    (f'%{keyword}%', f'%{keyword}%', f'%{keyword}%', size, offset)
                )
                total_row = db.query_one(
                    "SELECT COUNT(*) as cnt FROM users "
                    "WHERE nickname LIKE %s OR user_id LIKE %s OR remark LIKE %s",
                    (f'%{keyword}%', f'%{keyword}%', f'%{keyword}%')
                )
            else:
                rows = db.query(
                    "SELECT id, user_id, nickname, is_friend, is_blacklist, remark, role, "
                    "first_seen_at, last_active_at FROM users "
                    "ORDER BY last_active_at DESC LIMIT %s OFFSET %s",
                    (size, offset)
                )
                total_row = db.query_one("SELECT COUNT(*) as cnt FROM users")

            return jsonify({
                'code': 0,
                'data': rows,
                'total': total_row['cnt'] if total_row else 0,
                'page': page,
                'size': size
            })
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/users/<int:user_id>/role', methods=['PUT'])
    @require_super
    def set_user_role(user_id):
        """设置用户角色（仅超级管理员）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        role = data.get('role', '').strip()

        if role not in ('super', ''):
            return jsonify({'code': 400, 'msg': '角色值无效，仅支持 super 或空字符串'}), 400

        try:
            row = db.query_one("SELECT id, nickname FROM users WHERE user_id = %s", (user_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '用户不存在'}), 404

            db.execute("UPDATE users SET role = %s WHERE user_id = %s", (role, user_id))
            label = '超级管理员' if role == 'super' else '普通用户'
            audit_log(admin['id'], admin['username'], 'set_user_role', 'user', str(user_id),
                      {'role': role, 'nickname': row['nickname']})
            return jsonify({'code': 0, 'msg': f'用户 [{row["nickname"]}] 角色已设为 {label}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/users/<int:user_id>/blacklist', methods=['POST'])
    @require_auth
    def toggle_user_blacklist(user_id):
        """拉黑/取消拉黑用户"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        is_blacklist = 1 if data.get('is_blacklist') else 0

        try:
            db.execute("UPDATE users SET is_blacklist = %s WHERE user_id = %s", (is_blacklist, user_id))
            action = 'blacklist_user' if is_blacklist else 'unblacklist_user'
            audit_log(admin['id'], admin['username'], action, 'user', str(user_id))
            return jsonify({'code': 0, 'msg': f'已{"拉黑" if is_blacklist else "取消拉黑"}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/groups', methods=['GET'])
    @require_auth
    def list_groups():
        """获取群列表"""
        try:
            rows = db.query(
                "SELECT id, group_id, group_name, member_count, max_member_count, "
                "is_active, is_blacklist, join_at FROM groups_info ORDER BY is_active DESC, group_id ASC"
            )
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/groups/<int:group_id>/blacklist', methods=['POST'])
    @require_auth
    def toggle_group_blacklist(group_id):
        """拉黑/取消拉黑群"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        is_blacklist = 1 if data.get('is_blacklist') else 0

        try:
            db.execute("UPDATE groups_info SET is_blacklist = %s WHERE group_id = %s", (is_blacklist, group_id))
            action = 'blacklist_group' if is_blacklist else 'unblacklist_group'
            audit_log(admin['id'], admin['username'], action, 'group', str(group_id))
            return jsonify({'code': 0, 'msg': f'已{"拉黑" if is_blacklist else "取消拉黑"}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 定时任务 ----

    @app.route('/api/tasks', methods=['GET'])
    @require_auth
    def list_tasks():
        """获取定时任务列表"""
        try:
            rows = db.query(
                "SELECT id, plugin_name, cron_expression, handler, description, "
                "is_active, last_run_at, next_run_at, run_count, last_status, created_at "
                "FROM tasks ORDER BY plugin_name, id ASC"
            )
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/tasks/<int:task_id>/toggle', methods=['POST'])
    @require_auth
    def toggle_task(task_id):
        """启用/禁用定时任务"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        is_active = 1 if data.get('is_active') else 0

        try:
            row = db.query_one("SELECT id, plugin_name, handler FROM tasks WHERE id = %s", (task_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '任务不存在'}), 404

            db.execute("UPDATE tasks SET is_active = %s WHERE id = %s", (is_active, task_id))

            # 通过调度器实际启停
            scheduler = framework.scheduler
            if scheduler:
                task_key = f"plugin_{row['plugin_name']}_{task_id}"
                if is_active:
                    scheduler.resume_task(task_key)
                else:
                    scheduler.pause_task(task_key)

            action = 'enable_task' if is_active else 'disable_task'
            audit_log(admin['id'], admin['username'], action, 'task', str(task_id),
                      {'plugin': row['plugin_name'], 'handler': row['handler']})
            return jsonify({'code': 0, 'msg': f'已{"启用" if is_active else "禁用"}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/tasks/<int:task_id>/trigger', methods=['POST'])
    @require_auth
    def trigger_task(task_id):
        """手动触发定时任务立即执行"""
        admin = request.admin

        try:
            row = db.query_one(
                "SELECT id, plugin_name, handler, is_active FROM tasks WHERE id = %s",
                (task_id,)
            )
            if not row:
                return jsonify({'code': 404, 'msg': '任务不存在'}), 404

            # 获取插件模块的 handler 函数
            module = framework.plugin_loader.get_plugin_module(row['plugin_name'])
            if module is None:
                return jsonify({'code': 500, 'msg': f'插件 [{row["plugin_name"]}] 未加载'}), 500

            handler = getattr(module, row['handler'], None)
            if handler is None or not callable(handler):
                return jsonify({'code': 500, 'msg': f'处理函数 {row["handler"]} 不存在'}), 500

            # 执行任务
            try:
                handler()
                db.execute(
                    "UPDATE tasks SET last_run_at=NOW(), run_count=run_count+1, last_status='success' WHERE id=%s",
                    (task_id,)
                )
            except Exception as e:
                db.execute(
                    "UPDATE tasks SET last_run_at=NOW(), run_count=run_count+1, last_status='failure' WHERE id=%s",
                    (task_id,)
                )
                return jsonify({'code': 500, 'msg': f'执行失败: {e}'}), 500

            audit_log(admin['id'], admin['username'], 'trigger_task', 'task', str(task_id),
                      {'plugin': row['plugin_name'], 'handler': row['handler']})
            return jsonify({'code': 0, 'msg': '任务已触发'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/tasks', methods=['POST'])
    @require_auth
    def create_task():
        """创建自定义定时任务"""
        admin = request.admin
        data = request.get_json(silent=True) or {}

        cron_expression = (data.get('cron_expression') or '').strip()
        description = (data.get('description') or '').strip()
        handler_name = (data.get('handler') or '').strip()

        if not cron_expression:
            return jsonify({'code': 400, 'msg': 'Cron 表达式不能为空'}), 400
        if not description:
            return jsonify({'code': 400, 'msg': '任务描述不能为空'}), 400

        parts = cron_expression.split()
        if len(parts) != 5:
            return jsonify({'code': 400, 'msg': 'Cron 表达式必须为 5 段格式（分 时 日 月 周）'}), 400

        try:
            task_id = db.insert(
                "INSERT INTO tasks (plugin_name, cron_expression, handler, description, is_active) "
                "VALUES (%s, %s, %s, %s, 1)",
                ('__web__', cron_expression, handler_name or 'custom_task', description)
            )
            audit_log(admin['id'], admin['username'], 'create_task', 'task', str(task_id),
                      {'cron': cron_expression, 'description': description})
            return jsonify({'code': 0, 'msg': '任务已创建', 'data': {'id': task_id}})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/tasks/<int:task_id>', methods=['DELETE'])
    @require_auth
    def delete_task(task_id):
        """删除定时任务"""
        admin = request.admin

        try:
            row = db.query_one("SELECT id, plugin_name, description FROM tasks WHERE id = %s", (task_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '任务不存在'}), 404

            # 插件创建的任务给出提示
            plugin_name = row['plugin_name']
            if plugin_name != '__web__':
                return jsonify({
                    'code': 400,
                    'msg': f'该任务由插件 [{plugin_name}] 注册，请在插件管理页卸载插件或联系插件开发者'
                }), 400

            # 从调度器移除
            scheduler = framework.scheduler
            if scheduler:
                task_key = f"plugin_{plugin_name}_{task_id}"
                try:
                    scheduler.pause_task(task_key)
                except Exception:
                    pass

            db.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
            audit_log(admin['id'], admin['username'], 'delete_task', 'task', str(task_id),
                      {'plugin': plugin_name, 'description': row['description']})
            return jsonify({'code': 0, 'msg': '任务已删除'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 审计日志 ----

    @app.route('/api/audit_logs', methods=['GET'])
    @require_auth
    def list_audit_logs():
        """获取审计日志"""
        page = int(request.args.get('page', 1))
        size = min(int(request.args.get('size', 50)), 200)
        offset = (page - 1) * size

        try:
            rows = db.query(
                "SELECT id, admin_id, admin_name, action, target_type, target_name, "
                "detail, ip_address, result, error_message, created_at "
                "FROM audit_logs ORDER BY id DESC LIMIT %s OFFSET %s",
                (size, offset)
            )
            total_row = db.query_one("SELECT COUNT(*) as cnt FROM audit_logs")
            return jsonify({
                'code': 0,
                'data': rows,
                'total': total_row['cnt'] if total_row else 0,
                'page': page,
                'size': size
            })
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 运行日志（消息/连接/插件/框架，支持 SSE 实时推送）----

    @app.route('/api/runtime_logs', methods=['GET'])
    @require_auth
    def get_runtime_logs():
        """获取运行日志（支持分类/级别/关键词过滤和增量轮询）"""
        category = request.args.get('category', '')  # message|connection|plugin|system|framework
        level = request.args.get('level', '')  # DEBUG|INFO|WARN|ERROR
        keyword = request.args.get('keyword', '')
        limit = min(int(request.args.get('limit', 100)), 500)
        after_seq = int(request.args.get('after_seq', 0))

        try:
            logs = log_broker.get_logs(
                category=category or None,
                level=level or None,
                keyword=keyword or None,
                limit=limit,
                after_seq=after_seq,
            )
            return jsonify({
                'code': 0,
                'data': logs,
                'latest_seq': log_broker.get_stats().get('latest_seq', 0),
            })
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/runtime_logs/stats', methods=['GET'])
    @require_auth
    def get_runtime_log_stats():
        """获取运行日志统计"""
        try:
            return jsonify({'code': 0, 'data': log_broker.get_stats()})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/runtime_logs/clear', methods=['POST'])
    @require_auth
    def clear_runtime_logs():
        """清空运行日志"""
        admin = request.admin
        try:
            log_broker.clear()
            audit_log(admin['id'], admin['username'],
                      'clear_runtime_logs', 'logs', None, None, 'success', None)
            return jsonify({'code': 0, 'msg': '已清空'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/logs/sse')
    def logs_sse():
        """SSE 实时日志推送端点
        支持 token 认证方式：
        1. Authorization Header（普通 fetch）
        2. ?token=xxx 查询参数（EventSource 无法传自定义 Header）
        """
        # 从查询参数取 token（兼容 EventSource）
        token = _extract_token(request) or request.args.get('token', '')
        if not token:
            return jsonify({'code': 401, 'msg': '未提供认证令牌'}), 401
        admin = _verify_token(token)
        if not admin:
            return jsonify({'code': 401, 'msg': '令牌无效或已过期'}), 401

        def generate():
            q = log_broker.subscribe()
            try:
                # 先发送缓存中的历史日志（最近 50 条）
                history = log_broker.get_logs(limit=50)
                for entry in history:
                    data = json.dumps(entry, ensure_ascii=False)
                    yield f"id: {entry['seq']}\ndata: {data}\n\n"

                # 持续推送新日志
                while True:
                    try:
                        entry = q.get(timeout=30)
                        data = json.dumps(entry, ensure_ascii=False)
                        yield f"id: {entry['seq']}\ndata: {data}\n\n"
                    except queue.Empty:
                        # 发送心跳保持连接
                        yield ": heartbeat\n\n"
            finally:
                log_broker.unsubscribe(q)

        return Response(
            generate(),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no',
            }
        )

    # ---- 系统配置 ----

    @app.route('/api/config', methods=['GET'])
    @require_auth
    def list_config():
        """获取系统配置"""
        try:
            rows = db.query("SELECT config_key, config_value, description, updated_by FROM system_config")
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/config/<key>', methods=['PUT'])
    @require_super
    def update_config(key):
        """更新系统配置（仅超级管理员）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        value = data.get('value')

        if value is None:
            return jsonify({'code': 400, 'msg': '缺少 value'}), 400

        try:
            db.execute(
                "UPDATE system_config SET config_value = %s, updated_by = %s WHERE config_key = %s",
                (json.dumps(value, ensure_ascii=False), admin['username'], key)
            )
            audit_log(admin['id'], admin['username'], 'update_config', 'config', key, {'value': value})
            return jsonify({'code': 0, 'msg': '配置已更新'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 管理员管理（仅超级管理员）----

    @app.route('/api/admins', methods=['GET'])
    @require_super
    def list_admins():
        """获取管理员列表"""
        try:
            rows = db.query(
                "SELECT id, username, role, is_active, last_login_at, last_login_ip, created_at "
                "FROM admin_users ORDER BY id ASC"
            )
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/admins', methods=['POST'])
    @require_super
    def add_admin():
        """添加管理员"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        username = data.get('username', '').strip()
        password = data.get('password', '')
        role = data.get('role', 'admin')

        if not username or not password:
            return jsonify({'code': 400, 'msg': '用户名和密码不能为空'}), 400
        if role not in ('super', 'admin'):
            return jsonify({'code': 400, 'msg': '角色无效'}), 400
        if len(password) < 6:
            return jsonify({'code': 400, 'msg': '密码至少6位'}), 400

        try:
            existing = db.query_one("SELECT id FROM admin_users WHERE username = %s", (username,))
            if existing:
                return jsonify({'code': 409, 'msg': '用户名已存在'}), 409

            pwd_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(12)).decode('utf-8')
            db.execute(
                "INSERT INTO admin_users (username, password_hash, role) VALUES (%s, %s, %s)",
                (username, pwd_hash, role)
            )
            audit_log(admin['id'], admin['username'], 'add_admin', 'admin', username, {'role': role})
            return jsonify({'code': 0, 'msg': f'管理员 [{username}] 已添加'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/admins/<int:admin_id>', methods=['DELETE'])
    @require_super
    def delete_admin(admin_id):
        """删除管理员"""
        admin = request.admin
        if admin_id == admin['id']:
            return jsonify({'code': 400, 'msg': '不能删除自己'}), 400

        try:
            row = db.query_one("SELECT username FROM admin_users WHERE id = %s", (admin_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '管理员不存在'}), 404

            db.execute("DELETE FROM admin_users WHERE id = %s", (admin_id,))
            audit_log(admin['id'], admin['username'], 'delete_admin', 'admin', row['username'])
            return jsonify({'code': 0, 'msg': '已删除'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 框架操作 ----

    @app.route('/api/restart', methods=['POST'])
    @require_auth
    def restart_framework():
        """重启框架：用 os.execv 原地替换进程，不依赖外部进程管理器"""
        admin = request.admin
        audit_log(admin['id'], admin['username'], 'restart_framework', 'system', 'framework')

        # 异步停止并重启，先返回响应
        def _do_restart():
            import time
            time.sleep(1)
            try:
                framework.stop()
            except Exception:
                pass
            # os.execv 用当前 Python 解释器重载 main.py，原地替换进程
            python = sys.executable
            main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'main.py')
            os.chdir(os.path.dirname(main_py))
            os.execv(python, [python, main_py] + sys.argv[1:])

        threading.Thread(target=_do_restart, daemon=False).start()
        return jsonify({'code': 0, 'msg': '框架正在重启...'})

    # ---- 插件 WebUI 内嵌 ----

    @app.route('/api/plugin_webuis', methods=['GET'])
    @require_auth
    def list_plugin_webuis():
        """获取所有已注册的插件 WebUI 列表"""
        webuis = framework.plugin_loader.get_plugin_webuis()
        return jsonify({'code': 0, 'data': webuis})

    @app.route('/api/plugin_webui/<plugin_name>', methods=['GET'])
    @require_auth
    def serve_plugin_webui(plugin_name):
        """
        获取插件 WebUI 入口页面
        查询参数 ?entry=xxx.html 可指定入口文件（默认 index.html）
        """
        from flask import request as flask_request
        entry = flask_request.args.get('entry', 'index.html')
        web_dir = framework.plugin_loader.get_plugin_webui_path(plugin_name)
        if not web_dir:
            return jsonify({'code': 404, 'msg': f'插件 {plugin_name} 未提供 WebUI'}), 404
        entry_path = os.path.join(web_dir, entry)
        if not os.path.isfile(entry_path):
            return jsonify({'code': 404, 'msg': f'入口文件 {entry} 不存在'}), 404
        return send_from_directory(web_dir, entry)

    @app.route('/api/plugin_webui/<plugin_name>/assets/<path:filename>', methods=['GET'])
    @require_auth
    def serve_plugin_webui_assets(plugin_name, filename):
        """提供插件 WebUI 的静态资源文件（JS/CSS/图片等）"""
        web_dir = framework.plugin_loader.get_plugin_webui_path(plugin_name)
        if not web_dir:
            return jsonify({'code': 404, 'msg': 'WebUI 目录不存在'}), 404
        file_path = os.path.join(web_dir, filename)
        if not os.path.isfile(file_path):
            return jsonify({'code': 404, 'msg': '文件不存在'}), 404
        return send_from_directory(web_dir, filename)

    # ---- 前端静态文件 ----

    @app.route('/css/<path:filename>')
    def serve_css(filename):
        web_css = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'web', 'css')
        return send_from_directory(web_css, filename)

    @app.route('/js/<path:filename>')
    def serve_js(filename):
        web_js = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'web', 'js')
        return send_from_directory(web_js, filename)

    @app.route('/<page>.html')
    def serve_page(page):
        """提供 HTML 页面"""
        web_static = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'web')
        html_file = os.path.join(web_static, f'{page}.html')
        if os.path.isfile(html_file):
            return send_from_directory(web_static, f'{page}.html')
        return jsonify({'code': 404, 'msg': '页面不存在'}), 404

    @app.route('/')
    def serve_index():
        """根路径返回 index.html"""
        web_static = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'web')
        return send_from_directory(web_static, 'index.html')

    return app


class WebServer:
    """Web UI 服务器，在独立线程中运行"""

    def __init__(self, framework):
        self.framework = framework
        self.app = create_web_app(framework)
        web_cfg = framework.config.get('web', {})
        self.host = web_cfg.get('host', '0.0.0.0')
        self.port = web_cfg.get('port', 8080)
        self._thread = None
        self._server = None
        self._running = False

    def start(self):
        """启动 Web 服务器"""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="web-server")
        self._thread.start()
        logger.info(f"Web UI 已启动: http://{self.host}:{self.port}")

    def _run(self):
        """运行 Web 服务器"""
        try:
            # 使用 waitress 或 werkzeug 开发服务器
            try:
                from waitress import serve as waitress_serve
                waitress_serve(self.app, host=self.host, port=self.port, threads=8)
            except ImportError:
                # 回退到 Flask 开发服务器
                self.app.run(host=self.host, port=self.port, debug=False, use_reloader=False)
        except Exception as e:
            logger.error(f"Web UI 异常: {e}")

    def stop(self):
        """停止 Web 服务器"""
        self._running = False
        logger.info("Web UI 已停止")
