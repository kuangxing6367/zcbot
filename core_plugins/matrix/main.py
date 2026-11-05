# -*- coding: utf-8 -*-
"""
Matrix 接入端（matrix）

Client-Server API v3，长轮询 /sync 收事件 + PUT .../send 发消息。
不依赖 matrix-nio 等 SDK，只用 requests，方便在任意环境跑。

协议依据（Matrix Client-Server API v1.11+，2026-11 核对）：
  - 收：GET /_matrix/client/v3/sync?since=<token>&timeout=30000
    响应 next_batch 作为下次的 since；没有 since 时返回的是最近历史，
    首次接入必须先把 next_batch 存下来，否则重启会把历史消息重放一遍。
  - 发：PUT /_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txnId}
    txnId 是幂等键 —— 重试必须复用同一个 txnId，换一个就会重复发。
  - 鉴权：Authorization: Bearer <access_token>
  - 限流：HTTP 429 + {"errcode":"M_LIMIT_EXCEEDED","retry_after_ms":3000}
    必须按 retry_after_ms 等待，自己拍间隔会一直被限。
  - 致命错误：M_UNKNOWN_TOKEN（token 失效）重试无意义。

配置（core_plugins.yaml → matrix，默认 enabled: false）：
  homeserver / access_token / user_id / bot_name / sync_timeout / rooms
"""
import asyncio
import json
import logging
import os
import threading
import time
import uuid
from typing import Optional
from urllib.parse import quote, urlparse

import requests

from framework.messaging.protocol import ProtocolAdapter
from framework.messaging.backoff import Backoff

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "matrix",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "Matrix 接入端：Client-Server API v3 长轮询收发",
    "priority": 7,
    "official": True,
    "process": "core",
}


class MatrixApiError(RuntimeError):
    """Matrix 标准错误：{"errcode": "M_...", "error": "..."}"""

    def __init__(self, errcode: str, error: str = '',
                 retry_after_ms=None, status: int = 0):
        self.errcode = errcode or 'M_UNKNOWN'
        self.error = error or ''
        self.retry_after_ms = retry_after_ms
        self.status = status
        super().__init__(f'matrix {self.errcode}: {self.error or "未知错误"}')

    @property
    def fatal(self) -> bool:
        # 这几类是凭证/账号问题，重试不会自愈
        return self.errcode in ('M_UNKNOWN_TOKEN', 'M_MISSING_TOKEN',
                                'M_FORBIDDEN', 'M_USER_DEACTIVATED')


def _seg_from_content(content: dict) -> list:
    """Matrix 消息 content → 规范消息段。"""
    if not isinstance(content, dict):
        return []
    msgtype = str(content.get('msgtype') or '')
    body = content.get('body')
    segs = []
    if msgtype in ('m.text', 'm.notice', 'm.emote'):
        if body:
            segs.append({'type': 'text', 'data': {'text': str(body)}})
    elif msgtype == 'm.image':
        segs.append({'type': 'image', 'data': {
            'url': content.get('url'), 'file_id': content.get('url'),
            'name': body, 'width': content.get('w'),
            'height': content.get('h'), 'mime': content.get('mimetype'),
            'size': content.get('size'),
        }})
    elif msgtype in ('m.video',):
        segs.append({'type': 'video', 'data': {
            'url': content.get('url'), 'file_id': content.get('url'),
            'name': body, 'mime': content.get('mimetype'),
            'size': content.get('size'),
        }})
    elif msgtype in ('m.audio',):
        segs.append({'type': 'voice', 'data': {
            'url': content.get('url'), 'file_id': content.get('url'),
            'name': body, 'mime': content.get('mimetype'),
            'size': content.get('size'),
        }})
    elif msgtype in ('m.file',):
        segs.append({'type': 'file', 'data': {
            'url': content.get('url'), 'file_id': content.get('url'),
            'name': body, 'mime': content.get('mimetype'),
            'size': content.get('size'),
        }})
    else:
        if body:
            segs.append({'type': 'text', 'data': {'text': str(body)}})
    return segs


def _mxc_to_http(mxc_url: str, homeserver: str) -> str:
    """mxc://server/mediaid → 可下载的 HTTPS 地址（私有媒体仍需带 token）。"""
    if not isinstance(mxc_url, str) or not mxc_url.startswith('mxc://'):
        return mxc_url or ''
    rest = mxc_url[len('mxc://'):]
    if '/' not in rest:
        return mxc_url
    server, media_id = rest.split('/', 1)
    return (f'{homeserver.rstrip("/")}/_matrix/media/v3/download/'
            f'{quote(server)}/{quote(media_id)}')


def normalize_event(event: dict, room_id: str, bot_name: str,
                    self_id: str = '') -> Optional[dict]:
    """Matrix room event → 内部事件 dict。"""
    if not isinstance(event, dict):
        return None
    if event.get('type') != 'm.room.message':
        return None
    sender = str(event.get('sender') or '')
    if self_id and sender == self_id:
        return None
    content = event.get('content') or {}
    segs = _seg_from_content(content)
    if not segs:
        return None
    text = ''.join(s.get('data', {}).get('text', '') for s in segs
                   if s.get('type') == 'text')

    return {
        'type': 'message',
        'message_type': 'group',
        'sub_type': '',
        'bot_name': bot_name,
        'user_id': sender,
        'group_id': room_id,
        'message_id': event.get('event_id'),
        'message': text,
        'raw_message': text,
        'segments': segs,
        'sender': {'user_id': sender, 'nickname': sender},
        'adapter': 'matrix',
        'room_id': room_id,
        'raw': event,
    }


class MatrixAdapter(ProtocolAdapter):
    """Matrix 协议接入端。"""

    def __init__(self, framework, homeserver: str = '',
                 access_token: str = '', user_id: str = '',
                 bot_name: str = 'matrix', sync_timeout: int = 30,
                 rooms=None, reconnect_interval: int = 3):
        self.framework = framework
        self.homeserver = (homeserver or '').rstrip('/')
        self.access_token = (access_token or '').strip()
        self.user_id = (user_id or '').strip()
        self.bot_name = bot_name or 'matrix'
        self.sync_timeout = max(1, int(sync_timeout or 30))
        # 空 = 所有已加入房间；配置了就只有这些房间
        self.rooms = [r for r in (rooms or []) if r]
        self.reconnect_interval = max(1, int(reconnect_interval or 3))
        self._backoff = Backoff(base=self.reconnect_interval, maximum=60)

        self._connected = False
        self._closing = False
        self._task = None
        self._since = ''
        self._fatal_reason = ''
        self._loop = None
        self._http = requests.Session()
        self._http.headers.update({
            'Authorization': f'Bearer {self.access_token}',
            'User-Agent': 'ZCBOT (matrix adapter, 1.0)',
        })
        self._sync_error_warned = False

    # ── 连接自描述 ──────────────────────────────────────────────

    def get_connection_info(self) -> dict:
        return {
            'id': 'matrix',
            'name': 'Matrix',
            'config_section': 'matrix',
            'fields': [
                {'key': 'homeserver', 'label': 'Homeserver',
                 'type': 'string', 'required': True},
                {'key': 'access_token', 'label': 'Access Token',
                 'type': 'password', 'required': True},
                {'key': 'user_id', 'label': 'User ID (@bot:server)',
                 'type': 'string', 'required': True},
                {'key': 'bot_name', 'label': 'Bot 名', 'type': 'string'},
                {'key': 'sync_timeout', 'label': '长轮询超时(秒)',
                 'type': 'number'},
            ],
            'restart_keys': ['homeserver', 'access_token', 'user_id'],
            'endpoint_hint': '/_matrix/client/v3/sync',
            'guide': '用任意 Matrix 客户端登录机器人账号，'
                     '获取 access token（Element：设置 → 帮助与关于 → '
                     '访问令牌）填入；把机器人邀请进目标房间即可。'
                     '也支持自建 homeserver。'
                     'user_id 必填：/sync 会回推机器人自己发的事件，'
                     '缺了它自身消息回显过滤不生效，会命中命令后自激刷屏'
                     '（留空时框架会尝试用 /account/whoami 自动补全）。',
            'status': {
                'connected': self._connected,
                'since': bool(self._since),
                'fatal': self._fatal_reason,
            },
        }

    # ── since 持久化 ────────────────────────────────────────────

    def _state_file(self) -> str:
        from framework.config import get_home
        return os.path.join(get_home(), 'data', 'matrix_sync.json')

    def _state_key(self) -> str:
        parsed = urlparse(self.homeserver)
        return f'{parsed.netloc or self.homeserver}|{self.user_id}'

    def _load_since(self) -> str:
        try:
            with open(self._state_file(), 'r', encoding='utf-8') as f:
                state = json.load(f) or {}
            if state.get('key') != self._state_key():
                logger.info('matrix since 记录属于另一个账号/homeserver，忽略')
                return ''
            return str(state.get('since') or '')
        except FileNotFoundError:
            return ''
        except Exception as e:  # noqa: BLE001
            logger.warning('matrix since 读取失败，从头开始: %s', e)
            return ''

    def _save_since(self):
        try:
            path = self._state_file()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'key': self._state_key(), 'since': self._since,
                           'updated_at': time.strftime('%Y-%m-%d %H:%M:%S')}, f)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            logger.warning('matrix since 保存失败（不影响收消息）: %s', e)

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self):
        self._closing = False
        self._fatal_reason = ''
        self._since = self._since or self._load_since()
        self._schedule()

    def _schedule(self):
        loop = getattr(self.framework, 'loop', None)
        if loop is None or not loop.is_running():
            if not self._closing:
                t = threading.Timer(0.5, self._schedule)
                t.daemon = True
                t.start()
            return
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._sync_loop())

    async def _sync_loop(self):
        backoff = Backoff(base=self.reconnect_interval, maximum=60)
        while not self._closing:
            try:
                data = await asyncio.to_thread(self._sync)
                await self._accept_sync(data)
                self._connected = True
                self._sync_error_warned = False
                backoff.reset()
            except asyncio.CancelledError:
                raise
            except MatrixApiError as e:
                self._connected = False
                if e.fatal:
                    self._fatal_reason = f'{e.errcode}: {e.error}'
                    logger.error(
                        'matrix 凭证/权限错误（%s），已停止轮询：%s。'
                        '换 access_token 后需重启接入端。', e.errcode, e.error)
                    break
                if e.errcode == 'M_LIMIT_EXCEEDED':
                    wait = (e.retry_after_ms or 3000) / 1000.0
                    logger.warning('matrix 限流，按 retry_after_ms 等待 %.1fs',
                                   wait)
                    await asyncio.sleep(wait)
                    continue
                if not self._sync_error_warned:
                    self._sync_error_warned = True
                    logger.warning('matrix sync 失败: %s（后续同类错误降为 debug）', e)
                else:
                    logger.debug('matrix sync 失败: %s', e)
                await asyncio.sleep(backoff.next_delay(0))
            except Exception as e:  # noqa: BLE001
                self._connected = False
                logger.warning('matrix 轮询异常: %s', e)
                await asyncio.sleep(backoff.next_delay(0))

    async def _accept_sync(self, data: dict):
        next_batch = (data or {}).get('next_batch')
        if next_batch:
            self._since = str(next_batch)
            # 先分发、后落盘（at-least-once）。旧实现在这里就 _save_since()：
            # 进程若在落盘之后、事件入队之前崩溃，服务端不再重放该批 timeline，
            # 整批消息永久丢失。Telegram 的 offset 正是「整批处理完再落盘」。
        rooms = ((data or {}).get('rooms') or {}).get('join') or {}
        for room_id, payload in rooms.items():
            if self.rooms and room_id not in self.rooms:
                continue
            events = ((payload or {}).get('timeline') or {}).get('events') or []
            for ev in events:
                if self._closing:
                    return
                event = normalize_event(ev, room_id, self.bot_name, self.user_id)
                if event is None:
                    continue
                try:
                    await self.framework.dispatch_event(event)
                except Exception as e:  # noqa: BLE001
                    logger.error('matrix 事件分发失败: %s', e)
        # 本批全部入队后才推进 since；_closing 时故意不落盘，重启后重放
        if next_batch and not self._closing:
            self._save_since()

    # ── REST ────────────────────────────────────────────────────

    def _request(self, method: str, path: str, *, params=None,
                 json_body=None, timeout: int = 30) -> dict:
        url = f'{self.homeserver}{path}'
        try:
            resp = self._http.request(
                method, url, params=params, json=json_body, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            raise MatrixApiError('M_NETWORK', f'{type(e).__name__}: {e}')
        try:
            body = resp.json() if resp.content else {}
        except Exception:  # noqa: BLE001
            body = {}
        if resp.status_code >= 400:
            retry_after_ms = None
            if isinstance(body, dict):
                retry_after_ms = body.get('retry_after_ms')
            raise MatrixApiError(
                (body or {}).get('errcode') or f'HTTP_{resp.status_code}',
                (body or {}).get('error') or str(body)[:200],
                retry_after_ms, resp.status_code)
        return body if isinstance(body, dict) else {}

    def _sync(self) -> dict:
        params = {'timeout': self.sync_timeout * 1000}
        if self._since:
            params['since'] = self._since
        if self.rooms:
            params['filter'] = json.dumps({
                'room': {'rooms': self.rooms,
                         'timeline': {'limit': 50}}})
        return self._request('GET', '/_matrix/client/v3/sync', params=params,
                             timeout=self.sync_timeout + 20)

    def send_message(self, room_id: str, text: str,
                     reply_to: str = None) -> dict:
        """发文本。

        txnId 只在这一次调用里生成一次：Matrix 用它做幂等，
        同一个 txnId 重试不会重复发；换 txnId 就会真的发第二条。
        """
        txn_id = f'zcbot{int(time.time() * 1000)}{uuid.uuid4().hex[:8]}'
        content = {'msgtype': 'm.text', 'body': text}
        if reply_to:
            content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply_to}}
        path = (f'/_matrix/client/v3/rooms/{quote(room_id, safe="")}/send/'
                f'm.room.message/{txn_id}')
        try:
            data = self._request('PUT', path, json_body=content, timeout=30)
        except MatrixApiError as e:
            if e.errcode == 'M_LIMIT_EXCEEDED':
                return {'status': 'failed', 'retcode': -4, 'throttled': True,
                        'retry_after': int((e.retry_after_ms or 3000) / 1000),
                        'msg': f'matrix 限流，建议等待 '
                               f'{(e.retry_after_ms or 3000) / 1000:.0f}s'}
            if e.errcode in ('M_FORBIDDEN', 'M_UNKNOWN_TOKEN'):
                return {'status': 'failed', 'retcode': -1,
                        'error_code': e.errcode,
                        'msg': f'matrix 拒绝发送（{e.errcode}）：'
                               f'token 失效或机器人不在该房间'}
            return {'status': 'failed', 'retcode': -1,
                    'error_code': e.errcode, 'msg': str(e)}
        return {'status': 'ok', 'retcode': 0, 'data': data}

    # ── 消息契约 ────────────────────────────────────────────────

    def capabilities(self):
        from framework.messaging.contract import (
            CAP_FILE, CAP_IMAGE, CAP_TEXT, CAP_VIDEO, CAP_VOICE, Capabilities,
        )
        return Capabilities(
            inbound=[CAP_TEXT, CAP_IMAGE, CAP_VIDEO, CAP_VOICE, CAP_FILE],
            # 媒体出站要先上传到 /_matrix/media/v3/upload，本端暂未实现
            outbound=[CAP_TEXT],
        )

    def normalize_incoming(self, raw_message) -> list:
        if isinstance(raw_message, dict):
            return _seg_from_content(raw_message.get('content') or {})
        from framework.messaging.segments import normalize_message
        return normalize_message(raw_message)

    async def handle_event(self, raw_event: dict,
                           bot_name: str) -> Optional[dict]:
        room_id = raw_event.get('room_id') or raw_event.get('group_id') or ''
        return normalize_event(raw_event, room_id,
                               bot_name or self.bot_name, self.user_id)

    async def call_api(self, action, bot=None, **params) -> dict:
        action = (action or '').strip()
        if action in ('send_msg', 'send_group_msg', 'send_text'):
            room_id = (params.get('group_id') or params.get('room_id')
                       or params.get('channel_id'))
            text = params.get('message', params.get('text', ''))
            if not room_id:
                return {'status': 'failed', 'retcode': -2,
                        'msg': 'matrix 发送需要 group_id/room_id'}
            return await asyncio.to_thread(
                self.send_message, str(room_id), str(text),
                params.get('reply_to'))

        if action in ('get_status', 'get_login_info'):
            return {'status': 'ok', 'retcode': 0,
                    'data': {'online': self._connected,
                             'self_id': self.user_id,
                             'nickname': self.bot_name,
                             'since': bool(self._since)}}

        return {'status': 'failed', 'retcode': -10,
                'msg': f'matrix 不支持动作 {action}'}

    async def send_text(self, text, *, user_id=None, group_id=None,
                        source=None) -> dict:
        return await self.call_api('send_msg', bot=source, user_id=user_id,
                                   group_id=group_id, message=text)

    def get_connected_bots(self) -> list:
        return [self.bot_name] if self._connected else []

    async def stop(self):
        self._closing = True
        self._connected = False
        t, self._task = self._task, None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        try:
            self._http.close()
        except Exception:  # noqa: BLE001
            pass


# ── 注册入口 ──────────────────────────────────────────────────

_adapter_instance = None


def register(ctx):
    global _adapter_instance
    fw = ctx._framework
    cfg = fw.config.get('matrix', {})
    if isinstance(cfg, bool):
        if cfg is not True:
            ctx.log('Matrix 接入端已禁用 (matrix.enabled: true 才开启)')
            return
        cfg = {}
    if cfg.get('enabled') is not True:
        ctx.log('Matrix 接入端已禁用 (matrix.enabled: true 才开启)')
        return
    _adapter_instance = MatrixAdapter(
        fw,
        homeserver=cfg.get('homeserver') or '',
        access_token=cfg.get('access_token') or '',
        user_id=cfg.get('user_id') or '',
        bot_name=cfg.get('bot_name') or 'matrix',
        sync_timeout=cfg.get('sync_timeout') or 30,
        rooms=cfg.get('rooms') or None,
        reconnect_interval=cfg.get('reconnect_interval') or 3,
    )
    fw.services.register('protocol_adapter', _adapter_instance)
    fw.services.register('api_caller', _adapter_instance)
    # user_id 是「过滤自身消息回显」的唯一依据。旧配置常只填 homeserver +
    # access_token，导致 /sync 回推自己的消息 → 命中命令 → 再发 → 自激刷屏。
    # 优先用 whoami 自动补全，失败则明确告警（不阻断启动，交由部署方补配置）。
    if (not _adapter_instance.user_id and _adapter_instance.homeserver
            and _adapter_instance.access_token):
        try:
            me = _adapter_instance._request(
                'GET', '/_matrix/client/v3/account/whoami', timeout=5)
            _adapter_instance.user_id = (me or {}).get('user_id') or ''
        except Exception as e:  # noqa: BLE001
            logger.warning('Matrix 无法自动获取 user_id: %s', e)
        if _adapter_instance.user_id:
            ctx.log(f'Matrix 已自动补全 user_id: {_adapter_instance.user_id}')
        else:
            logger.warning(
                'Matrix 未配置 user_id 且 whoami 补全失败：自身消息回显无法过滤，'
                '可能造成自激刷屏，请在 core_plugins.yaml 补 matrix.user_id')
    if _adapter_instance.homeserver and _adapter_instance.access_token:
        _adapter_instance.start()
        ctx.log('Matrix 接入端已启动')
    else:
        ctx.log('Matrix 接入端缺少 homeserver/access_token，已跳过连接')
