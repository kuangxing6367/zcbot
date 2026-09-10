# -*- coding: utf-8 -*-
"""
权限系统（LuckPerms 风格）管理接口
"""
import logging

from flask import jsonify, request

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    db = ctx.db
    require_auth = ctx.require_auth

    def _perm_operator():
        """当前操作者标识（用于审计）"""
        admin = getattr(request, 'admin', None)
        return (admin or {}).get('username', 'system')

    @app.route('/api/perm/builtins', methods=['GET'])
    @require_auth
    def perm_builtins():
        """内置角色组（只读，由框架代码虚拟注入，不入库）"""
        from framework import perm as perm_mod
        return jsonify({'code': 0, 'data': [
            {'name': n, 'display_name': g['display_name'], 'weight': g['weight'],
             'inherits': g['inherits'], 'node': g['node']}
            for n, g in perm_mod.BUILTIN_GROUPS.items()
        ], 'context_keys': list(perm_mod.CONTEXT_KEYS)})

    @app.route('/api/perm/groups', methods=['GET'])
    @require_auth
    def perm_list_groups():
        from framework import perm as perm_mod
        try:
            return jsonify({'code': 0, 'data': perm_mod.list_groups(db)})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/groups', methods=['POST'])
    @require_auth
    def perm_create_group():
        from framework import perm as perm_mod
        data = request.get_json(silent=True) or {}
        try:
            perm_mod.create_group(
                db, (data.get('name') or '').strip(),
                display_name=data.get('display_name'),
                weight=int(data.get('weight') or 0),
                prefix=data.get('prefix'), suffix=data.get('suffix'),
                is_default=1 if data.get('is_default') else 0,
                operator=_perm_operator())
            return jsonify({'code': 0, 'msg': '权限组已创建'})
        except ValueError as e:
            return jsonify({'code': 400, 'msg': str(e)}), 400
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/groups/<name>', methods=['PUT'])
    @require_auth
    def perm_update_group(name):
        from framework import perm as perm_mod
        data = request.get_json(silent=True) or {}
        try:
            perm_mod.update_group(
                db, name,
                display_name=data.get('display_name'),
                weight=None if data.get('weight') is None else int(data.get('weight')),
                prefix=data.get('prefix'), suffix=data.get('suffix'),
                is_default=None if data.get('is_default') is None else (1 if data.get('is_default') else 0),
                operator=_perm_operator())
            return jsonify({'code': 0, 'msg': '权限组已更新'})
        except ValueError as e:
            return jsonify({'code': 400, 'msg': str(e)}), 400
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/groups/<name>', methods=['DELETE'])
    @require_auth
    def perm_delete_group(name):
        from framework import perm as perm_mod
        try:
            perm_mod.delete_group(db, name, operator=_perm_operator())
            return jsonify({'code': 0, 'msg': '权限组已删除'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/groups/<name>/nodes', methods=['GET'])
    @require_auth
    def perm_group_nodes(name):
        from framework import perm as perm_mod
        try:
            rows = db.query(
                "SELECT id, node, value, context_key, context_val, expire_at, created_at "
                "FROM perm_group_nodes WHERE group_name = %s ORDER BY id", (name,))
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/groups/<name>/nodes', methods=['POST'])
    @require_auth
    def perm_set_group_node(name):
        from framework import perm as perm_mod
        data = request.get_json(silent=True) or {}
        try:
            perm_mod.set_group_node(
                db, name, (data.get('node') or '').strip(),
                value=bool(data.get('value', True)),
                ctx_key=data.get('context_key'), ctx_val=data.get('context_val'),
                expire_at=data.get('expire_at'), operator=_perm_operator())
            return jsonify({'code': 0, 'msg': '节点已保存'})
        except ValueError as e:
            return jsonify({'code': 400, 'msg': str(e)}), 400
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/groups/<name>/nodes', methods=['DELETE'])
    @require_auth
    def perm_unset_group_node(name):
        from framework import perm as perm_mod
        data = request.get_json(silent=True) or {}
        try:
            perm_mod.unset_group_node(
                db, name, (data.get('node') or '').strip(),
                ctx_key=data.get('context_key'), ctx_val=data.get('context_val'),
                operator=_perm_operator())
            return jsonify({'code': 0, 'msg': '节点已删除'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/users/<int:user_id>', methods=['GET'])
    @require_auth
    def perm_user_detail(user_id):
        """用户权限详情：直接节点 + 生效快照"""
        from framework import perm as perm_mod
        ctx = request.args.get('context')
        context = {}
        if ctx:
            for part in ctx.split(';'):
                if '=' in part:
                    k, v = part.split('=', 1)
                    context[k.strip()] = v.strip()
        try:
            snapshot = perm_mod.resolve(db, user_id, context or None).to_dict()
            return jsonify({
                'code': 0,
                'data': {'raw_nodes': perm_mod.list_user_nodes(db, user_id),
                         'snapshot': snapshot},
            })
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/users/<int:user_id>/nodes', methods=['POST'])
    @require_auth
    def perm_set_user_node(user_id):
        from framework import perm as perm_mod
        data = request.get_json(silent=True) or {}
        try:
            perm_mod.set_user_node(
                db, user_id, (data.get('node') or '').strip(),
                value=bool(data.get('value', True)),
                ctx_key=data.get('context_key'), ctx_val=data.get('context_val'),
                expire_at=data.get('expire_at'), operator=_perm_operator())
            return jsonify({'code': 0, 'msg': '节点已保存'})
        except ValueError as e:
            return jsonify({'code': 400, 'msg': str(e)}), 400
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/users/<int:user_id>/nodes', methods=['DELETE'])
    @require_auth
    def perm_unset_user_node(user_id):
        from framework import perm as perm_mod
        data = request.get_json(silent=True) or {}
        try:
            perm_mod.unset_user_node(
                db, user_id, (data.get('node') or '').strip(),
                ctx_key=data.get('context_key'), ctx_val=data.get('context_val'),
                operator=_perm_operator())
            return jsonify({'code': 0, 'msg': '节点已删除'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/users/<int:user_id>/groups', methods=['POST'])
    @require_auth
    def perm_add_user_group(user_id):
        from framework import perm as perm_mod
        data = request.get_json(silent=True) or {}
        try:
            perm_mod.add_user_group(
                db, user_id, (data.get('group') or '').strip(),
                ctx_key=data.get('context_key'), ctx_val=data.get('context_val'),
                expire_at=data.get('expire_at'), operator=_perm_operator())
            return jsonify({'code': 0, 'msg': '已加入权限组'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/users/<int:user_id>/groups', methods=['DELETE'])
    @require_auth
    def perm_remove_user_group(user_id):
        from framework import perm as perm_mod
        data = request.get_json(silent=True) or {}
        try:
            perm_mod.remove_user_group(
                db, user_id, (data.get('group') or '').strip(),
                ctx_key=data.get('context_key'), ctx_val=data.get('context_val'),
                operator=_perm_operator())
            return jsonify({'code': 0, 'msg': '已移出权限组'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/users/<int:user_id>/track', methods=['POST'])
    @require_auth
    def perm_track_step(user_id):
        """升降级：body = {track, direction: promote|demote, context_key, context_val}"""
        from framework import perm as perm_mod
        data = request.get_json(silent=True) or {}
        direction = (data.get('direction') or 'promote').strip().lower()
        try:
            fn = perm_mod.promote if direction != 'demote' else perm_mod.demote
            r = fn(db, user_id, (data.get('track') or '').strip(),
                   ctx_key=data.get('context_key'), ctx_val=data.get('context_val'),
                   operator=_perm_operator())
            return jsonify({'code': 0, 'msg': f"{r['from'] or '(无)'} → {r['to']}", 'data': r})
        except ValueError as e:
            return jsonify({'code': 400, 'msg': str(e)}), 400
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/tracks', methods=['GET'])
    @require_auth
    def perm_list_tracks():
        from framework import perm as perm_mod
        try:
            return jsonify({'code': 0, 'data': perm_mod.list_tracks(db)})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/tracks', methods=['POST'])
    @require_auth
    def perm_save_track():
        from framework import perm as perm_mod
        data = request.get_json(silent=True) or {}
        try:
            perm_mod.save_track(db, (data.get('name') or '').strip(),
                                data.get('groups_order') or '',
                                display_name=data.get('display_name'),
                                operator=_perm_operator())
            return jsonify({'code': 0, 'msg': '轨道已保存'})
        except ValueError as e:
            return jsonify({'code': 400, 'msg': str(e)}), 400
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/tracks/<name>', methods=['DELETE'])
    @require_auth
    def perm_delete_track(name):
        from framework import perm as perm_mod
        try:
            perm_mod.delete_track(db, name, operator=_perm_operator())
            return jsonify({'code': 0, 'msg': '轨道已删除'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/check', methods=['POST'])
    @require_auth
    def perm_check():
        """权限检查器：body = {user_id, node, context:{...}, role}"""
        from framework import perm as perm_mod
        data = request.get_json(silent=True) or {}
        try:
            uid = int(data.get('user_id') or 0)
            node = (data.get('node') or '').strip()
            if not uid or not node:
                return jsonify({'code': 400, 'msg': 'user_id 与 node 必填'}), 400
            context = data.get('context') or None
            role = data.get('role') or None
            pset = perm_mod.resolve(db, uid, context, role)
            state = pset.check(node)
            return jsonify({
                'code': 0,
                'data': {
                    'node': node,
                    'state': 'true' if state is True else ('false' if state is False else 'undefined'),
                    'allowed': state is True,
                    'groups': pset.groups,
                    'primary_group': pset.primary_group,
                },
            })
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/audit', methods=['GET'])
    @require_auth
    def perm_audit_logs():
        from framework import perm as perm_mod
        try:
            limit = int(request.args.get('limit') or 100)
            data = perm_mod.list_audit(
                db,
                target_type=request.args.get('target_type'),
                target=request.args.get('target'),
                limit=min(limit, 500))
            return jsonify({'code': 0, 'data': data})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/perm/cleanup', methods=['POST'])
    @require_auth
    def perm_cleanup():
        """立即清理过期节点"""
        from framework import perm as perm_mod
        try:
            n = perm_mod.cleanup_expired(db)
            return jsonify({'code': 0, 'msg': f'已清理 {n} 条过期节点', 'data': {'removed': n}})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500
