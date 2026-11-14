"""P0 修复回归测试：L4 写缓冲不丢不重、超时不重试、降级标志、产物完整性。

铁律：路径由 __file__ / tmp_path 推导，不写死时区，不污染项目目录。
"""
import asyncio
import os
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from framework.core.event_buffer import EventBuffer
from framework.messaging.outbound_guard import OutboundGate


def _cfg(**over):
    buf = {'l1_max_bytes': 512 * 1024, 'sqlite_enabled': True}
    buf.update(over)
    return {'maxsize': 2000, 'buffer': buf}


def _ev(i=0, text='hello'):
    return {'type': 'message', 'bot_name': 'b', 'user_id': 1,
            'message': text, 'raw_message': text, 'seq': i}


# ── L4 写缓冲 ────────────────────────────────────────────────────────────

def test_l4_direct_consume_returns_bytes_watermark(tmp_path):
    """消费者直取 L4 必须回扣 _l4_bytes，否则攒批层越过 4MB 后永久失效。"""
    async def run():
        buf = EventBuffer(_cfg(), str(tmp_path))
        assert buf._l4_append(_ev(1), 100) is True
        assert buf.stats()['l4_bytes'] == 100
        ev, _done, src = await buf.get_async()
        assert src == 'l4' and ev['seq'] == 1
        assert buf.stats()['l4_bytes'] == 0
        # 回扣后仍能继续攒批
        assert buf._l4_append(_ev(2), 100) is True
        assert buf.stats()['l4_bytes'] == 100
        buf.close()
    asyncio.run(run())


def test_close_flushes_remaining_l4_to_l2(tmp_path):
    """停机时 L4 残留不能随进程蒸发：close() 必须同步落 L2，重启可读回。"""
    async def run():
        buf = EventBuffer(_cfg(), str(tmp_path))
        assert buf._l4_append(_ev(7), 120) is True
        buf.close()
        buf2 = EventBuffer(_cfg(), str(tmp_path))
        assert buf2._l2_rows() >= 1, 'L4 残留未落 L2，重启后将丢事件'
        buf2.close()
    asyncio.run(run())


def test_wait_drained_timeout_does_not_cancel_flush(tmp_path, monkeypatch):
    """旧实现用 wait_for 直接取消 flush 协程，batch 被取走后随协程销毁。

    shield 后超时只让 wait_drained 返回 False，flush 在后台继续跑完并落 L2。
    """
    async def run():
        buf = EventBuffer(_cfg(), str(tmp_path))
        assert buf._l4_append(_ev(9), 80) is True

        real = buf._l2_write_batch

        async def slow(events):
            await asyncio.sleep(0.2)
            return await real(events)

        monkeypatch.setattr(buf, '_l2_write_batch', slow)
        assert await buf.wait_drained(0.03) is False
        await asyncio.sleep(0.35)
        assert buf.stats()['l4_items'] == 0, '超时后批次不应留在 L4'
        assert buf._l2_rows() >= 1, '超时取消导致整批丢失（回归）'
        buf.close()
    asyncio.run(run())


# ── 出站重试契约 ──────────────────────────────────────────────────────────

def _gate(**retry):
    g = OutboundGate()
    cfg = {
        'rate_limit': {'enabled': False, 'window': 60, 'global_per_min': 1000,
                       'group_per_min': 1000, 'user_per_min': 1000, 'max_wait': 5.0},
        'retry': {'enabled': True, 'attempts': 3, 'backoff': 0},
    }
    cfg['retry'].update(retry)
    g.configure(cfg)
    return g


def test_timeout_error_is_never_retried_sync():
    """超时可能已送达（契约 -2），重试等于重复发消息。"""
    g = _gate()
    calls = []

    def call():
        calls.append(1)
        raise TimeoutError('网络超时')

    with pytest.raises(TimeoutError):
        g.execute_sync('send_group_msg', None, {'group_id': 1}, call)
    assert len(calls) == 1, '超时被重试了'


def test_timeout_error_is_never_retried_async():
    async def run():
        g = _gate()
        calls = []

        async def call():
            calls.append(1)
            raise TimeoutError('网络超时')

        with pytest.raises(TimeoutError):
            await g.execute_async('send_group_msg', None, {'group_id': 1}, call)
        assert len(calls) == 1, '超时被重试了'
    asyncio.run(run())


def test_ordinary_exception_still_retries():
    """本地通道异常沿用原有重试语义（防止把修法扩大成全面不重试）。"""
    g = _gate()
    calls = []

    def call():
        calls.append(1)
        raise RuntimeError('通道炸了')

    with pytest.raises(RuntimeError):
        g.execute_sync('send_group_msg', None, {'group_id': 1}, call)
    assert len(calls) == 4, '普通异常应保持 attempts+1 次调用'


# ── 数据库故障降级标志 ────────────────────────────────────────────────────

def test_db_fallback_sets_degraded_flag(tmp_path, monkeypatch):
    """降级后 base.db_degraded 必须为真，否则 /readyz 与指标一路说「正常」。"""
    from framework.database import db as dbmod

    def boom(_cfg):
        raise RuntimeError('db down')

    monkeypatch.setattr(dbmod, 'create_storage', boom)
    engine = dbmod.init_db({
        'type': 'sqlite',
        'path': str(tmp_path / 'zcbot.db'),
        'fallback_to_sim': True,
        'fallback_dir': str(tmp_path / 'fallback'),
    })
    assert getattr(engine, 'degraded', False) is True
    assert getattr(engine, 'debug_mode', False) is True


# ── WebUI 产物完整性门禁 ──────────────────────────────────────────────────

def test_webui_artifact_checker_real_tree():
    r = subprocess.run(
        [sys.executable, os.path.join(_ROOT, 'tools', 'check_webui_artifact.py'),
         '--root', os.path.join(_ROOT, 'core_plugins', 'webui', 'web')],
        capture_output=True, text=True, cwd=_ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


def test_webui_artifact_checker_rejects_missing_chunk(tmp_path):
    """v1.20.0 事故：index.html / chunk 引用了仓库里没有的 hash 文件。"""
    js = tmp_path / 'js'
    js.mkdir()
    (tmp_path / 'index.html').write_text(
        '<script type="module" src="./js/app.AAA.js"></script>', encoding='utf-8')
    (js / 'app.AAA.js').write_text(
        'import"./element-plus.MISSING.js"', encoding='utf-8')
    r = subprocess.run(
        [sys.executable, os.path.join(_ROOT, 'tools', 'check_webui_artifact.py'),
         '--root', str(tmp_path)],
        capture_output=True, text=True, cwd=_ROOT)
    assert r.returncode == 1, '缺失 chunk 必须被门禁拦下'
    assert 'element-plus.MISSING.js' in r.stdout
