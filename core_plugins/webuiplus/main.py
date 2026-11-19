"""WebUIplus（官方插件）：给 WebUI 补上布局/别名、消息查看、发消息、主题与动效。

依赖 WebUI（core_plugins/webui）。所有数据落在插件专属目录
``data/plugins_dat/<插件名>/``：配置 config.json、消息索引 messages.jsonl、
图片 media/。插件不接管官方前端，只注册自己的内嵌页和 REST 接口。

设计上刻意不用 emoji：图标是几何字符，日志和前端文案都是可搜索的中文。
"""
import json
import logging
import os
import threading
import time
from collections import deque

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    'name': 'WebUIplus',
    'version': '1.0.0',
    'author': 'ZCBOT',
    'desc': 'WebUI 增强：布局/别名、消息查看与发送、主题与动效',
    'priority': 10,
    'official': True,
    'process': 'core',
}

DEFAULTS = {
    'theme': 'auto',            # auto / light / dark
    'glass': True,              # 全局磨砂
    'intro': True,              # 首屏动画
    'display': 'card',          # card / queue
    'aliases': {},              # 用户/群 ID -> 显示名
    'layout': ['time', 'sender', 'text', 'images'],
    'max_messages': 200,
    'image_max_bytes': 8388608,  # 单图 8MB，超限只保留引用
}

MAX_MESSAGES = 1000
MAX_ALIASES = 500
MAX_TEXT_CHARS = 2000
MAX_REF_CHARS = 4096
_VALID_DISPLAY = ('card', 'queue')
_VALID_THEME = ('auto', 'light', 'dark')
_LAYOUT_FIELDS = ('time', 'sender', 'text', 'images', 'group')

_state = {
    'ctx': None,
    'dir': '',
    'media_dir': '',
    'config': dict(DEFAULTS),
    'messages': deque(maxlen=MAX_MESSAGES),
    'lock': threading.RLock(),
}


# ── 配置 ─────────────────────────────────────────────────

def _config_path():
    return os.path.join(_state['dir'], 'config.json')


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on', 'y')
    return bool(value)


def _normalize(raw):
    cfg = dict(DEFAULTS)
    if isinstance(raw, dict):
        for key in DEFAULTS:
            if key in raw:
                cfg[key] = raw[key]
    cfg['theme'] = cfg['theme'] if cfg['theme'] in _VALID_THEME else DEFAULTS['theme']
    cfg['display'] = cfg['display'] if cfg['display'] in _VALID_DISPLAY else DEFAULTS['display']
    cfg['glass'] = _as_bool(cfg['glass'])
    cfg['intro'] = _as_bool(cfg['intro'])
    try:
        cfg['max_messages'] = max(1, min(MAX_MESSAGES, int(cfg['max_messages'])))
    except (TypeError, ValueError):
        cfg['max_messages'] = DEFAULTS['max_messages']
    try:
        cfg['image_max_bytes'] = max(1024, min(50 * 1024 * 1024, int(cfg['image_max_bytes'])))
    except (TypeError, ValueError):
        cfg['image_max_bytes'] = DEFAULTS['image_max_bytes']
    aliases = cfg.get('aliases')
    cfg['aliases'] = {str(k)[:80]: str(v)[:80] for k, v in aliases.items()
                      } if isinstance(aliases, dict) else {}
    cfg['aliases'] = dict(list(cfg['aliases'].items())[:MAX_ALIASES])
    layout = cfg.get('layout')
    cfg['layout'] = [f for f in layout if f in _LAYOUT_FIELDS
                     ] if isinstance(layout, (list, tuple)) else list(DEFAULTS['layout'])
    if not cfg['layout']:
        cfg['layout'] = list(DEFAULTS['layout'])
    return cfg


def load_config(seed=None):
    path = _config_path()
    if not os.path.isfile(path):
        return _normalize(seed)
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning(f'[WebUIplus] 配置读取失败，使用默认值: {e}')
        return _normalize(None)
    return _normalize(raw)


def save_config(raw):
    cfg = _normalize(raw)
    path = _config_path()
    tmp = path + '.tmp'
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, 'w', encoding='utf-8', newline='') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        logger.error(f'[WebUIplus] 配置写入失败: {e}')
        return False, str(e)
    with _state['lock']:
        _state['config'] = cfg
        _state['messages'] = deque(list(_state['messages'])[-cfg['max_messages']:],
                                   maxlen=cfg['max_messages'])
    return True, cfg


# ── 消息采集 ─────────────────────────────────────────────

def _text_of(raw_event):
    from framework.messaging.segments import normalize_message, segments_to_text
    message = raw_event.get('message') if isinstance(raw_event, dict) else None
    text = segments_to_text(normalize_message(message), media_hint=False)
    return str(text)[:MAX_TEXT_CHARS]


def _image_segments(raw_event):
    from framework.messaging.segments import (
        canonical_type,
        media_ref,
        normalize_message,
    )
    message = raw_event.get('message') if isinstance(raw_event, dict) else None
    return [seg for seg in normalize_message(message)
            if canonical_type(seg.get('type')) == 'image' and media_ref(seg)]


def _persist_message(record):
    path = os.path.join(_state['dir'], 'messages.jsonl')
    try:
        os.makedirs(_state['dir'], exist_ok=True)
        with open(path, 'a', encoding='utf-8', newline='') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        _trim_journal(path)
    except OSError as e:
        logger.warning(f'[WebUIplus] 消息落盘失败: {e}')


def _trim_journal(path):
    limit = _state['config']['max_messages']
    with open(path, encoding='utf-8') as f:
        lines = f.readlines()
    if len(lines) <= limit:
        return
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.writelines(lines[-limit:])
    os.replace(tmp, path)


def capture_message(raw_event, bot_name=''):
    """原始消息钩子：只记录，不接管（返回 None 让框架继续正常处理）。"""
    if not isinstance(raw_event, dict) or raw_event.get('post_type') != 'message':
        return
    try:
        from framework.messaging.segments import media_ref
        cfg = _state['config']
        sender = raw_event.get('sender') if isinstance(raw_event.get('sender'), dict) else {}
        text = _text_of(raw_event)
        record = {
            'at': time.time(),
            'bot': str(bot_name or ''),
            'message_id': raw_event.get('message_id'),
            'user_id': str(raw_event.get('user_id') or ''),
            'group_id': str(raw_event.get('group_id') or ''),
            'nickname': str(sender.get('nickname') or sender.get('card') or '')[:80],
            'text': text,
            'images': [],
        }
        for seg in _image_segments(raw_event):
            item = {'ref': str(media_ref(seg))[:MAX_REF_CHARS], 'path': ''}
            if cfg['image_max_bytes'] and _state['ctx'] is not None:
                res = _state['ctx'].download_media(
                    seg, dest_dir=_state['media_dir'],
                    max_bytes=cfg['image_max_bytes'], timeout=10, bot=bot_name)
                if isinstance(res, dict) and res.get('status') == 'ok':
                    item['path'] = os.path.basename(str(res.get('path') or ''))
            record['images'].append(item)
        # 纯空消息（只有无法识别的段）没有查看价值，不落盘
        if not text and not record['images']:
            return
        with _state['lock']:
            _state['messages'].append(record)
        _persist_message(record)
    except Exception as e:
        logger.warning(f'[WebUIplus] 消息采集失败: {e}')
    return


def recent_messages(limit=50):
    try:
        limit = max(1, min(MAX_MESSAGES, int(limit)))
    except (TypeError, ValueError):
        limit = 50
    with _state['lock']:
        return list(_state['messages'])[-limit:]


# ── REST 视图（延迟导入 flask，未启用 WebUI 时不加载）─────────

def _json(data, code=0):
    from flask import jsonify
    return jsonify({'code': code, 'data': data})


def view_messages():
    from flask import request
    return _json(recent_messages(request.args.get('limit', 50)))


def view_config_get():
    with _state['lock']:
        return _json(dict(_state['config']))


def view_config_put():
    from flask import request
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _json('请求体必须是 JSON 对象', code=1), 400
    ok, result = save_config(body)
    return _json(result if ok else result, code=0 if ok else 1)


def view_send():
    from flask import request
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _json('请求体必须是 JSON 对象', code=1), 400
    user_id = body.get('user_id')
    group_id = body.get('group_id')
    message = body.get('message')
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_TEXT_CHARS:
        return _json(f'message 必须是 1~{MAX_TEXT_CHARS} 字符的文本', code=1), 400
    if user_id is None and group_id is None:
        return _json('必须提供 user_id 或 group_id', code=1), 400
    ctx = _state['ctx']
    if ctx is None:
        return _json('插件尚未就绪', code=1), 503
    try:
        result = ctx.send_msg(user_id=user_id, group_id=group_id,
                              message=message.strip(),
                              bot=body.get('bot') or None)
        return _json({'sent': True, 'result': result}), 200
    except Exception as e:
        logger.warning(f'[WebUIplus] 发消息失败: {e}')
        return _json(f'发送失败: {e}', code=1), 500


def view_media(filename):
    from flask import abort, send_from_directory
    # basename 二次收敛：路由参数即使被塞进分隔符也出不了 media 目录
    name = os.path.basename(str(filename or ''))
    if not name or not os.path.isfile(os.path.join(_state['media_dir'], name)):
        abort(404)
    return send_from_directory(_state['media_dir'], name)


# ── 生命周期 ─────────────────────────────────────────────

def register(ctx):
    fw = ctx._framework
    web_cfg = fw.config.get('web', {}) or {}
    if web_cfg.get('enabled') is False:
        ctx.log('[WebUIplus] WebUI 已禁用（web.enabled: false），跳过注册')
        return

    data_dir = ctx.get_data_dir()
    media_dir = os.path.join(data_dir, 'media')
    os.makedirs(media_dir, exist_ok=True)
    with _state['lock']:
        _state['ctx'] = ctx
        _state['dir'] = data_dir
        _state['media_dir'] = media_dir
        _state['config'] = load_config(fw.config.get('webuiplus'))
        _state['messages'] = deque(maxlen=_state['config']['max_messages'])

    ctx.on_raw_message(capture_message)
    ctx.register_api('/api/webuiplus/messages', view_messages, methods=['GET'],
                     description='WebUIplus 最近消息')
    ctx.register_api('/api/webuiplus/config', view_config_get, methods=['GET'],
                     description='读取 WebUIplus 布局与主题配置')
    ctx.register_api('/api/webuiplus/config', view_config_put, methods=['PUT'],
                     description='保存 WebUIplus 配置')
    ctx.register_api('/api/webuiplus/send', view_send, methods=['POST'],
                     description='从 WebUI 发送消息到 IM')
    ctx.register_api('/api/webuiplus/media/<path:filename>', view_media,
                     methods=['GET'], description='读取已落盘的图片')
    ctx.webui(title='WebUIplus', entry='index.html', icon='◇', order=60, sidebar=True)
    ctx.log(f'[WebUIplus] 已注册，数据目录: {data_dir}')


def unregister():
    with _state['lock']:
        _state['ctx'] = None
