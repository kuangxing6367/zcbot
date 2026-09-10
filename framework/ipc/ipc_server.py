# -*- coding: utf-8 -*-
"""
IPC 服务端（进程1 · 核心侧）

- 用 multiprocessing.connection.Listener（AF_INET 回环）监听宿主连接，authkey 握手鉴权。
- accept 后封装为 JsonRpcConnection，复用本对象的共享方法/事件表。
- 提供 request_host()（核心→宿主 RPC）与 send_event()（事件推送）。
"""
import logging
import multiprocessing.connection as mpc
import threading

from framework.ipc.protocol import JsonRpcConnection, IpcClosed

logger = logging.getLogger('zcbot')


class IpcServer:
    """核心侧 IPC 服务端（单宿主）"""

    def __init__(self, authkey, host='127.0.0.1', port=0):
        self._authkey = authkey
        self._host = host
        self._port = port
        # 共享方法/事件表：accept 前注册，accept 后注入到连接
        self._methods = {}
        self._events = {}
        # 创建监听器并拿到实际地址（供 spawn 宿主时传入）
        self._listener = mpc.Listener((host, port), family='AF_INET',
                                      authkey=authkey)
        self.address = self._listener.address   # ('127.0.0.1', actual_port)
        self.conn = None                        # 已接受的 JsonRpcConnection
        self._accept_thread = None
        self._loop = None
        self._closed = False
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self.conn is not None and not getattr(self.conn, '_closed', False)

    def register(self, method, handler):
        """注册核心侧方法服务（handler 可为同步或 async）"""
        self._methods[method] = handler

    def on(self, channel, handler):
        """订阅宿主 notify 的事件（当前核心侧未用，预留）"""
        self._events.setdefault(channel, []).append(handler)

    def set_loop(self, loop):
        """注入核心事件循环（req 的 handler 在此 loop 调度）"""
        self._loop = loop
        if self.conn is not None:
            self.conn.set_loop(loop)

    def start_accept_thread(self):
        """启动 accept 线程（阻塞等待宿主连接）"""
        if self._accept_thread is None or not self._accept_thread.is_alive():
            self._accept_thread = threading.Thread(
                target=self._accept_loop, daemon=True, name='ipc-accept')
            self._accept_thread.start()

    def _accept_loop(self):
        while not self._closed:
            try:
                raw_conn = self._listener.accept()
            except Exception as e:
                if self._closed:
                    break
                logger.error(f"IPC accept 失败: {e}")
                continue
            with self._lock:
                # 复用共享方法/事件表；若已有连接，先关旧的（单宿主）
                if self.conn is not None:
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                self.conn = JsonRpcConnection(
                    raw_conn, methods=self._methods, events=self._events)
                if self._loop is not None:
                    self.conn.set_loop(self._loop)
                self.conn.start()
            logger.info(f"IPC 宿主已连接: {self.address}")

    # ---- 对外 ----

    def request_host(self, method, params=None, timeout=30.0):
        """核心→宿主 RPC（同步，请在 executor 线程调用）"""
        if not self.connected:
            raise IpcClosed("宿主尚未连接")
        return self.conn.call(method, params, timeout)

    async def arequest_host(self, method, params=None, timeout=30.0):
        """核心→宿主 RPC（异步）"""
        if not self.connected:
            raise IpcClosed("宿主尚未连接")
        return await self.conn.acall(method, params, timeout)

    def send_event(self, event: dict):
        """推送事件到宿主（协议收到事件后调用）"""
        if not self.connected:
            raise IpcClosed("宿主未连接，事件丢弃")
        self.conn.notify('event', event)

    async def asend_event(self, event: dict):
        """异步推送事件到宿主"""
        if not self.connected:
            raise IpcClosed("宿主未连接，事件丢弃")
        self.conn.notify('event', event)

    def close(self):
        self._closed = True
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        try:
            self._listener.close()
        except Exception:
            pass
