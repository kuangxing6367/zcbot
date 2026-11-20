"""本地回环控制通道服务端。

框架启动完成后监听 127.0.0.1 的随机端口，把端口与每次启动重新生成的令牌写入
state_dir/control.json（POSIX 权限 0600）。只提供黑匣子读取命令，不提供写文件、
执行命令或改配置的入口。
"""
import asyncio
import hmac
import json
import logging
import os
import secrets
import stat
import time

from aiohttp import web

from framework import blackbox
from framework.control._common import (
    CONTROL_VERSION,
    HOST,
    LOOPBACK_HOSTS,
    control_path,
    load_info,
    pid_alive,
)
from framework.paths import state_dir as default_state_dir
from framework.paths import version_path

logger = logging.getLogger('zcbot')

MAX_BODY = 64 * 1024
REQUEST_TIMEOUT = 5.0
CLEAR_DENIED = '服务端未允许清空黑匣子'

__all__ = ['CLEAR_DENIED', 'ControlServer', 'start_control']


class CommandError(Exception):
    """命令级错误：作为 ok:false 返回，不把异常细节发给客户端。"""


def _framework_version():
    try:
        with open(version_path(), 'r', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return '0.0.0+unknown'


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _events(limit):
    events = []
    for eid in blackbox.recent_event_ids(limit=limit):
        events.append({
            'event_id': eid,
            'stuck_at': blackbox.stuck_at(eid),
            'steps': len(blackbox.get_event_trace(eid)),
        })
    return events


def execute(cmd, args, allow_clear=False):
    """执行命令；黑匣子数据只经 framework.blackbox 的模块级函数取得。"""
    if cmd == 'ping':
        return {'pong': True}
    if cmd == 'blackbox.stats':
        return blackbox.stats()
    if cmd == 'blackbox.frames':
        kwargs = {}
        if 'limit' in args:
            kwargs['limit'] = max(0, _as_int(args['limit'], 0))
        if 'since' in args:
            since = _as_float(args['since'])
            if since is not None:
                kwargs['since'] = since
        if 'event_id' in args:
            kwargs['event_id'] = str(args['event_id'])
        return {'frames': blackbox.get_frames(**kwargs)}
    if cmd == 'blackbox.trace':
        eid = str(args.get('event_id', ''))
        return {
            'event_id': eid,
            'frames': blackbox.get_event_trace(eid),
            'stuck_at': blackbox.stuck_at(eid),
        }
    if cmd == 'blackbox.recent':
        limit = max(1, min(200, _as_int(args.get('limit', 30), 30)))
        events = _events(limit)
        return {'ids': [e['event_id'] for e in events], 'events': events}
    if cmd == 'blackbox.clear':
        if not allow_clear:
            raise CommandError(CLEAR_DENIED)
        if args.get('confirm') is not True:
            raise CommandError('清空需要 confirm: true')
        blackbox.clear()
        return {'cleared': True}
    raise CommandError('unknown command')


class ControlServer:
    def __init__(self, state_dir=None, port=0, allow_clear=False):
        self.state_dir = state_dir or default_state_dir()
        self.path = control_path(self.state_dir)
        self.port = int(port or 0)
        self.allow_clear = bool(allow_clear)
        self.token = secrets.token_hex(32)
        self.host = HOST
        self.runner = None
        self.site = None
        self.started_at = 0
        self.actual_port = 0
        self.log_path = os.path.join(self.state_dir, 'logs', 'control_access.log')
        self._audit_lock = asyncio.Lock()

    async def start(self):
        stale = load_info(self.path)
        if stale and not pid_alive(stale.get('pid')):
            try:
                os.remove(self.path)
            except OSError as exc:
                logger.debug('控制通道残留文件删除失败: %s', exc)

        app = web.Application(client_max_size=MAX_BODY)
        app.router.add_post('/rpc', self._handle_rpc)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host=HOST, port=self.port)
        try:
            await site.start()
        except Exception:
            await runner.cleanup()
            raise
        self.runner = runner
        self.site = site
        self.actual_port = runner.addresses[0][1]
        self.started_at = int(time.time())
        try:
            self._write_info()
        except Exception:
            await runner.cleanup()
            self.runner = None
            self.site = None
            raise
        logger.info('本地控制通道已启动: %s:%s（pid=%s）',
                    HOST, self.actual_port, os.getpid())
        return self

    async def stop(self):
        runner, self.runner, self.site = self.runner, None, None
        if runner is not None:
            await runner.cleanup()
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug('control.json 删除失败: %s', exc)
        logger.info('本地控制通道已停止')

    def _write_info(self):
        os.makedirs(self.state_dir, exist_ok=True)
        info = {
            'version': CONTROL_VERSION,
            'pid': os.getpid(),
            'host': HOST,
            'port': self.actual_port,
            'token': self.token,
            'started_at': self.started_at,
            'framework_version': _framework_version(),
        }
        with open(self.path, 'w', encoding='utf-8', newline='') as f:
            json.dump(info, f, ensure_ascii=False, sort_keys=True)
            f.write('\n')
        self._apply_permissions()

    def _apply_permissions(self):
        if os.name != 'posix':
            logger.debug('control.json 权限 0600 在非 POSIX 平台不生效（os.name=%s）', os.name)
            return
        try:
            os.chmod(self.path, 0o600)
            mode = stat.S_IMODE(os.stat(self.path).st_mode)
        except OSError as exc:
            logger.debug('control.json 权限设置失败: %s', exc)
            return
        if mode != 0o600:
            logger.debug('control.json 实际权限为 %o，未达到 0600', mode)

    def _authorize(self, request):
        remote = request.remote or ''
        if remote not in LOOPBACK_HOSTS:
            return 403, 'forbidden'
        header = request.headers.get('Authorization', '')
        token = header[7:] if header.startswith('Bearer ') else ''
        if not token or not hmac.compare_digest(token, self.token):
            return 401, 'unauthorized'
        return 0, ''

    async def _handle_rpc(self, request):
        cmd = ''
        try:
            payload = json.loads(await request.read())
            if isinstance(payload, dict):
                cmd = str(payload.get('cmd', ''))
                args = payload.get('args') or {}
            else:
                args = {}
            if not isinstance(args, dict):
                args = {}
        except (ValueError, UnicodeDecodeError):
            args = {}

        status, error = self._authorize(request)
        if status:
            body = {'ok': False, 'error': error}
        else:
            try:
                data = await asyncio.wait_for(
                    asyncio.to_thread(execute, cmd, args, self.allow_clear),
                    REQUEST_TIMEOUT,
                )
                body = {'ok': True, 'data': data}
            except CommandError as exc:
                body = {'ok': False, 'error': str(exc)}
            except asyncio.TimeoutError:
                body = {'ok': False, 'error': 'request timeout'}
            except Exception:
                logger.exception('控制通道命令执行异常: %s', cmd)
                body = {'ok': False, 'error': 'internal error'}

        await self._audit(cmd, request.remote or '', body['ok'])
        return web.json_response(body, status=status or 200)

    async def _audit(self, cmd, remote, ok):
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            line = json.dumps({
                'ts': time.time(),
                'cmd': cmd,
                'remote': remote,
                'frames': blackbox.stats()['frames'],
                'ok': bool(ok),
            }, ensure_ascii=False, sort_keys=True)
            async with self._audit_lock:
                with open(self.log_path, 'a', encoding='utf-8', newline='') as f:
                    f.write(line + '\n')
        except OSError as exc:
            logger.debug('控制通道审计写入失败: %s', exc)


async def start_control(state_dir=None, cfg=None, port=None):
    """框架启动收尾调用；cfg 为 framework.control 配置段。"""
    cfg = cfg or {}
    if cfg.get('enabled', True) is False:
        logger.info('本地控制通道已禁用（framework.control.enabled: false）')
        return None
    if port is None:
        port = cfg.get('port', 0)
    server = ControlServer(
        state_dir=state_dir,
        port=port,
        allow_clear=cfg.get('allow_clear', False),
    )
    return await server.start()
