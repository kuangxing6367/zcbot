# -*- coding: utf-8 -*-
"""IIPC 客户端（Python 插件侧）：复刻 zcbot IPC 的双向信封，但连接 Go 主进程。

与旧 Python IPC（multiprocessing.connection + pickle + 回环 TCP）不同，这里：
- 传输用纯 TCP + 行分隔 JSON（调试友好、零 pickle 开销）
- 调用模式改为"事件推送 + 动作批量回传"，规避逐调用同步往返
"""
import json
import queue
import socket
import threading


class _Pending:
    """一次 call 的等待对象：既要能 set()/wait()（原 threading.Event 语义），
    又要能携带 result / error（threading.Event 没有的属性，原实现在这里炸了）。"""

    def __init__(self):
        self._ev = threading.Event()
        self.result = None
        self.error = None

    def set(self, result=None, error=None):
        self.result = result
        self.error = error
        self._ev.set()

    def wait(self, timeout):
        return self._ev.wait(timeout)

    def is_set(self):
        return self._ev.is_set()


class IIPCClient:
    def __init__(self, addr, auth):
        host, port = addr.rsplit(":", 1)
        self._sock = socket.create_connection((host, int(port)))
        self._f = self._sock.makefile("rwb")
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending = {}
        self._methods = {}
        self._events = {}
        self._closed = False
        self._lost = threading.Event()  # 连接丢失信号：读线程退出即置位
        self._event_q = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._dispatch = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._dispatch.start()

    # ---- 对外接口 ----
    def is_alive(self):
        """连接是否存活。读线程退出（EOF / 连接被重置）即视为断开。"""
        return not self._lost.is_set()

    def wait_closed(self, timeout=None):
        return self._lost.wait(timeout)

    def call(self, method, params=None, timeout=30.0):
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            ready = _Pending()
            self._pending[mid] = ready
        try:
            self._send({"t": "req", "id": mid, "method": method,
                        "params": params if params is not None else {}})
            if not ready.wait(timeout):
                with self._lock:
                    self._pending.pop(mid, None)
                raise TimeoutError(f"IPC 超时: {method}")
            with self._lock:
                entry = self._pending.pop(mid)
            if entry.error:
                raise RuntimeError(f"IPC 错误 {method}: {entry.error}")
            return entry.result
        except KeyError:
            raise RuntimeError(f"未收到响应: {method}")

    def notify(self, channel, payload):
        self._send({"t": "evt", "channel": channel, "payload": payload})

    def fire(self, method, params=None):
        """发 req 帧但不等待响应（fire-and-forget）——用于日志等高频小消息。

        内核照常处理该方法并尝试回 res；响应 id=0，客户端读循环对 id=0
        无 pending 条目，直接忽略，无害。
        """
        self._send({"t": "req", "id": 0, "method": method,
                    "params": params if params is not None else {}})

    def on(self, channel, handler):
        self._events.setdefault(channel, []).append(handler)

    def register(self, method, handler):
        self._methods[method] = handler

    # ---- 内部 ----
    def _send(self, msg):
        with self._lock:
            self._f.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            self._f.flush()

    def _read_loop(self):
        try:
            self._read_loop_inner()
        except Exception:
            pass
        finally:
            self._lost.set()

    def _read_loop_inner(self):
        for line in self._f:
            if not line:
                break
            try:
                msg = json.loads(line)
            except Exception:
                continue
            t = msg.get("t")
            if t == "req":
                h = self._methods.get(msg.get("method"))
                if h:
                    try:
                        res = h(msg.get("params") or {})
                        self._send({"t": "res", "id": msg["id"], "result": res})
                    except Exception as e:
                        self._send({"t": "res", "id": msg["id"], "error": str(e)})
                else:
                    self._send({"t": "res", "id": msg["id"], "error": "unknown method"})
            elif t == "res":
                mid = msg.get("id")
                entry = self._pending.get(mid)
                if entry is not None:
                    entry.set(result=msg.get("result"), error=msg.get("error"))
            elif t == "evt":
                # 关键：事件分发必须离开读线程，否则用户 handler 里若做阻塞式
                # call()，读线程被占住、响应永远读不到 → 死锁（这是 /ping 卡死的根因）。
                self._event_q.put((msg.get("channel"), msg.get("payload")))

    def _dispatch_loop(self):
        while True:
            item = self._event_q.get()
            if item is None:
                break
            channel, payload = item
            for h in self._events.get(channel, []):
                try:
                    h(payload)
                except Exception:
                    pass
