# -*- coding: utf-8 -*-
"""
调试模式 SQL 模拟引擎基准（读/写吞吐 + 磁盘写次数）

对比内存缓冲（debug_flush_ms>0）与同步落盘（debug_flush_ms=0）的实测吞吐。
运行：python tools/bench_sql_sim.py
"""
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.database.sql_sim import SqlSimEngine

# 统计真实落盘次数（原子写 = os.replace）
_REPLACE_N = 0
_ORIG_REPLACE = os.replace


def _counting_replace(src, dst):
    global _REPLACE_N
    _REPLACE_N += 1
    return _ORIG_REPLACE(src, dst)


os.replace = _counting_replace


def _count_files(dirpath):
    return sum(len(files) for _, _, files in os.walk(dirpath))


def bench(label, tmp, n_insert=300, n_select=2000, **cfg):
    global _REPLACE_N
    eng = SqlSimEngine({'type': 'debug', 'fallback_dir': tmp, **cfg})
    eng.execute("CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY, "
                "k INT, v VARCHAR(64), ts DATETIME)")
    _REPLACE_N = 0

    t0 = time.perf_counter()
    for i in range(n_insert):
        eng.execute("INSERT INTO t (k, v, ts) VALUES (%s, %s, NOW())", (i % 50, f'v{i}'))
    t_write = time.perf_counter() - t0

    t0 = time.perf_counter()
    for i in range(n_select):
        eng.query("SELECT * FROM t WHERE k = %s ORDER BY id DESC LIMIT 10", (i % 50,))
    t_read = time.perf_counter() - t0

    eng.close()
    writes = _REPLACE_N
    print(f"[{label}] insert {n_insert} 行: {t_write:.3f}s "
          f"({n_insert / t_write:.0f} 行/s) | select {n_select} 次: {t_read:.3f}s "
          f"({n_select / t_read:.0f} 次/s) | 落盘写次数={writes}")
    shutil.rmtree(tmp, ignore_errors=True)


def bench_user_recording(tmp, n_msg=3000, conns=30, **cfg):
    """模拟多连接消息负载：每消息 1 次用户查询 + stats_writer 式用户数据批量落库"""
    eng = SqlSimEngine({'type': 'debug', 'fallback_dir': tmp, **cfg})
    eng.execute("CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY, "
                "user_id BIGINT, nickname VARCHAR(64))")
    eng.execute("CREATE TABLE groups_info (id INT AUTO_INCREMENT PRIMARY KEY, "
                "group_id BIGINT, group_name VARCHAR(64))")
    eng.execute("CREATE TABLE group_members (id INT AUTO_INCREMENT PRIMARY KEY, "
                "group_id BIGINT, user_id BIGINT, message_count INT)")
    for i in range(500):  # 预热 500 个已有用户
        eng.execute("INSERT INTO users (user_id, nickname) VALUES (%s, %s)",
                    (10000 + i, f'u{i}'))
    t0 = time.perf_counter()
    pending = []
    flushes = 0
    for m in range(n_msg):
        uid = 10000 + (m % 500)
        eng.query_one("SELECT * FROM users WHERE user_id = %s", (uid,))
        pending.append((uid, 20000 + (m % conns)))
        if len(pending) >= 500:  # 模拟 stats_writer 5s 周期批量落库
            users, groups, members = {}, {}, {}
            for uid, gid in pending:
                users[uid] = f'u{uid % 500}'
                groups[gid] = f'g{gid}'
                members[(gid, uid)] = members.get((gid, uid), 0) + 1
            eng.execute_many("INSERT INTO users (user_id, nickname) VALUES (%s, %s)",
                             list(users.items()))
            eng.execute_many("INSERT INTO groups_info (group_id, group_name) VALUES (%s, %s)",
                             list(groups.items()))
            eng.execute_many("INSERT INTO group_members (group_id, user_id, message_count) "
                             "VALUES (%s, %s, %s)",
                             [(g, u, c) for (g, u), c in members.items()])
            pending = []
            flushes += 1
    t = time.perf_counter() - t0
    eng.close()
    print(f"[用户数据记录 conns={conns}] {n_msg} 消息: {t:.3f}s "
          f"({n_msg / t:.0f} 消息/s, {flushes} 次批量落库)")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    print(f"SqlSimEngine 基准（pid={os.getpid()}）")
    bench('同步落盘 flush_ms=0 ', tempfile.mkdtemp(prefix='sqlsim_sync_'), debug_flush_ms=0)
    bench('内存缓冲 flush_ms=1s', tempfile.mkdtemp(prefix='sqlsim_buf_'), debug_flush_ms=1000)
    bench_user_recording(tempfile.mkdtemp(prefix='sqlsim_rec0_'), debug_flush_ms=0)
    bench_user_recording(tempfile.mkdtemp(prefix='sqlsim_rec1_'), debug_flush_ms=1000)
