# -*- coding: utf-8 -*-
"""
轻量 IPC 信封协议（基于 multiprocessing.connection）

核心/宿主两侧复用同一个 JsonRpcConnection：
- 传输用 multiprocessing.connection（pickle 帧，原生支持 datetime/bytes），
  authkey 在 Listener/Client 层已做内置摘要鉴权。
- 信封字段 JSON 风格可读：
    {"t":"req|res|event|ping|pong","id":int,"method":str,"params":dict,
     "result":..., "error":str, "channel":str,"payload":...}
- 双向：
    * 请求侧 call()/acall() 发 req，等待 res（id 匹配）
    * 服务侧 register(method, handler) 处理 req，结果回发
    * 任意侧 notify(channel,payload) 广播 event，对端 on(channel,handler) 订阅
- 读线程处理入站消息；req 的执行可提交线程池，或（设置 loop 后）调度到事件循环。

线程模型：
- call() 是同步阻塞（供 executor 线程 / 同步 handler 使用）
- acall() 内部转 to_thread 执行 call，不阻塞事件循环
- req 到达后：
    * 若本端已 set_loop(loop)（有事件循环），用 run_coroutine_threadsafe 调度，
      async handler 直接 await，同步 handler 转 to_thread
    * 否则在线程池执行
"""
import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger('zcbot')


class IpcClosed(Exception):
    """IPC 通道已关闭/超时"""


class RemoteError(Exception):
    """对端方法执行抛出异常（携带对端错误信息）"""


class JsonRpcConnection:
    """双向信封连接：读线程 + pending + 方法注册表 + 事件订阅"""

    def __init__(self, conn, methods=None, events=None):
        self._conn = conn
        self._id_lock = threading.Lock()
        self._next_id = 0
        self._pending = {}      # id -> threading.Event
        self._results = {}      # id -> (ok, value)
        # 共享方法/事件表：可由外部（IpcServer/IpcClient）注入，accept 后复用
        self._methods = methods if methods is not None else {}
        self._events = events if events is not None else {}
        self._read_thread = threading.Thread(
            target=self._read_loop, daemon=True, name='ipc-read')
        self._executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix='ipc-handler')
        self._send_lock = threading.Lock()   # multiprocessing.Connection.send 非线程安全
        self._closed = False
        self._loop = None       # 由外部 set_loop 指定事件循环（异步 handler 调度）

    # ---- 对外接口 ----

    def set_loop(self, loop):
        """设置本端事件循环（有 loop 时 req 的 handler 调度到该 loop 执行）"""
        self._loop = loop

    def register(self, method, handler):
        """注册本地方法服务（handler 可为同步函数或 async 函数）"""
        self._methods[method] = handler

    def on(self, channel, handler):
        """订阅对端 notify 的事件（handler 在读线程被同步调用）"""
        self._events.setdefault(channel, []).append(handler)

    def notify(self, channel, payload):
        """单向发送事件（不等响应）"""
        self._send({'t': 'event', 'channel': channel, 'payload': payload})

    def call(self, method, params=None, timeout=30.0):
        """同步 RPC（阻塞当前线程直到对端返回）"""
        msg_id = self._next_id_safe()
        ev = threading.Event()
        self._pending[msg_id] = ev
        self._send({'t': 'req', 'id': msg_id, 'method': method,
                    'params': params if params is not None else {}})
        if not ev.wait(timeout):
            self._pending.pop(msg_id, None)
            raise IpcClosed(f"RPC 超时: {method}")
        ok, value = self._results.pop(msg_id)
        if not ok:
            raise RemoteError(value)
        return value

    async def acall(self, method, params=None, timeout=30.0):
        """异步 RPC（不阻塞事件循环）"""
        return await asyncio.to_thread(self.call, method, params, timeout)

    def close(self):
        self._closed = True
        try:
            self._conn.close()
        except Exception:
            pass
        self._executor.shutdown(wait=False)

    # ---- 内部 ----

    def _next_id_safe(self):
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _send(self, msg):
        if self._closed:
            raise IpcClosed("IPC 通道已关闭")
        try:
            with self._send_lock:
                self._conn.send(msg)
        except Exception as e:
            self._closed = True
            raise IpcClosed(f"IPC 发送失败: {e}") from e

    def _safe_send(self, msg):
        try:
            with self._send_lock:
                self._conn.send(msg)
        except Exception:
            pass

    def start(self):
        """启动读线程"""
        self._read_thread.start()

    def _read_loop(self):
        while not self._closed:
            try:
                msg = self._conn.recv()
            except Exception as e:
                # 对端正常关闭/进程退出都会触发 recv 异常（如 WinError 10054），
                # 属于预期内的通道关闭，降级为 info 避免误报；真正的 RPC 失败
                # 会由调用方以超时/RemoteError 形式暴露。
                if not self._closed:
                    logger.info(f"IPC 连接关闭（对端断开）: {e}")
                self._closed = True
                break
            try:
                self._dispatch(msg)
            except Exception as e:
                logger.error(f"IPC 消息处理异常: {e}")

    def _dispatch(self, msg):
        t = msg.get('t')
        if t == 'req':
            self._handle_req(msg)
        elif t == 'res':
            self._handle_res(msg)
        elif t == 'event':
            self._handle_event(msg)
        elif t == 'ping':
            self._safe_send({'t': 'pong'})
        # pong 无需处理

    def _handle_res(self, msg):
        msg_id = msg.get('id')
        ev = self._pending.pop(msg_id, None)
        if ev is None:
            return
        if 'error' in msg:
            self._results[msg_id] = (False, msg.get('error'))
        else:
            self._results[msg_id] = (True, msg.get('result'))
        ev.set()

    def _handle_event(self, msg):
        handlers = self._events.get(msg.get('channel'), [])
        payload = msg.get('payload')
        for h in list(handlers):
            try:
                h(payload)
            except Exception as e:
                logger.error(f"IPC 事件处理异常 [{msg.get('channel')}]: {e}")

    def _handle_req(self, msg):
        if self._loop is not None and self._loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(
                self._exec_req_async(msg), self._loop)

            def _on_done(f):
                try:
                    result = f.result()
                except RemoteError as e:
                    result = {'t': 'res', 'id': msg['id'], 'error': str(e)}
                except Exception as e:
                    result = {'t': 'res', 'id': msg['id'], 'error': str(e)}
                else:
                    result = {'t': 'res', 'id': msg['id'], 'result': result}
                self._safe_send(result)

            fut.add_done_callback(_on_done)
        else:
            self._executor.submit(self._exec_req_sync, msg)

    async def _exec_req_async(self, msg):
        method = msg.get('method')
        handler = self._methods.get(method)
        if handler is None:
            raise RemoteError(f"未知方法: {method}")
        params = msg.get('params') or {}
        if asyncio.iscoroutinefunction(handler):
            return await handler(**params)
        return await asyncio.to_thread(handler, **params)

    def _exec_req_sync(self, msg):
        method = msg.get('method')
        handler = self._methods.get(method)
        try:
            if handler is None:
                result = {'t': 'res', 'id': msg['id'],
                          'error': f"未知方法: {method}"}
            else:
                r = handler(**msg.get('params') or {})
                if asyncio.iscoroutine(r):
                    r = asyncio.run(r)
                result = {'t': 'res', 'id': msg['id'], 'result': r}
        except Exception as e:
            result = {'t': 'res', 'id': msg['id'], 'error': str(e)}
        self._safe_send(result)
