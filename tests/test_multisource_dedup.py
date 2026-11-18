"""多源去重（reliability.multisource_dedup）回归。

核心约束：默认关——多份适配器/多个 bot 收到同一条消息时默认逐个处理；
开启后按「内容 + 发送者(可关) + 群」的 16 字节指纹跨 bot 合并，且必须
明确警示「可能误删真实消息」。与同 bot 的 message_id 重放去重正交。

运行：py -3 -m pytest tests/test_multisource_dedup.py -q
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.messaging.inbound_guard import InboundGuard


def _msg(bot='a', mid=1, uid=1001, gid=2001, text='hello'):
    return {
        'post_type': 'message', 'bot_name': bot, 'message_id': mid,
        'user_id': uid, 'group_id': gid, 'raw_message': text, 'message': text,
    }


def _cfg(enabled=True, window=5, include_sender=True):
    return {
        'dedup_window': 0,
        'multisource_dedup': {
            'enabled': enabled, 'window': window,
            'include_sender': include_sender,
        },
    }


# ── 默认语义：关，逐个处理 ────────────────────────────────────
def test_default_off_passes_every_source():
    g = InboundGuard({'dedup_window': 0})
    assert g.ms_enabled is False
    assert g.check_multisource(_msg(bot='a', mid=1)) is True
    assert g.check_multisource(_msg(bot='b', mid=2)) is True
    assert g.check_multisource(_msg(bot='a', mid=3)) is True
    assert g._ms_seen == {}


def test_window_zero_means_off_even_when_enabled_true():
    g = InboundGuard(_cfg(enabled=True, window=0))
    assert g.check_multisource(_msg(bot='a')) is True
    assert g.check_multisource(_msg(bot='b')) is True
    assert g._ms_seen == {}


# ── 开启后的跨实例/跨 bot 合并 ────────────────────────────────
def test_enabled_dedups_across_bots_and_message_ids():
    g = InboundGuard(_cfg())
    assert g.check_multisource(_msg(bot='a', mid=1)) is True
    # bot 和 message_id 都不进指纹：这正是多源场景要拦的重复
    assert g.check_multisource(_msg(bot='b', mid=9999)) is False


def test_sender_and_group_scope():
    g = InboundGuard(_cfg())
    assert g.check_multisource(_msg(uid=1001, gid=2001)) is True
    assert g.check_multisource(_msg(uid=1002, gid=2001)) is True   # 不同人
    assert g.check_multisource(_msg(uid=1001, gid=2002)) is True   # 不同群
    assert g.check_multisource(_msg(uid=1001, gid=2001, mid=7)) is False


def test_include_sender_false_dedups_different_users():
    """关掉 sender 是更危险的显式选择：不同人发相同内容也会被合并。"""
    g = InboundGuard(_cfg(include_sender=False))
    assert g.check_multisource(_msg(uid=1, mid=1)) is True
    assert g.check_multisource(_msg(uid=2, mid=2)) is False
    assert g.check_multisource(_msg(uid=3, mid=3, gid=9999)) is True


def test_window_expiry_and_sweep():
    g = InboundGuard(_cfg(window=1))
    fp = g.multisource_fingerprint(_msg())
    assert g.check_multisource(_msg()) is True
    assert g.check_multisource(_msg(bot='b', mid=2)) is False
    time.sleep(1.05)
    assert g.check_multisource(_msg(bot='b', mid=3)) is True   # 窗口过了再次放行
    g._ms_seen[fp] = time.time() - 1
    g.sweep()
    assert g._ms_seen == {}


# ── 失败安全 / 内存有界 ───────────────────────────────────────
def test_non_message_or_unusable_text_passes():
    g = InboundGuard(_cfg())
    odd = [
        {'post_type': 'notice', 'raw_message': 'x', 'user_id': 1},
        {'post_type': 'message', 'raw_message': '', 'user_id': 1},
        {'post_type': 'message', 'raw_message': '   ', 'user_id': 1},
        {'post_type': 'message', 'raw_message': 'x', 'user_id': None,
         'sender': None},
        {'post_type': 'message', 'raw_message': 'x', 'user_id': None,
         'sender': 'not-a-dict'},
    ]
    for e in odd:
        assert g.check_multisource(e) is True, e
    assert g._ms_seen == {}


def test_fingerprint_is_bounded_digest():
    """超长消息不能把去重表按原文撑大；相同内容必须得到同一摘要。"""
    g = InboundGuard(_cfg())
    long_text = '图片说明' * 25000
    fp = g.multisource_fingerprint(_msg(text=long_text))
    assert isinstance(fp, bytes) and len(fp) == 16
    assert fp == g.multisource_fingerprint(
        _msg(bot='b', mid=2, text=long_text))


# ── 走真实 dispatch_event 的接线验证 ──────────────────────────
def test_dispatch_wiring_default_off_then_on():
    from framework.core.dispatch import FrameworkDispatchMixin

    class _Svc:
        def adapter_for_source(self, source):
            raise LookupError('stub')

    class _Hooks:
        async def trigger_async(self, *a, **kw):
            return []

    class _Stub:
        dispatch_event = FrameworkDispatchMixin.dispatch_event

        def __init__(self, cfg):
            self.config = cfg
            self.services = _Svc()
            self.hooks = _Hooks()
            self._event_workers = []
            self._running = False
            self.processed = []

        async def _process_event(self, event):
            self.processed.append(event)

        async def reply_text(self, target, text):
            raise AssertionError('本用例不应触发限流回复')

    async def _run():
        off = _Stub({'reliability': {'dedup_window': 0}})
        for bot, mid in (('a', 1), ('b', 2)):
            await off.dispatch_event(_msg(bot=bot, mid=mid, text='同一句话'))
        assert len(off.processed) == 2, '默认必须逐个处理'

        on = _Stub({'reliability': {
            'dedup_window': 0,
            'multisource_dedup': {'enabled': True, 'window': 5},
        }})
        for bot, mid in (('a', 1), ('b', 2)):
            await on.dispatch_event(_msg(bot=bot, mid=mid, text='同一句话'))
        assert len(on.processed) == 1, '开启后跨 bot 重复只处理首条'

    asyncio.run(_run())


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
