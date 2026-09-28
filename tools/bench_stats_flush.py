# -*- coding: utf-8 -*-
"""验证 stats_writer 批量写库优化：注入 N 条注册请求 -> stop() 触发 _flush 计时。
用法：python tools/bench_stats_flush.py [N]"""
import asyncio
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _write_cfg(tmp):
    cfg = os.path.join(tmp, 'bench.yaml')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(
            "platform: zcbot\n"
            "database:\n"
            "  type: sqlite\n"
            f"  path: {os.path.join(tmp, 'bench.db')}\n"
            "log:\n"
            "  level: ERROR\n"
            "web:\n"
            "  host: 127.0.0.1\n"
            "  port: 0\n"
            "core_plugins:\n"
            "  onebot_adapter: false\n  webui: false\n  http_api: false\n"
            "  http_inject: false\n  ws_client: false\n  qq_official: false\n"
            "  telegram: false\n  discord: false\n  session: false\n"
            "  scheduler: false\n  image_renderer: false\n"
            "event_queue:\n"
            "  workers: 1\n"
        )
    return cfg


def step(name, dt):
    print(f"    [{name}] {dt*1000:8.1f} ms")


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    from framework.core import Framework

    tmp = tempfile.mkdtemp(prefix='zcflush_')
    fw = Framework(config_path=_write_cfg(tmp), role='standard')
    await fw.start(wait_ready=True)

    # 注入 n 条注册请求（含重复用户/群，验证聚合语义）
    sw = fw.stats_writer
    t0 = time.perf_counter()
    for i in range(n):
        uid = i % 1000 + 1
        gid = i % 10 + 1
        sw.register_user(
            uid,
            {"nickname": f"user{uid}", "card": f"card{uid}",
             "group_name": f"group{gid}", "role": "member", "title": ""},
            "group", gid,
        )
    t1 = time.perf_counter()
    step(f'register_user x{n} 入队', t1 - t0)

    # 直接触发 _flush（线程中执行），统计耗时
    t0 = time.perf_counter()
    await asyncio.to_thread(sw._flush)
    t1 = time.perf_counter()
    step(f'_flush 一次 ({n} 条)', t1 - t0)
    print(f"  _flush 单次耗时 {t1-t0:.4f} s，均摊 {(t1-t0)/n*1e6:.1f} us/事件")

    # 校验落库结果
    db = fw.db
    users = db.query("SELECT COUNT(*) c FROM users")[0]['c']
    groups = db.query("SELECT COUNT(*) c FROM groups_info")[0]['c']
    members = db.query("SELECT COUNT(*) c FROM group_members")[0]['c']
    total_msg = db.query("SELECT SUM(message_count) s FROM group_members")[0]['s']
    print(f"  落库行数: users={users} groups={groups} members={members} "
          f"message_count合计={total_msg} (期望 users={min(n,1000)} groups=10 members=1000 合计={n})")

    # 再验证幂等：第二次 flush 不产生新行
    t0 = time.perf_counter()
    await asyncio.to_thread(sw._flush)
    t1 = time.perf_counter()
    step('空 _flush', t1 - t0)
    users2 = db.query("SELECT COUNT(*) c FROM users")[0]['c']
    assert users2 == users, f"幂等性破坏: {users} -> {users2}"

    # stop 路径总耗（含 task cancel）
    t0 = time.perf_counter()
    await fw.stop()
    t1 = time.perf_counter()
    step('fw.stop 总计', t1 - t0)
    print(f"  fw.stop 总耗时 {t1-t0:.3f} s")


if __name__ == '__main__':
    asyncio.run(main())