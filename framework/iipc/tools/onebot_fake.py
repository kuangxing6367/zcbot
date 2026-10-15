# -*- coding: utf-8 -*-
"""OneBot 11 假实现（联调用）：std-only WebSocket 服务端。

用途：在没有真实 OneBot 实现（如 NapCat/LLOneBot）的环境下，联调验证
IIPC 全链路：

    假 OneBot(WS 服务端) <──反向WS──> Rust 适配器 <──IIPC──> Go 内核 <──> Python 插件

- accept 适配器的反向 WS 连接（RFC 6455 握手，内置 SHA-1 + base64）
- 解码客户端帧（带掩码）→ 解析 OneBot API 请求（带 echo）
- 编码服务端帧（不掩码）→ 回 API 响应 / 主动推事件
- 处理 PING（适配器 30s 保活）→ 回 PONG

不依赖任何第三方库；线程模型：单 accept + 单读循环（联调规模足够）。
"""
import base64
import hashlib
import json
import os
import queue
import socket
import threading
import time

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 8, 9, 0xA


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已关闭")
        buf += chunk
    return buf


def ws_server_accept(sock):
    """服务端握手：读 HTTP 升级请求，回 101。返回 Sec-WebSocket-Key（调试用）。"""
    sock.settimeout(10)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("握手前连接关闭")
        buf += chunk
    head = buf.decode("latin1")
    key = None
    for line in head.split("\r\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip().lower() == "sec-websocket-key":
                key = v.strip()
    if not key:
        raise ValueError("升级请求缺 Sec-WebSocket-Key")
    accept = base64.b64encode(
        hashlib.sha1((key + WS_MAGIC).encode("ascii")).digest()
    ).decode("ascii")
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
    )
    sock.sendall(resp.encode("ascii"))
    sock.settimeout(None)
    return key


def ws_read_frame(sock):
    """读一个完整消息（自动拼分片、解客户端掩码、处理 PING 由调用方决定）。"""
    payload = b""
    cur_op = None
    while True:
        hdr = recv_exact(sock, 2)
        fin = (hdr[0] & 0x80) != 0
        opcode = hdr[0] & 0x0F
        masked = (hdr[1] & 0x80) != 0
        ln = hdr[1] & 0x7F
        if ln == 126:
            ln = int.from_bytes(recv_exact(sock, 2), "big")
        elif ln == 127:
            ln = int.from_bytes(recv_exact(sock, 8), "big")
        key = recv_exact(sock, 4) if masked else None
        data = recv_exact(sock, ln) if ln else b""
        if key is not None:
            data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
        if opcode in (OP_PING, OP_PONG, OP_CLOSE):
            return fin, opcode, data  # 控制帧不拼接，直接上交
        if opcode != OP_CONT:
            cur_op = opcode
        payload += data
        if fin:
            return True, cur_op, payload


def ws_write_frame(sock, opcode, payload=b"", is_client=False):
    """写帧。is_client=True 时按 RFC 6455 加掩码（客户端→服务端必须掩码）。"""
    n = len(payload)
    if is_client:
        key = os.urandom(4)
        if n < 126:
            hdr = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            hdr = bytes([0x80 | opcode, 0x80 | 126]) + n.to_bytes(2, "big")
        else:
            hdr = bytes([0x80 | opcode, 0x80 | 127]) + n.to_bytes(8, "big")
        masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        sock.sendall(hdr + key + masked)
        return
    hdr = bytes([0x80 | opcode])
    if n < 126:
        hdr += bytes([n])
    elif n < 65536:
        hdr += bytes([126]) + n.to_bytes(2, "big")
    else:
        hdr += bytes([127]) + n.to_bytes(8, "big")
    sock.sendall(hdr + payload)


def ws_client_handshake(sock, host, port):
    """客户端握手：发升级请求，读 101。"""
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        "GET / HTTP/1.1\r\nHost: %s:%d\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
        % (host, port, key)
    )
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("握手前连接关闭: %r" % buf)
        buf += chunk
    if b"101" not in buf.split(b"\r\n")[0]:
        raise ValueError("握手失败: %r" % buf.split(b"\r\n")[0])


class FakeOneBot:
    """假 OneBot 实现：accept + 读循环 + api 队列 + 推事件。"""

    def __init__(self, host="127.0.0.1", port=0):
        """port=0 时自动选端口（self.port 可读）；也可显式指定（联调固定端口用）。"""
        self.host = host
        self.lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.lsock.bind((host, port))
        self.lsock.listen(4)
        self.port = self.lsock.getsockname()[1]
        self.api_q = queue.Queue()   # 收到的 OneBot API 请求（dict）
        self.events_out = []          # 主动推出去的事件（记录）
        self.sock = None
        self.is_client = False        # True=客户端模式（连适配器反向端口）
        self._stop = threading.Event()
        self._thread = None
        self.connected = threading.Event()
        self.conn_count = 0          # 累计 accept 的连接数（重连测试用）

    def start(self):
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="fake-onebot")
        self._thread.start()
        return self

    def connect_to(self, host, port, timeout=10):
        """客户端模式：主动连适配器的反向 WS 监听端口（正向 WS 语义）。"""
        s = socket.create_connection((host, port), timeout=timeout)
        ws_client_handshake(s, host, port)
        self.sock = s
        self.is_client = True
        self.connected.set()
        print("[fake-onebot] 已连入适配器反向 WS %s:%d" % (host, port))
        self._thread = threading.Thread(target=self._read_loop, args=(s,),
                                        daemon=True, name="fake-onebot-cli")
        self._thread.start()
        return self

    def _serve(self):
        """循环 accept：适配器断线重连时会再次连入（重连测试依赖）。"""
        while not self._stop.is_set():
            try:
                self.lsock.settimeout(1.0)
                conn, _ = self.lsock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.lsock.settimeout(None)
            try:
                ws_server_accept(conn)
            except Exception as e:
                print("[fake-onebot] 握手失败: %s" % e)
                conn.close()
                continue
            self.sock = conn
            self.is_client = False
            self.conn_count += 1
            self.connected.set()
            print("[fake-onebot] 适配器已连入 #%d @ %s"
                  % (self.conn_count, conn.getpeername()))
            threading.Thread(target=self._read_loop, args=(conn,),
                             daemon=True, name="fake-ob-%d" % self.conn_count
                             ).start()

    def drop_conn(self):
        """服务端主动断开当前连接（模拟 OneBot 侧掉线）。"""
        old = self.sock
        self.sock = None
        self.connected.clear()
        if old:
            try:
                old.close()
            except Exception:
                pass

    def _read_loop(self, conn):
        is_client = getattr(self, "is_client", False)
        try:
            while not self._stop.is_set():
                fin, opcode, payload = ws_read_frame(conn)
                if opcode == OP_PING:
                    ws_write_frame(conn, OP_PONG, payload, is_client=is_client)
                    continue
                if opcode == OP_CLOSE:
                    break
                if opcode not in (OP_TEXT, OP_BIN):
                    continue
                text = payload.decode("utf-8", "replace")
                for line in text.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        v = json.loads(line)
                    except Exception as e:
                        print("[fake-onebot] 帧解析失败: %s <- %s" % (e, line))
                        continue
                    self._on_api(v, conn)
        except Exception as e:
            if not self._stop.is_set():
                print("[fake-onebot] 读循环结束: %s" % e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _on_api(self, v, conn=None):
        action = v.get("action", "")
        echo = v.get("echo", "")
        # 到达时刻：测试侧算真实延迟必须用"入队时刻"，而不是"消费者取出时刻"
        v["_recv_ts"] = time.time()
        self.api_q.put(v)
        # 与 qpsbench 同口径：**每个 action 都必须应答**（带 echo）。
        # 不回响应会让内核 CallAdapter 干等超时（默认 14s），SDK 的 send
        # flush 线程被卡住，后续全部动作批量延迟——2026-10-14 实测教训。
        if action == "get_login_info":
            data = {"user_id": 12345, "nickname": "fakebot"}
        elif action == "get_status":
            data = {"online": True, "good": True}
        elif action == "get_version_info":
            data = {"app_name": "fake-onebot", "app_version": "1.0",
                    "protocol_version": "v11"}
        else:
            data = {}
        resp = {"status": "ok", "retcode": 0, "data": data}
        if echo:
            resp["echo"] = echo
        try:
            ws_write_frame(conn or self.sock, OP_TEXT,
                           json.dumps(resp, ensure_ascii=False).encode("utf-8"),
                           is_client=self.is_client)
        except Exception as e:
            print("[fake-onebot] 回响应失败: %s" % e)

    # ── 测试侧 API ──

    def wait_api(self, action, timeout=10):
        """等一个指定 action 的 API 请求（顺带丢弃其它）。返回请求 dict。"""
        deadline = time.time() + timeout
        skipped = []
        while time.time() < deadline:
            try:
                v = self.api_q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if v.get("action") == action:
                return v
            skipped.append(v.get("action"))
        raise AssertionError("等待 api.call %s 超时（已见: %s）" % (action, skipped))

    def respond(self, req, result):
        """回 API 响应（同 echo 关联）。"""
        resp = {"status": "ok", "retcode": 0, "data": result,
                "echo": req.get("echo", "")}
        ws_write_frame(self.sock, OP_TEXT, json.dumps(resp).encode("utf-8"),
                           is_client=self.is_client)

    def push_event(self, evt):
        """主动推一条 OneBot 事件给适配器。"""
        ws_write_frame(self.sock, OP_TEXT,
                       json.dumps(evt, ensure_ascii=False).encode("utf-8"),
                       is_client=self.is_client)
        self.events_out.append(evt)
        print("[fake-onebot] => 事件 post_type=%s" % evt.get("post_type"))

    def close(self):
        self._stop.set()
        try:
            if self.sock:
                ws_write_frame(self.sock, OP_CLOSE, is_client=self.is_client)
                self.sock.close()
        except Exception:
            pass
        try:
            self.lsock.close()
        except Exception:
            pass


def group_msg(gid, uid, text):
    return {
        "post_type": "message", "message_type": "group",
        "time": int(time.time()), "self_id": 1, "group_id": gid, "user_id": uid,
        "message": text, "raw_message": text,
        "sender": {"user_id": uid, "nickname": "tester", "role": "member"},
        "message_id": 1000 + int(time.time() * 1000) % 100000,
    }


def private_msg(uid, text):
    return {
        "post_type": "message", "message_type": "private",
        "time": int(time.time()), "self_id": 1, "user_id": uid,
        "message": text, "raw_message": text,
        "sender": {"user_id": uid, "nickname": "tester"},
        "message_id": 2000 + int(time.time() * 1000) % 100000,
    }
