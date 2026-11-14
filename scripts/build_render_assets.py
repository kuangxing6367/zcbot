"""打包 image_renderer 的运行时资源（字体子集 + 各平台原生扩展）。

产物 assets.zip 结构（与 core_plugins/image_renderer/assets.py 的解析逻辑一一对应）：

    manifest.json
    fonts/msyh_subset_common.ttf
    fonts/msyh_subset_ext.ttf
    fonts/charset_common.txt
    fonts/charset_ext.txt
    native/<platform>/zcbot_render.(pyd|so)

用法：
    python scripts/build_render_assets.py                 # 从插件目录收集（本地构建好的产物）
    python scripts/build_render_assets.py --out dist/      # 指定输出目录
    python scripts/build_render_assets.py --strict        # 缺任一平台产物即失败（CI 用）

设计说明：
  - 字体与二进制都不再进源码仓库，这个脚本是它们唯一的打包入口。
  - 生成 manifest.json 记录版本与每个文件的 sha256，便于部署侧核对。
  - --strict 在 CI 里用：正式发布必须 7 个平台齐全，否则装机时会因缺当前平台产物回退失败。
"""
import argparse
import hashlib
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, 'core_plugins', 'image_renderer')

FONT_FILES = ('msyh_subset_common.ttf', 'msyh_subset_ext.ttf',
              'charset_common.txt', 'charset_ext.txt')

PLATFORMS = {
    'win64': 'zcbot_render.pyd',
    'win32': 'zcbot_render.pyd',
    'linux-x86_64': 'zcbot_render.so',
    'linux-aarch64': 'zcbot_render.so',
    'linux-i686': 'zcbot_render.so',
    'linux-armv7': 'zcbot_render.so',
    'linux-loongarch64': 'zcbot_render.so',
}

# 可选产物（有就带，没有不阻塞）：字体子集是"锦上添花"，缺失时运行时会回退系统字体
OPTIONAL_FONTS = {'charset_common.txt', 'charset_ext.txt'}


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(262144), b''):
            h.update(chunk)
    return h.hexdigest()


def read_version():
    for name in ('VERSION',):
        p = os.path.join(ROOT, name)
        if os.path.isfile(p):
            return open(p, encoding='utf-8').read().strip()
    return '0.0.0'


def collect(strict):
    """返回 [(zip 内路径, 本地绝对路径)]，同时报告缺失项。"""
    entries, missing_fonts, missing_native = [], [], []

    for fn in FONT_FILES:
        src = os.path.join(PLUGIN, fn)
        if os.path.isfile(src):
            entries.append(('fonts/' + fn, src))
        elif fn not in OPTIONAL_FONTS:
            missing_fonts.append(fn)

    for plat, fname in PLATFORMS.items():
        src = os.path.join(PLUGIN, 'native', 'bin', plat, fname)
        if os.path.isfile(src):
            entries.append((f'native/{plat}/{fname}', src))
        else:
            missing_native.append(plat)

    return entries, missing_fonts, missing_native


def main(argv=None):
    ap = argparse.ArgumentParser(description='打包 image_renderer 运行时资源')
    ap.add_argument('--out', default=os.path.join(ROOT, 'dist'))
    ap.add_argument('--strict', action='store_true',
                    help='缺任一平台或字体即返回非零（正式发布用）')
    args = ap.parse_args(argv)

    entries, missing_fonts, missing_native = collect(args.strict)

    print(f'收集资源：{len(entries)} 个文件')
    for arc, src in entries:
        print(f'  {arc:<44} {os.path.getsize(src) / 1048576.0:>8.2f}MB')
    if missing_fonts:
        print(f'缺字体（非 charset）: {missing_fonts}')
    if missing_native:
        print(f'缺平台产物: {missing_native}')

    if not entries:
        print('没有任何可打包的文件，退出')
        return 2
    if args.strict and (missing_fonts or missing_native):
        print('--strict 模式下要求资源齐全，退出')
        return 3

    os.makedirs(args.out, exist_ok=True)
    out_zip = os.path.join(args.out, 'assets.zip')

    files_meta = {}
    for arc, src in entries:
        files_meta[arc] = {
            'sha256': sha256_of(src),
            'size': os.path.getsize(src),
        }
    manifest = {
        'version': read_version(),
        'generated_by': 'scripts/build_render_assets.py',
        'files': files_meta,
    }

    with zipfile.ZipFile(out_zip, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr('manifest.json',
                    json.dumps(manifest, ensure_ascii=False, indent=2,
                               sort_keys=True).encode('utf-8'))
        for arc, src in entries:
            zf.write(src, arc)

    size = os.path.getsize(out_zip)
    digest = sha256_of(out_zip)
    with open(out_zip + '.sha256', 'w', encoding='utf-8') as f:
        f.write(f'{digest}  assets.zip\n')

    print(f'\n已生成: {out_zip}  ({size / 1048576.0:.2f}MB)')
    print(f'sha256: {digest}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
