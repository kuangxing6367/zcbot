"""Web 聊天：把机器人消息镜像到面板，并允许从面板发消息进入处理链。

- 收：订阅 `EVENT_BEFORE_DISPATCH`，把消息事件落进 webchat_messages；
- 发：走真实的 `dispatch.dispatch_event(wait=True)`，插件收到的事件与 QQ 消息同构。

只要求登录（不要求 super）：这是日常聊天界面，不是配置面。
"""
import asyncio
import logging
import time

from flask import jsonify, request

from framework.hooks import HookPoints, HookPriority

logger = logging.getLogger('zcbot')


def _text_of(event):
    msg = event.get('message')
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        return ''.join(str(seg.get('data', {}).get('text', ''))
                       for seg in msg if isinstance(seg, dict) and seg.get('type') == 'text')
    return ''


def register(ctx):
    app = ctx.app
    db = ctx.db
    framework = ctx.framework
    require_auth = ctx.require_auth

    # ---- 接收侧：镜像消息 ----
    def mirror(event):
        try:
            if event.get('post_type') != 'message':
                return True
            text = _text_of(event)
            if not text:
                return True
            db.execute(
                "INSERT INTO webchat_messages (direction, sender, peer_id, content, created_at) "
                "VALUES ('in', %s, %s, %s, %s)",
                (str(event.get('user_id') or ''),
                 str(event.get('group_id') or event.get('user_id') or ''),
                 text[:4000], str(int(time.time()))))
        except Exception as e:
            logger.debug('[webchat] 消息镜像失败: %s', e)
        return True

    try:
        framework.hooks.register(HookPoints.EVENT_BEFORE_DISPATCH, 'webchat.mirror',
                                 mirror, priority=HookPriority.LATE)
    except Exception as e:
        logger.debug('[webchat] hook 注册失败: %s', e)

    @app.route('/api/webchat/messages', methods=['GET'])
    @require_auth
    def webchat_messages():
        try:
            limit = int(request.args.get('limit', 100) or 100)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(500, limit))
        try:
            rows = db.query(
                "SELECT id, direction, sender, peer_id, content, created_at "
                "FROM webchat_messages ORDER BY id DESC LIMIT %s", (limit,))
            rows.reverse()
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            logger.error('[webchat] 读取消息失败: %s', e)
            return jsonify({'code': 500, 'msg': '读取消息失败'}), 500

    @app.route('/api/webchat/send', methods=['POST'])
    @require_auth
    def webchat_send():
        data = request.get_json(silent=True) or {}
        text = str(data.get('text') or '').strip()
        if not text:
            return jsonify({'code': 400, 'msg': '消息不能为空'}), 400
        if len(text) > 4000:
            return jsonify({'code': 400, 'msg': '消息过长（上限 4000 字符）'}), 400
        try:
            user_id = int(data.get('user_id') or 10001)
            group_id = int(data.get('group_id') or 0)
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': 'QQ 号 / 群号必须是整数'}), 400

        try:
            db.execute(
                "INSERT INTO webchat_messages (direction, sender, peer_id, content, created_at) "
                "VALUES ('out', 'web', %s, %s, %s)",
                (str(group_id or user_id), text, str(int(time.time()))))
        except Exception as e:
            logger.error('[webchat] 写入消息失败: %s', e)
            return jsonify({'code': 500, 'msg': '写入消息失败'}), 500

        raw = {
            'post_type': 'message',
            'message_type': 'group' if group_id else 'private',
            'sub_type': 'normal',
            'self_id': 10001,
            'user_id': user_id,
            'group_id': group_id,
            'message_id': int(time.time() * 1000),
            'message': [{'type': 'text', 'data': {'text': text}}],
            'raw_message': text,
            'time': int(time.time()),
        }
        loop = getattr(framework, 'loop', None)
        if loop is not None and not loop.is_closed():
            try:
                future = asyncio.run_coroutine_threadsafe(
                    framework.dispatch_event(raw, wait=True), loop)
                future.result(timeout=30)
            except Exception as e:
                logger.warning('[webchat] 注入处理链失败: %s', e)
                return jsonify({'code': 200, 'msg': '已记录，但注入处理链失败: %s' % e})
        else:
            return jsonify({'code': 200, 'msg': '已记录；框架事件循环不可用，未注入处理链'})
        return jsonify({'code': 0, 'msg': '已发送'})
