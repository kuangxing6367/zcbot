"""配置凭据加密回归：标准库对称加密 + 环境变量密钥 + 明文一次性迁移。

铁律：不碰真实 config.yaml / core_plugins.yaml；密钥只写在 monkeypatch 的
环境变量里；每次加解密都验证「落盘无明文、运行时拿到原文」。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from framework import config as cfgmod
from framework import secretbox
from framework.api.app_helpers import make_app_helpers


def _set_key(monkeypatch, key='unit-test-master-key'):
    monkeypatch.setenv('ZCBOT_TOKEN_KEY', key)
    monkeypatch.delenv('ZCBOT_CONFIG_SECRET', raising=False)


def _clear_key(monkeypatch):
    monkeypatch.delenv('ZCBOT_TOKEN_KEY', raising=False)
    monkeypatch.delenv('ZCBOT_CONFIG_SECRET', raising=False)


# ── 基础加解密 ───────────────────────────────────────────────
def test_no_key_is_plaintext_passthrough(monkeypatch):
    _clear_key(monkeypatch)
    assert secretbox.available() is False
    assert secretbox.encrypt_value('abc') == 'abc'
    assert secretbox.decrypt_value('abc') == 'abc'


def test_roundtrip_is_random_and_authenticated(monkeypatch):
    _set_key(monkeypatch)
    a = secretbox.encrypt_value('my-token-1234')
    b = secretbox.encrypt_value('my-token-1234')
    assert a.startswith(secretbox.PREFIX)
    assert b.startswith(secretbox.PREFIX)
    assert a != b                       # 随机 nonce：同明文不得同密文
    assert 'my-token-1234' not in a
    assert secretbox.decrypt_value(a) == 'my-token-1234'
    assert secretbox.decrypt_value(b) == 'my-token-1234'


def _flip(body, idx):
    """把 base64url 正文第 idx 个字符换成另一个合法字符。"""
    out = list(body)
    out[idx] = 'A' if out[idx] != 'A' else 'B'
    return ''.join(out)


def test_tamper_and_wrong_key_fail_closed(monkeypatch):
    _set_key(monkeypatch)
    cipher = secretbox.encrypt_value('real-token')
    # 必须改「有意义的位」：base64 每 3 字节出 4 个字符，长度不是 3 的倍数时
    # 末位字符带 2 个填充位，而 b64decode 不校验填充位 —— 改末位有 1/4 概率
    # 解码出的字节完全不变，那不是篡改，只是同一份密文的另一种写法。
    # 正文中点一定落在有效位区间，改它必须认证失败。
    body = cipher[len(secretbox.PREFIX):]
    mid = len(body) // 2
    tampered = cipher[:len(secretbox.PREFIX)] + _flip(body, mid)
    assert tampered != cipher
    assert secretbox.decrypt_value(tampered) == tampered

    _set_key(monkeypatch, 'a-totally-different-key')
    assert secretbox.decrypt_value(cipher) == cipher


def test_no_position_yields_a_forged_plaintext(monkeypatch):
    """逐位篡改的不变量：要么被拒（返回密文），要么只是等价编码。

    这条把 base64 填充位的真实行为钉住 —— 允许「换一种写法解出同一明文」，
    绝不允许「换出别的明文」。反过来若哪天实现丢了认证校验，改中间位就会
    解出乱码而不是原样返回，这里会立刻红。
    """
    _set_key(monkeypatch)
    plain = 'real-token'
    cipher = secretbox.encrypt_value(plain)
    body = cipher[len(secretbox.PREFIX):]
    head = cipher[:len(secretbox.PREFIX)]
    for idx in range(len(body)):
        got = secretbox.decrypt_value(head + _flip(body, idx))
        assert got in (head + _flip(body, idx), plain), (
            '第 %d 位篡改后解出了第三种结果：%r' % (idx, got))
    # 至少有一个位置必须被拒，否则说明认证标签根本没起作用
    assert any(secretbox.decrypt_value(head + _flip(body, i)) != plain
               for i in range(len(body)))


def test_placeholders_and_masks_are_not_encrypted(monkeypatch):
    _set_key(monkeypatch)
    for value in ('${ONEBOT_TOKEN}', '***', ''):
        assert secretbox.encrypt_value(value) == value


def test_recursive_config_walk(monkeypatch):
    _set_key(monkeypatch)
    cfg = {
        'onebot': {'access_token': 'tok-a', 'listen_port': 6830,
                   'instances': [{'listen_port': 6831, 'access_token': 'tok-b'}]},
        'ws_client': {'token': 'tok-c'},
        'web': {'session_timeout': 3600},
    }
    assert secretbox.encrypt_config_secrets(cfg) == 3
    assert cfg['onebot']['listen_port'] == 6830
    assert cfg['web']['session_timeout'] == 3600
    assert all(str(cfg['onebot'][k]).startswith(secretbox.PREFIX)
               for k in ('access_token',))
    assert cfg['onebot']['instances'][0]['access_token'].startswith(secretbox.PREFIX)
    assert cfg['ws_client']['token'].startswith(secretbox.PREFIX)
    assert secretbox.decrypt_config_secrets(cfg) == 3
    assert cfg['onebot']['access_token'] == 'tok-a'
    assert cfg['onebot']['instances'][0]['access_token'] == 'tok-b'
    assert cfg['ws_client']['token'] == 'tok-c'


# ── 文件级迁移 ───────────────────────────────────────────────
def test_rewrite_secret_file_preserves_comments(monkeypatch, tmp_path):
    _set_key(monkeypatch)
    path = tmp_path / 'config.yaml'
    original = (
        '# 顶层注释，必须保留\n'
        'onebot:\n'
        '  listen_port: 6830      # 端口不是凭据\n'
        '  access_token: "plain-token"  # 接入令牌\n'
        '  token: ${ENV_TOKEN}    # 占位符不加密\n'
        'web:\n'
        '  session_timeout: 3600\n'
    )
    path.write_text(original, encoding='utf-8')
    assert secretbox.rewrite_secret_file(str(path)) == 1
    text = path.read_text(encoding='utf-8')
    assert '# 顶层注释，必须保留' in text
    assert 'listen_port: 6830      # 端口不是凭据' in text
    assert 'token: ${ENV_TOKEN}    # 占位符不加密' in text
    assert 'plain-token' not in text
    assert '# 接入令牌' in text
    assert ('access_token: "' + secretbox.PREFIX) in text
    doc = yaml.safe_load(text)
    assert secretbox.decrypt_value(doc['onebot']['access_token']) == 'plain-token'


def test_load_config_migrates_once_and_returns_plaintext(monkeypatch, tmp_path):
    _set_key(monkeypatch)
    path = tmp_path / 'config.yaml'
    path.write_text(
        'onebot:\n  listen_port: 6831\n  access_token: "legacy-token"\n',
        encoding='utf-8')
    # 隔离官方插件扫描：本用例只验配置加载链路
    monkeypatch.setattr(cfgmod, '_autoload_core_plugins', lambda config: {})

    cfg = cfgmod.load_config(str(path))
    assert cfg['onebot']['access_token'] == 'legacy-token'
    first = path.read_text(encoding='utf-8')
    assert 'legacy-token' not in first
    assert secretbox.PREFIX in first

    cfg2 = cfgmod.load_config(str(path))
    assert cfg2['onebot']['access_token'] == 'legacy-token'
    assert path.read_text(encoding='utf-8') == first   # 第二次不再改写


def test_env_placeholder_stays_dynamic(monkeypatch, tmp_path):
    _set_key(monkeypatch)
    monkeypatch.setenv('UNIT_TEST_TOKEN', 'from-env')
    path = tmp_path / 'config.yaml'
    path.write_text('onebot:\n  access_token: ${UNIT_TEST_TOKEN}\n', encoding='utf-8')
    monkeypatch.setattr(cfgmod, '_autoload_core_plugins', lambda config: {})
    cfg = cfgmod.load_config(str(path))
    assert cfg['onebot']['access_token'] == 'from-env'
    assert '${UNIT_TEST_TOKEN}' in path.read_text(encoding='utf-8')


# ── WebUI 写回链路 ───────────────────────────────────────────
def test_webui_section_write_encrypts_secret(monkeypatch, tmp_path):
    _set_key(monkeypatch)
    path = tmp_path / 'config.yaml'
    path.write_text('onebot:\n  listen_port: 6830\n  access_token: "old"\n',
                    encoding='utf-8')

    class FW:
        config_path = str(path)
        config = {}

    helpers = make_app_helpers(FW(), None, None)
    assert helpers['_update_yaml_section'](
        'onebot', {'listen_port': 6830, 'access_token': 'new-webui-token'})

    text = path.read_text(encoding='utf-8')
    assert 'new-webui-token' not in text
    assert 'old' not in text
    assert secretbox.PREFIX in text
    doc = yaml.safe_load(text)
    assert secretbox.decrypt_value(doc['onebot']['access_token']) == 'new-webui-token'


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
