# -*- coding: utf-8 -*-
"""断线重连退避单测：指数增长 / 封顶 / 抖动 / 存活时长判定 / 适配器接线。

铁律：路径用 __file__ 推导、不写死时区、不碰真实目录。
"""
import importlib
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.messaging.backoff import Backoff


def check(name, cond, detail=''):
    if cond:
        print('通过 ' + name)
        return
    print('失败 ' + name + (('：' + detail) if detail else ''))
    raise SystemExit('断言失败：' + name)


def test_exponential_growth():
    b = Backoff(base=5, maximum=60, jitter=0)
    seq = [b.next_delay(0) for _ in range(6)]
    check('退避序列：5 → 10 → 20 → 40 → 封顶 60 并保持',
          seq == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0], str(seq))


def test_jitter_only_extends():
    # 每次新建实例只取第一个退避值，避免退避增长混进抖动断言
    samples = [Backoff(base=10, maximum=10, jitter=0.25).next_delay(0)
               for _ in range(60)]
    check('抖动：不会短于基础退避（不缩短等待）',
          all(d >= 10.0 for d in samples), str(min(samples)))
    check('抖动：不超过 base*(1+jitter)',
          all(d <= 12.5 + 1e-6 for d in samples), str(max(samples)))
    check('抖动：多次取值不完全相同（确实随机化了）',
          len(set(samples)) > 1, str(sorted(set(samples))[:5]))


def test_jitter_zero_is_deterministic():
    b = Backoff(base=4, maximum=60, jitter=0)
    check('jitter=0：退避值确定可预期',
          [b.next_delay(0) for _ in range(3)] == [4.0, 8.0, 16.0])


def test_stable_connection_resets():
    b = Backoff(base=5, maximum=60, jitter=0, stable_after=30)
    b.next_delay(0)
    b.next_delay(0)
    b.next_delay(0)
    check('连续失败已退避到 20s', b.failures == 3, str(b.failures))
    delay = b.next_delay(120)   # 上次连接活了 120 秒 → 稳定
    check('连接存活超过 stable_after：退避清零回到初始值',
          delay == 5.0 and b.failures == 1, str((delay, b.failures)))


def test_flapping_connection_does_not_reset():
    b = Backoff(base=5, maximum=60, jitter=0, stable_after=30)
    seq = [b.next_delay(2) for _ in range(4)]  # 每次只连上 2 秒就被断开
    check('连上即被踢（存活 2s）不算成功，退避继续增长',
          seq == [5.0, 10.0, 20.0, 40.0], str(seq))


def test_maximum_never_below_base():
    b = Backoff(base=30, maximum=10, jitter=0)
    seq = [b.next_delay(0) for _ in range(4)]
    check('上限小于起始值时被抬到起始值（不允许越退越快）',
          seq == [30.0, 30.0, 30.0, 30.0], str(seq))


def test_reset_api():
    b = Backoff(base=5, maximum=60, jitter=0)
    b.next_delay(0)
    b.next_delay(0)
    b.reset()
    check('reset()：失败计数归零，下一次从初始值开始',
          b.failures == 0 and b.next_delay(0) == 5.0)


def test_describe():
    b = Backoff(base=5, maximum=45, jitter=0.1, stable_after=15)
    d = b.describe()
    check('describe()：暴露退避参数供状态页展示',
          d['base'] == 5.0 and d['maximum'] == 45.0
          and d['jitter'] == 0.1 and d['stable_after'] == 15.0, str(d))


def _stub_framework():
    return types.SimpleNamespace(loop=None, config={}, services=None)


def test_adapters_use_backoff():
    """三个 WS 适配器都必须持有 Backoff —— 否则改回固定间隔没人发现。"""
    cases = [
        ('ws_client', 'core_plugins.ws_client.main', 'WsClientAdapter',
         {'url': 'ws://127.0.0.1:1/ws'}),
        ('qq_official', 'core_plugins.qq_official.main', 'QQOfficialAdapter',
         {'app_id': 'x', 'app_secret': 'y'}),
        ('discord', 'core_plugins.discord.main', 'DiscordAdapter',
         {'token': 'z'}),
    ]
    for label, mod_path, cls_name, kwargs in cases:
        try:
            mod = importlib.import_module(mod_path)
        except Exception as e:  # noqa: BLE001  缺依赖时跳过而不是误判失败
            print('跳过 %s（导入失败：%s）' % (label, e))
            continue
        cls = getattr(mod, cls_name, None)
        if cls is None:
            candidates = [n for n in dir(mod) if n.endswith('Adapter')]
            check('%s：找到适配器类' % label, bool(candidates), str(candidates))
            cls = getattr(mod, candidates[0])
        try:
            inst = cls(_stub_framework(), **kwargs)
        except Exception as e:  # noqa: BLE001
            check('%s：构造函数可无副作用创建' % label, False, str(e))
        check('%s：持有统一退避实例' % label,
              isinstance(getattr(inst, '_backoff', None), Backoff), cls_name)


def test_no_fixed_interval_sleep_left():
    """源码级守卫：WS 适配器的 supervise 循环不得再按固定间隔 sleep。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ('core_plugins/ws_client/main.py', 'core_plugins/discord/main.py',
                'core_plugins/qq_official/main.py'):
        path = os.path.join(root, rel)
        with open(path, encoding='utf-8') as f:
            src = f.read()
        check('%s：重连等待走退避' % rel,
              '_backoff.next_delay' in src, '未发现 _backoff.next_delay')
        check('%s：不再固定间隔重连' % rel,
              'asyncio.sleep(self.reconnect_interval)' not in src, '仍有固定间隔')


def run_all():
    for fn in (test_exponential_growth, test_jitter_only_extends,
               test_jitter_zero_is_deterministic, test_stable_connection_resets,
               test_flapping_connection_does_not_reset, test_maximum_never_below_base,
               test_reset_api, test_describe, test_adapters_use_backoff,
               test_no_fixed_interval_sleep_left):
        fn()
    print('\n全部通过：断线重连退避 10 组')


if __name__ == '__main__':
    run_all()
