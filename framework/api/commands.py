# -*- coding: utf-8 -*-
"""
命令管理：静态/动态命令 + 关键词自动回复（dynamic_commands）
"""
import logging
import re

from flask import jsonify, request

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    db = ctx.db
    require_auth = ctx.require_auth
    audit_log = ctx.audit_log

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

    # ---- 关键词自动回复管理（dynamic_commands 表，系统级动态命令）----

    _KEYWORD_MATCH_TYPES = ('exact', 'prefix', 'contains', 'regex')

    def _refresh_router_keywords():
        """使 router 关键词规则表立即重建（增删改后调用）"""
        try:
            router = getattr(framework, 'router', None)
            if router is not None:
                router._invalidate_cache()
        except Exception:
            pass

    def _validate_keyword_payload(data: dict, partial: bool = False) -> tuple:
        """校验关键词回复参数，返回 (error_msg_or_None, cleaned_dict)"""
        out = {}
        if 'keyword' in data or not partial:
            keyword = (data.get('keyword') or '').strip()
            if not keyword:
                return '触发关键词不能为空', None
            if len(keyword) > 200:
                return '触发关键词过长（最多 200 字符）', None
            out['keyword'] = keyword
        if 'response' in data or not partial:
            response = (data.get('response') or '').strip()
            if not response:
                return '回复内容不能为空', None
            out['response'] = response
        if 'match_type' in data or not partial:
            match_type = (data.get('match_type') or 'exact').strip().lower()
            if match_type not in _KEYWORD_MATCH_TYPES:
                return f"无效的匹配方式（允许: {'/'.join(_KEYWORD_MATCH_TYPES)}）", None
            if match_type == 'regex':
                try:
                    re.compile(out.get('keyword', data.get('keyword') or ''))
                except re.error as e:
                    return f'正则表达式无效: {e}', None
            out['match_type'] = match_type
        if 'plugin_name' in data:
            out['plugin_name'] = (data.get('plugin_name') or 'system').strip()[:50] or 'system'
        if 'handler' in data:
            handler = (data.get('handler') or '').strip()
            if len(handler) > 100:
                return 'handler 过长（最多 100 字符，格式 plugin:func）', None
            if handler and ':' not in handler:
                return 'handler 格式应为 plugin:func', None
            out['handler'] = handler
        if 'is_active' in data:
            out['is_active'] = 1 if data.get('is_active') else 0
        return None, out

    @app.route('/api/dynamic-commands', methods=['GET'])
    @require_auth
    def list_keyword_replies():
        """获取关键词自动回复列表（dynamic_commands 表）"""
        try:
            rows = db.query(
                "SELECT id, keyword, response, match_type, handler, plugin_name, "
                "is_active, hit_count, created_at, updated_at "
                "FROM dynamic_commands ORDER BY id DESC"
            )
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/dynamic-commands', methods=['POST'])
    @require_auth
    def create_keyword_reply():
        """新增关键词自动回复规则"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        err, cleaned = _validate_keyword_payload(data)
        if err:
            return jsonify({'code': 400, 'msg': err}), 400
        try:
            kw_id = db.insert(
                "INSERT INTO dynamic_commands "
                "(keyword, response, match_type, handler, plugin_name, is_active) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (cleaned['keyword'], cleaned['response'], cleaned['match_type'],
                 cleaned.get('handler', ''),
                 cleaned.get('plugin_name', 'system'),
                 cleaned.get('is_active', 1))
            )
            audit_log(admin['id'], admin['username'], 'create_keyword_reply',
                      'dynamic_command', str(kw_id), cleaned)
            _refresh_router_keywords()
            return jsonify({'code': 0, 'msg': '关键词回复已添加', 'data': {'id': kw_id}})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/dynamic-commands/<int:kw_id>', methods=['PUT'])
    @require_auth
    def update_keyword_reply(kw_id):
        """更新关键词自动回复规则"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        err, cleaned = _validate_keyword_payload(data, partial=True)
        if err:
            return jsonify({'code': 400, 'msg': err}), 400
        if not cleaned:
            return jsonify({'code': 400, 'msg': '没有需要更新的字段'}), 400
        try:
            row = db.query_one(
                "SELECT id, keyword FROM dynamic_commands WHERE id = %s", (kw_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '规则不存在'}), 404
            sets = ", ".join(f"{k} = %s" for k in cleaned)
            db.execute(
                f"UPDATE dynamic_commands SET {sets} WHERE id = %s",
                (*cleaned.values(), kw_id)
            )
            audit_log(admin['id'], admin['username'], 'update_keyword_reply',
                      'dynamic_command', str(kw_id), cleaned)
            _refresh_router_keywords()
            return jsonify({'code': 0, 'msg': '关键词回复已更新'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/dynamic-commands/<int:kw_id>/toggle', methods=['POST'])
    @require_auth
    def toggle_keyword_reply(kw_id):
        """启用/禁用关键词自动回复"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        is_active = 1 if data.get('is_active') else 0
        try:
            row = db.query_one(
                "SELECT id, keyword FROM dynamic_commands WHERE id = %s", (kw_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '规则不存在'}), 404
            db.execute(
                "UPDATE dynamic_commands SET is_active = %s WHERE id = %s",
                (is_active, kw_id))
            action = 'enable' if is_active else 'disable'
            audit_log(admin['id'], admin['username'], f'{action}_keyword_reply',
                      'dynamic_command', str(kw_id), {'keyword': row['keyword']})
            _refresh_router_keywords()
            return jsonify({'code': 0, 'msg': f'已{"启用" if is_active else "禁用"}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/dynamic-commands/<int:kw_id>', methods=['DELETE'])
    @require_auth
    def delete_keyword_reply(kw_id):
        """删除关键词自动回复规则"""
        admin = request.admin
        try:
            row = db.query_one(
                "SELECT id, keyword FROM dynamic_commands WHERE id = %s", (kw_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '规则不存在'}), 404
            db.execute("DELETE FROM dynamic_commands WHERE id = %s", (kw_id,))
            audit_log(admin['id'], admin['username'], 'delete_keyword_reply',
                      'dynamic_command', str(kw_id), {'keyword': row['keyword']})
            _refresh_router_keywords()
            return jsonify({'code': 0, 'msg': '已删除'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 静态命令管理（别名/启停）----

    @app.route('/api/commands/<int:cmd_id>/alias', methods=['PUT'])
    @require_auth
    def update_command_alias(cmd_id):
        """更新静态命令的别名/描述/权限"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        alias = data.get('alias', '').strip()
        description = data.get('description', '').strip()
        require_level = data.get('require_level', '').strip()
        require_perm = (data.get('require_perm') or '').strip().lower()
        # 校验权限等级
        if require_level and require_level not in ('', 'admin', 'super'):
            return jsonify({'code': 400, 'msg': '无效的权限等级（允许: admin/super）'}), 400

        try:
            row = db.query_one("SELECT id, plugin_name, handler FROM commands WHERE id = %s", (cmd_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '命令不存在'}), 404

            try:
                db.execute(
                    "UPDATE commands SET alias = %s, description = %s, "
                    "require_level = %s, require_perm = %s WHERE id = %s",
                    (alias if alias else None, description if description else None,
                     require_level, require_perm, cmd_id)
                )
            except Exception:
                # 极老库无 require_perm 列 → 只用旧三列更新
                db.execute(
                    "UPDATE commands SET alias = %s, description = %s, require_level = %s WHERE id = %s",
                    (alias if alias else None, description if description else None,
                     require_level, cmd_id)
                )
            audit_log(admin['id'], admin['username'], 'update_command_alias',
                      'command', str(cmd_id),
                      {'plugin': row['plugin_name'], 'handler': row['handler'],
                       'alias': alias, 'description': description,
                       'require_level': require_level, 'require_perm': require_perm})
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
