# -*- coding: utf-8 -*-
"""官方插件配置在 WebUI 面板里能不能看到、能不能改。

背景：官方插件的配置写在 core_plugins.yaml，启动时由 load_config 合并进
主配置的同名段，且 config.yaml 覆盖 core_plugins.yaml（framework/config.py）。
但 /api/config/yaml 只读 config.yaml 原始文件、/api/config/yaml/<section>
又有写死的段白名单——新装的部署里 config.yaml 根本没有 image_renderer 这类
段，于是「面板上看不到、也改不了」，只能手改 yaml。

这里固定三件事：
1. GET 会补出官方插件段（且不带 enabled）；
2. 这些段允许 PUT，落盘后成为覆盖方；
3. 白名单外的段仍然拒绝，不能因为放行插件段就变成任意段可写。
"""
import os
import shutil

import pytest
import yaml

pytest.importorskip('flask')

from flask import Flask, request                      # noqa: E402

import framework.api.config as cfg_api                # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeCtx:
    """只提供 register() 需要的几样东西；不启动数据库与鉴权。"""

    def __init__(self, app, yaml_path):
        self.app = app
        self.db = None
        self.framework = _FakeFramework()
        self.require_auth = _passthrough
        self.require_super = _passthrough
        self.audit_log = lambda *a, **kw: None
        self._yaml_config_path = lambda: yaml_path
        self._read_yaml_section = self._read
        self._update_yaml_section = self._write

    def _read(self, section):
        with open(self._yaml_config_path(), encoding='utf-8') as f:
            doc = yaml.safe_load(f) or {}
        v = doc.get(section)
        return dict(v) if isinstance(v, dict) else {}

    def _write(self, section, values):
        path = self._yaml_config_path()
        with open(path, encoding='utf-8') as f:
            doc = yaml.safe_load(f) or {}
        doc[section] = values
        with open(path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
        return True


class _FakeFramework:
    def __init__(self):
        import framework.config as fcfg
        self.config = fcfg.get_config()


def _passthrough(fn):
    def view(*a, **kw):
        request.admin = {'id': 1, 'username': 'tester', 'role': 'super'}
        return fn(*a, **kw)
    view.__name__ = fn.__name__
    return view


@pytest.fixture()
def client(tmp_path):
    src = os.path.join(_ROOT, 'config.yaml')
    if not os.path.isfile(src):
        pytest.skip('缺少 config.yaml，跳过配置面板用例')
    dst = tmp_path / 'config.yaml'
    shutil.copyfile(src, dst)
    app = Flask(__name__)
    cfg_api.register(_FakeCtx(app, str(dst)))
    return app.test_client()


def test_official_plugin_section_is_visible(client):
    r = client.get('/api/config/yaml')
    body = r.get_json()
    assert body['code'] == 0
    sec = body['data'].get('image_renderer')
    assert isinstance(sec, dict) and 'backend' in sec, \
        '面板看不到该段就等于改不了，用户只能去手改 yaml'


def test_enabled_is_not_copied_into_config_section(client):
    """enabled 的唯一权威是 core_plugins.yaml，混进 config.yaml 就是第二处真相。"""
    sec = client.get('/api/config/yaml').get_json()['data']['image_renderer']
    assert 'enabled' not in sec


def test_official_plugin_section_is_writable(client, tmp_path):
    r = client.put('/api/config/yaml/image_renderer',
                   json={'data': {'backend': 'rust', 'asset_consent': 'auto'}})
    body = r.get_json()
    assert r.status_code == 200 and body['code'] == 0, body
    assert body['data']['restart_required'] is True, '该段没有即时应用能力，必须提示重启'
    with open(tmp_path / 'config.yaml', encoding='utf-8') as f:
        saved = (yaml.safe_load(f) or {}).get('image_renderer')
    assert saved['backend'] == 'rust' and saved['asset_consent'] == 'auto'


def test_unknown_section_still_rejected(client):
    """放行插件段不能顺带把白名单废掉。"""
    r = client.put('/api/config/yaml/evil_section', json={'data': {'x': 1}})
    assert r.status_code == 400


def test_deploy_sections_remain_writable(client):
    r = client.put('/api/config/yaml/log', json={'data': {'level': 'INFO'}})
    assert r.status_code == 200 and r.get_json()['code'] == 0
