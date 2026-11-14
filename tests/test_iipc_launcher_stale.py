"""IIPC 启动器「二进制陈旧检测」回归。

历史实现是「exe 存在就直接用」：源码改了协议（例如握手新增 auth 帧）但
二进制没重编，新客户端与旧内核握手超时 —— 表现只是「连不上，没原因」，
最难排查。这里钉住：源码比二进制新就必须重编，且没有工具链时退回旧二进制
不能把整个链路弄挂。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

launcher = pytest.importorskip('framework.iipc.launcher')


def _mk(path, mtime):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('x')
    os.utime(path, (mtime, mtime))


def test_missing_binary_is_stale(tmp_path):
    assert launcher._binary_is_stale(str(tmp_path / 'nope'), str(tmp_path), ('*.go',))


def test_binary_newer_than_sources_is_fresh(tmp_path):
    exe = tmp_path / 'zcbot_kernel.exe'
    src = tmp_path / 'main.go'
    _mk(str(src), 1000)
    _mk(str(exe), 2000)
    assert launcher._binary_is_stale(str(exe), str(tmp_path), ('*.go',)) is False


def test_source_newer_than_binary_is_stale(tmp_path):
    """典型场景：git pull 更新了源码，二进制还是旧的。"""
    exe = tmp_path / 'zcbot_kernel.exe'
    src = tmp_path / 'ipc_server.go'
    _mk(str(exe), 1000)
    _mk(str(src), 2000)
    assert launcher._binary_is_stale(str(exe), str(tmp_path), ('*.go',)) is True


def test_deep_subdir_source_counts(tmp_path):
    """Rust 的源码在 src/bin/*.rs，递归扫描必须看得到。"""
    exe = tmp_path / 'target' / 'release' / 'zcbot_adapter.exe'
    _mk(str(exe), 1000)
    _mk(str(tmp_path / 'src' / 'bin' / 'hub.rs'), 2000)
    assert launcher._binary_is_stale(
        str(exe), str(tmp_path / 'src'), ('*.rs',)) is True


def test_skip_build_env_respected(monkeypatch):
    monkeypatch.setenv('ZCBOT_IIPC_SKIP_BUILD', '1')
    assert launcher._skip_build_requested() is True
    for v in ('', '0', 'false', 'False'):
        monkeypatch.setenv('ZCBOT_IIPC_SKIP_BUILD', v)
        assert launcher._skip_build_requested() is False, v
