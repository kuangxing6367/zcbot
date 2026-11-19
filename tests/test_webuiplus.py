"""WebUIplus：配置归一化、原始消息捕获、REST 参数校验与注册流程。

core 插件不在常规 import 路径上，这里用 spec_from_file_location 加载，
与加载器真实加载方式一致。Flask 视图在测试请求上下文里跑，鉴权由框架
的 register_api 负责，这里只测视图自身的输入边界。
"""
import importlib.util
import json
import os
import sys
import types

import pytest

_PLUGIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'core_plugins', 'webuiplus')
_SPEC = importlib.util.spec_from_file_location(
    'webuiplus_main_test', os.path.join(_PLUGIN_DIR, 'main.py'))
wp = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = wp
_SPEC.loader.exec_module(wp)


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    with wp._state['lock']:
        wp._state['dir'] = str(tmp_path)
        wp._state['media_dir'] = str(tmp_path / 'media')
        os.makedirs(wp._state['media_dir'], exist_ok=True)
        wp._state['config'] = wp._normalize(None)
        wp._state['messages'] = __import__('collections').deque(maxlen=wp.MAX_MESSAGES)
        wp._state['ctx'] = None
    yield


def _event(text='你好', images=None, msg_type='private'):
    segments = [{'type': 'text', 'data': {'text': text}}]
    for ref in images or []:
        segments.append({'type': 'image', 'data': {'file': ref}})
    return {
        'post_type': 'message', 'message_type': msg_type,
        'message': segments, 'user_id': 123, 'group_id': 456 if msg_type == 'group' else 0,
        'message_id': 1, 'sender': {'nickname': '张三'},
    }


def test_normalize_rejects_bad_values():
    cfg = wp._normalize({
        'theme': '霓虹', 'display': '瀑布', 'glass': 'false',
        'max_messages': -5, 'image_max_bytes': 'abc',
        'aliases': {'123': '运营', 456: 7},
        'layout': ['time', '不存在的字段', 'sender'],
    })
    assert cfg['theme'] == 'auto' and cfg['display'] == 'card'
    assert cfg['glass'] is False and cfg['max_messages'] == 1
    assert cfg['image_max_bytes'] == wp.DEFAULTS['image_max_bytes']
    assert cfg['aliases'] == {'123': '运营', '456': '7'}
    assert cfg['layout'] == ['time', 'sender']


def test_config_roundtrip_and_empty_layout_fallback():
    ok, cfg = wp.save_config({'theme': 'dark', 'layout': [], 'aliases': {'g1': '群一'}})
    assert ok is True and cfg['theme'] == 'dark'
    assert cfg['layout'] == list(wp.DEFAULTS['layout'])
    loaded = wp.load_config()
    assert loaded['theme'] == 'dark' and loaded['aliases'] == {'g1': '群一'}
    assert os.path.isfile(wp._config_path())
    assert not os.path.exists(wp._config_path() + '.tmp')


def test_load_config_corrupt_file_falls_back_to_defaults():
    with open(wp._config_path(), 'w', encoding='utf-8') as f:
        f.write('{不是 json')
    assert wp.load_config() == wp._normalize(None)


def test_capture_message_extracts_text_and_images():
    record_path = os.path.join(wp._state['dir'], 'messages.jsonl')
    assert wp.capture_message(_event('在吗', ['http://x/a.png']), 'bot1') is None
    msgs = wp.recent_messages()
    assert len(msgs) == 1
    assert msgs[0]['text'] == '在吗'
    assert msgs[0]['images'][0]['ref'] == 'http://x/a.png'
    assert msgs[0]['nickname'] == '张三' and msgs[0]['bot'] == 'bot1'
    assert os.path.isfile(record_path)
    with open(record_path, encoding='utf-8') as f:
        assert json.loads(f.readline())['text'] == '在吗'


def test_capture_ignores_non_message_events_and_malformed():
    assert wp.capture_message({'post_type': 'notice'}, '') is None
    assert wp.capture_message(None, '') is None
    assert wp.capture_message({'post_type': 'message', 'message': [None, 3]}, '') is None
    assert wp.recent_messages() == []


def test_recent_messages_limit_and_truncation():
    for i in range(5):
        wp.capture_message(_event(str(i)), '')
    assert len(wp.recent_messages(2)) == 2
    assert wp.recent_messages(2)[-1]['text'] == '4'
    long_text = '字' * (wp.MAX_TEXT_CHARS + 100)
    wp.capture_message(_event(long_text), '')
    assert len(wp.recent_messages(1)[0]['text']) == wp.MAX_TEXT_CHARS


def test_register_checks_webui_enabled_and_registers_all_endpoints():
    calls = []
    ctx = types.SimpleNamespace(
        _framework=types.SimpleNamespace(config={'web': {'enabled': True}, 'webuiplus': {}}),
        get_data_dir=lambda: os.path.join(wp._state['dir'], 'data'),
        on_raw_message=lambda h: calls.append(('raw', h)),
        register_api=lambda p, h, **k: calls.append(('api', p, k.get('methods'))),
        webui=lambda **k: calls.append(('webui', k)),
        log=lambda m: calls.append(('log', m)),
    )
    os.makedirs(ctx.get_data_dir(), exist_ok=True)
    wp.register(ctx)
    paths = [c[1] for c in calls if c[0] == 'api']
    assert '/api/webuiplus/messages' in paths
    assert '/api/webuiplus/config' in paths and '/api/webuiplus/send' in paths
    assert any(c[0] == 'raw' for c in calls)
    assert [c for c in calls if c[0] == 'webui'][0][1]['sidebar'] is True
    wp.unregister()


def test_register_skips_when_webui_disabled():
    calls = []
    ctx = types.SimpleNamespace(
        _framework=types.SimpleNamespace(config={'web': {'enabled': False}}),
        log=lambda m: calls.append(m),
    )
    wp.register(ctx)
    assert calls and 'WebUI 已禁用' in calls[0]


def _flask_ctx(app=None):
    flask = pytest.importorskip('flask')
    return flask.Flask(__name__).test_request_context


def test_view_send_validates_input(monkeypatch):
    ctx = _flask_ctx()
    monkeypatch.setattr(wp, '_state', dict(wp._state, ctx=types.SimpleNamespace(send_msg=lambda **k: k)))
    with ctx('/api/webuiplus/send', json={}):
        assert wp.view_send()[1] == 400
    with ctx('/api/webuiplus/send', json={'message': 'hi'}):
        assert wp.view_send()[1] == 400
    with ctx('/api/webuiplus/send', json={'user_id': 1, 'message': '   '}):
        assert wp.view_send()[1] == 400
    with ctx('/api/webuiplus/send', json={'group_id': 2, 'message': '你好'}):
        resp, code = wp.view_send()
        assert code == 200


def test_view_send_handles_adapter_failure(monkeypatch):
    def boom(**k):
        raise RuntimeError('offline')
    monkeypatch.setattr(wp, '_state', dict(wp._state, ctx=types.SimpleNamespace(send_msg=boom)))
    with _flask_ctx()('/api/webuiplus/send', json={'user_id': 1, 'message': 'hi'}):
        resp, code = wp.view_send()
        assert code == 500 and 'offline' in resp.get_json()['data']


def test_view_media_rejects_traversal():
    flask = pytest.importorskip('flask')
    app = flask.Flask(__name__)
    with app.test_request_context('/'):
        with pytest.raises(Exception):
            wp.view_media('../../config.yaml')


def test_frontend_assets_exist_and_no_emoji():
    for rel in ('web/index.html', 'web/js/app.js', 'web/css/app.css', 'main.py'):
        path = os.path.join(_PLUGIN_DIR, rel.replace('/', os.sep))
        assert os.path.isfile(path), rel
        with open(path, encoding='utf-8') as f:
            text = f.read()
        assert not any(ord(ch) > 0xFFFF for ch in text), rel


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
