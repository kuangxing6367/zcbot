# -*- coding: utf-8 -*-
"""日志合并汇总的三处语义修正

线上观察（正常启动日志）：
    [14:04:09] WARNING [日志合并] 该错误在 10s 内重复 1 次：⚠ 安全提示: Web 面板监听 0.0.0.0...
两个问题：
1. 「重复 1 次」= 窗口内只出现过一次，首发已原样打过，这条纯属重复且措辞失真；
   更糟的是插件依赖已自愈成功后，还会补一条「官方插件 [webui] 加载失败」的旧错误。
2. WARNING 被叫「该错误」，看日志的人以为线上真报错了。

断言：count<=1 不发；WARNING→该告警；ERROR→该错误；INFO 不参与合并。
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.coalesce_log import LogCoalescer  # noqa: E402


class Sink(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


def _with_sink(fn):
    root = logging.getLogger()
    sink = Sink()
    c = LogCoalescer(window=10.0)
    root.addHandler(sink)
    root.addFilter(c)
    try:
        fn(c)
    finally:
        root.removeHandler(sink)
        root.removeFilter(c)
        c.stop()
    return sink.msgs


def test_single_occurrence_no_summary():
    """只出现一次 → 不能补「重复 1 次」"""
    def run(c):
        c._emit_summary('k', {'count': 1, 'levelno': logging.WARNING,
                              'name': 'zcbot', 'sample': '只有一次'})
    msgs = _with_sink(run)
    assert msgs == [], f'count=1 不应发汇总，实际: {msgs}'
    return 'count=1 不补汇总'


def test_warning_label():
    def run(c):
        c._emit_summary('k', {'count': 3, 'levelno': logging.WARNING,
                              'name': 'zcbot', 'sample': '安全提示'})
    msgs = _with_sink(run)
    assert len(msgs) == 1, msgs
    assert '该告警' in msgs[0], msgs[0]
    assert '该错误' not in msgs[0], msgs[0]
    assert '重复 3 次' in msgs[0], msgs[0]
    return 'WARNING → 该告警'


def test_error_label():
    def run(c):
        c._emit_summary('k', {'count': 2, 'levelno': logging.ERROR,
                              'name': 'zcbot', 'sample': '数据库不可用'})
    msgs = _with_sink(run)
    assert len(msgs) == 1 and '该错误' in msgs[0], msgs
    return 'ERROR → 该错误'


def test_info_not_coalesced():
    """INFO 不参与合并（保持完整可见）"""
    def run(c):
        for i in range(3):
            rec = logging.LogRecord('zcbot', logging.INFO, '', 0, '普通信息', (), None)
            assert c.filter(rec) is True, 'INFO 被合并了'
    _with_sink(run)
    return 'INFO 不合并'


def test_repeat_emits_once_per_window():
    """窗口内重复 → 只发首条，汇总在窗口到期时补一条"""
    def run(c):
        for _ in range(4):
            rec = logging.LogRecord('zcbot', logging.ERROR, '', 0, '同一条错误', (), None)
            c.filter(rec)
        for k, b in list(c._buckets.items()):
            c._emit_summary(k, b)
    msgs = _with_sink(run)
    assert len(msgs) == 1, msgs
    assert '重复 4 次' in msgs[0], msgs
    return '重复计数与汇总正常'


if __name__ == '__main__':
    tests = [test_single_occurrence_no_summary, test_warning_label,
             test_error_label, test_info_not_coalesced,
             test_repeat_emits_once_per_window]
    ok = fail = 0
    for t in tests:
        try:
            print(f'PASS {t.__name__}: {t()}')
            ok += 1
        except AssertionError as e:
            print(f'FAIL {t.__name__}: {e}')
            fail += 1
        except Exception as e:  # noqa: BLE001
            print(f'ERROR {t.__name__}: {type(e).__name__}: {e}')
            fail += 1
    print(f'\n{ok} passed, {fail} failed')
    sys.exit(1 if fail else 0)
