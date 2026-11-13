# -*- coding: utf-8 -*-
"""ZCBOT CLI 公共工具（路径 / 版本 / 插件发现 / 模板 / YAML 原地改写）。

不依赖任何第三方库（仅标准库），保证 CLI 在最小环境下也能跑。
"""
import ast
import os
import re
import sys

PROG = "zb"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read_version():
    """当前内核版本（源码读 VERSION，wheel 读包元数据）。

    委托 ``framework.migration.version.current_version``，不再在这里重复一套
    文件探测：原实现只认 ``REPO_ROOT/VERSION``，而 wheel 里没有这个文件
    （它不属于任何包），于是 pip 安装态下 ``zb -v`` 永远输出版本 ``0.0.0``。
    """
    try:
        from framework.migration.version import current_version
        v = current_version().raw
        # ``0.0.0+unknown`` 表示四处来源全都没读到，此时给调用方一个可读的
        # 明确标记，而不是伪装成合法版本号 0.0.0。
        return v.replace('+unknown', '') if v.startswith('0.0.0+') else v
    except Exception:
        # CLI 是最后一道可用的救命入口，任何异常都不允许把 --version 弄崩。
        cand = os.path.join(REPO_ROOT, "VERSION")
        try:
            with open(cand, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except Exception:
            pass
        return "0.0.0+unknown"


def read_file(path, default=""):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


# --------------------------------------------------------------------------
# 插件 / 适配器发现
# --------------------------------------------------------------------------
def _read_meta(main_py):
    """用 ast 安全提取 ``__plugin_meta__`` 字面量（不 import 插件代码，避免拉依赖）。"""
    try:
        tree = ast.parse(read_file(main_py))
    except Exception:
        return {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__plugin_meta__":
                    try:
                        val = ast.literal_eval(node.value)
                        return val if isinstance(val, dict) else {}
                    except Exception:
                        return {}
    return {}


def _kernel_plugin_dirs():
    """内核实际使用的（用户插件目录, 官方插件目录）。

    pip 安装态下官方插件在 ``site-packages/core_plugins``、用户插件在
    ``<home>/data/plugins``，两者都不在进程 cwd。CLI 若只看 cwd，就会在
    运行时明明加载了官方插件的情况下报「未找到任何插件」。
    """
    try:
        from framework import paths
        return paths.user_plugins_dir(), paths.core_plugins_dir()
    except Exception:
        return None, None


def _iter_plugin_dirs(working_dir):
    """yield (plugin_id, main_py_path, kind) —— kind: 'user' / 'core'。

    同时纳入 ``plugins_disabled/``（被卸载到禁用区的用户插件，标记仍为 user，
    但其启用态由 ``plugin_enabled_state`` 判定为 False）。

    目录顺序按「运行时真正读的位置」优先，再补 cwd 下的历史布局；同名插件
    以先出现的为准（``seen`` 去重），保证 CLI 视图与内核视图一致。
    """
    user_root, core_root = _kernel_plugin_dirs()
    roots = []
    if user_root:
        roots.append((user_root, "user"))
        roots.append((user_root.rstrip("/\\") + "_disabled", "user"))
    if core_root:
        roots.append((core_root, "core"))
    roots += [
        (os.path.join(working_dir, "data", "plugins"), "user"),
        (os.path.join(working_dir, "data", "plugins_disabled"), "user"),
        (os.path.join(working_dir, "plugins"), "user"),
        (os.path.join(working_dir, "plugins_disabled"), "user"),
        (os.path.join(working_dir, "core_plugins"), "core"),
    ]
    seen = set()
    for root, kind in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            main_py = os.path.join(root, name, "main.py")
            if os.path.isfile(main_py) and name not in seen:
                seen.add(name)
                yield name, main_py, kind


def discover_plugins(working_dir):
    """返回 [(id, path, meta, kind)]。"""
    out = []
    for pid, main_py, kind in _iter_plugin_dirs(working_dir):
        out.append((pid, main_py, _read_meta(main_py), kind))
    return out


def discover_adapters(working_dir):
    """适配器即 core_plugins 下带网络接入特征的条目；这里直接返回 core 插件。"""
    return [(pid, p, meta, kind) for pid, p, meta, kind in discover_plugins(working_dir)
            if kind == "core"]


def plugin_enabled_state(working_dir, plugin_id, kind):
    """尽力推断插件启用状态（不依赖运行中的框架）。"""
    if kind == "core":
        # 与内核同一套解析：pip 安装态清单在 <home>/data/core_plugins.yaml
        yaml_path = os.path.join(working_dir, "data", "core_plugins.yaml")
        try:
            from framework import paths
            resolved = paths.core_plugins_yaml_path()
            if resolved and os.path.isfile(resolved):
                yaml_path = resolved
        except Exception:
            if not os.path.isfile(yaml_path):
                yaml_path = os.path.join(working_dir, "core_plugins.yaml")
        return _yaml_get_enabled(yaml_path, "core_plugins", plugin_id, default=False)
    # 用户插件：默认启用；plugins_disabled/ 中的视为禁用
    if os.path.isdir(os.path.join(working_dir, "plugins_disabled", plugin_id)):
        return False
    py = os.path.join(working_dir, "plugins", plugin_id, "plugin.yaml")
    if os.path.isfile(py):
        return _yaml_get_enabled(py, plugin_id, plugin_id, default=True)
    return True


# --------------------------------------------------------------------------
# YAML 原地改写（保留注释 / 结构）
# --------------------------------------------------------------------------
def _yaml_get_enabled(yaml_path, top_key, plugin_id, default=None):
    """读 ``top_key:\n  <plugin_id>:\n    enabled: <bool>`` 的 enabled 值。"""
    text = read_file(yaml_path)
    if not text:
        return default
    lines = text.splitlines()
    in_top = False
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not in_top:
            if re.match(r"^%s\s*:\s*$" % re.escape(top_key), line):
                in_top = True
            i += 1
            continue
        # 在 top_key 块内
        m = re.match(r"^  ([A-Za-z0-9_]+):", line)
        if m:
            if m.group(1) == plugin_id:
                # 扫描该插件块内的 enabled（4 空格缩进）
                j = i + 1
                while j < n and re.match(r"^    [A-Za-z0-9_]", lines[j]):
                    em = re.match(r"^    enabled\s*:\s*(.*)$", lines[j])
                    if em:
                        v = em.group(1).strip().lower()
                        return v in ("true", "1", "yes", "on")
                    j += 1
                return default
            # 同级其它插件 id（仍在同一 top_key 块内），继续向下找
            i += 1
            continue
        if re.match(r"^    [A-Za-z0-9_]", line):
            # 上一个插件块的内容行（4 空格缩进），跳过但不退出 top_key 块
            i += 1
            continue
        # 缩进回到顶层外，top_key 块结束
        in_top = False
        i += 1
    return default


def set_core_plugin_enabled(yaml_path, plugin_id, enabled):
    """在 core_plugins.yaml 中按行原地设置 core_plugins.<plugin_id>.enabled。

    找不到 ``enabled:`` 则在该插件块内插入一行；文件不存在则新建。
    返回 (ok, detail)。
    """
    if not os.path.isfile(yaml_path):
        text = "core_plugins:\n"
    else:
        text = read_file(yaml_path)
    lines = text.splitlines()
    out = []
    i = 0
    n = len(lines)
    in_block = False
    placed = False
    while i < n:
        line = lines[i]
        if not in_block:
            out.append(line)
            if re.match(r"^core_plugins\s*:\s*$", line):
                in_block = True
            i += 1
            continue
        m = re.match(r"^  ([A-Za-z0-9_]+):", line)
        if m:
            out.append(line)
            if m.group(1) == plugin_id:
                # 处理该插件块内的 enabled
                j = i + 1
                block = []
                while j < n and re.match(r"^    [A-Za-z0-9_]", lines[j]):
                    block.append(lines[j])
                    j += 1
                found = False
                for bl in block:
                    em = re.match(r"^    enabled\s*:\s*", bl)
                    if em:
                        out.append("    enabled: %s" % ("true" if enabled else "false"))
                        found = True
                    else:
                        out.append(bl)
                if not found:
                    out.append("    enabled: %s" % ("true" if enabled else "false"))
                i = j
                placed = True
                in_block = False
                continue
            i += 1
            continue
        # 块结束
        out.append(line)
        in_block = False
        i += 1
    if not placed:
        # 插件条目不存在：在 core_plugins 块里追加
        if not any(re.match(r"^core_plugins\s*:\s*$", l) for l in out):
            out.append("core_plugins:")
        out.append("  %s:" % plugin_id)
        out.append("    enabled: %s" % ("true" if enabled else "false"))
    new_text = "\n".join(out).rstrip() + "\n"
    try:
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write(new_text)
    except Exception as e:  # noqa: BLE001
        return False, "写入失败: %s" % e
    return True, "core_plugins.%s.enabled = %s" % (plugin_id, enabled)


def enable_user_plugin(working_dir, plugin_id, enabled):
    """用户插件启用/禁用：在 plugins/ 与 plugins_disabled/ 之间移动目录。"""
    src = os.path.join(working_dir, "plugins", plugin_id)
    dst = os.path.join(working_dir, "plugins_disabled", plugin_id)
    if enabled:
        if os.path.isdir(dst):
            try:
                import shutil
                shutil.move(dst, src)
                return True, "已启用（从 plugins_disabled 移回 plugins）"
            except Exception as e:  # noqa: BLE001
                return False, "移回失败: %s" % e
        if os.path.isdir(src):
            return True, "已启用"
        return False, "未找到插件目录: plugins/%s" % plugin_id
    else:
        if os.path.isdir(src):
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                import shutil
                shutil.move(src, dst)
                return True, "已禁用（移入 plugins_disabled）"
            except Exception as e:  # noqa: BLE001
                return False, "移出失败: %s" % e
        return True, "已是禁用状态"


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def render_table(rows, headers):
    """极简表格渲染（固定列宽左对齐）。"""
    cols = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for idx in range(cols):
            widths[idx] = max(widths[idx], len(str(row[idx])))
    fmts = ["{:<%d}" % w for w in widths]
    fmt = "  ".join(fmts)
    line = fmt.format(*headers)
    out = [line, "-" * len(line)]
    for row in rows:
        out.append(fmt.format(*[str(c) for c in row]))
    return "\n".join(out)


def run_python(args, python_path=None):
    """起一个子进程跑 python，返回 (returncode, combined_output)。"""
    import subprocess
    py = python_path or sys.executable
    proc = subprocess.run([py] + list(args), capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out
