# -*- coding: utf-8 -*-
"""
llm_load 装载器测试（离线，不需要真实模型）

覆盖四件事：
1. manifest 校验：缺文件 / 哈希不符 / 完全一致三种状态判定正确
2. 释放：能把载荷写进目标目录，且只有 manifest 收录的文件才被管理
3. 幂等：释放后的再校验必须通过——这是「敢不敢立刻重启」的前提
4. 自愈：目标目录被改坏后，重新释放可抹平差异

运行：python tests/test_llm_load.py
"""
import importlib.util
import os
import shutil
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core_plugins.llm_load.main import (  # noqa: E402
    read_manifest, release, verify,
)

_ZIP = os.path.join(_REPO_ROOT, 'core_plugins', 'llm_load', 'llm_core.zip')
_MAIN = os.path.join(_REPO_ROOT, 'plugins', 'llm_core', 'main.py')


def test_manifest_readable():
    """载荷里能读到版本号与文件哈希表"""
    m = read_manifest(_ZIP)
    assert m.get('plugin') == 'llm_core', f"payload plugin 名不对: {m.get('plugin')}"
    assert m.get('version'), "manifest 缺 version"
    assert 'llm_core/main.py' in [f'llm_core/{k}' for k in m['files']] or 'main.py' in m['files'], \
        "manifest 里应该有 main.py"
    return f"v{m['version']} / {len(m['files'])} files"


def test_verify_missing_and_release():
    """空目录 → 判定缺失 → 释放 → 校验通过"""
    tmp = tempfile.mkdtemp()
    try:
        target = os.path.join(tmp, 'plugins', 'llm_core')
        os.makedirs(target, exist_ok=True)
        m = read_manifest(_ZIP)

        report = verify(target, m)
        assert not report['ok'], "空目录应当判定为不一致"
        assert report['missing'], "空目录应当报告缺失文件"
        assert len(report['missing']) == report['total'], "缺的就是全部"

        outcome = release(_ZIP, target, m)
        assert not outcome['failed'], f"释放失败: {outcome['failed']}"
        assert outcome['written'], "应该写进了文件"

        after = verify(target, m)
        assert after['ok'], f"释放后仍不一致: 缺失{after['missing']} 不符{after['mismatch']}"
        return f"释放 {len(outcome['written'])} 个文件后校验通过"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_tamper_then_heal():
    """改坏一个文件 → 判定不符 → 重新释放 → 恢复"""
    tmp = tempfile.mkdtemp()
    try:
        target = os.path.join(tmp, 'llm_core')
        m = read_manifest(_ZIP)
        release(_ZIP, target, m)
        assert verify(target, m)['ok']

        victim = os.path.join(target, 'tools.py')
        with open(victim, 'a', encoding='utf-8') as f:
            f.write('\n# 人为破坏\n')

        bad = verify(target, m)
        assert not bad['ok'], "被改过的文件应当判定为不一致"
        assert 'tools.py' in bad['mismatch'], f"应报告 tools.py 不符: {bad['mismatch']}"

        release(_ZIP, target, m)
        healed = verify(target, m)
        assert healed['ok'], f"自愈失败: {healed['mismatch']}"
        return "篡改后重新释放可自愈"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_extra_files_untouched():
    """manifest 未收录的文件（用户自己的数据）不应被删"""
    tmp = tempfile.mkdtemp()
    try:
        target = os.path.join(tmp, 'llm_core')
        m = read_manifest(_ZIP)
        release(_ZIP, target, m)

        mine = os.path.join(target, 'my_private_data.json')
        with open(mine, 'w', encoding='utf-8') as f:
            f.write('{"keep": 1}')

        release(_ZIP, target, m)
        assert os.path.isfile(mine), "未收录文件被误删了"
        assert verify(target, m)['ok']
        return "释放不误伤未收录文件"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_release_idempotent():
    """连续释放两次，结果与一次相同（幂等）"""
    tmp = tempfile.mkdtemp()
    try:
        target = os.path.join(tmp, 'llm_core')
        m = read_manifest(_ZIP)
        release(_ZIP, target, m)
        before = {rel: os.path.getsize(os.path.join(target, *rel.split('/')))
                  for rel in m['files']}
        release(_ZIP, target, m)
        after = {rel: os.path.getsize(os.path.join(target, *rel.split('/')))
                 for rel in m['files']}
        assert before == after, "重复释放导致文件大小变化"
        return "重复释放幂等"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_migratable_files_follow_framework():
    """配置/文档类文件的家在 plugins_dat：被迁走后不能判定为缺失。

    这条守的是「启动即重启」的死循环——框架启动时会把 README.md /
    _conf_schema.json 之类的文件从代码目录迁到 plugins_dat，装载器如果
    只认代码目录，就会每次启动都认为它们缺失，于是每次都释放、每次都重启。
    """
    tmp = tempfile.mkdtemp()
    try:
        target = os.path.join(tmp, 'llm_core')
        dat = os.path.join(tmp, 'plugins_dat', 'llm_core')
        m = read_manifest(_ZIP)

        outcome = release(_ZIP, target, m, dat)
        assert not outcome['failed'], f"释放失败: {outcome['failed']}"
        assert os.path.isfile(os.path.join(dat, 'README.md')), \
            "README.md 应当直接落到数据目录"
        assert os.path.isfile(os.path.join(target, 'main.py')), \
            "代码文件应当留在代码目录"
        assert verify(target, m, dat)['ok'], "释放后校验应当通过"

        # 模拟框架启动时的迁移：把代码目录里的这两类文件挪走
        for name in ('README.md', '_conf_schema.json'):
            p = os.path.join(target, name)
            if os.path.isfile(p):
                os.remove(p)
        after = verify(target, m, dat)
        assert after['ok'], f"文件被迁到数据目录后不应判定缺失: {after['missing']}"
        return "配置/文档文件按数据目录约定判定，不会误报缺失"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_live_target_synced():
    """若本机已经释放过，检查它是否与当前载荷一致（提示作用，不算失败）"""
    if not os.path.isdir(os.path.join(_REPO_ROOT, 'plugins', 'llm_core')):
        return "尚未释放到 plugins/，跳过"
    m = read_manifest(_ZIP)
    dat = os.path.join(_REPO_ROOT, 'data', 'plugins_dat', 'llm_core')
    report = verify(os.path.join(_REPO_ROOT, 'plugins', 'llm_core'), m, dat)
    assert report['ok'], f"现网副本与载荷不一致：缺失{report['missing']} 不符{report['mismatch']}"
    return f"本机 plugins/llm_core 与载荷一致（{report['total']} 个文件）"


if __name__ == '__main__':
    cases = [
        test_manifest_readable,
        test_verify_missing_and_release,
        test_tamper_then_heal,
        test_extra_files_untouched,
        test_migratable_files_follow_framework,
        test_release_idempotent,
        test_live_target_synced,
    ]
    failed = 0
    for fn in cases:
        try:
            note = fn()
            print(f"PASS  {fn.__name__}: {note or ''}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    sys.exit(1 if failed else 0)
