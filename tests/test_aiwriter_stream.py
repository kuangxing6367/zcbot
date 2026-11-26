"""AI 智能体的流式接口：事件要真流出来，停止要真能停。

原来的 `/api/aiwriter/chat` 是阻塞请求——一轮跑多久，浏览器就只有一个
「正在处理」，看不到过程也中断不了。这里锁住新链路的行为。
"""
import json

from flask import Flask

from framework.api import aiwriter as aiwriter_api


class _FakeSvc:
    """最小 svc：按顺序吐文本增量，支持取消探针。"""

    def __init__(self):
        self.last_change_id = None
        self.cancelled = False

    def active_session(self):
        return 'terminal'

    def model_name(self):
        return 'fake-model'

    def run(self, text, on_event=None, session=None, should_stop=None):
        emit = on_event or (lambda kind, payload=None: None)
        emit('text', '你')
        if should_stop and should_stop():
            self.cancelled = True
            return '你'
        emit('tool_start', {'name': 'read', 'args': {'path': 'a.py'}})
        emit('tool_end', {'name': 'read', 'ok': True, 'result': 'ok'})
        emit('text', '好')
        return '你好'

    def config(self):
        return {'model': 'fake-model', 'mode': 'workspace'}

    def configure(self, patch):
        if patch.get('mode') == 'bad':
            return False, 'mode 不行'
        return True, patch


class _FakeFramework:
    def __init__(self, svc):
        self.services = {'aiwriter': svc}


class _Ctx:
    def __init__(self, svc):
        self.app = Flask(__name__)
        self.framework = _FakeFramework(svc)
        self.audit_log = lambda *a, **k: None

        def require_super(fn):
            def wrapper(*a, **k):
                from flask import request
                request.admin = {'id': 1, 'username': 'admin', 'role': 'super'}
                return fn(*a, **k)
            wrapper.__name__ = getattr(fn, '__name__', 'wrapped')
            return wrapper
        self.require_super = require_super


def _client(svc=None):
    svc = svc or _FakeSvc()
    ctx = _Ctx(svc)
    aiwriter_api.register(ctx)
    return ctx.app.test_client(), svc


def _events(resp):
    """把 SSE 响应体解析成事件列表。"""
    out = []
    for block in resp.get_data(as_text=True).split('\n\n'):
        for line in block.split('\n'):
            if line.startswith('data:'):
                out.append(json.loads(line[5:].strip()))
    return out


def test_stream_emits_incremental_events():
    client, _svc = _client()
    resp = client.post('/api/aiwriter/chat/stream', json={'text': '你好', 'session': 's1'})
    assert resp.status_code == 200
    assert resp.mimetype == 'text/event-stream'

    events = _events(resp)
    kinds = [e['kind'] for e in events]
    assert 'text' in kinds and 'tool_start' in kinds and 'tool_end' in kinds
    assert kinds[-1] == 'done', kinds

    done = events[-1]['payload']
    assert done['text'] == '你好'
    assert done['session'] == 's1'
    assert done['cancelled'] is False
    # 文本是增量推的，不是最后一次性给
    assert [e['payload'] for e in events if e['kind'] == 'text'] == ['你', '好']


def test_stream_rejects_empty_text():
    client, _svc = _client()
    resp = client.post('/api/aiwriter/chat/stream', json={'text': '   '})
    assert resp.status_code == 400


def test_cancel_without_running_job_is_friendly():
    client, _svc = _client()
    resp = client.post('/api/aiwriter/chat/cancel', json={'session': 'nope'})
    assert resp.status_code == 200
    assert resp.get_json()['data']['cancelled'] is False


def test_config_get_and_put():
    client, _svc = _client()
    r = client.get('/api/aiwriter/config')
    assert r.status_code == 200
    assert r.get_json()['data']['model'] == 'fake-model'

    r = client.put('/api/aiwriter/config', json={'temperature': 0.5})
    assert r.status_code == 200
    assert r.get_json()['code'] == 0

    r = client.put('/api/aiwriter/config', json={'mode': 'bad'})
    assert r.status_code == 400
    assert 'mode' in r.get_json()['msg']


def test_status_reports_idle():
    client, _svc = _client()
    r = client.get('/api/aiwriter/chat/status?session=s9')
    assert r.status_code == 200
    assert r.get_json()['data']['running'] is False


if __name__ == '__main__':
    for fn in (test_stream_emits_incremental_events, test_stream_rejects_empty_text,
               test_cancel_without_running_job_is_friendly, test_config_get_and_put,
               test_status_reports_idle):
        fn()
        print('ok', fn.__name__)
