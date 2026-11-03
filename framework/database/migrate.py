# -*- coding: utf-8 -*-
"""数据库迁移命令行（运维入口）

用法（在项目根目录执行）::

    python -m framework.database.migrate status
    python -m framework.database.migrate up
    python -m framework.database.migrate history
    python -m framework.database.migrate check
    python -m framework.database.migrate baseline --version 9
    python -m framework.database.migrate status --config /path/config.yaml

退出码：0 正常；1 有未应用迁移 / 校验和漂移 / 迁移失败；2 运行环境错误。

定位：框架启动时本来就会自动跑迁移（run_pending），本命令用于
「不上线也想确认结构版本」「CI 里校验迁移是否漏做」「运维接管老库做基线」。
"""
import argparse
import sys

from framework.database import migrations as M


def _load_database(config_path=None):
    """按 config.yaml 建立存储后端（不触发降级：CLI 出错要显式失败）"""
    from framework.config import load_config
    from framework.database.db import _parse_sqlite_type
    from framework.database.storage import create_storage

    config = load_config(config_path)
    db_config = _parse_sqlite_type(config.get('database', {}))
    dtype = db_config.get('type', 'sqlite')
    if dtype == 'debug':
        raise SystemExit(
            "当前配置 database.type=debug（本地 SQL 模拟引擎，无真实 schema），"
            "迁移命令不适用。")
    db = create_storage(db_config)
    from framework.database.init_db import auto_init_database
    from framework.database.schema import MIGRATIONS
    auto_init_database(db)
    return db, MIGRATIONS


def _print_status(st) -> int:
    print(f"数据库类型   : {st['db_type']}")
    print(f"账本表       : {'已建立' if st['ledger'] else '缺失'}")
    print(f"当前版本     : {('v%d' % st['current']) if st['current'] else '(无)'}")
    print(f"最新版本     : {('v%d' % st['latest']) if st['latest'] else '(无)'}")
    print(f"已应用/总数  : {st['applied_count']}/{st['total']}")
    if st['pending']:
        print(f"\n待应用 {len(st['pending'])} 版：")
        for p in st['pending']:
            desc = f"  {p['description']}" if p.get('description') else ''
            print(f"  v{p['version']:<4} {p['name']}{desc}")
    if st['drift']:
        print(f"\n校验和漂移 {len(st['drift'])} 处（已应用的迁移被改动，必须改为新增一版）：")
        for d in st['drift']:
            print(f"  v{d['version']:<4} {d['name']}  账本 {d['recorded']} / 当前 {d['actual']}")
    if not st['pending'] and not st['drift']:
        print("\n结构已是最新，无需迁移。")
    return 0 if (not st['pending'] and not st['drift']) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog='python -m framework.database.migrate',
        description='ZCBOT 数据库迁移管理')
    parser.add_argument('command',
                        choices=['status', 'up', 'history', 'check', 'baseline'])
    parser.add_argument('--config', default=None, help='config.yaml 路径')
    parser.add_argument('--version', type=int, default=None,
                        help='baseline 的目标版本号')
    args = parser.parse_args(argv)

    try:
        db, registry = _load_database(args.config)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"无法连接数据库: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    try:
        if args.command == 'status':
            return _print_status(M.status(db, registry))

        if args.command == 'check':
            st = M.status(db, registry)
            if st['drift']:
                print(f"校验和漂移 {len(st['drift'])} 处：")
                for d in st['drift']:
                    print(f"  v{d['version']} {d['name']} 账本 {d['recorded']} / 当前 {d['actual']}")
                return 1
            if st['pending']:
                print(f"有 {len(st['pending'])} 版迁移未应用："
                      + ", ".join('v%d' % p['version'] for p in st['pending']))
                return 1
            print(f"迁移检查通过：v{st['current']}，无漂移。")
            return 0

        if args.command == 'up':
            from framework.database.schema import _auto_create_tables
            report = _auto_create_tables(db) or {}
            applied = report.get('applied') or []
            if applied:
                print(f"已应用 {len(applied)} 版："
                      + ", ".join('v%d %s(%dms)' % (a['version'], a['name'],
                                                    a['duration_ms'])
                                  for a in applied))
            else:
                print("没有待应用的迁移。")
            if report and not report.get('ok', True):
                for f in report.get('failed') or []:
                    print(f"失败 v{f['version']} {f['name']}: {f['error']}",
                          file=sys.stderr)
                return 1
            st = M.status(db, registry)
            print(f"当前版本 v{st['current']}（最新 v{st['latest']}）")
            return 1 if st['pending'] else 0

        if args.command == 'history':
            st = M.status(db, registry)
            if not st['history']:
                print("账本为空（尚无迁移记录）。")
                return 0
            print(f"{'版本':<8}{'名称':<34}{'结果':<8}{'耗时':<8}{'来源':<10}应用时间")
            for r in st['history']:
                ts = r['applied_at'] or '-'
                print(f"v{r['version']:<7}{r['name']:<34}"
                      f"{'成功' if r['success'] else '失败':<8}"
                      f"{(str(r['duration_ms']) + 'ms'):<8}{r['origin']:<10}{ts}")
            return 0

        if args.command == 'baseline':
            if not args.version:
                print("baseline 需指定 --version N", file=sys.stderr)
                return 2
            result = M.stamp(db, registry, args.version)
            print(f"已基线认定到 v{args.version}：记录 {len(result['stamped'])} 版（未执行迁移体）")
            return 0
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
