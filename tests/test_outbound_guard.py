"""出站闸门单测：节流（全局/群/用户三级）/ 等待与拒绝 / 重试取舍 / 查询动作直通 / 接线。

铁律：路径用 __file__ 推导、时间戳不写死时区、不污染真实目录。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.messaging.outbound_guard import OutboundGate


def check(name, cond, detail=''):
    print(('通过 ' if cond else '失败 ') + name + (('：' + detail) if detail else ''))
    if not cond:
        raise SystemExit('断言失败：' + name)


def _gate(**kw):
    g = OutboundGate()
    cfg = {
        'rate_limit': {'enabled': True, 'window': 60, 'global_per_min': 0,
                       'group_per_min': 0, 'user_per_min': 0, 'max_wait': 5.0},
        'retry': {'enabled': False, 'attempts': 1, 'backoff': 0},
    }
    for sec, vals in kw.items():
        cfg.setdefault(sec, {}).update(vals)
    g.configure(cfg)
    return g


OK = {'status': 'ok', 'retcode': 0, 'data': {'message_id': 1}}


def test_non_send_action_passthrough():
    g = _gate(rate_limit={'global_per_min': 1})
    calls = []

    def call():
        calls.append(1)
        return OK

    for _ in range(5):
        g.execute_sync('get_group_member_list', None, {'group_id': 1}, call)
    check('直通：查询动作不受节流', len(calls) == 5, str(len(calls)))
    check('直通：查询动作不计入发送统计', g.stats()['sent'] == 0, str(g.stats()))


def test_global_throttle_rejects_when_wait_exceeds():
    g = _gate(rate_limit={'global_per_min': 2, 'max_wait': 0})
    for _ in range(2):
        g.execute_sync('send_group_msg', None, {'group_id': 1}, lambda: OK)
    r = g.execute_sync('send_group_msg', None, {'group_id': 1}, lambda: OK)
    check('全局节流：超限且不允许等待时拒绝', r.get('retcode') == -4 and r.get('throttled') is True,
          str(r))
    check('全局节流：拒绝被计数', g.stats()['throttled'] == 1, str(g.stats()))


def test_group_scope_isolated():
    g = _gate(rate_limit={'global_per_min': 100, 'group_per_min': 1, 'max_wait': 0})
    r1 = g.execute_sync('send_group_msg', None, {'group_id': 100}, lambda: OK)
    r2 = g.execute_sync('send_group_msg', None, {'group_id': 200}, lambda: OK)
    r3 = g.execute_sync('send_group_msg', None, {'group_id': 100}, lambda: OK)
    check('群级节流：不同群互不影响',
          r1.get('retcode') == 0 and r2.get('retcode') == 0 and r3.get('retcode') == -4,
          str((r1.get('retcode'), r2.get('retcode'), r3.get('retcode'))))


def test_user_scope_not_limiting_group():
    g = _gate(rate_limit={'global_per_min': 100, 'user_per_min': 1, 'max_wait': 0})
    a = g.execute_sync('send_private_msg', None, {'user_id': 7}, lambda: OK)
    b = g.execute_sync('send_private_msg', None, {'user_id': 8}, lambda: OK)
    # 群消息不消耗 user 桶
    c = g.execute_sync('send_group_msg', None, {'group_id': 9}, lambda: OK)
    d = g.execute_sync('send_private_msg', None, {'user_id': 7}, lambda: OK)
    check('用户级节流：按用户区分且不影响群消息',
          a.get('retcode') == 0 and b.get('retcode') == 0
          and c.get('retcode') == 0 and d.get('retcode') == -4,
          str([x.get('retcode') for x in (a, b, c, d)]))


def test_window_expiry_allows_again():
    g = _gate(rate_limit={'global_per_min': 1, 'window': 1, 'max_wait': 0})
    g.execute_sync('send_msg', None, {'user_id': 1}, lambda: OK)
    blocked = g.execute_sync('send_msg', None, {'user_id': 1}, lambda: OK)
    time.sleep(1.1)
    passed = g.execute_sync('send_msg', None, {'user_id': 1}, lambda: OK)
    check('窗口：过期后重新放行',
          blocked.get('retcode') == -4 and passed.get('retcode') == 0,
          str((blocked.get('retcode'), passed.get('retcode'))))


def test_wait_instead_of_drop():
    """允许等待时应当等待后发出，而不是丢弃"""
    g = _gate(rate_limit={'global_per_min': 1, 'window': 1, 'max_wait': 5})
    g.execute_sync('send_msg', None, {'user_id': 1}, lambda: OK)
    t0 = time.time()
    r = g.execute_sync('send_msg', None, {'user_id': 1}, lambda: OK)
    elapsed = time.time() - t0
    check('等待：超限时等待而非丢弃', r.get('retcode') == 0, str(r))
    check('等待：确实等了接近一个窗口', 0.5 <= elapsed <= 2.0, '%.2fs' % elapsed)
    check('等待：等待时长被统计', g.stats()['waited_ms'] > 0, str(g.stats()))


def test_retry_on_local_channel_error():
    g = _gate(retry={'enabled': True, 'attempts': 1, 'backoff': 0})
    seq = [{'status': 'failed', 'retcode': -3, 'msg': '连接已断开'}, OK]
    calls = []

    def call():
        calls.append(1)
        return seq[len(calls) - 1]

    r = g.execute_sync('send_group_msg', None, {'group_id': 1}, call)
    check('重试：-3 本地异常会重试并成功', r.get('retcode') == 0 and len(calls) == 2, str((r, len(calls))))
    check('重试：次数被统计', g.stats()['retried'] == 1, str(g.stats()))


def test_no_retry_on_timeout_or_disconnect():
    for code, label in ((-2, '超时'), (-1, '未连接')):
        g = _gate(retry={'enabled': True, 'attempts': 1, 'backoff': 0})
        calls = []

        def call(code=code):
            calls.append(1)
            return {'status': 'failed', 'retcode': code, 'msg': label}

        r = g.execute_sync('send_group_msg', None, {'group_id': 1}, call)
        check(f'重试：{label}({code}) 不重试（避免重复消息/无效重试）',
              len(calls) == 1 and r.get('retcode') == code, str((len(calls), r)))
        check(f'重试：{label} 计入失败', g.stats()['failed'] == 1, str(g.stats()))


def test_retry_exception_reraises_after_exhausted():
    g = _gate(retry={'enabled': True, 'attempts': 1, 'backoff': 0})
    calls = []

    def call():
        calls.append(1)
        raise RuntimeError('通道炸了')

    raised = None
    try:
        g.execute_sync('send_group_msg', None, {'group_id': 1}, call)
    except RuntimeError as e:
        raised = e
    check('重试：异常耗尽后原样抛出（不吞）', raised is not None and len(calls) == 2,
          str((raised, len(calls))))


def test_disabled_means_passthrough():
    g = _gate(rate_limit={'enabled': False, 'global_per_min': 1, 'max_wait': 0},
              retry={'enabled': False, 'attempts': 3, 'backoff': 0})
    calls = []

    def call():
        calls.append(1)
        return {'status': 'failed', 'retcode': -3}

    for _ in range(3):
        r = g.execute_sync('send_msg', None, {'user_id': 1}, call)
        check('关闭：不节流也不重试', r.get('retcode') == -3, str(r))
    check('关闭：每次只调用一次', len(calls) == 3, str(len(calls)))


def test_hits_capacity_eviction():
    """键容量超限时要能淘汰，不能抛异常（历史上 popitem(last=) 就在这里炸过）"""
    g = _gate(rate_limit={'global_per_min': 0, 'user_per_min': 0, 'max_wait': 0})
    g._HITS_CAP = 8
    try:
        for i in range(200):
            g.execute_sync('send_private_msg', None, {'user_id': i}, lambda: OK)
    except Exception as e:
        raise SystemExit(f'断言失败：容量淘汰抛异常 {type(e).__name__}: {e}')
    check('容量：键数受上限约束', len(g._hits) <= 8, str(len(g._hits)))


def test_gate_failure_passes_through():
    g = _gate(rate_limit={'global_per_min': 1})
    g._plan = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('闸门自身故障'))
    r = g.execute_sync('send_msg', None, {'user_id': 1}, lambda: OK)
    check('失败安全：闸门自身异常时放行', r.get('retcode') == 0, str(r))


def test_async_path():
    g = _gate(rate_limit={'global_per_min': 1, 'max_wait': 0},
              retry={'enabled': True, 'attempts': 1, 'backoff': 0})

    async def scenario():
        seen = []

        async def call():
            seen.append(1)
            if len(seen) == 1:
                return {'status': 'failed', 'retcode': -3}
            return OK

        first = await g.execute_async('send_group_msg', None, {'group_id': 1}, call)
        second = await g.execute_async('send_group_msg', None, {'group_id': 1}, call)
        return first, second, seen

    first, second, seen = asyncio.run(scenario())
    check('异步：-3 重试后成功', first.get('retcode') == 0 and len(seen) == 2, str((first, len(seen))))
    check('异步：随后超限被拒', second.get('retcode') == -4, str(second))


def test_adapter_acall_goes_through_gate():
    """接线验证：ProtocolAdapter.acall 必须经过闸门，不是只在 ApiCaller 上生效"""
    from framework.messaging.outbound_guard import gate as singleton
    from framework.messaging.protocol import ProtocolAdapter

    old = singleton.describe()
    singleton.configure({'rate_limit': {'enabled': True, 'window': 60,
                                        'global_per_min': 1, 'max_wait': 0},
                         'retry': {'enabled': False}})

    class _A(ProtocolAdapter):
        adapter_id = 'probe'

        def __init__(self):
            self.sent = 0

        async def handle_event(self, raw_event, bot_name):
            return None

        async def call_api(self, action, bot=None, **params):
            self.sent += 1
            return {'status': 'ok', 'retcode': 0}

        def get_connected_bots(self):
            return ['probe']

        def start(self):
            pass

        async def stop(self):
            pass

    try:
        a = _A()
        first = asyncio.run(a.acall('send_group_msg', None, group_id=1))
        second = asyncio.run(a.acall('send_group_msg', None, group_id=1))
        check('接线：适配器 acall 的首条正常发出', first.get('retcode') == 0, str(first))
        check('接线：适配器 acall 的第二条被节流拦下',
              second.get('retcode') == -4 and a.sent == 1, str((second, a.sent)))
        query = asyncio.run(a.acall('get_group_member_list', None, group_id=1))
        check('接线：适配器 acall 的查询动作直通', query.get('retcode') == 0 and a.sent == 2,
              str((query, a.sent)))
    finally:
        singleton.configure(old)


def run_all():
    for fn in (test_non_send_action_passthrough, test_global_throttle_rejects_when_wait_exceeds,
               test_group_scope_isolated, test_user_scope_not_limiting_group,
               test_window_expiry_allows_again, test_wait_instead_of_drop,
               test_retry_on_local_channel_error, test_no_retry_on_timeout_or_disconnect,
               test_retry_exception_reraises_after_exhausted, test_disabled_means_passthrough,
               test_hits_capacity_eviction, test_gate_failure_passes_through,
               test_async_path, test_adapter_acall_goes_through_gate):
        fn()
    print('\n全部通过：出站闸门 14 组')


if __name__ == '__main__':
    run_all()
