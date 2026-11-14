"""后端注册与 provisioning。

后端可用性完全由环境变量决定：没配 MySQL 就自动跳过，不会让默认的
`pytest tests/` 因缺少外部依赖而失败。这与 SQLAlchemy 的「没配 --dburi 就
只跑 SQLite」是同一个思路。
"""
import os
import shutil
import tempfile

import pytest

# MySQL 连接参数的环境变量名（与框架 config.yaml 的 database 段同名）
_MYSQL_ENV = {
    'host': 'ZCBOT_MYSQL_HOST',
    'port': 'ZCBOT_MYSQL_PORT',
    'user': 'ZCBOT_MYSQL_USER',
    'password': 'ZCBOT_MYSQL_PASSWORD',
    'database': 'ZCBOT_MYSQL_DATABASE',
}


class Backend:
    """一个可测试的数据库后端。"""

    def __init__(self, name, config, tmpdir=None):
        self.name = name
        self.config = config
        self.tmpdir = tmpdir

    def __repr__(self):
        return f'<Backend {self.name}>'

    # ── 生命周期 ────────────────────────────────────────────
    def provision(self):
        """准备一个干净的空库（每次会话开始时调用一次）。"""
        if self.name == 'sqlite':
            for suffix in ('', '-wal', '-shm'):
                p = self.config['path'] + suffix
                if os.path.exists(p):
                    os.remove(p)
            return
        # MySQL：删库重建，保证 schema 由框架自己从 init.sql 建出来
        import pymysql
        conn = pymysql.connect(
            host=self.config['host'], port=self.config['port'],
            user=self.config['user'], password=self.config['password'],
            charset='utf8mb4', autocommit=True)
        try:
            cur = conn.cursor()
            name = self.config['database']
            cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
            cur.execute(f"CREATE DATABASE `{name}` DEFAULT CHARACTER SET utf8mb4 "
                        "COLLATE utf8mb4_unicode_ci")
        finally:
            conn.close()

    def teardown(self):
        if self.name == 'sqlite':
            for suffix in ('', '-wal', '-shm'):
                p = self.config['path'] + suffix
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        if self.tmpdir and os.path.isdir(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def make_database(self):
        """新建一个框架 Database 实例（不建表）。"""
        from framework.database.db import Database
        return Database(dict(self.config))


def _sqlite_backend():
    d = tempfile.mkdtemp(prefix='zcbot_conf_')
    return Backend('sqlite', {'type': 'sqlite',
                              'path': os.path.join(d, 'conf.db')}, tmpdir=d)


def _mysql_backend():
    missing = [env for env in _MYSQL_ENV.values() if not os.environ.get(env)]
    if missing:
        pytest.skip("未配置 MySQL 后端，跳过（缺少 {}）".format(', '.join(missing)))
    cfg = {'type': 'mysql'}
    for key, env in _MYSQL_ENV.items():
        cfg[key] = os.environ[env]
    cfg['port'] = int(cfg['port'])
    return Backend('mysql', cfg)


_FACTORY = {
    'sqlite': _sqlite_backend,
    'mysql': _mysql_backend,
}


def available_backend_names():
    return list(_FACTORY)


def make_backend(name):
    fn = _FACTORY.get(name)
    if fn is None:
        pytest.skip("未知后端: {}（可选 {}）".format(name, ', '.join(_FACTORY)))
    return fn()
