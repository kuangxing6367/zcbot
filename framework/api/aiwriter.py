"""AI 智能体的 Web 接口：会话与改动。

分两个域，因为这是用户视角里的两件事：

- **会话**：这轮对话存在哪、之前聊过什么。以前只有终端里能看见，
  浏览器里只有一个「发一句话」的输入框——本次把会话摆到明面上；
- **改动**：这一轮动了哪些文件、diff 长什么样、能不能一键退回去。

实现边界：
- 真本事都在 aiwriter 服务里（`framework.services['aiwriter']`），这里只做
  HTTP 适配 + 权限 + 审计，不重复实现任何 diff/回滚逻辑；
- 全部要求超级管理员：diff 里就是源码，跟文件浏览器同级敏感；
- 插件未启用时返回 503 而不是 500，前端据此给出「去启用 aiwriter」的指引。
"""
import logging

from flask import jsonify, request

logger = logging.getLogger('zcbot')


def _svc(framework):
    """取 aiwriter 服务；未启用返回 (None, 原因)。"""
    try:
        svc = framework.services.get('aiwriter')
    except Exception as e:  # 服务注册表异常不该把整个 WebUI 带崩
        logger.debug('[aiwriter] 取服务失败: %s', e)
        svc = None
    if svc is None:
        return None, '未启用 aiwriter 插件（core_plugins.yaml → aiwriter.enabled: true）'
    return svc, None


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    require_super = ctx.require_super
    audit_log = ctx.audit_log

    def _guard():
        svc, why = _svc(framework)
        if svc is None:
            return None, (jsonify({'code': 503, 'msg': why}), 503)
        return svc, None

    # ── 对话 ───────────────────────────────────────────────────────────
    @app.route('/api/aiwriter/chat', methods=['POST'])
    @require_super
    def aiwriter_chat():
        """把一句话交给智能体，返回最终回复与执行事件。

        与终端 `code` 走同一个 `svc.run`，会话、工具、改动账本口径完全一致。
        `on_event` 收集中间事件（思考 / 工具调用 / 改动），前端据此展示过程。
        """
        svc, bad = _guard()
        if bad:
            return bad
        admin = request.admin
        data = request.get_json(silent=True) or {}
        text = str(data.get('text') or '').strip()
        if not text:
            return jsonify({'code': 400, 'msg': '消息不能为空'}), 400
        if len(text) > 8000:
            return jsonify({'code': 400, 'msg': '消息过长（上限 8000 字符）'}), 400
        session = str(data.get('session') or '').strip()

        events = []

        def _on_event(kind, payload):
            try:
                events.append({'kind': str(kind),
                               'payload': str(payload)[:2000]})
            except Exception:
                pass

        try:
            final = svc.run(text, on_event=_on_event, session=session or None)
        except Exception as e:
            logger.error('[aiwriter] 对话失败: %s', e)
            try:
                audit_log(admin['id'], admin['username'], 'aiwriter_chat', 'aiwriter',
                          text[:120], {'ok': False, 'err': str(e)[:200]})
            except Exception as _exc:
                logger.debug('[aiwriter] 审计写入失败: %s', _exc)
            return jsonify({'code': 500, 'msg': '智能体执行失败: %s' % e}), 500

        try:
            audit_log(admin['id'], admin['username'], 'aiwriter_chat', 'aiwriter',
                      text[:120], {'ok': True, 'events': len(events),
                                   'change_id': getattr(svc, 'last_change_id', None)})
        except Exception as _exc:
            logger.debug('[aiwriter] 审计写入失败: %s', _exc)
        return jsonify({'code': 0, 'data': {
            'text': final or '',
            'events': events,
            'change_id': getattr(svc, 'last_change_id', None),
            'active': svc.active_session(),
        }})

    # ── 会话 ───────────────────────────────────────────────────────────
    @app.route('/api/aiwriter/sessions', methods=['GET'])
    @require_super
    def aiwriter_sessions():
        svc, bad = _guard()
        if bad:
            return bad
        try:
            data = {'active': svc.active_session(), 'sessions': svc.sessions(),
                    'workspace': svc.workspace()}
        except Exception as e:
            logger.error('[aiwriter] 列会话失败: %s', e)
            return jsonify({'code': 500, 'msg': '读取会话失败: %s' % e}), 500
        return jsonify({'code': 0, 'data': data})

    @app.route('/api/aiwriter/sessions', methods=['POST'])
    @require_super
    def aiwriter_session_new():
        svc, bad = _guard()
        if bad:
            return bad
        admin = request.admin
        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()
        ok, res = svc.new_session(name)
        try:
            audit_log(admin['id'], admin['username'], 'aiwriter_session_new',
                      'aiwriter', name[:128] or '(默认名)', {'ok': bool(ok)})
        except Exception as _exc:
            logger.debug('[aiwriter] 审计写入失败: %s', _exc)
        if not ok:
            return jsonify({'code': 400, 'msg': str(res)}), 400
        return jsonify({'code': 0, 'data': {'key': res}})

    @app.route('/api/aiwriter/sessions/<path:key>', methods=['GET'])
    @require_super
    def aiwriter_session_detail(key):
        svc, bad = _guard()
        if bad:
            return bad
        try:
            msgs = svc.history(key) or []
        except Exception as e:
            logger.error('[aiwriter] 读取会话历史失败 %s: %s', key, e)
            return jsonify({'code': 500, 'msg': '读取会话历史失败: %s' % e}), 500
        return jsonify({'code': 0, 'data': {
            'key': key,
            'messages': [{'role': m.get('role'), 'content': m.get('content')} for m in msgs],
        }})

    @app.route('/api/aiwriter/sessions/<path:key>/activate', methods=['POST'])
    @require_super
    def aiwriter_session_activate(key):
        svc, bad = _guard()
        if bad:
            return bad
        admin = request.admin
        ok, res = svc.set_session(key)
        try:
            audit_log(admin['id'], admin['username'], 'aiwriter_session_activate',
                      'aiwriter', key[:128], {'ok': bool(ok)})
        except Exception as _exc:
            logger.debug('[aiwriter] 审计写入失败: %s', _exc)
        if not ok:
            return jsonify({'code': 400, 'msg': str(res)}), 400
        return jsonify({'code': 0, 'data': {'key': res}})

    @app.route('/api/aiwriter/sessions/<path:key>', methods=['DELETE'])
    @require_super
    def aiwriter_session_delete(key):
        svc, bad = _guard()
        if bad:
            return bad
        admin = request.admin
        ok, res = svc.delete_session(key)
        try:
            audit_log(admin['id'], admin['username'], 'aiwriter_session_delete',
                      'aiwriter', key[:128], {'ok': bool(ok)})
        except Exception as _exc:
            logger.debug('[aiwriter] 审计写入失败: %s', _exc)
        if not ok:
            return jsonify({'code': 400, 'msg': str(res)}), 400
        return jsonify({'code': 0, 'data': {'msg': str(res), 'active': svc.active_session()}})

    # ── 改动 ───────────────────────────────────────────────────────────
    @app.route('/api/aiwriter/changes', methods=['GET'])
    @require_super
    def aiwriter_changes():
        svc, bad = _guard()
        if bad:
            return bad
        try:
            limit = int(request.args.get('limit', 50) or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(200, limit))
        try:
            return jsonify({'code': 0, 'data': svc.changes(limit=limit)})
        except Exception as e:
            logger.error('[aiwriter] 列改动失败: %s', e)
            return jsonify({'code': 500, 'msg': '读取改动记录失败: %s' % e}), 500

    @app.route('/api/aiwriter/changes/<change_id>', methods=['GET'])
    @require_super
    def aiwriter_change_detail(change_id):
        svc, bad = _guard()
        if bad:
            return bad
        try:
            return jsonify({'code': 0, 'data': svc.change(change_id)})
        except Exception as e:
            logger.warning('[aiwriter] 读取改动记录失败 %s: %s', change_id, e)
            return jsonify({'code': 404, 'msg': str(e)}), 404

    @app.route('/api/aiwriter/changes/<change_id>/rollback', methods=['POST'])
    @require_super
    def aiwriter_change_rollback(change_id):
        svc, bad = _guard()
        if bad:
            return bad
        admin = request.admin
        try:
            result = svc.rollback(change_id)
        except Exception as e:
            logger.warning('[aiwriter] 回滚失败 %s: %s', change_id, e)
            try:
                audit_log(admin['id'], admin['username'], 'aiwriter_rollback',
                          'aiwriter', change_id, {'ok': False, 'err': str(e)[:200]})
            except Exception as _exc:
                logger.debug('[aiwriter] 审计写入失败: %s', _exc)
            return jsonify({'code': 400, 'msg': str(e)}), 400
        try:
            audit_log(admin['id'], admin['username'], 'aiwriter_rollback', 'aiwriter',
                      change_id, {'ok': True, 'restored': len(result.get('restored') or []),
                                  'skipped': len(result.get('skipped') or [])})
        except Exception as _exc:
            logger.debug('[aiwriter] 审计写入失败: %s', _exc)
        return jsonify({'code': 0, 'data': result})
