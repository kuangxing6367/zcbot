"""状态目录迁移命令行（内核内置入口）。

    python -m framework.migration --inspect              只读体检
    python -m framework.migration --inspect --json       机器可读报告
    python -m framework.migration --dry-run              预演，不写任何文件
    python -m framework.migration                        执行一次自动迁移
    python -m framework.migration --home DIR --force     指定 home / 忽略 mixed 告警

``--home`` 只影响本次探测与迁移目标，不会修改真实部署的环境变量。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import migrator

logger = logging.getLogger('zcbot')

_STATUS_RC = {'partial': 1, 'error': 1}


def _print_human(data: dict, is_inspect: bool) -> None:
    if is_inspect:
        v = data['kernel_version']
        lay = data['layout']
        pol = data['policy']
        print('内核版本   :', v['version'],
              '(VERSION/pyproject 一致)' if data['version_sources']['consistent'] else '(版本来源有漂移)')
        print('home       :', lay['home'])
        print('布局代际   :', lay['layout'], '| 置信度', lay['confidence'])
        for c in lay['detected_candidates']:
            print('  可能版本 : %-28s %s [%s]' % (c['range'], c['label'], c['confidence']))
        for w in lay['warnings']:
            print('  警告     :', w)
        print('自动迁移   :', '开启' if pol['enabled'] else '关闭',
              '| 判定来源', pol['source'], '|', pol['reason'])
        print('账本       :', data['ledger_note'])
        print('待办动作   :', data['pending_count'])
        for a in data['actions']:
            print('  %-13s %s -> %s' % (a['kind'], a['src'], a['dst']))
        return
    print('状态       :', data['status'])
    print('内核版本   :', data['kernel_version'])
    print('布局代际   :', data['layout'])
    print('说明       :', data.get('detail', ''))
    for c in data.get('detected_candidates', []):
        print('  可能版本 :', c['range'], c['label'])
    for a in data.get('actions', []):
        print('  %-13s %s -> %s%s' % (
            a['kind'], a['src'], a['dst'],
            '' if a.get('ok', True) else '  ERROR: %s' % a.get('error', '')))
    if data.get('ledger_path'):
        print('账本       :', data['ledger_path'])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='python -m framework.migration',
                                 description='状态目录迁移（内核内置，默认自动开启）')
    ap.add_argument('--home', default=None, help='指定 ZCBOT_HOME（不改动真实部署）')
    ap.add_argument('--config', default=None, help='指定 config.yaml 路径（用于读开关）')
    ap.add_argument('--inspect', action='store_true', help='只读体检，不写任何文件')
    ap.add_argument('--dry-run', action='store_true', help='预演迁移动作，不写任何文件')
    ap.add_argument('--force', action='store_true',
                    help='忽略新旧布局并存告警（仍然绝不覆盖已有文件）')
    ap.add_argument('--json', action='store_true', help='输出 JSON 报告')
    args = ap.parse_args(argv)

    if args.home:
        os.environ['ZCBOT_HOME'] = os.path.abspath(args.home)
    try:
        if args.inspect:
            data = migrator.inspect(config_path=args.config)
            is_inspect = True
        else:
            data = migrator.migrate(dry_run=args.dry_run, force=args.force,
                                    config_path=args.config)
            is_inspect = False
    except Exception as e:
        logger.debug('[main] 捕获异常后按降级路径继续: %s', e)
        data = {'status': 'error', 'detail': '%s: %s' % (type(e).__name__, e),
                'actions': [], 'kernel_version': '', 'layout': '',
                'detected_candidates': []}
        is_inspect = False

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(data, is_inspect)
    return _STATUS_RC.get(data.get('status'), 0)


if __name__ == '__main__':
    sys.exit(main())
