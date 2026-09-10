# -*- coding: utf-8 -*-
"""
仪表盘 + WebUI 扩展 + 群级插件开关
"""
import logging
import time

from flask import jsonify, request

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    db = ctx.db
    require_auth = ctx.require_auth
    audit_log = ctx.audit_log
    _get_framework_local_version = ctx._get_framework_local_version

    # ---- 仪表盘 ----

    _dashboard_stats_cache = {'data': None, 't': 0}

    def _collect_dashboard_stats() -> dict:
        """仪表盘统计（MySQL COUNT 可能全表扫描，调用方需缓存）"""
        data = {}
        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM plugins WHERE is_active = 1")
            data['plugins_active'] = row['cnt'] if row else 0
            row = db.query_one("SELECT COUNT(*) as cnt FROM plugins")
            data['plugins_total'] = row['cnt'] if row else 0
        except Exception:
            data['plugins_active'] = 0
            data['plugins_total'] = 0

        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM commands")
            data['commands_total'] = row['cnt'] if row else 0
            row = db.query_one("SELECT COUNT(*) as cnt FROM dynamic_commands WHERE is_active = 1")
            data['dynamic_commands'] = row['cnt'] if row else 0
        except Exception:
            data['commands_total'] = 0
            data['dynamic_commands'] = 0

        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM users")
            data['users_total'] = row['cnt'] if row else 0
            row = db.query_one("SELECT COUNT(*) as cnt FROM groups_info WHERE is_active = 1")
            data['groups_active'] = row['cnt'] if row else 0
        except Exception:
            data['users_total'] = 0
            data['groups_active'] = 0

        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM tasks WHERE is_active = 1")
            data['tasks_active'] = row['cnt'] if row else 0
        except Exception:
            data['tasks_active'] = 0
        return data

    @app.route('/api/dashboard', methods=['GET'])
    @require_auth
    def dashboard():
        """仪表盘数据（统计部分 10s 缓存，避免 MySQL COUNT 全表扫描拖慢页面）"""
        data = {}
        now = time.time()
        cached = _dashboard_stats_cache.get('data')
        if cached is not None and (now - _dashboard_stats_cache['t']) < 10:
            data.update(cached)
        else:
            data.update(_collect_dashboard_stats())
            _dashboard_stats_cache['data'] = dict(data)
            _dashboard_stats_cache['t'] = now

        # OneBot 连接状态（实时）
        data['bots'] = framework.ws_server.get_connected_bots()
        data['ws_port'] = framework.config.get('onebot', {}).get('listen_port', 6830)

        # 框架信息
        data['framework_name'] = 'ZCBOT'
        fw_ver = _get_framework_local_version()
        data['framework_version'] = fw_ver or 'unknown'
        data['framework_alpha'] = 'alpha' in fw_ver
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

    # ---- WebUI 群组/用户管理页插件扩展 ----

    @app.route('/api/extensions/groups', methods=['GET'])
    @require_auth
    def ui_ext_groups_meta():
        """群组管理页插件扩展元信息"""
        try:
            return jsonify({'code': 0, 'data': framework.plugin_loader.get_ui_extensions('groups')})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/extensions/users', methods=['GET'])
    @require_auth
    def ui_ext_users_meta():
        """用户管理页插件扩展元信息"""
        try:
            return jsonify({'code': 0, 'data': framework.plugin_loader.get_ui_extensions('users')})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/groups/extensions/data', methods=['POST'])
    @require_auth
    def ui_ext_groups_data():
        """批量获取多个群的扩展数据（当前页渲染用）"""
        try:
            body = request.get_json(silent=True) or {}
            ids = [int(x) for x in (body.get('ids') or []) if str(x).isdigit()]
            out = {}
            for gid in ids:
                out[str(gid)] = framework.plugin_loader.call_ui_extensions('groups', gid)
            return jsonify({'code': 0, 'data': out})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/users/extensions/data', methods=['POST'])
    @require_auth
    def ui_ext_users_data():
        """批量获取多个用户的扩展数据（当前页渲染用）"""
        try:
            body = request.get_json(silent=True) or {}
            ids = [int(x) for x in (body.get('ids') or []) if str(x).isdigit()]
            out = {}
            for uid in ids:
                out[str(uid)] = framework.plugin_loader.call_ui_extensions('users', uid)
            return jsonify({'code': 0, 'data': out})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/groups/<int:gid>/extensions', methods=['GET'])
    @require_auth
    def ui_ext_group_detail(gid):
        """单个群的扩展详情（详情弹窗用，含 column+panel 全量）"""
        try:
            return jsonify({'code': 0, 'data': framework.plugin_loader.call_ui_extensions('groups', gid)})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/users/<int:uid>/extensions', methods=['GET'])
    @require_auth
    def ui_ext_user_detail(uid):
        """单个用户的扩展详情（详情弹窗用）"""
        try:
            return jsonify({'code': 0, 'data': framework.plugin_loader.call_ui_extensions('users', uid)})
        except Exception as e:
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
