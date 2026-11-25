"""富媒体特殊消息处理：登记非文本/图片段，供面板查看与排查。

图片已有 image_renderer 与 media_guard 的完整链路；语音、视频、文件、名片、
位置等段此前没有专用落点，本模块只做「识别 + 登记 + 查看」，下载仍走
media_io 的统一闸门，不在本模块另开文件读取口子。
"""
import json
import logging
import time

from flask import jsonify, request

from framework.hooks import HookPoints, HookPriority
from framework.messaging.segments import ALL_TYPES

logger = logging.getLogger('zcbot')

# 已有专用处理或纯文本，不计入富媒体登记
_SKIP = frozenset({'text', 'at', 'image'})


def _types_of(event):
    msg = event.get('message')
    if isinstance(msg, str):
        return []
    if not isinstance(msg, list):
        return []
    out = []
    for seg in msg:
        if not isinstance(seg, dict):
            continue
        typ = str(seg.get('type') or '')
        if typ and typ not in _SKIP:
            out.append((typ, seg.get('data') or {}))
    return out


def register(ctx):
    app = ctx.app
    db = ctx.db
    framework = ctx.framework
    require_auth = ctx.require_auth

    def record(event):
        try:
            if event.get('post_type') != 'message':
                return True
            for typ, data in _types_of(event):
                detail = json.dumps(data, ensure_ascii=False)[:2000]
                db.execute(
                    "INSERT INTO media_messages (seg_type, user_id, group_id, detail, created_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (typ[:40], str(event.get('user_id') or ''),
                     str(event.get('group_id') or ''), detail, str(int(time.time()))))
        except Exception as e:
            logger.debug('[media] 登记失败: %s', e)
        return True

    try:
        framework.hooks.register(HookPoints.EVENT_BEFORE_DISPATCH, 'media.record',
                                 record, priority=HookPriority.LATE)
    except Exception as e:
        logger.debug('[media] hook 注册失败: %s', e)

    @app.route('/api/media/messages', methods=['GET'])
    @require_auth
    def media_messages():
        try:
            limit = int(request.args.get('limit', 100) or 100)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(500, limit))
        seg_type = (request.args.get('type') or '').strip()[:40]
        where = "WHERE seg_type = %s" if seg_type else ''
        params = (seg_type, limit) if seg_type else (limit,)
        try:
            rows = db.query(
                "SELECT id, seg_type, user_id, group_id, detail, created_at "
                "FROM media_messages %s ORDER BY id DESC LIMIT %%s" % where, params)
            return jsonify({'code': 0, 'data': rows, 'known_types': sorted(ALL_TYPES)})
        except Exception as e:
            logger.error('[media] 查询失败: %s', e)
            return jsonify({'code': 500, 'msg': '查询失败'}), 500
