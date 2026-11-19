"""llm_core 自动化任务专用身份（保留用户）测试。

跑的是 zip 载荷里的代码，与线上一致。重点验证：
会话隔离（不占真人会话）、不同 task 互不串线、Web API 参数校验与投递。
"""
import asyncio
import os
import sys
import types

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import test_llm_core as base

m = base.mods()
main_mod = m['main']


class _LoopCtx:
    """带同步调异步桥接的 ctx 替身"""

    def __init__(self, send_fail=False):
        self._framework = base._StubFramework()
        self._current_bot = 'stub'
        self.commands = []
        self.raw_handlers = []
        self.event_handlers = []
        self.web_pages = []
        self.apis = []
        self.config = {}
        self.msgs = []
        self.send_fail = send_fail

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def command(self, pattern, handler, **kw):
        self.commands.append((pattern, handler, kw))

    def on_raw_message(self, handler):
        self.raw_handlers.append(handler)

    def on(self, name, handler):
        self.event_handlers.append((name, handler))

    def webui(self, title, entry='index.html', icon=None, order=50, sidebar=False):
        self.web_pages.append({'title': title, 'entry': entry, 'sidebar': sidebar})

    def register_api(self, path, handler, methods=None, auth=True, description=None):
        self.apis.append(path)

    def has_perm(self, user_id, node, context=None, role=None):
        return False

    def log(self, msg, level='info'):
        pass

    async def asend_msg(self, **kw):
        self.msgs.append(kw)

    def send_msg(self, **kw):
        if self.send_fail:
            raise RuntimeError('适配器离线')
        self.msgs.append(kw)

    def call_async(self, coro):
        class Fut:
            def __init__(self, c):
                self.c = c

            def result(self, timeout=None):
                return asyncio.run(self.c)
        return Fut(coro)


def _ready(send_fail=False):
    ctx = _LoopCtx(send_fail)
    main_mod.register(ctx)
    svc = ctx._framework.services.get('llm_core')
    provider = base._fake_provider_script(m, [
        m['providers'].ProviderReply(content='自动化回答'),
        m['providers'].ProviderReply(content='自动化回答二'),
    ])
    svc.unregister_provider('fake')
    svc.register_provider(provider, default=True)
    return ctx, svc


def test_constants_are_reserved():
    assert main_mod.AUTOMATION_USER_ID == '__zcbot_automation__'
    assert main_mod.AUTOMATION_SOURCE == 'automation'
    assert ':' not in main_mod._automation_task_id('a:b/c')


def test_automation_uses_separate_conversation():
    ctx, svc = _ready()
    real = svc.conversation('stub', None, 10001)
    real.append({'role': 'user', 'content': '真人消息'})
    auto = svc.automation_conversation('群总结')
    assert auto.key == 'automation:群总结:__zcbot_automation__'
    assert auto.key != real.key
    assert not any(x.get('content') == '真人消息' for x in auto._messages)


def test_tasks_do_not_share_history():
    ctx, svc = _ready()
    a = svc.automation_conversation('群总结')
    b = svc.automation_conversation('每日抓取')
    a.append({'role': 'user', 'content': '仅 A'})
    assert a.key != b.key
    assert not any(x.get('content') == '仅 A' for x in b._messages)


def test_aautomation_runs_and_isolates_from_chat_gate():
    ctx, svc = _ready()
    main_mod._CHAT_GATES.append(lambda uid: '真人被拦' if uid == 10001 else None)
    try:
        auto = asyncio.run(svc.aautomation('总结群聊', task='群总结'))
        assert auto.ok and auto.text == '自动化回答', auto
    finally:
        main_mod._CHAT_GATES.clear()
    assert not ctx.msgs, '自动化不应自己发消息'


def test_aautomation_rejects_empty_text():
    ctx, svc = _ready()
    try:
        asyncio.run(svc.aautomation('  '))
        raise AssertionError('空文本必须报错')
    except ValueError:
        pass


def test_view_automation_validates_and_delivers():
    try:
        from flask import Flask
    except ImportError:
        return
    ctx, svc = _ready()
    app = Flask(__name__)
    app.add_url_rule('/api/llm_core/automation', 'automation',
                     main_mod._view_automation, methods=['POST'])
    app.add_url_rule('/api/llm_core/automation/sessions', 'auto_sessions',
                     main_mod._view_automation_sessions, methods=['GET'])
    main_mod.ctx = ctx
    try:
        client = app.test_client()
        assert client.post('/api/llm_core/automation', json={}).get_json()['code'] == 1
        assert client.post('/api/llm_core/automation',
                           json={'text': 'x', 'user_id': 'abc'}).get_json()['code'] == 1
        r = client.post('/api/llm_core/automation', json={
            'text': '总结群聊', 'task': '群总结', 'group_id': 20002})
        body = r.get_json()
        assert body['code'] == 0, body
        assert body['data']['text'] == '自动化回答' and body['data']['delivered'] is True
        assert ctx.msgs and ctx.msgs[-1]['group_id'] == 20002
        sessions = client.get('/api/llm_core/automation/sessions').get_json()
        assert sessions['code'] == 0 and sessions['data']['count'] >= 1
        assert all(s['meta']['source'] == 'automation' for s in sessions['data']['sessions'])
    finally:
        main_mod.ctx = None


def test_view_automation_reports_delivery_failure():
    try:
        from flask import Flask
    except ImportError:
        return
    ctx, svc = _ready(send_fail=True)
    main_mod.ctx = ctx
    app = Flask(__name__)
    app.add_url_rule('/api/llm_core/automation', 'automation2',
                     main_mod._view_automation, methods=['POST'])
    try:
        body = app.test_client().post('/api/llm_core/automation', json={
            'text': '推送', 'user_id': 1}).get_json()
        assert body['code'] == 1 and '投递失败' in body['msg']
        assert body['data']['text'] == '自动化回答'
    finally:
        main_mod.ctx = None


def test_register_exposes_automation_apis():
    ctx = _LoopCtx()
    main_mod.register(ctx)
    assert '/api/llm_core/automation' in ctx.apis
    assert '/api/llm_core/automation/sessions' in ctx.apis


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
