"""核心插件配置 core_plugins.yaml ↔ core_config 表同步回归。

锁定用户拍板的行为：
1. 文件是权威：手改 yaml 后 sync_file_to_db 全量覆写库，敏感键加密落库；
2. yaml 带说明块、synced_at 同步时间、每项 # id:N 注释；
3. 接口读取一律脱敏（含超管）：敏感值只给 ***；
4. 面板改动写库 + 回写 yaml；
5. 空值/占位符不被当真凭据写进去。
"""
from __future__ import annotations

import os
import tempfile

import pytest
import yaml


@pytest.fixture
def env(monkeypatch):
    from framework import core_config_sync as ccs
    from framework import secretbox

    tmp = tempfile.mkdtemp(prefix='zcbot_ccs_')
    yaml_path = os.path.join(tmp, 'core_plugins.yaml')
    cfg = {
        'core_plugins': {
            'webui': {'enabled': True, 'port': 8080},
            'qq_official': {
                'enabled': False,
                'app_id': '123456',
                'app_secret': 'super-secret-value',
                'intents': 100663296,
            },
            'aiwriter': {'enabled': False, 'api_key': '', 'model': 'deepseek-chat'},
        }
    }
    with open(yaml_path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

    # 用临时目录的内置密钥，避免污染真实 data/
    from framework import paths
    monkeypatch.setattr(paths, 'state_dir', lambda: tmp)
    monkeypatch.setattr(secretbox, '_builtin_secret_cache', None)

    class FakeDB:
        def __init__(self):
            self._rows = []
            self._next = 1

        def execute(self, sql, params=None):
            if sql.startswith('DELETE FROM core_config'):
                self._rows = []
            elif sql.startswith('INSERT INTO core_config'):
                p, k, v, ts, src = params
                self._rows.append({'id': self._next, 'plugin_name': p, 'config_key': k,
                                   'config_value': v, 'synced_at': ts, 'source': src})
                self._next += 1
            elif sql.startswith('UPDATE core_config SET synced_at'):
                ts = params[0]
                for r in self._rows:
                    r['synced_at'] = ts
            elif sql.startswith('UPDATE core_config'):
                v, ts, src, p, k = params
                for r in self._rows:
                    if r['plugin_name'] == p and r['config_key'] == k:
                        r.update(config_value=v, synced_at=ts, source=src)

        def query(self, sql, params=None):
            return list(self._rows)

        def query_one(self, sql, params=None):
            p, k = params
            for r in self._rows:
                if r['plugin_name'] == p and r['config_key'] == k:
                    return r
            return None

    yield {'ccs': ccs, 'db': FakeDB(), 'yaml_path': yaml_path, 'tmp': tmp}


def test_file_to_db_encrypts_and_syncs(env):
    ccs, db, path = env['ccs'], env['db'], env['yaml_path']
    info = ccs.sync_file_to_db(db, path, synced_at=1000)
    assert info['count'] == 9, info
    assert info['synced_at'] == 1000

    rows = {r['config_key']: r for r in db._rows}
    # 敏感值加密落库
    assert rows['app_secret']['config_value'].startswith('enc:v1:')
    assert 'super-secret-value' not in rows['app_secret']['config_value']
    # 空值不加密，普通值明文
    assert rows['api_key']['config_value'] == ''
    assert rows['port']['config_value'] == 8080
    assert all(r['synced_at'] == '1000' and r['source'] == 'file' for r in db._rows)


def test_annotate_yaml_has_header_time_and_ids(env):
    ccs, db, path = env['ccs'], env['db'], env['yaml_path']
    info = ccs.sync_file_to_db(db, path, synced_at=1234)
    id_map = ccs.get_id_map(db)
    ccs.annotate_yaml(path, id_map, info['synced_at'])

    text = open(path, encoding='utf-8').read()
    assert ccs._HEADER_TITLE in text
    assert '# synced_at: 1234' in text
    assert '# id:' in text
    # 注解后 yaml 仍可正常解析
    doc = yaml.safe_load(text)
    assert doc['core_plugins']['qq_official']['app_secret'] == 'super-secret-value'


def test_api_read_always_masks_secrets(env):
    ccs, db, path = env['ccs'], env['db'], env['yaml_path']
    ccs.sync_file_to_db(db, path, synced_at=2000)

    items = ccs.get_config_for_api(db, masked=True)
    by_key = {i['key']: i for i in items}
    assert by_key['app_secret']['value'] == '***'
    assert by_key['app_secret']['sensitive'] is True
    assert by_key['api_key']['value'] == '***'
    assert by_key['port']['value'] == 8080
    assert by_key['port']['sensitive'] is False
    # 即使 masked=False 也不该在任何对外路径调用；内部解密正确性单独验
    raw = {i['key']: i['value'] for i in ccs.get_config_for_api(db, masked=False)}
    assert raw['app_secret'] == 'super-secret-value'


def test_panel_update_writes_db_and_yaml(env):
    ccs, db, path = env['ccs'], env['db'], env['yaml_path']
    ccs.sync_file_to_db(db, path, synced_at=3000)
    ccs.annotate_yaml(path, ccs.get_id_map(db), 3000)

    assert ccs.update_from_panel(db, path, 'qq_official', 'app_id', '999999') is True
    row = db.query_one('x', ('qq_official', 'app_id'))
    assert row['config_value'] == '999999'
    assert row['source'] == 'panel'
    doc = yaml.safe_load(open(path, encoding='utf-8').read())
    assert str(doc['core_plugins']['qq_official']['app_id']) == '999999'


def test_panel_update_ignores_mask_placeholder(env):
    ccs, db, path = env['ccs'], env['db'], env['yaml_path']
    ccs.sync_file_to_db(db, path, synced_at=4000)
    assert ccs.update_from_panel(db, path, 'qq_official', 'app_secret', '***') is False
    # 库里仍是原密文，没被 '***' 覆盖
    row = db.query_one('x', ('qq_official', 'app_secret'))
    assert row['config_value'].startswith('enc:v1:')


def test_file_wins_over_panel(env):
    """手改文件后再同步 → 全量覆写库，面板改动被文件覆盖（文件赢）"""
    ccs, db, path = env['ccs'], env['db'], env['yaml_path']
    ccs.sync_file_to_db(db, path, synced_at=5000)
    ccs.update_from_panel(db, path, 'webui', 'port', '8082')
    assert db.query_one('x', ('webui', 'port'))['config_value'] == '8082'

    # 用户手改文件回 8081 并新增键，重启同步
    doc = yaml.safe_load(open(path, encoding='utf-8').read())
    doc['core_plugins']['webui']['port'] = 8081
    doc['core_plugins']['webui']['new_key'] = 'hello'
    with open(path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
    ccs.sync_file_to_db(db, path, synced_at=6000)

    assert db.query_one('x', ('webui', 'port'))['config_value'] == 8081
    assert db.query_one('x', ('webui', 'new_key'))['config_value'] == 'hello'
