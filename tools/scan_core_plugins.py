# -*- coding: utf-8 -*-
"""官方插件清单扫描工具（独立进程）

职责：扫描 core_plugins/ 目录，把「已安装但 core_plugins.yaml 缺失」的
插件补进清单（enabled: false，发现即禁用），让框架严格按 core_plugins.yaml
加载官方插件——这是官方插件启停与配置的**唯一权威**；config.yaml 的
core_plugins 段已废弃，不再参与启停判定。

用法（在仓库根目录执行）：
  python tools/scan_core_plugins.py                 # dry-run：只打印将做的变更
  python tools/scan_core_plugins.py --write         # 落盘同步（新插件补块，enabled=false）
  python tools/scan_core_plugins.py --enable onebot_adapter webui   # 显式启用（即时写盘）
  python tools/scan_core_plugins.py --disable scheduler             # 显式禁用（即时写盘）

返回码：0 = 无变化或变更已落盘；1 = dry-run 发现变化但未写盘（供脚本判断）。
"""
import argparse
import os
import sys

# 允许从任意 cwd 运行（仓库根 = 本文件上级的上级）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import yaml  # noqa: E402

import framework.config as fc  # noqa: E402


def _load_yaml(yaml_path):
    """读 yaml；损坏现场保留 .bak 后重建（与框架启动逻辑一致）"""
    data = {}
    if os.path.isfile(yaml_path):
        try:
            with open(yaml_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"[scan] 读取 {yaml_path} 失败: {e}，备份后重建")
            bak = f"{yaml_path}.bak"
            if not os.path.exists(bak):
                try:
                    os.replace(yaml_path, bak)
                    print(f"[scan] 已备份损坏文件 -> {bak}")
                except Exception as be:
                    print(f"[scan] 备份失败: {be}")
    cps = data.get('core_plugins') if isinstance(data, dict) else None
    return cps if isinstance(cps, dict) else {}


def main():
    ap = argparse.ArgumentParser(
        description='官方插件清单扫描：发现新插件并同步 core_plugins.yaml')
    ap.add_argument('--write', action='store_true',
                    help='落盘同步（默认 dry-run 只打印差异）')
    ap.add_argument('--enable', nargs='+', metavar='NAME',
                    help='显式启用一个或多个已安装插件（立即写盘）')
    ap.add_argument('--disable', nargs='+', metavar='NAME',
                    help='显式禁用一个或多个插件（立即写盘）')
    args = ap.parse_args()

    installed = fc._scan_core_plugins()
    yaml_path = fc.CORE_PLUGINS_YAML
    cps = _load_yaml(yaml_path)

    changes = []
    # 1. 新发现插件 → 补块（enabled 强制 False，绝不静默启用）
    for name in installed:
        if name not in cps or not isinstance(cps[name], dict):
            blk = dict(fc._CORE_PLUGIN_SCHEMA.get(name, {}))
            blk['enabled'] = False
            cps[name] = blk
            changes.append(f"新增插件 [{name}]（enabled: false，待显式启用）")

    # 2. 已卸载插件 → 从清单移除
    for name in list(cps.keys()):
        if name not in installed:
            del cps[name]
            changes.append(f"移除已卸载插件 [{name}]")

    # 3. 显式启停（用户意图，优先于上面补块的 enabled:false）
    explicit = []
    if args.enable:
        for name in args.enable:
            if name not in installed:
                changes.append(f"错误: 插件 [{name}] 未安装（core_plugins/{name}/main.py 不存在）")
                continue
            if cps[name].get('enabled') is not True:
                cps[name]['enabled'] = True
                changes.append(f"启用插件 [{name}]")
                explicit.append(name)
    if args.disable:
        for name in args.disable:
            if name in cps:
                if cps[name].get('enabled') is not False:
                    cps[name]['enabled'] = False
                    changes.append(f"禁用插件 [{name}]")
                    explicit.append(name)
            else:
                changes.append(f"跳过: 插件 [{name}] 不在清单中")

    if not changes:
        print(f"[scan] 清单无变化（已安装 {len(installed)} 个官方插件）")
        return 0

    print(f"[scan] 检测到 {len(changes)} 项变更：")
    for c in changes:
        print(f"  - {c}")

    should_write = args.write or bool(explicit)
    if not should_write:
        print("\n[dry-run] 未写盘。添加 --write 落盘同步。")
        return 1

    with open(yaml_path, 'w', encoding='utf-8') as f:
        yaml.safe_dump({'core_plugins': cps}, f,
                       allow_unicode=True, sort_keys=False)
    print(f"\n[scan] 已同步 -> {yaml_path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())