# -*- coding: utf-8 -*-
"""数据库基准测试（性能）。

方法对齐 sysbench 的通行做法，只是 workload 换成框架真实访问模式：
  - 固定数据集（--rows，默认 2 万行）
  - 线程数递增（--threads 1,2,4,8,16），观察吞吐随并发的变化与饱和点
  - 报告 ops/s（吞吐）+ p50/p95/p99 延迟
  - 每个场景独立计时，互不串味

与一致性套件分开：这里不判对错，只出数字。所以不做成 pytest 用例，
避免把机器负载算进 CI。

用法：
    python tests/db_conformance/bench.py --db sqlite --rows 20000
    ZCBOT_MYSQL_HOST=... python tests/db_conformance/bench.py --db mysql \\
        --threads 1,2,4,8 --duration 5
"""
import argparse
import os
import statistics
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 场景定义：(名称, 说明, 权重)  —— 权重复刻机器人真实读写比
SCENARIOS = {
    'point': ('按唯一键点查', 0.50),
    'range': ('按时间列范围统计', 0.20),
    'insert': ('插入一条消息', 0.20),
    'update': ('更新最后活跃时间', 0.10),
}


def _pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def _prepare(db, rows):
    with db.transaction():
        db.execute("DROP TABLE IF EXISTS bench_users")
        db.execute(
            "CREATE TABLE bench_users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id VARCHAR(64) NOT NULL, nickname VARCHAR(64), "
            "is_blacklist INTEGER DEFAULT 0, last_active_at VARCHAR(32), "
            "UNIQUE KEY uk_user_id (user_id))")
    # 补建普通索引（SQLite 侧 DDL 里内联 INDEX 会被方言层剥离，这里显式建）
    try:
        db.execute("CREATE INDEX bench_users_idx_blacklist "
                   "ON bench_users (is_blacklist)")
    except Exception:
        pass

    batch = 2000
    for start in range(0, rows, batch):
        chunk = [('u%07d' % i, 'nick%d' % i, 1 if i % 100 == 0 else 0,
                  '2026-11-%02d 12:00:00' % (i % 28 + 1))
                 for i in range(start, min(start + batch, rows))]
        db.execute_many(
            "INSERT INTO bench_users (user_id, nickname, is_blacklist, last_active_at)"
            " VALUES (%s,%s,%s,%s)", chunk)


def _runner(db, scenario, thread_id, deadline, rows, lat, lock, counters, run_tag):
    import random
    rnd = random.Random(thread_id * 7919)
    local_lat = []
    ops = 0
    while time.perf_counter() < deadline:
        uid = 'u%07d' % rnd.randrange(rows)
        t0 = time.perf_counter()
        try:
            if scenario == 'point':
                db.query_one("SELECT * FROM bench_users WHERE user_id = %s", (uid,))
            elif scenario == 'range':
                day = '%02d' % rnd.randint(1, 28)
                db.query("SELECT COUNT(*) AS c FROM bench_users "
                         "WHERE last_active_at < %s", ('2026-11-%s 00:00:00' % day,))
            elif scenario == 'insert':
                db.execute(
                    "INSERT INTO bench_users (user_id, nickname, last_active_at)"
                    " VALUES (%s,%s,%s)",
                    ('%s_n%d_%d' % (run_tag, thread_id, ops), 'x',
                     '2026-11-06 12:00:00'))
            else:
                db.execute(
                    "UPDATE bench_users SET last_active_at = %s WHERE user_id = %s",
                    ('2026-11-06 13:00:00', uid))
        except Exception as e:
            counters['err'] += 1
            if counters['err'] <= 3:
                counters.setdefault('errmsg', []).append(
                    '{}: {}'.format(type(e).__name__, str(e)[:80]))
            continue
        local_lat.append((time.perf_counter() - t0) * 1000)
        ops += 1
    with lock:
        lat.extend(local_lat)
        counters['ops'] += ops


def run_case(db, scenario, n_threads, duration, rows):
    lat = []
    lock = threading.Lock()
    counters = {'ops': 0, 'err': 0}
    deadline = time.perf_counter() + duration
    run_tag = '%d' % int(time.time() * 1000 % 10_000_000)
    ths = [threading.Thread(target=_runner,
                            args=(db, scenario, i, deadline, rows, lat, lock,
                                  counters, run_tag))
           for i in range(n_threads)]
    t0 = time.perf_counter()
    for t in ths:
        t.start()
    for t in ths:
        t.join(duration + 60)
    elapsed = time.perf_counter() - t0
    return {
        'ops': counters['ops'],
        'err': counters['err'],
        'errmsg': counters.get('errmsg', []),
        'elapsed': elapsed,
        'tps': counters['ops'] / elapsed if elapsed else 0,
        'p50': _pct(lat, 50),
        'p95': _pct(lat, 95),
        'p99': _pct(lat, 99),
        'max': max(lat) if lat else 0,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description='数据库基准测试')
    ap.add_argument('--db', action='append', default=None,
                    help='sqlite / mysql，可重复')
    ap.add_argument('--rows', type=int, default=20000)
    ap.add_argument('--threads', default='1,2,4,8')
    ap.add_argument('--duration', type=float, default=3.0)
    ap.add_argument('--scenarios', default='point,range,insert,update')
    args = ap.parse_args(argv)

    from tests.db_conformance.backends import make_backend

    names = args.db or ['sqlite']
    thread_list = [int(x) for x in args.threads.split(',') if x.strip()]
    scen_list = [s.strip() for s in args.scenarios.split(',') if s.strip()]

    print('=' * 88)
    print('数据库基准  数据量={} 行  每档 {} s'.format(args.rows, args.duration))
    print('=' * 88)

    for name in names:
        try:
            backend = make_backend(name)
        except Exception as e:
            print('跳过 {}: {}'.format(name, e))
            continue
        backend.provision()
        db = backend.make_database()
        print('\n### 后端: {}  ({})'.format(name, _describe(db)))
        t0 = time.perf_counter()
        _prepare(db, args.rows)
        print('    建表+灌数据 {:.2f}s'.format(time.perf_counter() - t0))

        for scen in scen_list:
            label = SCENARIOS.get(scen, (scen,))[0]
            print('\n  [{}] {}'.format(scen, label))
            print('    {:>7} {:>10} {:>9} {:>9} {:>9} {:>9} {:>7}'.format(
                'threads', 'ops/s', 'p50ms', 'p95ms', 'p99ms', 'maxms', 'err'))
            for nt in thread_list:
                r = run_case(db, scen, nt, args.duration, args.rows)
                print('    {:>7} {:>10.1f} {:>9.2f} {:>9.2f} {:>9.2f} '
                      '{:>9.2f} {:>7}'.format(
                          nt, r['tps'], r['p50'], r['p95'], r['p99'],
                          r['max'], r['err']))
                if r['err']:
                    print('           错误样例: {}'.format(r['errmsg'][:2]))
        try:
            db.execute("DROP TABLE IF EXISTS bench_users")
        except Exception:
            pass
        db.close()
        backend.teardown()
    return 0


def _describe(db):
    try:
        if db.db_type == 'sqlite':
            import sqlite3
            return 'SQLite {}'.format(sqlite3.sqlite_version)
        v = db.scalar("SELECT VERSION() AS v")
        cust = db.scalar("SELECT CONNECTION_ID() AS c")
        return 'MySQL {} (conn {})'.format(v, cust)
    except Exception:
        return db.db_type


if __name__ == '__main__':
    sys.exit(main())
