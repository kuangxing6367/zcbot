"""一致性测试套件的 pytest 装配。

后端选择：--db 可重复，默认 sqlite。
同一套用例会在每个指定后端上各跑一遍（pytest 参数化）。
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tests.db_conformance.backends import make_backend  # noqa: E402
from tests.db_conformance.requirements import caps_of  # noqa: E402


def pytest_addoption(parser):
    group = parser.getgroup('db-conformance', '多后端一致性测试')
    group.addoption(
        '--db', action='append', default=None, metavar='NAME',
        help='要测试的后端，可重复：--db sqlite --db mysql（默认 sqlite）')


def pytest_generate_tests(metafunc):
    if 'backend' not in metafunc.fixturenames:
        return
    names = metafunc.config.getoption('db') or ['sqlite']
    metafunc.parametrize('backend', names, indirect=True, scope='session')


@pytest.fixture(scope='session')
def backend(request):
    """当前测试的后端（session 级，会话开始时空库 provisioning 一次）。"""
    b = make_backend(request.param)
    b.provision()
    yield b
    b.teardown()


@pytest.fixture(scope='session')
def caps(backend):
    return caps_of(backend)


@pytest.fixture(scope='session')
def schema_db(backend):
    """跑完整 init.sql 建表后的 Database（整个会话共用一份 schema）。"""
    from framework.database.init_db import auto_init_database
    db = backend.make_database()
    auto_init_database(db)
    yield db
    try:
        db.close()
    except Exception:
        pass


@pytest.fixture
def db(backend):
    """函数级 Database：已建好 schema，用例可随意写数据。"""
    from framework.database.init_db import auto_init_database
    d = backend.make_database()
    auto_init_database(d)
    yield d
    try:
        d.close()
    except Exception:
        pass


@pytest.fixture
def empty_db(backend):
    """函数级 Database：只有空库，不建任何表（用于 DDL 类用例）。"""
    d = backend.make_database()
    yield d
    try:
        d.close()
    except Exception:
        pass


@pytest.fixture
def table(empty_db, request):
    """建一张测试用表并在结束时删除，返回表名。

    用例里用 `table` 当表名，避免不同用例互相干扰（后端是共享库）。
    """
    import re
    base = re.sub(r'[^a-z0-9]+', '_', request.node.name.lower())[:40].strip('_')
    name = 't_' + base
    created = []

    def _make(ddl_body, suffix=''):
        tname = (name + suffix)[:60]
        empty_db.execute(f'DROP TABLE IF EXISTS {tname}')
        empty_db.execute(f'CREATE TABLE {tname} ({ddl_body})')
        created.append(tname)
        return tname

    yield _make

    for t in created:
        try:
            empty_db.execute(f'DROP TABLE IF EXISTS {t}')
        except Exception:
            pass
