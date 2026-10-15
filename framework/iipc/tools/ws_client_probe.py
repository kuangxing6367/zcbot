# -*- coding: utf-8 -*-
"""最小 WS 客户端复现：连 FakeOneBot，掩码发一帧，读 3s 看收到什么。"""
import base64
import hashlib
import json
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from onebot_fake import FakeOneBot  # noqa: E402


def client_handshake(sock):
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        "GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % key
    )
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("handshake closed: %r" % buf)
        buf += chunk
    print("[py-client] 101? ->", buf.split(b"\r\n")[0])


def send_masked(sock, text):
    payload = text.encode()
    key = os.urandom(4)
    hdr = bytes([0x81])
    n = len(payload)
    assert n < 126
    hdr += bytes([0x80 | n])
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    sock.sendall(hdr + key + masked)


def recv_frame(sock, timeout=3.0):
    sock.settimeout(timeout)
    try:
        hdr = sock.recv(2)
        if len(hdr) < 2:
            return ("short", hdr)
        opcode = hdr[0] & 0x0F
        ln = hdr[1] & 0x7F
        if ln == 126:
            ln = int.from_bytes(sock.recv(2), "big")
        data = b""
        while len(data) < ln:
            data += sock.recv(ln - len(data))
        return (opcode, data)
    except socket.timeout:
        return ("timeout", b"")


def main():
    ob = FakeOneBot().start()
    s = socket.create_connection(("127.0.0.1", ob.port), timeout=5)
    client_handshake(s)
    if not ob.connected.wait(5):
        print("[py-client] FakeOneBot 未接受连接!")
        return
    print("[py-client] 连接建立，发一条 send_msg 帧（模拟适配器）")
    send_masked(s, json.dumps({"action": "send_msg", "params": {"message": "hi"},
                               "echo": "iipc-1"}))
    for i in range(6):
        op, data = recv_frame(s, 1.0)
        print("[py-client] 帧 opcode=%s data=%r" % (op, data[:120]))
        if op == "timeout":
            continue
        if op == 8:
            print("[py-client] <<< 收到 CLOSE（问题在 FakeOneBot 或协议错位）")
            break
        if op in (1, 2) and data:
            print("[py-client] <<< 正常数据帧")
    ob.close()
    s.close()


if __name__ == "__main__":
    main()
