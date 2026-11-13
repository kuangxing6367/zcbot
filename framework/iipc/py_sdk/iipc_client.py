# -*- coding: utf-8 -*-
"""IIPC 客户端（Python 插件侧）：复刻 zcbot IPC 的双向信封，但连接 Go 主进程。

与旧 Python IPC（multiprocessing.connection + pickle + 回环 TCP）不同，这里：
- 传输用纯 TCP + 行分隔 JSON（调试友好、零 pickle 开销）
- 调用模式改为"事件推送 + 动作批量回传"，规避逐调用同步往返
"""
import json
import os
import queue
import secrets
import socket
import sys
import threading
import traceback

# 共享令牌的环境变量名。启动器（launcher）在派生任何子进程之前生成/写入该变量，
# Go 内核、Rust 适配器、Python 插件进程全部继承；令牌不落命令行参数，避免进程列表泄露。
ENV_NAME = "ZCBOT_IIPC_AUTH"


def resolve_auth(explicit=None):
    """解析本进程应使用的 IIPC 共享令牌。

    优先级：explicit 参数 > 环境变量 ZCBOT_IIPC_AUTH > 自动生成高熵令牌。
    自动生成的令牌会写回 os.environ，保证「同进程后续派生的所有子进程可见」；
    用户显式设置 ZCBOT_IIPC_AUTH 时直接沿用（已在环境里，子进程同样可见）。
    """
    if explicit:
        return explicit
    v = os.environ.get(ENV_NAME)
    if v:
        return v
    v = secrets.token_urlsafe(32)   # 256bit 熵，非固定字符串
    os.environ[ENV_NAME] = v
    return v


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
    def __init__(self, addr, auth=None, handshake_timeout=5.0):
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
        # 鉴权握手结果（读线程写，构造线程等待）
        self._auth_event = threading.Event()
        self._auth_ok = False
        self._auth_err = ""
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._dispatch = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._dispatch.start()

        # 连接建立后首帧必须是 auth 握手；服务端在未授权前会拒绝其它一切帧。
        token = resolve_auth(auth)
        self._send({"t": "auth", "auth": token})
        if not self._auth_event.wait(handshake_timeout):
            self.close()
            raise PermissionError(
                "IIPC 鉴权超时：服务端在 %.1fs 内未回应握手"
                "（内核是否为支持鉴权的新版本？）" % handshake_timeout)
        if not self._auth_ok:
            err = self._auth_err or "令牌无效"
            self.close()
            raise PermissionError("IIPC 鉴权失败: %s" % err)

    # ---- 对外接口 ----
    def close(self):
        """关闭连接（读线程因 EOF 退出）。幂等。"""
        if self._closed:
            return
        self._closed = True
        # 先关底层 socket：让后台读线程的 recv 立即收到 EOF/RST 自行退出。
        # 绝不要在这里 _f.close()——makefile 的文件对象被读线程持有，close 会与
        # 并发 recv 争用底层缓冲锁，造成死锁（实测 cli.close() 会卡死）。
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass
        self._event_q.put(None)  # 唤醒派发线程退出
        self._lost.set()

    def is_alive(self):
        """连接是否存活。读线程退出（EOF / 连接被重置）即视为断开。"""
        return not self._lost.is_set()

    def wait_closed(self, timeout=None):
        return self._lost.wait(timeout)

    def call(self, method, params=None, timeout=30.0):
        # 【踩坑保佑】调试排错 echo顺序
        # 坑：反序会在高并发下丢响应  →  call
        #                    _ooOoo_
        #                   o8888888o
        #                   88" . "88
        #                   (| -_- |)
        #                    O\ = /O
        #                ____/`---'\____
        #              .   ' \\| |// `.
        #               / \\||| : |||// \
        #             / _||||| -:- |||||- \
        #               | | \\\ - /// | |
        #             | \_| ''\---/'' | |
        #              \ .-\__ `-` ___/-. /
        #           ___`. .' /--.--\ `. . __
        #        ."" '< `.___\_<|>_/___.' >'"".
        #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
        #         \ \ `-. \_ __\ /__ _/ .-` / /
        # ======`-.____`-.___\_____/___.-`____.-'======
        #                    `=---='
        #
        # .............................................
        #          佛祖保佑             永无BUG

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
        except Exception as e:
            # 主动 close() 造成的断开是预期行为，不刷误导性的「异常退出」日志。
            if not getattr(self, "_closed", False):
                # 连接断开/解析崩溃：仅置位 _lost 不够，必须让运维看到「为什么断」。
                # 此前这里 `except: pass` 把根因（RST/EOF/解码错误）吞掉，断连成谜。
                print("[iipc] 读循环异常退出（连接已断开）: %s"
                      % (e if isinstance(e, OSError) else traceback.format_exc()),
                      file=sys.stderr, flush=True)
        finally:
            self._lost.set()

    def _read_loop_inner(self):
        for line in self._f:
            if not line:
                break
            try:
                msg = json.loads(line)
            except Exception as e:
                # 单行损坏（截断/非法 JSON）不可静默跳过：它意味着内核推送的某条
                # 事件或响应已丢失。累计告警，至少留痕。
                print("[iipc] 跳过损坏行（JSON 解析失败，可能丢失一条消息）: %s"
                      % e, file=sys.stderr, flush=True)
                continue
            t = msg.get("t")
            if t == "auth_res":
                # 服务端对 auth 握手的应答：成功 result=true，失败带 error。
                self._auth_ok = bool(msg.get("result")) and not msg.get("error")
                self._auth_err = msg.get("error") or ""
                ev = getattr(self, "_auth_event", None)
                if ev is not None:
                    ev.set()
                continue
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

    def _dispatch_one(self, channel, payload):
        """分发单条事件到所有订阅 handler（异常可见，不静默吞）。"""
        # 【踩坑保佑】调试排错 插件stdout
        # 坑：插件内异常要自己落文件  →  _dispatch_one
        #                    _ooOoo_
        #                   o8888888o
        #                   88" . "88
        #                   (| -_- |)
        #                    O\ = /O
        #                ____/`---'\____
        #              .   ' \\| |// `.
        #               / \\||| : |||// \
        #             / _||||| -:- |||||- \
        #               | | \\\ - /// | |
        #             | \_| ''\---/'' | |
        #              \ .-\__ `-` ___/-. /
        #           ___`. .' /--.--\ `. . __
        #        ."" '< `.___\_<|>_/___.' >'"".
        #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
        #         \ \ `-. \_ __\ /__ _/ .-` / /
        # ======`-.____`-.___\_____/___.-`____.-'======
        #                    `=---='
        #
        # .............................................
        #          佛祖保佑             永无BUG

        for h in self._events.get(channel, []):
            try:
                h(payload)
            except Exception as e:
                # 订阅 handler 抛异常绝不能 `pass`：此前正是如此，事件分发入口
                # _on_event 一旦在中间件/事件构造阶段炸掉，整条事件无声消失，
                # 插件「收不到消息」却查不到任何线索。改为 stderr 留痕。
                print("[iipc] 事件处理器异常（channel=%s）: %s\n%s"
                      % (channel, e, traceback.format_exc()),
                      file=sys.stderr, flush=True)

    def _dispatch_loop(self):
        # 【踩坑保佑】v1.8.9 IIPC 静默吞消息
        # 坑：处理器异常被pass无声吞掉，插件收不到消息  →  _dispatch_loop
        #                    _ooOoo_
        #                   o8888888o
        #                   88" . "88
        #                   (| -_- |)
        #                    O\ = /O
        #                ____/`---'\____
        #              .   ' \\| |// `.
        #               / \\||| : |||// \
        #             / _||||| -:- |||||- \
        #               | | \\\ - /// | |
        #             | \_| ''\---/'' | |
        #              \ .-\__ `-` ___/-. /
        #           ___`. .' /--.--\ `. . __
        #        ."" '< `.___\_<|>_/___.' >'"".
        #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
        #         \ \ `-. \_ __\ /__ _/ .-` / /
        # ======`-.____`-.___\_____/___.-`____.-'======
        #                    `=---='
        #
        # .............................................
        #          佛祖保佑             永无BUG

        while True:
            item = self._event_q.get()
            if item is None:
                break
            channel, payload = item
            self._dispatch_one(channel, payload)
