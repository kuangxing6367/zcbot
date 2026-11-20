"""本地控制通道客户端（只用标准库）。

令牌只从 state_dir/control.json 读取，不进入命令行参数，也不打印。
"""
import json
import string
import urllib.error
import urllib.request

from framework.control._common import (
    CONTROL_VERSION,
    control_path,
    is_loopback,
    load_info,
    pid_alive,
)

NO_INSTANCE_MESSAGE = '未发现运行中的 zcbot 实例（控制通道文件不存在或进程已退出）'

__all__ = ['NO_INSTANCE_MESSAGE', 'Client', 'ControlError', 'NoRunningInstance', 'discover']


class NoRunningInstance(Exception):
    """control.json 不存在、内容无效或记录进程已退出。"""


class ControlError(Exception):
    """控制通道返回了失败结果或无法连接。"""


def discover(state_dir=None):
    """读取并校验 control.json；失败抛 NoRunningInstance。"""
    info = load_info(control_path(state_dir))
    if not info or info.get('version') != CONTROL_VERSION:
        raise NoRunningInstance(NO_INSTANCE_MESSAGE)
    if not pid_alive(info.get('pid')):
        raise NoRunningInstance(NO_INSTANCE_MESSAGE)
    if not is_loopback(info.get('host')):
        raise NoRunningInstance(NO_INSTANCE_MESSAGE)
    port = info.get('port')
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise NoRunningInstance(NO_INSTANCE_MESSAGE)
    token = info.get('token')
    if not isinstance(token, str) or len(token) != 64:
        raise NoRunningInstance(NO_INSTANCE_MESSAGE)
    if any(c not in string.hexdigits for c in token):
        raise NoRunningInstance(NO_INSTANCE_MESSAGE)
    return info


class Client:
    def __init__(self, info, timeout=3):
        self.host = info['host']
        self.port = int(info['port'])
        self.url = f'http://{self.host}:{self.port}/rpc'
        self.timeout = timeout
        self._token = info['token']

    @classmethod
    def connect(cls, state_dir=None, timeout=3):
        return cls(discover(state_dir), timeout=timeout)

    def call(self, cmd, **args):
        body = json.dumps(
            {'cmd': cmd, 'args': args}, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(
            self.url,
            data=body,
            method='POST',
            headers={
                'Authorization': 'Bearer ' + self._token,
                'Content-Type': 'application/json',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise ControlError('控制通道拒绝非回环来源') from exc
            if exc.code == 401:
                raise ControlError('控制通道令牌校验失败') from exc
            raise ControlError(f'控制通道返回 HTTP {exc.code}') from exc
        except urllib.error.URLError as exc:
            raise ControlError(f'无法连接运行中的实例: {exc.reason}') from exc
        try:
            result = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ControlError('控制通道返回了无法解析的响应') from exc
        if not isinstance(result, dict) or not result.get('ok'):
            error = result.get('error', 'unknown error') if isinstance(result, dict) else 'unknown error'
            raise ControlError(str(error))
        return result.get('data')

    def stats(self):
        return self.call('blackbox.stats')

    def frames(self, limit=None, since=None, event_id=None):
        args = {}
        if limit is not None:
            args['limit'] = limit
        if since is not None:
            args['since'] = since
        if event_id is not None:
            args['event_id'] = event_id
        return self.call('blackbox.frames', **args)

    def trace(self, event_id):
        return self.call('blackbox.trace', event_id=event_id)

    def recent(self, limit=30):
        return self.call('blackbox.recent', limit=limit)

    def clear(self, confirm=True):
        return self.call('blackbox.clear', confirm=confirm)
