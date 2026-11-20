"""本地控制通道：回环监听、令牌校验、文件生命周期与 CLI 端到端。

服务端在自己的线程与事件循环里运行，客户端与 CLI 子进程从外部连接，
与真实「另一个进程查看运行中框架」的路径一致。
"""
import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest
import yaml
from aiohttp.web_request import BaseRequest

from framework import blackbox
from framework.config import _CONFIG_SCHEMA, _DEFAULT_CONFIG, validate_config
from framework.control.client import Client, ControlError, NoRunningInstance
from framework.control.server import ControlServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_INSTANCE = '未发现运行中的 zcbot 实例（控制通道文件不存在或进程已退出）'


class ServerThread:
    """在独立线程里跑控制服务，让同步客户端可以跨进程式调用。"""

    def __init__(self, **kwargs):
        self.server = ControlServer(**kwargs)
        self.ready = threading.Event()
        self.loop = None
        self.stop_event = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        async def main():
            self.loop = asyncio.get_running_loop()
            await self.server.start()
            self.stop_event = asyncio.Event()
            self.ready.set()
            await self.stop_event.wait()
            await self.server.stop()

        asyncio.run(main())

    def start(self):
        self.thread.start()
        assert self.ready.wait(10)

    def stop(self):
        if self.loop is not None and self.stop_event is not None:
            self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(10)


@pytest.fixture
def channel(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    state = home / 'data'
    runner = ServerThread(state_dir=str(state))
    runner.start()
    blackbox.clear()
    monkeypatch.setenv('ZCBOT_HOME', str(home))
    info = json.loads((state / 'control.json').read_text(encoding='utf-8'))
    yield runner.server, info, Client.connect(state_dir=str(state)), home
    runner.stop()
    blackbox.clear()


def _post(info, cmd='ping', args=None, token='good', payload=None):
    body = payload if payload is not None else {'cmd': cmd, 'args': args or {}}
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    headers = {'Content-Type': 'application/json'}
    if token == 'good':
        headers['Authorization'] = 'Bearer ' + info['token']
    elif token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/rpc",
        data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8')
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def _cli_env(home):
    env = os.environ.copy()
    env['ZCBOT_HOME'] = str(home)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['CODEBUDDY_SAFE_DELETE_ENABLED'] = '0'
    for key in ('ZCBOT_CONFIG_PATH', 'ZCBOT_STATE_LAYOUT'):
        env.pop(key, None)
    return env


def _cli(home, *extra):
    return subprocess.run(
        [sys.executable, '-m', 'framework.cli', 'debug', 'blackbox', *extra],
        cwd=ROOT, env=_cli_env(home), capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=30)


def _record_frames():
    blackbox.record('ingest', event_id='e-done', msg='进入内核')
    blackbox.record('done', event_id='e-done', msg='处理结束')
    blackbox.record('ingest', event_id='e-stuck', msg='停在这一步')


def test_control_file_fields_and_loopback_bind(channel):
    server, info, _client, _home = channel
    assert info['version'] == 1
    assert info['pid'] == os.getpid()
    assert info['host'] == '127.0.0.1'
    assert info['framework_version'] == '1.28.5'
    assert len(info['token']) == 64
    assert set(info['token']) <= set('0123456789abcdef')
    assert 1 <= info['port'] <= 65535
    assert server.runner.addresses[0][0] == '127.0.0.1'


def test_control_file_permissions(channel):
    _server, _info, _client, home = channel
    if os.name != 'posix':
        pytest.skip('Windows 上 chmod 不生效')
    path = home / 'data' / 'control.json'
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_rpc_commands(channel):
    _server, info, client, _home = channel
    _record_frames()
    assert _post(info)[1]['data']['pong'] is True
    stats = client.stats()
    assert stats['frames'] >= 3
    frames = client.frames(limit=2)['frames']
    assert len(frames) == 2
    assert frames[-1]['eid'] == 'e-stuck'
    trace = client.trace('e-done')
    assert [f['stage'] for f in trace['frames']] == ['ingest', 'done']
    assert trace['stuck_at'] is None
    recent = client.recent()
    assert set(recent['ids']) == {'e-done', 'e-stuck'}
    stuck = [e for e in recent['events'] if e['stuck_at']]
    assert stuck[0]['event_id'] == 'e-stuck'
    assert stuck[0]['stuck_at']['stage'] == 'ingest'


def test_auth_missing_and_wrong_token(channel):
    _server, info, _client, _home = channel
    assert _post(info, token=None)[0] == 401
    assert _post(info, token='0' * 64)[0] == 401
    with pytest.raises(NoRunningInstance):
        Client.connect(state_dir=str(_home / 'missing'))


def test_non_loopback_remote_forbidden(channel, monkeypatch):
    _server, info, _client, _home = channel
    monkeypatch.setattr(BaseRequest, 'remote', property(lambda self: '10.1.2.3'))
    status, body = _post(info)
    assert status == 403
    assert body['ok'] is False


def test_unknown_command_does_not_leak_traceback(channel):
    _server, info, _client, _home = channel
    status, body = _post(info, cmd='os.system')
    assert status == 200
    assert body == {'ok': False, 'error': 'unknown command'}


def test_audit_log_lines(channel):
    _server, _info, client, home = channel
    client.stats()
    with pytest.raises(ControlError):
        client.clear()
    path = home / 'data' / 'logs' / 'control_access.log'
    lines = [json.loads(line) for line in
             path.read_text(encoding='utf-8').splitlines() if line.strip()]
    assert {'ts', 'cmd', 'remote', 'frames', 'ok'} <= set(lines[0])
    assert lines[0]['remote'] == '127.0.0.1'
    assert any(line['cmd'] == 'blackbox.stats' and line['ok'] for line in lines)
    assert any(line['cmd'] == 'blackbox.clear' and not line['ok'] for line in lines)


def test_stop_removes_control_file(tmp_path):
    st = ServerThread(state_dir=str(tmp_path))
    st.start()
    assert (tmp_path / 'control.json').is_file()
    st.stop()
    assert not (tmp_path / 'control.json').exists()


def test_token_regenerated_on_each_start(tmp_path):
    first = ServerThread(state_dir=str(tmp_path))
    first.start()
    old = json.loads((tmp_path / 'control.json').read_text(encoding='utf-8'))['token']
    first.stop()
    second = ServerThread(state_dir=str(tmp_path))
    second.start()
    new = json.loads((tmp_path / 'control.json').read_text(encoding='utf-8'))['token']
    second.stop()
    assert old != new
    assert len(new) == 64


def test_stale_file_removed_when_pid_dead(tmp_path):
    stale_path = tmp_path / 'control.json'
    stale = {
        'version': 1, 'pid': 99999999, 'host': '127.0.0.1', 'port': 1,
        'token': 'a' * 64, 'started_at': 0, 'framework_version': '1.28.4',
    }
    with open(stale_path, 'w', encoding='utf-8', newline='') as f:
        json.dump(stale, f)
    st = ServerThread(state_dir=str(tmp_path))
    st.start()
    info = json.loads(stale_path.read_text(encoding='utf-8'))
    st.stop()
    assert info['pid'] == os.getpid()
    assert info['token'] != stale['token']
    assert info['framework_version'] == '1.28.5'


def test_clear_disabled_by_default(channel):
    _server, info, client, _home = channel
    _record_frames()
    with pytest.raises(ControlError, match='服务端未允许'):
        client.clear(confirm=True)
    assert client.stats()['frames'] >= 3
    status, body = _post(info)
    assert status == 200


def test_clear_enabled_requires_confirm(tmp_path):
    blackbox.clear()
    st = ServerThread(state_dir=str(tmp_path), allow_clear=True)
    st.start()
    try:
        info = json.loads((tmp_path / 'control.json').read_text(encoding='utf-8'))
        client = Client.connect(state_dir=str(tmp_path))
        blackbox.record('ingest', event_id='e-clear', msg='待清空')
        with pytest.raises(ControlError, match='confirm'):
            client.clear(confirm=False)
        assert client.stats()['frames'] == 1
        assert client.clear(confirm=True)['cleared'] is True
        assert client.stats()['frames'] == 0
        assert _post(info)[0] == 200
    finally:
        st.stop()
        blackbox.clear()


def test_request_body_limit(channel):
    _server, info, _client, _home = channel
    status, _body = _post(info, payload={'cmd': 'ping', 'args': {'pad': 'x' * 70000}})
    assert status == 413


def test_cli_json_end_to_end(channel):
    _server, _info, _client, home = channel
    _record_frames()
    result = _cli(home, '--json')
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['stats']['frames'] >= 3
    assert data['recent']['ids']


def test_cli_clear_disabled_exit_code(channel):
    _server, _info, _client, home = channel
    result = _cli(home, '--clear', '--yes')
    assert result.returncode != 0
    assert '服务端未允许' in result.stdout + result.stderr


def test_cli_no_instance(tmp_path):
    result = _cli(tmp_path / 'empty-home', '--json')
    assert result.returncode == 3
    assert NO_INSTANCE in result.stdout


def test_cli_trace_and_stuck_output(channel):
    _server, _info, _client, home = channel
    _record_frames()
    trace = _cli(home, '--trace', 'e-done')
    assert trace.returncode == 0
    assert '完整链路（2 帧）' in trace.stdout
    assert '状态: 已完成' in trace.stdout
    stuck = _cli(home, '--stuck')
    assert stuck.returncode == 0
    assert 'e-stuck: 卡在 #1 ingest' in stuck.stdout
    assert 'e-done' not in stuck.stdout
    last = _cli(home, '--last', '2')
    assert last.returncode == 0
    assert '最近 2 帧' in last.stdout


def test_cli_follow_streams_new_frames(channel):
    _server, _info, _client, home = channel
    proc = subprocess.Popen(
        [sys.executable, '-m', 'framework.cli', 'debug', 'blackbox', '--follow'],
        cwd=ROOT, env=_cli_env(home), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
    try:
        time.sleep(2)
        blackbox.record('ingest', event_id='e-follow', msg='跟随可见帧')
        time.sleep(3)
    finally:
        proc.terminate()
        out, _err = proc.communicate(timeout=10)
    assert '跟随可见帧' in out


def test_config_three_places_in_sync():
    expected = {'enabled': True, 'port': 0, 'allow_clear': False}
    defaults = yaml.safe_load(_DEFAULT_CONFIG)
    with open(os.path.join(ROOT, 'config.yaml'), encoding='utf-8') as f:
        actual = yaml.safe_load(f)
    assert defaults['framework']['control'] == expected
    assert actual['framework']['control'] == expected
    schema = _CONFIG_SCHEMA['framework']['keys']['control']['keys']
    assert set(schema) == set(expected)
    issues = [str(issue) for issue in validate_config(actual)]
    assert not [issue for issue in issues if 'control' in issue]
