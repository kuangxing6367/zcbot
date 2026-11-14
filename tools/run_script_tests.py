# -*- coding: utf-8 -*-
"""脚本式测试运行器（CI 与本地共用）。

为什么需要它
============

``tests/`` 下混着两类文件：

1. **纯 pytest 用例** —— 函数体里有 ``assert`` / ``pytest.raises``，
   交给 ``pytest tests`` 收集执行即可。
2. **脚本式自测** —— 断言写在 ``check()`` 里、失败时 ``sys.exit(1)``，
   入口在 ``main()``。pytest 对这类文件有两种失效方式：

   - 文件里没有 ``def test_*``：pytest 收集 0 个用例，**整个文件根本不执行**；
   - 有 ``def test_*`` 但函数体只是调用 ``check()`` 或什么都不断言：
     pytest 收集到了，**但零 assert，永远 PASS**。

   两种都表现为 CI 绿而实际没验证。

历史事故
========

v1.22.0 审计发现：两个 workflow（``ci.yml`` / ``tests.yml``）各自维护一份
手写文件清单，两份内容互相错位——26 个测试文件、342 个用例不在任何 CI 里。
其中包含三个安全套件，以及当时刚修完回归的 ``test_buffer_l2_file`` 等。

手写清单的失效模式是静默的：新增文件忘了加进 YAML 不报错，评审也看不出来。
所以这里改成**静态分析自动判定**，清单不再手写。

判据
====

对每个 ``tests/**/test_*.py``：

- 没有 ``def test_*``                                     -> 必须脚本执行
- 有 ``def test_*``，但全部函数体内无 assert/raise/fail     -> 必须脚本执行
- 其余                                                     -> 交给 pytest

用法
====

    python tools/run_script_tests.py --list   # 只列出判定结果，不执行
    python tools/run_script_tests.py          # 逐个执行脚本式文件
    python tools/run_script_tests.py --check  # 校验 pytest 收集数不低于基线

``--check`` 用于防止有人给 pytest 加 ``--ignore`` / ``-k`` 把用例悄悄丢掉。
基线常量在 ``MIN_COLLECTED``，新增测试后手动上调。
"""
from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# pytest 可收集用例数的下限。加测试时上调；下调必须说明理由，
# 否则「测试变少了」这件事会静默通过。
MIN_COLLECTED = 1000

# 单文件执行上限（秒）。黑盒用例会拉起真实进程，留足余量。
PER_FILE_TIMEOUT = 420

# pytest 自身会收集、但不是测试目标的文件
SKIP_NAMES = {"conftest.py", "__init__.py"}

_ASSERT_CALL_HINTS = ("pytest.raises", "pytest.fail", ".fail(", "pytest.skip")


def _is_test_func(node: ast.AST) -> bool:
    return (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    )


def _has_assertion(func: ast.AST) -> bool:
    """函数体内是否存在任何能让 pytest 判失败的构造。"""
    for node in ast.walk(func):
        if isinstance(node, (ast.Assert, ast.Raise)):
            return True
        if isinstance(node, ast.Call):
            try:
                callee = ast.unparse(node.func)
            except Exception:  # pragma: no cover - 极端语法
                continue
            if any(hint in callee for hint in _ASSERT_CALL_HINTS):
                return True
    return False


def classify(path: Path) -> str:
    """返回 'script' / 'pytest' / 'skip'。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        # 解析不了就别猜，交给 pytest 去报错
        return "pytest"

    test_funcs = [n for n in ast.walk(tree) if _is_test_func(n)]
    if not test_funcs:
        return "script"
    if all(not _has_assertion(f) for f in test_funcs):
        return "script"
    return "pytest"


def discover() -> tuple[list[Path], list[Path]]:
    scripts: list[Path] = []
    pytest_files: list[Path] = []
    for path in sorted(TESTS.rglob("*.py")):
        if path.name in SKIP_NAMES or not path.name.startswith("test_"):
            continue
        target = scripts if classify(path) == "script" else pytest_files
        target.append(path)
    return scripts, pytest_files


def run_scripts(scripts: list[Path]) -> int:
    if not scripts:
        print("没有需要脚本执行的文件。")
        return 0

    print(f"脚本式测试 {len(scripts)} 个文件，逐个执行：\n")
    failures: list[tuple[str, int, str]] = []
    started = time.time()

    for path in scripts:
        rel = path.relative_to(ROOT).as_posix()
        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, str(path)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PER_FILE_TIMEOUT,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            rc, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            rc = -1
            out = f"超时（>{PER_FILE_TIMEOUT}s）"

        cost = time.time() - t0
        if rc == 0:
            print(f"  PASS  {rel}  ({cost:.1f}s)")
        else:
            print(f"  FAIL  {rel}  (rc={rc}, {cost:.1f}s)")
            failures.append((rel, rc, out))

    total = time.time() - started
    print(f"\n合计 {len(scripts)} 个文件 / {total:.1f}s / 失败 {len(failures)}")

    if failures:
        print("\n" + "=" * 70)
        for rel, rc, out in failures:
            print(f"\n---- {rel} (rc={rc}) ----")
            tail = out.strip().splitlines()[-60:]
            print("\n".join(tail))
        print("=" * 70)
        return 1
    return 0


def check_collected() -> int:
    """断言 pytest 能收集到的用例数不低于基线。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = [ln for ln in (proc.stdout or "").strip().splitlines() if "tests collected" in ln]
    if not tail:
        print("无法解析 pytest 收集结果：")
        print((proc.stdout or "")[-2000:])
        print((proc.stderr or "")[-2000:])
        return 1

    try:
        collected = int(tail[-1].split()[0])
    except (ValueError, IndexError):
        print(f"无法解析收集数：{tail[-1]}")
        return 1

    print(f"pytest 收集到 {collected} 个用例（下限 {MIN_COLLECTED}）")
    if collected < MIN_COLLECTED:
        print(
            f"\n收集数低于基线 {MIN_COLLECTED}。用例变少通常意味着：\n"
            "  - pytest 调用里被加了 --ignore / -k / --deselect\n"
            "  - 测试文件被误删或改名\n"
            "确认是有意变更后，上调 tools/run_script_tests.py 里的 MIN_COLLECTED。"
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="脚本式测试自动发现与执行")
    parser.add_argument("--list", action="store_true", help="只列出判定结果，不执行")
    parser.add_argument("--check", action="store_true", help="校验 pytest 收集数不低于基线")
    args = parser.parse_args()

    if args.check:
        return check_collected()

    scripts, pytest_files = discover()

    if args.list:
        print(f"脚本执行（{len(scripts)}）：")
        for p in scripts:
            print("  " + p.relative_to(ROOT).as_posix())
        print(f"\npytest 执行（{len(pytest_files)}）：")
        for p in pytest_files:
            print("  " + p.relative_to(ROOT).as_posix())
        return 0

    return run_scripts(scripts)


if __name__ == "__main__":
    raise SystemExit(main())
