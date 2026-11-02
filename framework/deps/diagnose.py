# -*- coding: utf-8 -*-
"""插件加载失败的「平台化」诊断。

把可通配化的报错（如 ``No module named X``、``cannot import name Y from X``、
原生扩展加载失败等）转成中文可操作提示 + 安装命令 + 原始消息，而不是只吐原始
traceback 文案。这是框架级能力，加载器 / 适配器 / Web UI 均可复用，而非一次性脚手架。

关键约束（来自 framework/deps）：框架安装依赖时强制使用 ``sys.executable -m pip``
（见 deps/__init__.py 的 auto_install_dependencies / pip_install_with_mirror），即「框架
当前运行的同一 Python 解释器」。因此这里给出的安装命令也用 ``sys.executable``，保证
提示装进正确的环境，而不是用户 PATH 上另一个 pip。
"""
import re
import sys

# ── 可通配化错误模式（按优先级匹配）────────────────────────────

# ModuleNotFoundError: No module named 'xxx' / ImportError: No module named xxx
_RE_NO_MODULE = re.compile(r"No module named\s+['\"]?([^'\"\s]+)['\"]?", re.IGNORECASE)

# ImportError: cannot import name 'Y' from 'X'（通常是版本不兼容 / API 变动）
_RE_CANNOT_IMPORT = re.compile(
    r"cannot import name\s+['\"]([^'\"]+)['\"]\s+from\s+['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)

# 原生扩展加载失败：Windows 的 DLL load failed / Linux 的 .so 加载失败
_RE_NATIVE = re.compile(
    r"(DLL load failed|cannot open shared object|\.so[^\n]*cannot open|\.pyd[^\n]*not found)",
    re.IGNORECASE,
)


def _pip_install_cmd(module):
    """与框架自身安装行为一致的命令：装进框架运行的同一 Python 环境。

    返回 ``<sys.executable> -m pip install <module>``。
    """
    return f"{sys.executable} -m pip install {module}"


def _pip_upgrade_cmd(module):
    return f"{sys.executable} -m pip install {module} --upgrade"


def classify_load_error(exc):
    """把插件加载期异常归类为已知模式。

    返回 dict：
        {
            'kind':   'missing_module' | 'import_member' | 'native_load' | None,
            'module': 相关顶层模块名（如 'requests'），无则 None,
            'hint':   中文可操作提示（含安装命令），无匹配时为空串,
            'raw':    原始错误消息（始终保留，保证透明可溯源）,
        }
    无论是否匹配，``raw`` 始终为原始消息。
    """
    raw = str(exc)
    out = {'kind': None, 'module': None, 'hint': '', 'raw': raw}

    m = _RE_NO_MODULE.search(raw)
    if m:
        top = m.group(1).split('.')[0]
        out['kind'] = 'missing_module'
        out['module'] = top
        out['hint'] = (
            f"检测到可能存在依赖缺失：模块「{top}」未安装。\n"
            f"请使用框架运行的同一 Python 环境安装（不要装到别的解释器）：\n"
            f"  {_pip_install_cmd(top)}\n"
            f"（若项目使用虚拟环境 / uv / pipx，请在该环境对应的解释器下执行安装）"
        )
        return out

    m = _RE_CANNOT_IMPORT.search(raw)
    if m:
        name, mod = m.group(1), m.group(2)
        out['kind'] = 'import_member'
        out['module'] = mod
        out['hint'] = (
            f"从「{mod}」导入「{name}」失败，通常是依赖版本不兼容或 API 变动。\n"
            f"请核对插件要求的版本约束，必要时升级：\n"
            f"  {_pip_upgrade_cmd(mod)}"
        )
        return out

    if _RE_NATIVE.search(raw):
        out['kind'] = 'native_load'
        out['hint'] = (
            "原生扩展（.pyd / .so）加载失败，通常是系统运行库缺失：\n"
            "  - Windows：请安装对应版本的 Visual C++ Redistributable；\n"
            "  - Linux：请安装该模块依赖的系统库（如 libGL、libssl 等）。\n"
            "补齐系统依赖后重试加载插件。"
        )
        return out

    return out


def diagnose_plugin_load_error(plugin_name, exc):
    """返回可直接写入日志的中文诊断文本（含友好提示 + 原始消息）。

    未匹配已知模式时返回统一中文前缀包裹的原始错误，保证日志呈现一致（平台化），
    而不是各插件各写各的英文 traceback 文案。
    """
    raw = str(exc)
    info = classify_load_error(exc)
    if info['kind']:
        return f"[{plugin_name}] 加载失败：{info['hint']}\n  原始错误：{raw}"
    return f"[{plugin_name}] 加载失败（未归类错误）：{raw}"
