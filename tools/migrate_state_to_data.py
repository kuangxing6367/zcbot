"""兼容入口：状态目录迁移逻辑已迁入内核 ``framework/migration/``。

本文件只保留旧命令行的参数与输出形态（``--home`` / ``--dry-run``），
真正的版本检测、代际探测、开关判定、账本与执行全部由内核子系统负责：

    python -m framework.migration --inspect        # 体检
    python -m framework.migration --dry-run        # 预演
    python -m framework.migration                  # 执行一次

启动期默认自动迁移，无需手工调用本脚本；``config.yaml`` 的
``framework.auto_migrate: false`` 可关闭。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework import paths
from framework.migration import migrator


def main():
    ap = argparse.ArgumentParser(description='（兼容入口）状态目录迁移')
    ap.add_argument('--home', default=None, help='ZCBOT_HOME（默认按框架规则解析）')
    ap.add_argument('--config', default=None, help='指定 config.yaml（用于读开关）')
    ap.add_argument('--dry-run', action='store_true', help='只打印动作，不落盘')
    ap.add_argument('--force', action='store_true', help='忽略新旧并存告警（仍不覆盖）')
    ap.add_argument('--inspect', action='store_true', help='只读体检')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    args = ap.parse_args()

    if args.home:
        os.environ['ZCBOT_HOME'] = os.path.abspath(args.home)
    if args.inspect:
        from framework.migration.__main__ import main as _cli_main
        return _cli_main(['--inspect'] + (['--json'] if args.json else []))

    result = migrator.migrate(dry_run=args.dry_run, force=args.force,
                              config_path=args.config)
    if args.json:
        import json
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 1 if result.get('status') in ('partial', 'error') else 0

    print('home  =', result['home'])
    print('state =', os.path.join(result['home'], 'data'))
    print('内核版本 =', result['kernel_version'],
          '| 布局代际 =', result['layout'], '| 状态 =', result['status'])
    for a in result.get('actions', []):
        print('  %-13s %s -> %s' % (a['kind'], a['src'], a['dst']))
    if args.dry_run:
        print('DRY-RUN：未写入任何文件')
        return 0
    print('resolve_config_path      =', paths.resolve_config_path())
    print('core_plugins_yaml_path   =', paths.core_plugins_yaml_path())
    print('user_plugins_dir         =', paths.user_plugins_dir())
    print(result.get('detail', ''))
    return 1 if result.get('status') in ('partial', 'error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
