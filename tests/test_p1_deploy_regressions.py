"""P1 部署坑回归：相对库路径、实例锁语义、同步桥自锁。

铁律：路径由 __file__ / tmp_path 推导，不写死时区，不污染项目目录。
"""
import asyncio
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_sqlite_relative_path_resolves_to_home(tmp_path, monkeypatch):
    """旧实现按 cwd 解析：systemd 无 WorkingDirectory 时库建到 /data 并分叉。"""
    import framework.config as cfgmod
    from framework.database.db_conn import DatabaseConnMixin

    monkeypatch.setattr(cfgmod, 'get_home', lambda: str(tmp_path))

    class FakeDB(DatabaseConnMixin):
        config = {'path': 'data/zcbot.db'}

    d = FakeDB()
    d._init_sqlite()
    assert d._db_path == os.path.join(str(tmp_path), 'data', 'zcbot.db')
    assert (tmp_path / 'data').is_dir()


def test_sqlite_absolute_path_untouched(tmp_path):
    from framework.database.db_conn import DatabaseConnMixin

    target = str(tmp_path / 'custom.db')

    class FakeDB(DatabaseConnMixin):
        config = {'path': target}

    d = FakeDB()
    d._init_sqlite()
    assert d._db_path == target


def test_instance_lock_path_prefers_writable_home(tmp_path, monkeypatch):
    """pip 安装 / 只读挂载时代码根不可写，锁必须落在 get_home()。"""
    import framework.bootstrap.instance_lock as il
    import framework.config as cfgmod

    monkeypatch.setattr(cfgmod, 'get_home', lambda: str(tmp_path))
    p = il.instance_lock_path('/definitely/read-only/site-packages')
    assert p.startswith(str(tmp_path))
    assert p.endswith(os.path.join('data', 'zcbot.lock'))


def test_instance_lock_unavailable_skips_protection(tmp_path, monkeypatch):
    """创建失败应返回 False（跳过保护），不能返回 None（被当成已有实例而退出）。"""
    import framework.bootstrap.instance_lock as il

    def boom(_path):
        raise OSError('read-only file system')

    monkeypatch.setattr(il, 'open_lock_file', boom)
    assert il.acquire_instance_lock(str(tmp_path)) is False


def test_sync_call_inside_running_loop_fails_fast():
    """async handler 内误用同步 call() 必须立刻报错，而不是冻结循环 15 秒。"""
    from framework.messaging.protocol import ProtocolAdapter

    class Adapter(ProtocolAdapter):
        async def handle_event(self, raw_event, bot_name):
            return None

        async def call_api(self, action, bot=None, **params):
            return {'status': 'ok', 'retcode': 0}

        def get_connected_bots(self):
            return []

        def start(self):
            pass

        async def stop(self):
            pass

    async def run():
        a = Adapter()

        class FW:
            hooks = None
            loop = asyncio.get_running_loop()

        a.framework = FW()
        t0 = time.monotonic()
        r = a.call('send_msg', None, message='x')
        elapsed = time.monotonic() - t0
        assert r['status'] == 'failed' and r['retcode'] == -1
        assert 'acall' in r['msg']
        assert elapsed < 1.0, f'仍然冻结了 {elapsed:.2f}s'
    asyncio.run(run())
