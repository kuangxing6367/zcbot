"""审计详情脱敏：敏感键的值不落库，非敏感字段保持原样。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.core.audit_writer import _detail_json
from framework.security.redact import (
    MAX_TEXT,
    REDACTED,
    is_sensitive_key,
    redact_detail,
)


def test_flat_sensitive_keys_are_replaced():
    raw = {
        'password': 'p@ssw0rd',
        'api_key': 'sk-123456',
        'Authorization': 'Bearer abc',
        'action': 'config.update',
        'count': 7,
        'ok': True,
    }
    out = redact_detail(raw)
    assert out['password'] == REDACTED
    assert out['api_key'] == REDACTED
    assert out['Authorization'] == REDACTED
    assert out['action'] == 'config.update'
    assert out['count'] == 7
    assert out['ok'] is True
    text = json.dumps(out, ensure_ascii=False)
    assert 'p@ssw0rd' not in text and 'sk-123456' not in text


def test_key_variants_are_recognized():
    for key in ('apiKey', 'API-KEY', 'api key', 'private_key', 'secret',
                'token', 'sessionId', 'cookie', 'recovery_code', 'totp',
                'credential', 'passwd'):
        assert is_sensitive_key(key), key
    for key in ('action', 'target', 'result', 'updated_at', 'username'):
        assert not is_sensitive_key(key), key


def test_nested_structures_are_walked():
    raw = {
        'outer': {
            'items': [
                {'token': 'secret-value', 'name': 'first'},
                {'nested': {'password': 'another-secret'}},
            ],
        },
        'note': 'plain text',
    }
    out = redact_detail(raw)
    assert out['outer']['items'][0]['token'] == REDACTED
    assert out['outer']['items'][0]['name'] == 'first'
    assert out['outer']['items'][1]['nested']['password'] == REDACTED
    assert out['note'] == 'plain text'
    text = json.dumps(out, ensure_ascii=False)
    assert 'secret-value' not in text and 'another-secret' not in text


def test_long_plain_text_is_clipped():
    out = redact_detail({'message': 'x' * (MAX_TEXT + 50)})
    assert out['message'].endswith('…')
    assert len(out['message']) == MAX_TEXT + 1


def test_non_json_value_is_stringified():
    class Obj:
        def __str__(self):
            return 'obj-text'

    out = redact_detail({'obj': Obj()})
    assert out['obj'] == 'obj-text'
    assert json.dumps(out, ensure_ascii=False)


def test_audit_writer_detail_json_uses_redaction():
    encoded = _detail_json({'token': 'sk-should-not-land', 'action': 'login'})
    assert encoded
    out = json.loads(encoded)
    assert out['token'] == REDACTED
    assert out['action'] == 'login'
    assert 'sk-should-not-land' not in encoded


def test_empty_detail_stays_none():
    assert _detail_json(None) is None
    assert _detail_json({}) is None
