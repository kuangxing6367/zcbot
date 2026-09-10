# -*- coding: utf-8 -*-
"""
IPC 客户端（进程2 · 宿主侧）

- 用 multiprocessing.connection.Client 连接核心，authkey 握手鉴权。
- 封装为 JsonRpcConnection；提供 call/acall（宿主→核心 RPC）、notify。
- PING 保活线程 + 断线检测。
"""
import logging
import multiprocessing.connection as mpc
import threading
import time

from framework.ipc.protocol import JsonRpcConnection

logger = logging.getLogger('zcbot')


class IpcClient:
    """宿主侧 IPC 客户端"""

    def __init__(self, address, authkey, heartbeat_interval=5.0):
        self._address = address
        self._authkey = authkey
        self._heartbeat_interval = heartbeat_interval
        self.conn = None
        self._closed = False
        self._hb_thread = None
        self._lock = threading.Lock()

    def connect(self, timeout=30.0):
        """连接核心（阻塞直到 accept；用带超时的 Worker 连接）"""
        # Client 本身无超时参数，用线程封装超时
        holder = {}

        def _do():
            try:
                raw = mpc.Client(self._address, family='AF_INET',
                                 authkey=self._authkey)
                holder['conn'] = raw
            except Exception as e:
                holder['error'] = e

        t = threading.Thread(target=_do, daemon=True, name='ipc-connect')
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise TimeoutError(f"连接核心超时: {self._address}")
        if 'error' in holder:
            raise holder['error']
        with self._lock:
            self.conn = JsonRpcConnection(holder['conn'])
            self.conn.start()
        return self.conn

    def register(self, method, handler):
        """注册宿主侧方法服务（核心→宿主 RPC）"""
        with self._lock:
            self.conn.register(method, handler)

    def on(self, channel, handler):
        """订阅核心推送的事件"""
        with self._lock:
            self.conn.on(channel, handler)

    def set_loop(self, loop):
        with self._lock:
            if self.conn is not None:
                self.conn.set_loop(loop)

    def call(self, method, params=None, timeout=30.0):
        if self.conn is None:
            raise RuntimeError("IPC 未连接")
        return self.conn.call(method, params, timeout)

    async def acall(self, method, params=None, timeout=30.0):
        if self.conn is None:
            raise RuntimeError("IPC 未连接")
        return await self.conn.acall(method, params, timeout)

    def notify(self, channel, payload):
        if self.conn is None:
            raise RuntimeError("IPC 未连接")
        self.conn.notify(channel, payload)

    def start_heartbeat(self):
        """启动 PING 保活线程"""
        if self._hb_thread is not None and self._hb_thread.is_alive():
            return
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name='ipc-heartbeat')
        self._hb_thread.start()

    def _heartbeat_loop(self):
        while not self._closed:
            time.sleep(self._heartbeat_interval)
            if self._closed:
                break
            try:
                if self.conn is not None:
                    self.conn.notify('ping', None)
            except Exception:
                break

    @property
    def connected(self) -> bool:
        return self.conn is not None and not self.conn._closed

    def close(self):
        self._closed = True
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
