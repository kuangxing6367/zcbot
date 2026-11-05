# -*- coding: utf-8 -*-
"""WebUI 发布产物完整性校验（CI 门禁 / 本地发版前自检）。

背景：v1.20.0 曾因单文件 >1MB 的 element-plus chunk 走分批推送时漏推，
index.html 与全部业务 chunk 静态 import 它，但仓库里没有该文件 —— 结果
所有新 clone 的 WebUI 白屏。原 CI 门禁只检查「index.html 存在 + js 数量>0」，
查不出「引用了不存在的 hash 文件」，故补此校验。

校验内容：
  1. index.html 中所有 ./js/、./css/ 引用必须真实存在；
  2. 所有已提交 chunk 内引号包裹的 ./xxx.js 静态引用必须真实存在；
  3. js/ 下至少有一个产物文件，且不允许残留没有任何 chunk 引用的旧 hash 大文件；
  4. 第三方许可声明必须随产物分发（MIT 要求保留版权声明，而压缩 + 混淆会把
     源文件里的注释全部删掉，THIRD-PARTY-NOTICES.txt 是它们在产物中的唯一载体）。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

# index.html 里的 src/href="./js/xxx.js" 或 "./css/xxx.css"
_HTML_REF = re.compile(r'(?:src|href)=["\']\./((?:js|css)/[A-Za-z0-9_.-]+\.(?:js|css))["\']')
# chunk 内的静态 import: from"./x.js" / import"./x.js" / import("./x.js")
_JS_REF = re.compile(r'["\']\./([A-Za-z0-9_.-]+\.js)["\']')


def check(root: pathlib.Path) -> list[str]:
    errors: list[str] = []
    refs: set[str] = set()

    index = root / 'index.html'
    if not index.is_file():
        return [f'缺少 {index}']

    html = index.read_text(encoding='utf-8', errors='replace')
    for m in _HTML_REF.finditer(html):
        refs.add(m.group(1))

    js_dir = root / 'js'
    if not js_dir.is_dir():
        return [f'缺少 {js_dir}']

    chunks = sorted(js_dir.glob('*.js'))
    if not chunks:
        return [f'{js_dir} 下无 JS 产物']

    for chunk in chunks:
        text = chunk.read_text(encoding='utf-8', errors='replace')
        for m in _JS_REF.finditer(text):
            refs.add('js/' + m.group(1))

    for ref in sorted(refs):
        if not (root / ref).is_file():
            errors.append(f'引用了不存在的文件: {ref}')

    # 大文件（>=500KB）若没有任何引用，多半是上一代构建残留，提示清理
    for chunk in chunks:
        if chunk.stat().st_size >= 500 * 1024 and ('js/' + chunk.name) not in refs:
            errors.append(f'无引用的旧版大文件残留: js/{chunk.name}')

    # ── 第三方许可声明必须随产物分发 ──
    # vite 构建时 terser 的 format.comments=false 加混淆器会把 element-plus /
    # vue / vue-router 的版权注释全部删掉，产物 js 里 license 出现次数为 0。
    # MIT 要求副本保留版权声明，故用独立的 NOTICES 文件承载，并且必须真的
    # 复制进产物目录（只放在仓库根不算随副本分发）。
    notices = root / 'THIRD-PARTY-NOTICES.txt'
    if not notices.is_file():
        errors.append('缺少 THIRD-PARTY-NOTICES.txt（第三方组件版权声明必须随产物分发）')
    elif notices.stat().st_size < 512:
        errors.append('THIRD-PARTY-NOTICES.txt 内容过短，疑似占位文件')
    else:
        text = notices.read_text(encoding='utf-8', errors='replace')
        # 打进来的第三方库固定是这几个，缺任一说明声明不完整
        for pkg in ('element-plus', 'vue', 'vue-router'):
            if pkg not in text:
                errors.append(f'THIRD-PARTY-NOTICES.txt 未声明 {pkg}')

    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description='校验 WebUI 构建产物引用完整性')
    ap.add_argument('--root', default='core_plugins/webui/web',
                    help='产物目录，默认 core_plugins/webui/web')
    args = ap.parse_args()
    root = pathlib.Path(args.root)
    errors = check(root)
    if errors:
        for e in errors:
            print(f'::error::{e}')
        print(f'WebUI 产物校验失败：{len(errors)} 处问题')
        return 1
    print(f'WebUI 产物校验通过：{root}（引用完整、无旧版大文件残留）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
