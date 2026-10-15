# -*- coding: utf-8 -*-
"""测试端口工具（tools/portutil.py）

铁律：基准/验收脚本**一律不写死端口**。开发机上同时跑着 zcbot 主进程
（WebUI :8080 / OneBot 反向 WS :6830 / IIPC 适配器默认 :6701 等），写死端口
轻则「端口被占」跑不起来，重则抢占别人正在用的端口。

本模块向内核要「当前空闲端口」：bind(0) 让 OS 分配，取到后立即关闭并交回使用。
存在极小的 TOCTOU 窗口（拿到端口到真正监听之间），故所有脚本都配套 wait_tcp
做就绪等待；若运气极差被抢，脚本会因「内核未就绪」显式失败而不是静默连错进程。
"""
import socket
import time


def free_port(host="127.0.0.1"):
    """向 OS 要一个当前空闲的端口（bind :0 → 读端口 → 关闭）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def wait_tcp(port, timeout=15, host="127.0.0.1"):
    """等某个端口可以被 connect（服务已 listen）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


def pick_addr(env_name, default_port=None):
    """取脚本用的 ipc 地址：优先环境变量（覆盖用），否则动态分配空闲端口。"""
    v = __import__("os").environ.get(env_name)
    if v:
        return v
    return "127.0.0.1:%d" % free_port()
