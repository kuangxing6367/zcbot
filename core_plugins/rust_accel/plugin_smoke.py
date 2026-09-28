# -*- coding: utf-8 -*-
"""rust_accel 官方插件封装冒烟（T4 验收）

fake framework + 真实 rust_accel(release) 二进制 + 真实 WS 客户端：
验证事件注入框架 / 广播回执 / stats 监控 / 连接状态 / 优雅停机。
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, r"E:\工程\机器人\zgric_onebot11_35xLe")

import websockets

from core_plugins.rust_accel.main import RustAccelService  # noqa: E402

BIN = r"C:/rust_accel_target/release/rust_accel.exe"


class FakeServices:
    def __init__(self):
        self.registered = {}

    def register(self, name, service):
        self.registered[name] = service

    def get(self, name, default=None):
        return self.registered.get(name, default)


class FakeFW:
    def __init__(self):
        self.config = {'rust_accel': {}, 'core_plugins': {}, 'onebot': {}}
        self.loop = None
        self._pending_tasks = set()
        self.events = []
        self.services = FakeServices()

    async def dispatch_event(self, event, wait=False):
        self.events.append(event)


async def main():
    cfg = {'enabled': True, 'ws_host': '127.0.0.1', 'ws_port': 0,
           'stats_interval_secs': 1, 'binary_path': BIN, 'access_token': ''}
    fw = FakeFW()
    fw.loop = asyncio.get_running_loop()
    svc = RustAccelService(fw, cfg)
    assert svc.start() is True, "start() 应返回 True"

    for _ in range(50):
        if svc._writer is not None:
            break
        await asyncio.sleep(0.1)
    assert svc._writer is not None, "IPC 应已连接"
    assert svc.ws_port, "应拿到 WS 实际端口"
    print(f"[OK] 子进程就绪 ws_port={svc.ws_port} ipc_port={svc.ipc_port}")

    async with websockets.connect(
        f"ws://127.0.0.1:{svc.ws_port}/",
        additional_headers={"X-Self-ID": "bot1"},
        max_size=32 * 1024 * 1024,
    ) as ws:
        for _ in range(50):
            if 'bot1' in svc.get_connected_bots():
                break
            await asyncio.sleep(0.1)
        assert svc.get_connected_bots() == ['bot1'], svc.get_connected_bots()
        print("[OK] 连接状态上报 bot1")

        # 接收链路：WS 事件 -> 框架事件注入
        await ws.send(json.dumps({"post_type": "message", "message_type": "group",
                                  "group_id": 1, "user_id": 2,
                                  "message": [{"type": "text", "data": {"text": "hi"}}],
                                  "sender": {"user_id": 2}}))
        for _ in range(50):
            if fw.events:
                break
            await asyncio.sleep(0.1)
        assert fw.events, "事件应注入框架"
        ev = fw.events[0]
        assert ev.get('bot_name') == 'bot1' and ev.get('adapter') == 'onebot', ev
        assert ev.get('user_id') == 2 and ev.get('_est_size', 0) > 0
        print("[OK] 事件注入框架", {k: ev.get(k) for k in ('type', 'message_type', 'user_id', '_est_size')})

        # 广播链路：acall -> WS action 帧 -> echo 回帧 -> call_resp
        resp_fut = asyncio.create_task(svc.acall('send_group_msg', group_id=1, message='hello'))
        got = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert got['action'] == 'send_group_msg' and got['echo'], got
        await ws.send(json.dumps({"status": "ok", "retcode": 0, "echo": got['echo']}))
        resp = await asyncio.wait_for(resp_fut, 5)
        assert resp['ok'] is True and resp['result']['retcode'] == 0, resp
        print("[OK] 广播回执 elapsed_ms=", resp.get('elapsed_ms'))

        # 监控：stats 周期推送并可查询（等待含本次活动数据的快照，
        # 启动瞬间的全 0 快照不算）
        st = None
        for _ in range(40):
            cur = svc.get_stats()
            if (cur and cur.get('api', {}).get('ok') == 1
                    and cur.get('fwd', {}).get('events') == 1):
                st = cur
                break
            await asyncio.sleep(0.1)
        assert st, "含已发生活动的 stats 应已上报"
        assert st['api']['ok'] == 1 and st['fwd']['events'] == 1, st
        stt = svc.status
        assert stt['running_proc'] is True and stt['connected_bots'] == ['bot1']
        print("[OK] 监控上报 api.ok=%s events=%s conn=%s" % (
            st['api']['ok'], st['fwd']['events'], st['conn']['now']))

    # 优雅停机
    await svc.stop()
    assert svc.proc is None, "进程应已清理"
    print("[OK] 优雅停机")

    # 插件入口路径：register / unregister（随框架启停）
    from core_plugins.rust_accel import main as plug_mod
    fw.config['rust_accel'] = cfg
    plug_mod._service = None
    ctx = type('Ctx', (), {
        '_framework': fw,
        'log': lambda *a: print("[ctx.log]", a[0])})()

    plug_mod.register(ctx)
    assert fw.services.get('rust_accel') is not None, "应注册 rust_accel 服务"
    assert plug_mod._service is not None and plug_mod._service.status['running_proc']
    print("[OK] register 启动并注册服务")

    plug_mod.unregister()
    assert plug_mod._service is None, "unregister 应清空服务引用"
    # loop 线程内卸载走后台线程收尾，轮询等待进程清理完成
    deadline = time.time() + 10
    while time.time() < deadline:
        if fw.services.get('rust_accel').status['running_proc'] is False:
            break
        await asyncio.sleep(0.2)
    assert fw.services.get('rust_accel').status['running_proc'] is False, \
        "unregister 后子进程应已被清理"
    print("[OK] unregister 优雅关闭")

    print("PLUGIN_SMOKE_PASS")


if __name__ == "__main__":
    asyncio.run(main())