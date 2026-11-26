"""打包元数据与 CI 版本矩阵的一致性

由来：`requires-python` 曾经长期写着 `>=3.10`，而 CI 只跑 3.10–3.14 的 compileall、
真正的全量测试矩阵并不覆盖最低版本；反过来，改 Python 下限时也很容易只改一处。
这里把「对外声明的下限」和「实际测过的最低版本」钉在一起：两边不一致直接红，
避免再出现「声明支持 3.10，但 3.10 上从来没跑过」或者「下限抬到 3.12，CI 还在测 3.10」。
"""
import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _pyproject():
    with open(ROOT / 'pyproject.toml', 'rb') as f:
        return tomllib.load(f)


def _floor(spec: str) -> tuple[int, int]:
    """>=3.12 → (3, 12)；只关心下限，上界与排除项不参与比较"""
    m = re.search(r'>=\s*(\d+)\.(\d+)', spec)
    assert m, f'requires-python 没写下限: {spec!r}'
    return int(m.group(1)), int(m.group(2))


def _ci_matrix():
    data = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8'))
    versions = data['jobs']['compile']['strategy']['matrix']['python-version']
    return [tuple(int(p) for p in str(v).split('.')[:2]) for v in versions]


def test_requires_python_matches_ci_minimum():
    meta = _pyproject()['project']
    declared = _floor(meta['requires-python'])
    tested_min = min(_ci_matrix())
    assert declared == tested_min, (
        f'requires-python 下限 {declared[0]}.{declared[1]} 与 CI 最低版本 '
        f'{tested_min[0]}.{tested_min[1]} 不一致；改下限时同步 ci.yml 的矩阵')


def test_classifiers_cover_ci_matrix():
    meta = _pyproject()['project']
    declared = set(meta.get('classifiers') or [])
    missing = [
        f'{major}.{minor}' for major, minor in _ci_matrix()
        if f'Programming Language :: Python :: {major}.{minor}' not in declared
    ]
    assert not missing, f'CI 在测但没有在 classifiers 里声明: {missing}'


def test_ci_versions_not_below_floor():
    floor = _floor(_pyproject()['project']['requires-python'])
    below = [f'{a}.{b}' for a, b in _ci_matrix() if (a, b) < floor]
    assert not below, f'CI 仍在测低于下限的版本: {below}'
