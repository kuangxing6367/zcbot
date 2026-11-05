# -*- coding: utf-8 -*-
"""MySQL 配置类型校验 + 借连接快速失败（不把一次失败放大成 30s 假死）"""
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.database.db_conn import DatabaseConnMixin


class PooledDBTooManyRequests(Exception):
    pass


class PooledDBPoolNotOpen(Exception):
    pass


def _obj(config):
    return types.SimpleNamespace(config=config, _pool_wait_timeout=30)


class _Db(DatabaseConnMixin):
    """最小可用实例：只为借连接路径提供 mixin 方法"""

    def __init__(self, pool, wait=30.0, pool_max=10):
        self._pool = pool
        self._pool_wait_timeout = wait
        self._pool_max = pool_max


def test_normalize_ok():
    o = _obj({'type': 'mysql', 'host': '127.0.0.1', 'port': 3306,
              'user': 'root', 'password': 123456, 'database': 'zcbot',
              'charset': 'utf8mb4'})
    p = DatabaseConnMixin._normalize_mysql_params(o)
    assert p['password'] == '123456', '数字密码应被转成字符串'
    assert p['port'] == 3306 and isinstance(p['port'], int)
    return '正常配置归一化正确'


def test_normalize_defaults():
    p = DatabaseConnMixin._normalize_mysql_params(_obj({'type': 'mysql'}))
    assert p == {'host': '127.0.0.1', 'port': 3306, 'user': 'root',
                 'password': '', 'database': 'zcbot', 'charset': 'utf8mb4'}, p
    return '缺省值正确'


def test_nested_dict_rejected():
    """把参数多嵌一层（database.mysql.host）要报出具体键名"""
    bad = _obj({'type': 'mysql', 'host': {'addr': '127.0.0.1'},
                'database': {'name': 'zcbot'}})
    try:
        DatabaseConnMixin._normalize_mysql_params(bad)
    except ValueError as e:
        msg = str(e)
        assert 'database.host' in msg, msg
        assert 'database.database' in msg, msg
        assert '嵌套' in msg, msg
        return '嵌套结构被拦下并指出键名'
    raise AssertionError('本应抛 ValueError')


def test_port_not_int():
    try:
        DatabaseConnMixin._normalize_mysql_params(
            _obj({'type': 'mysql', 'port': 'abc'}))
    except ValueError as e:
        assert 'database.port' in str(e)
        return 'port 非整数被拦下'
    raise AssertionError('本应抛 ValueError')


def test_pool_waitable_classification():
    assert DatabaseConnMixin._is_pool_waitable(PooledDBTooManyRequests())
    assert DatabaseConnMixin._is_pool_waitable(PooledDBPoolNotOpen())
    # 类型错误 / 认证失败不是可等待异常
    assert not DatabaseConnMixin._is_pool_waitable(TypeError(
        "unsupported operand type(s) for +: 'dict' and 'bytes'"))
    assert not DatabaseConnMixin._is_pool_waitable(Exception('Access denied'))
    return '异常分类正确'


def test_fail_fast_no_retry():
    """非池满异常必须立即上抛，不能被重试成 30s 假死"""
    calls = []

    class FakePool:
        def connection(self):
            calls.append(1)
            raise TypeError(
                "unsupported operand type(s) for +: 'dict' and 'bytes'")

    o = _Db(FakePool(), wait=30.0)
    t0 = time.time()
    try:
        o._get_conn_mysql()
    except TypeError:
        elapsed = time.time() - t0
        assert len(calls) == 1, f'不应重试，实际尝试 {len(calls)} 次'
        assert elapsed < 1.0, f'应快速失败，实际耗时 {elapsed:.1f}s'
        return f'立即失败（{len(calls)} 次尝试，{elapsed*1000:.0f}ms）'
    raise AssertionError('本应抛 TypeError')


def test_pool_full_still_retries_then_reports():
    """池满异常仍要有界等待并给出「繁忙」结论"""
    calls = []

    class FakePool:
        def connection(self):
            calls.append(1)
            raise PooledDBTooManyRequests('pool is full')

    o = _Db(FakePool(), wait=0.2)
    try:
        o._get_conn_mysql()
    except RuntimeError as e:
        assert '连接池繁忙' in str(e), str(e)
        assert len(calls) > 1, '池满时应重试'
        return f'池满仍有界重试 {len(calls)} 次'
    raise AssertionError('本应抛 RuntimeError')


if __name__ == '__main__':
    ok = fail = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                detail = fn()
                print(f'PASS {name}: {detail}')
                ok += 1
            except Exception as e:
                print(f'FAIL {name}: {type(e).__name__}: {e}')
                fail += 1
    print(f'\n{ok} passed, {fail} failed')
    sys.exit(1 if fail else 0)
