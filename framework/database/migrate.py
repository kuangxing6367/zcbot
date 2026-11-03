# -*- coding: utf-8 -*-
"""数据库迁移命令行（运维 / 变更执行入口）

用法（在项目根目录执行）::

    python -m framework.database.migrate status
    python -m framework.database.migrate plan
    python -m framework.database.migrate up
    python -m framework.database.migrate up --approve CHG-2026-0117 --reason "清理重复命令行"
    python -m framework.database.migrate history
    python -m framework.database.migrate runs
    python -m framework.database.migrate check
    python -m framework.database.migrate verify
    python -m framework.database.migrate baseline --version 9

定位：框架启动时会自动跑 **additive** 迁移；``rewrite`` / ``destructive``
属于需要人负责的变更 —— 前者自动执行但会先备份，后者必须在本命令行里带
变更单号显式批准。这也是审计要的「申请人与执行人分离」：开机进程不是批准人。

退出码：0 正常；1 有未应用迁移 / 漂移 / 失败 / 待授权；2 运行环境错误。
"""
import argparse
import getpass
import os
import sys

from framework.database import migrations as M


def _default_operator() -> str:
    """操作人标识：环境变量优先（自动化场景），否则取系统登录名。"""
    for key in ('ZCBOT_MIGRATION_OPERATOR', 'USER', 'USERNAME', 'LOGNAME'):
        val = (os.environ.get(key) or '').strip()
        if val:
            return val
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ''


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
            flag = '  [需授权]' if p.get('requires_approval') else ''
            print(f"  v{p['version']:<4} {p['name']:<32} "
                  f"{p.get('level', ''):<12}{flag}{desc}")
    if st['blocked']:
        print(f"\n其中 {len(st['blocked'])} 条被授权闸门拦下：")
        for b in st['blocked']:
            print(f"  v{b['version']} {b['name']} — {b['blocked_reason']}")
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
        description='ZCBOT 数据库迁移管理（变更分级 + 授权 + 证据链）')
    parser.add_argument('command',
                        choices=['status', 'plan', 'up', 'history', 'runs',
                                 'check', 'verify', 'baseline'])
    parser.add_argument('--config', default=None, help='config.yaml 路径')
    parser.add_argument('--version', type=int, default=None,
                        help='baseline 的目标版本号')
    parser.add_argument('--approve', default='', metavar='TICKET',
                        help='变更单号：批准执行 destructive 迁移（必填）')
    parser.add_argument('--reason', default='', help='变更原因（写进流水）')
    parser.add_argument('--operator', default='', help='操作人（默认取系统登录名）')
    parser.add_argument('--no-backup', action='store_true',
                        help='跳过执行前备份（不推荐；会失去回退点）')
    parser.add_argument('--limit', type=int, default=20,
                        help='runs 显示的条数（默认 20）')
    args = parser.parse_args(argv)

    try:
        db, registry = _load_database(args.config)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"无法连接数据库: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    from framework.database.schema import migrations_context
    ctx = migrations_context(
        db, source='cli',
        operator=(args.operator or _default_operator()),
        ticket=args.approve,
        reason=args.reason,
        allow_destructive=bool(args.approve),
        backup=(False if args.no_backup else True))

    try:
        if args.command == 'status':
            return _print_status(M.status(db, registry, ctx))

        if args.command == 'plan':
            p = M.plan(db, registry, ctx)
            print(f"当前版本 v{p['current']}（最新 v{p['latest']}），"
                  f"待执行 {p['pending_count']} 版")
            print(f"上下文：source={ctx.source} operator={ctx.operator or '(未记录)'}"
                  f"{' 变更单=' + ctx.ticket if ctx.ticket else ' 无变更单'}")
            if not p['steps']:
                print("没有待执行的迁移。")
                return 0
            print()
            for s in p['steps']:
                mark = '需授权' if s['requires_approval'] else '可直接执行'
                backup = '先备份' if s['will_backup'] else '无需备份'
                print(f"  v{s['version']:<4} {s['name']:<32} {s['level']:<12}"
                      f"{mark} / {backup}")
                if s['description']:
                    print(f"       {s['description']}")
                if s['blocked_reason']:
                    print(f"       阻塞：{s['blocked_reason']}")
            return 0 if p['ok'] else 1

        if args.command == 'check':
            st = M.status(db, registry, ctx)
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

        if args.command == 'verify':
            v = M.verify(db, registry, ctx)
            if v['ok']:
                print(f"结构自检通过：v{v['current']}（最新 v{v['latest']}）")
                return 0
            print("结构自检未通过：")
            for p in v['problems']:
                print(f"  - {p}")
            return 1

        if args.command == 'up':
            from framework.database.schema import _auto_create_tables
            report = _auto_create_tables(db, ctx) or {}
            applied = report.get('applied') or []
            if applied:
                print(f"已应用 {len(applied)} 版："
                      + ", ".join('v%d %s(%s,%dms)' % (
                          a['version'], a['name'], a.get('level', ''),
                          a['duration_ms']) for a in applied))
            else:
                print("没有待应用的迁移。")
            if report.get('backup'):
                print(f"执行前备份：{report['backup']}")
            if report.get('blocked'):
                for b in report['blocked']:
                    print(f"待授权 v{b['version']} {b['name']}（{b['level']}）："
                          f"{b['reason']}", file=sys.stderr)
                return 1
            if report and not report.get('ok', True):
                for f in report.get('failed') or []:
                    print(f"失败 v{f['version']} {f['name']}: {f['error']}",
                          file=sys.stderr)
                return 1
            st = M.status(db, registry, ctx)
            print(f"当前版本 v{st['current']}（最新 v{st['latest']}）")
            return 1 if st['pending'] else 0

        if args.command == 'history':
            st = M.status(db, registry, ctx)
            if not st['history']:
                print("账本为空（尚无迁移记录）。")
                return 0
            print(f"{'版本':<8}{'名称':<34}{'级别':<12}{'结果':<8}{'耗时':<8}"
                  f"{'来源':<10}{'操作人':<16}变更单")
            for r in st['history']:
                print(f"v{r['version']:<7}{r['name']:<34}"
                      f"{(r.get('level') or 'additive'):<12}"
                      f"{'成功' if r['success'] else '失败':<8}"
                      f"{(str(r['duration_ms']) + 'ms'):<8}"
                      f"{r['origin']:<10}{(r.get('operator') or '-'):<16}"
                      f"{r.get('ticket') or '-'}")
            return 0

        if args.command == 'runs':
            rows = M.runs(db, args.limit)
            if not rows:
                print("暂无执行流水（未执行过迁移，或流水表不可用）。")
                return 0
            print(f"{'时间(UTC)':<28}{'版本':<7}{'级别':<12}{'结果':<6}"
                  f"{'来源':<8}{'操作人':<14}{'变更单':<16}备份/错误")
            for r in rows:
                extra = r['backup'] or r['error']
                print(f"{r['started_at']:<28}v{r['version']:<6}{r['level']:<12}"
                      f"{'成功' if r['success'] else '失败':<6}{r['source']:<8}"
                      f"{(r['operator'] or '-'):<14}{(r['ticket'] or '-'):<16}"
                      f"{extra[:40]}")
            return 0

        if args.command == 'baseline':
            if not args.version:
                print("baseline 需指定 --version N", file=sys.stderr)
                return 2
            result = M.stamp(db, registry, args.version, ctx)
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
