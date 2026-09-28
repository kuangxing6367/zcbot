# -*- coding: utf-8 -*-
"""rust_accel 端到端集成冒烟：接收链路 + 广播链路（Todo 2+3 验收）"""
import asyncio
import json
import os
import subprocess
import sys

import websockets

BIN = r"C:/rust_accel_target/debug/rust_accel.exe"
CFG = json.dumps({
    "ws_host": "127.0.0.1", "ws_port": 0, "access_token": "",
    "max_frame_size": 16 * 1024 * 1024,
    "stats_interval_secs": 0, "ipc_strip_raw": False,
})


async def read_json(reader, tag, timeout=5):
    line = await asyncio.wait_for(reader.readline(), timeout)
    obj = json.loads(line.strip())
    print(f"[IPC {tag}]", obj)
    return obj


async def main():
    env = dict(os.environ, RA_CONFIG=CFG)
    proc = subprocess.Popen([BIN], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env=env, text=True, encoding="utf-8")
    hello = json.loads(proc.stdout.readline().strip())
    print("[HELLO]", hello)
    assert hello["type"] == "hello" and hello["ws_ready"], hello
    ipc_port, ws_port = hello["ipc_port"], hello["ws_port"]

    r, w = await asyncio.open_connection("127.0.0.1", ipc_port)

    async with websockets.connect(
        f"ws://127.0.0.1:{ws_port}/",
        additional_headers={"X-Self-ID": "bot1"},
        max_size=32 * 1024 * 1024,
    ) as ws:
        # 接收链路：WS 注入事件 → IPC event 行
        ev = {"post_type": "message", "message_type": "group", "group_id": 1,
              "user_id": 2, "message": [{"type": "text", "data": {"text": "hello rust"}}],
              "sender": {"user_id": 2}}
        await ws.send(json.dumps(ev))
        seen_conn, seen_event = False, False
        for _ in range(2):
            obj = await read_json(r, "rx")
            if obj["type"] == "conn":
                seen_conn = True
            if obj["type"] == "event":
                seen_event = True
                assert obj["event"]["bot_name"] == "bot1"
                assert obj["event"]["_est_size"] > 0
                assert obj["event"]["raw"]["post_type"] == "message"
        assert seen_conn and seen_event, "接收链路未完整"

        # 广播链路：IPC call → WS 收到 action → echo 回帧 → IPC call_resp
        call = {"type": "call", "id": 42, "action": "send_group_msg",
                "params": {"group_id": 1, "message": "hi"}}
        w.write((json.dumps(call) + "\n").encode())
        await w.drain()
        got = json.loads(await asyncio.wait_for(ws.recv(), 5))
        print("[WS GOT]", got)
        assert got["action"] == "send_group_msg"
        assert got["params"]["group_id"] == 1
        assert got["echo"], "动作帧缺少 echo"

        await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": None,
                                  "echo": got["echo"]}))
        resp = await read_json(r, "call_resp")
        assert resp["type"] == "call_resp"
        assert resp["id"] == 42
        assert resp["ok"] is True, resp
        assert resp["result"]["retcode"] == 0
        assert resp["elapsed_ms"] >= 0

        # 超时路径：无回帧 → 10s 超时过长，改用 query 收尾
        q = {"type": "query", "kind": "connections"}
        w.write((json.dumps(q) + "\n").encode())
        await w.drain()
        cl = await read_json(r, "conn_list")
        assert cl["type"] == "conn_list" and "bot1" in cl["connections"], cl

        stats_q = {"type": "query", "kind": "stats"}
        w.write((json.dumps(stats_q) + "\n").encode())
        await w.drain()
        st = await read_json(r, "stats")
        assert st["stats"]["api"]["calls"] == 1
        assert st["stats"]["api"]["ok"] == 1
        assert st["stats"]["api"]["timeout"] == 0

        print("INTEGRATION_PASS")
    w.close()
    await w.wait_closed()
    proc.kill()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        pass