"""发版前本地门禁：在 push / tag 之前把 CI 会拦的问题一次查完。

背景：只改 VERSION、漏改 pyproject.toml，或在测试里写死版本号，这类问题
本地 pytest 全绿、CI 才红。发版前跑一遍本脚本，把三处版本、CHANGELOG、
ruff 与硬编码断言查清，不需要等远端 workflow 给结论。

用法::

    python tools/release_check.py                  # 以 VERSION 为期望版本
    python tools/release_check.py --version 1.28.9  # 显式指定要发的版本
    python tools/release_check.py --skip-ruff      # 只查静态项（不推荐）

退出码 0 = 全部通过，1 = 至少一项失败。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RUFF_CHECKS = (
    ('F821 only', ['ruff', 'check', '--isolated', '--select', 'F821', '.']),
    ('CI gate', [
        'ruff', 'check', '--select',
        'UP009,I001,UP032,F541,PIE790,RET501,PLR1711,RUF022,FURB167,'
        'UP006,UP045,RUF100', '.',
    ]),
)

# 断言里写死的具体版本号：版本一升就红，必须改成从 VERSION 读取。
_HARDCODED_VERSION = re.compile(
    r'assert[^\n]{0,160}?(?:==|!=)\s*[\'"]((?:\d+\.){2}\d+)[\'"]')


class Report:
    def __init__(self):
        self.failed = 0

    def ok(self, name, detail=''):
        print(f'[OK]   {name}' + (f': {detail}' if detail else ''))

    def fail(self, name, detail=''):
        self.failed += 1
        print(f'[FAIL] {name}' + (f': {detail}' if detail else ''), file=sys.stderr)


def read_version():
    return (ROOT / 'VERSION').read_text(encoding='utf-8').strip()


def pyproject_version():
    try:
        import tomllib
    except ImportError:  # Python 3.10 环境
        try:
            import tomli as tomllib
        except ImportError:
            return None, '缺少 tomllib / tomli，无法读取 pyproject.toml'
    try:
        with open(ROOT / 'pyproject.toml', 'rb') as f:
            return tomllib.load(f)['project']['version'], ''
    except Exception as e:
        return None, repr(e)


def check_versions(rep, version):
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?', version):
        rep.fail('VERSION 格式', repr(version))
        return
    rep.ok('VERSION 格式', version)

    pv, err = pyproject_version()
    if pv is None:
        rep.fail('读取 pyproject.toml', err)
    elif pv != version:
        rep.fail('VERSION / pyproject.toml 一致', f'VERSION={version} pyproject={pv}')
    else:
        rep.ok('VERSION / pyproject.toml 一致', version)


def check_changelog(rep, version):
    text = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8', errors='replace')
    if re.search(rf'^##\s*\[?v?{re.escape(version)}\]?\b', text, re.MULTILINE):
        rep.ok('CHANGELOG 含当前版本', version)
    else:
        rep.fail('CHANGELOG 含当前版本', f'未找到 {version} 条目')


def check_tag(rep, version):
    """tag 还没打时跳过；已打则必须等于 v<version>。"""
    try:
        out = subprocess.run(
            ['git', '-C', str(ROOT), 'tag', '--list', f'v{version}'],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        rep.fail('git tag 检查', repr(e))
        return
    if out.returncode != 0:
        rep.fail('git tag 检查', out.stderr.strip())
    elif out.stdout.strip():
        rep.ok('tag 与 VERSION 一致', out.stdout.strip())
    else:
        rep.ok('tag 尚未创建', f'打 tag 时用 v{version}')


def check_hardcoded_versions(rep, version):
    hits = []
    tests_dir = ROOT / 'tests'
    for path in sorted(tests_dir.rglob('*.py')):
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeError):
            continue
        for line_no, line in enumerate(lines, 1):
            match = _HARDCODED_VERSION.search(line)
            # 只拦当前版本；9.8.7 / 1.0.0 这类 fixtures 里的占位版本不动。
            if match and match.group(1) == version:
                hits.append(f'{path.relative_to(ROOT).as_posix()}:{line_no}')
    if hits:
        rep.fail('测试里的版本硬编码', '; '.join(hits[:10]))
    else:
        rep.ok('测试里的版本硬编码', '0 处')


def check_ruff(rep):
    if not shutil.which('ruff'):
        rep.fail('ruff 可用', 'PATH 中找不到 ruff')
        return
    for name, cmd in RUFF_CHECKS:
        try:
            out = subprocess.run(
                cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=180,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            rep.fail(f'ruff {name}', repr(e))
            continue
        if out.returncode == 0:
            rep.ok(f'ruff {name}')
        else:
            tail = (out.stdout + out.stderr).strip().splitlines()[-5:]
            rep.fail(f'ruff {name}', ' | '.join(tail))


def main():
    parser = argparse.ArgumentParser(description='zcbot 发版前本地门禁')
    parser.add_argument('--version', default='', help='期望版本，缺省读 VERSION')
    parser.add_argument('--skip-ruff', action='store_true', help='跳过 ruff 检查')
    args = parser.parse_args()

    version = args.version.strip() or read_version()
    print(f'发版前门禁：{version}（{ROOT}）')
    print('-' * 60)
    rep = Report()
    check_versions(rep, version)
    check_changelog(rep, version)
    check_tag(rep, version)
    check_hardcoded_versions(rep, version)
    if not args.skip_ruff:
        check_ruff(rep)
    print('-' * 60)
    if rep.failed:
        print(f'未通过：{rep.failed} 项。修完再发版。')
        return 1
    print('全部通过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
