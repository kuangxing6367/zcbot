"""telegram 接入端 offset 持久化。

要钉住的行为：offset 只放内存时，进程重启会把 Telegram 最近 24 小时未确认的
旧消息重新投一遍（用户收到重复回复），而入站去重窗口在重启后已清空、挡不住。
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_adapter_module():
    path = ROOT / 'core_plugins' / 'telegram' / 'main.py'
    spec = importlib.util.spec_from_file_location('zcbot_test_telegram', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tg = _load_adapter_module()
TelegramAdapter = tg.TelegramAdapter


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('ZCBOT_HOME', str(tmp_path))
    (tmp_path / 'data').mkdir(parents=True, exist_ok=True)
    return tmp_path


def _adapter(token='123:ABC'):
    return TelegramAdapter(framework=None, token=token, bot_name='tgbot')


def test_offset_saved_and_restored(home):
    a = _adapter()
    assert a._offset == 0, '没有状态文件时应从 0 开始'

    a._offset = 4242
    a._save_offset()

    state = json.loads((home / 'data' / 'telegram_offset.json').read_text(encoding='utf-8'))
    assert state['offset'] == 4242
    assert 'ABC' not in json.dumps(state), '状态文件不得出现 token 明文'

    b = _adapter()
    assert b._offset == 4242, '重启后必须接着上次位置，否则会重放旧消息'


def test_offset_ignored_when_token_changed(home):
    a = _adapter(token='111:OLD')
    a._offset = 999
    a._save_offset()

    b = _adapter(token='222:NEW')
    assert b._offset == 0, '换 bot 后沿用旧 offset 会静默跳过新 bot 的消息'


def test_corrupt_state_falls_back_to_zero(home):
    (home / 'data' / 'telegram_offset.json').write_text('{not json', encoding='utf-8')
    a = _adapter()
    assert a._offset == 0


def test_save_failure_does_not_raise(home, monkeypatch):
    a = _adapter()
    a._offset = 10

    def boom(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(tg.os, 'makedirs', boom)
    a._save_offset()          # 不得抛异常：状态文件写不进去不能让收消息停摆
    assert a._offset == 10


def test_atomic_write_leaves_no_tmp(home):
    a = _adapter()
    a._offset = 7
    a._save_offset()
    files = sorted(p.name for p in (home / 'data').iterdir())
    assert files == ['telegram_offset.json'], f'不应残留临时文件: {files}'
