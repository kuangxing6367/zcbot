"""消息处理链工作流的 REST 管理接口。

- 读取：super / admin 均可（配置本身就是敏感面，普通成员无面板账号）；
- 写入：全部 require_super，并写审计；
- 保存前统一跑 ``validate_nodes``，配置写错当场报错，不静默落库。
"""
import json
import logging

from flask import jsonify, request

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    require_auth = ctx.require_auth
    require_super = ctx.require_super
    audit_log = ctx.audit_log
    framework = ctx.framework

    def store():
        return getattr(framework, 'workflow_store', None)

    def _plan():
        s = store()
        return s if s is not None else None

    def _audit(action, target, detail):
        admin = getattr(request, 'admin', None) or {}
        audit_log(admin.get('id'), admin.get('username'), action,
                  'workflow', str(target) if target is not None else None, detail)

    @app.route('/api/workflows', methods=['GET'])
    @require_auth
    def workflows_list():
        s = _plan()
        if s is None:
            return jsonify({'code': 500, 'msg': '工作流存储未初始化'}), 500
        return jsonify({'code': 0, 'data': s.list()})

    @app.route('/api/workflows/nodes/validate', methods=['POST'])
    @require_auth
    def workflows_validate():
        from framework.workflow import validate_nodes

        data = request.get_json(silent=True) or {}
        issues = validate_nodes(data.get('nodes'))
        return jsonify({'code': 0, 'data': {'issues': issues, 'valid': not issues}})

    @app.route('/api/workflows/preview', methods=['POST'])
    @require_auth
    def workflows_preview():
        """按给定 QQ / 群号预览某条工作流会选中哪些插件。"""
        from framework.workflow import apply_workflow

        data = request.get_json(silent=True) or {}
        plan = apply_workflow(data.get('workflow'),
                              user_id=data.get('user_id'),
                              group_id=data.get('group_id'))
        return jsonify({'code': 0, 'data': plan.as_dict()})

    @app.route('/api/workflows', methods=['POST'])
    @require_super
    def workflows_create():
        from framework.workflow import validate_nodes

        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()
        nodes = data.get('nodes') or []
        if not name:
            return jsonify({'code': 400, 'msg': '缺少名称'}), 400
        issues = validate_nodes(nodes)
        if issues:
            return jsonify({'code': 400, 'msg': '；'.join(issues)}), 400
        s = _plan()
        if s is None:
            return jsonify({'code': 500, 'msg': '工作流存储未初始化'}), 500
        try:
            wid = s.create(name, nodes, data.get('description') or '',
                           data.get('enabled', True), data.get('is_default', False))
            if data.get('is_default'):
                _clear_other_defaults(s, wid)
            s.set_bindings(wid, data.get('user_ids') or [])
        except Exception as e:
            logger.error('创建工作流失败: %s', e)
            return jsonify({'code': 500, 'msg': f'创建失败: {e}'}), 500
        _audit('workflow_create', wid, {'name': name, 'nodes': len(nodes)})
        return jsonify({'code': 0, 'data': {'id': wid}})

    @app.route('/api/workflows/<int:wid>', methods=['GET'])
    @require_auth
    def workflows_get(wid):
        s = _plan()
        if s is None:
            return jsonify({'code': 500, 'msg': '工作流存储未初始化'}), 500
        item = s.get(wid)
        if item is None:
            return jsonify({'code': 404, 'msg': '工作流不存在'}), 404
        item['user_ids'] = [
            str(r['user_id']) for r in s.bindings(wid)
        ]
        return jsonify({'code': 0, 'data': item})

    @app.route('/api/workflows/<int:wid>', methods=['PUT'])
    @require_super
    def workflows_update(wid):
        from framework.workflow import validate_nodes

        s = _plan()
        if s is None:
            return jsonify({'code': 500, 'msg': '工作流存储未初始化'}), 500
        if s.get(wid) is None:
            return jsonify({'code': 404, 'msg': '工作流不存在'}), 404
        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()
        nodes = data.get('nodes') or []
        if not name:
            return jsonify({'code': 400, 'msg': '缺少名称'}), 400
        issues = validate_nodes(nodes)
        if issues:
            return jsonify({'code': 400, 'msg': '；'.join(issues)}), 400
        try:
            s.update(wid, name, nodes, data.get('description') or '',
                     data.get('enabled', True), data.get('is_default', False))
            if data.get('is_default'):
                _clear_other_defaults(s, wid)
            s.set_bindings(wid, data.get('user_ids') or [])
        except Exception as e:
            logger.error('更新工作流失败: %s', e)
            return jsonify({'code': 500, 'msg': f'更新失败: {e}'}), 500
        _audit('workflow_update', wid, {'name': name, 'nodes': len(nodes)})
        return jsonify({'code': 0, 'data': {'id': wid}})

    @app.route('/api/workflows/<int:wid>', methods=['DELETE'])
    @require_super
    def workflows_delete(wid):
        s = _plan()
        if s is None:
            return jsonify({'code': 500, 'msg': '工作流存储未初始化'}), 500
        s.delete(wid)
        _audit('workflow_delete', wid, {})
        return jsonify({'code': 0, 'data': {'id': wid}})

    @app.route('/api/workflows/generate', methods=['POST'])
    @require_super
    def workflows_generate():
        """按 QQ 号列表生成一条工作流（节点编辑器最简单入口）。"""
        from framework.workflow import validate_nodes

        data = request.get_json(silent=True) or {}
        user_ids = data.get('user_ids') or []
        plugins = data.get('plugins') or []
        if not isinstance(user_ids, list) or not isinstance(plugins, list):
            return jsonify({'code': 400, 'msg': 'user_ids / plugins 必须是数组'}), 400
        nodes = [{'id': 'user', 'type': 'user',
                  'config': {'values': [str(u).strip() for u in user_ids if str(u).strip()]}}]
        nodes += [
            {'id': f'p{i}', 'type': 'plugin', 'config': {'plugin': str(p).strip()}}
            for i, p in enumerate(plugins) if str(p).strip()
        ]
        issues = validate_nodes(nodes)
        if issues:
            return jsonify({'code': 400, 'msg': '；'.join(issues)}), 400
        return jsonify({'code': 0, 'data': {'nodes': nodes}})


def _clear_other_defaults(store, keep_id):
    """保证只有一条默认工作流。"""
    try:
        store.db.execute(
            'UPDATE workflows SET is_default=0 WHERE id<>%s AND is_default=1',
            (keep_id,))
        store.invalidate()
    except Exception as e:
        logger.warning('清理其它默认工作流失败: %s', e)
