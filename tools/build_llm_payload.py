# -*- coding: utf-8 -*-
"""
打包 llm_core 载荷 → core_plugins/llm_load/llm_core.zip

llm_load 在框架启动时按 manifest 校验 plugins/llm_core 的每一个文件，
缺失或哈希不一致就释放覆盖，所以**这里是唯一的真源**：

    core_plugins/llm_load/src/llm_core/   源码（人在这里改）
            │
            │  python tools/build_llm_payload.py --write
            ▼
    core_plugins/llm_load/llm_core.zip    载荷（机器在这里读）

manifest 结构::

    {
      "plugin": "llm_core",
      "version": "1.0.0",          # 取自 src/llm_core/main.py 的 __version__
      "built_at": "2026-09-30T00:00:00",
      "files": {"main.py": {"md5": "...", "size": 1024}, ...}
    }

是否触发重释放由**每个文件的 md5** 决定，不是版本号：llm_load 逐文件比对目标
目录与 manifest 里的 md5，任何不一致就覆盖回去并重启。`version` 字段只用于展示
与日志，改源码重新 ``--write`` 后即便不手动加版本号，内容变化本身就会让 md5 对不上
从而触发释放——所以「改了源码忘加版本号 = 改动不生效」的说法不成立，正常 ``--write`` 即可。

用法（仓库根目录执行）：

    python tools/build_llm_payload.py            # dry-run，打印将打包的文件
    python tools/build_llm_payload.py --write    # 落盘生成/更新 zip
    python tools/build_llm_payload.py --check    # 校验 src 真源与载荷 zip、以及现网 plugins/llm_core 是否都对齐
"""
import argparse
import ast
import datetime as _dt
import hashlib
import json
import os
import sys
import zipfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_SRC_ROOT = os.path.join(_REPO_ROOT, 'core_plugins', 'llm_load', 'src', 'llm_core')
_ZIP_PATH = os.path.join(_REPO_ROOT, 'core_plugins', 'llm_load', 'llm_core.zip')
_TARGET_DIR = os.path.join(_REPO_ROOT, 'plugins', 'llm_core')
_PLUGIN_NAME = 'llm_core'
_DAT_DIR = os.path.join(_REPO_ROOT, 'data', 'plugins_dat', _PLUGIN_NAME)

_SKIP_DIRS = {'__pycache__', '.git', '.idea'}
_SKIP_SUFFIX = ('.pyc', '.pyo', '.bak')


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _read_version() -> str:
    """从 main.py 静态解析 __version__（不执行插件代码）"""
    path = os.path.join(_SRC_ROOT, 'main.py')
    with open(path, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename=path)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == '__version__':
                    try:
                        return str(ast.literal_eval(node.value))
                    except Exception:  # noqa: BLE001
                        break
    raise RuntimeError("main.py 里找不到 __version__，无法打包")


def _collect_files() -> dict:
    """按相对路径收集源码文件 → {relpath: bytes}"""
    files = {}
    for root, dirs, fnames in os.walk(_SRC_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in sorted(fnames):
            if fname.endswith(_SKIP_SUFFIX):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, _SRC_ROOT).replace(os.sep, '/')
            with open(full, 'rb') as f:
                files[rel] = f.read()
    return files


def _build_manifest(files: dict, version: str) -> dict:
    return {
        'plugin': _PLUGIN_NAME,
        'version': version,
        'built_at': _dt.datetime.now().replace(microsecond=0).isoformat(),
        'files': {rel: {'md5': _md5(data), 'size': len(data)}
                  for rel, data in sorted(files.items())},
    }


def do_build(write: bool) -> int:
    version = _read_version()
    files = _collect_files()
    if not files:
        print("[build] 源码目录为空，无内容可打包")
        return 1
    manifest = _build_manifest(files, version)

    print(f"[build] 载荷 {_PLUGIN_NAME} v{version}，共 {len(files)} 个文件：")
    for rel in sorted(files):
        print(f"  - {rel} ({files[rel] and len(files[rel])} B)")

    if not write:
        print("\n[dry-run] 未写盘。加 --write 生成/更新 "
              f"{os.path.relpath(_ZIP_PATH, _REPO_ROOT)}")
        return 0

    os.makedirs(os.path.dirname(_ZIP_PATH), exist_ok=True)
    tmp_path = _ZIP_PATH + '.tmp'
    with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('manifest.json',
                    json.dumps(manifest, ensure_ascii=False, indent=2))
        for rel in sorted(files):
            zf.writestr(f"{_PLUGIN_NAME}/{rel}", files[rel])
    if os.path.exists(_ZIP_PATH):
        os.remove(_ZIP_PATH)
    os.replace(tmp_path, _ZIP_PATH)
    print(f"\n[build] 已写入 {os.path.relpath(_ZIP_PATH, _REPO_ROOT)}")
    print("[build] 提示：下次框架启动时由 llm_load 按文件 md5 比对自动释放"
          "（内容变了即触发覆盖与重启，与 version 无关）")
    return 0


def do_check() -> int:
    """校验两件事：① src 真源是否与载荷 zip 一致；② 现网 plugins/llm_core 是否对齐。

    ① 若改了 src 忘了 --write，zip 就是陈旧的——此时 ② 可能仍「一致」（现网==旧 zip），
       但真源已经领先，CI 必须抓到。② 复用 llm_load 的 verify()，按框架约定去数据目录
       找被迁移的配置/文档，避免把「已迁移」误报成「缺失」。
    """
    if not os.path.isfile(_ZIP_PATH):
        print(f"[check] 载荷不存在: {_ZIP_PATH}")
        return 1
    with zipfile.ZipFile(_ZIP_PATH, 'r') as zf:
        manifest = json.loads(zf.read('manifest.json').decode('utf-8'))

    rc = 0
    version = manifest['version']

    # ① src 真源 vs zip manifest
    src_files = _collect_files()
    stale = []
    for rel, data in src_files.items():
        spec = manifest['files'].get(rel)
        if spec is None:
            stale.append(f"载荷缺该文件（src 新增未打包）{rel}")
        elif _md5(data) != spec['md5']:
            stale.append(f"src 与载荷哈希不符 {rel}")
    for rel in manifest['files']:
        if rel not in src_files:
            stale.append(f"载荷含 src 已删除的文件 {rel}")
    if stale:
        print(f"[check] src 真源与载荷 zip 不一致（v{version}），请加 --write：")
        for line in stale:
            print(f"  - {line}")
        rc = 1
    else:
        print(f"[check] src 真源与载荷 zip 一致（{len(src_files)} 个文件）")

    # ② 现网 plugins/llm_core vs zip（按框架迁移约定，配置/文档可在数据目录）
    from core_plugins.llm_load.main import read_manifest, verify
    rep = verify(_TARGET_DIR, read_manifest(_ZIP_PATH),
                 _DAT_DIR if os.path.isdir(_DAT_DIR) else None)
    if rep['ok']:
        print(f"[check] {version} 与现网一致（{rep['total']} 个文件，"
              f"含数据目录 {os.path.relpath(_DAT_DIR, _REPO_ROOT)}）")
    else:
        print(f"[check] 载荷 v{rep['version']} 与现网不一致：")
        for r in rep['missing']:
            print(f"  - 缺失 {r}")
        for r in rep['mismatch']:
            print(f"  - 不一致 {r}")
        rc = 1
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description='打包 llm_core 载荷')
    ap.add_argument('--write', action='store_true', help='落盘写 zip')
    ap.add_argument('--check', action='store_true', help='校验现网是否对齐')
    args = ap.parse_args()
    if args.check:
        return do_check()
    return do_build(args.write)


if __name__ == '__main__':
    sys.exit(main())
