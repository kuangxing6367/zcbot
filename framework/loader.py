"""
插件加载器
负责：发现插件目录、动态导入 main.py、调用 register(ctx)、1分钟心跳刷新
同时支持读取 plugin.yaml 配置文件（GitHub 更新源、配置项、文档）
支持读取 _conf_schema.json 配置 schema
"""
import importlib
import importlib.metadata
import importlib.util
import gc
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Dict, Optional

import psutil
import yaml

logger = logging.getLogger('zcbot')

# 仪表盘卡片执行线程池（共享，避免每次请求创建线程；慢卡片隔离在此池）
_cards_executor = None

# ── pip 安装工具（清华源 + 自动回退）──────────────────────────────

# 镜像源列表（按优先级，第一个是清华源，后续是回退）
_PIP_MIRRORS = [
    'https://pypi.tuna.tsinghua.edu.cn/simple',
    'https://mirrors.aliyun.com/pypi/simple',
    'https://pypi.douban.com/simple',
    'https://pypi.org/simple',  # 官方源（最后回退）
]


def pip_install_with_mirror(pip_exec, packages, timeout=120) -> dict:
    """
    使用清华源安装 pip 包，失败自动回退到下一个镜像源
    :param pip_exec: pip 可执行路径（如 sys.executable 或 venv 内的 pip）
    :param packages: 要安装的包列表（如 ['requests>=2.28']）、单个包名字符串、或 requirements 文件路径
    :param timeout: 单次安装超时（秒）
    :return: {'success': bool, 'mirror': str, 'error': str}
    """
    install_args = [pip_exec, '-m', 'pip', 'install']
    packages_list = []

    if isinstance(packages, str):
        pkg = packages
        # 判断是否为 requirements 文件路径（以 .txt 结尾的路径）
        if pkg.endswith('.txt') and os.path.isfile(pkg):
            install_args.extend(['-r', pkg])
        else:
            # 普通包名字符串（如 'requests>=2.28'）
            install_args.append(pkg)
        packages_list = [pkg]
    else:
        # 列表：逐项处理，识别 .txt 文件路径，转换为 -r 参数
        for pkg in packages:
            if isinstance(pkg, str) and pkg.endswith('.txt') and os.path.isfile(pkg):
                install_args.extend(['-r', pkg])
            else:
                install_args.append(pkg)
            packages_list.append(pkg)

    last_error = ''
    for mirror in _PIP_MIRRORS:
        try:
            cmd = install_args + ['-i', mirror, '--trusted-host', _get_host(mirror)]
            logger.info(f"pip 安装中（镜像: {mirror}）: {packages_list}")
            subprocess.check_call(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
            return {'success': True, 'mirror': mirror, 'error': ''}
        except subprocess.CalledProcessError as e:
            last_error = f"exit {e.returncode}"
            logger.warning(f"pip 安装失败（镜像 {mirror}）: {last_error}，尝试下一个镜像源...")
        except subprocess.TimeoutExpired:
            last_error = f"超时({timeout}s)"
            logger.warning(f"pip 安装超时（镜像 {mirror}）: {last_error}，尝试下一个镜像源...")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"pip 安装异常（镜像 {mirror}）: {last_error}")

    return {'success': False, 'mirror': '', 'error': last_error}


def pip_install_all(plugin_name: str, deps: list):
    """批量安装依赖到当前 Python 环境"""
    logger.info(f"[{plugin_name}] 安装依赖到当前环境: {', '.join(deps)}")
    for dep in deps:
        try:
            r = pip_install_with_mirror(sys.executable, dep, timeout=120)
            if r['success']:
                logger.info(f"[{plugin_name}] 依赖安装成功: {dep}")
            else:
                logger.warning(f"[{plugin_name}] 依赖安装失败: {dep} - {r.get('error')}")
        except Exception as e:
            logger.warning(f"[{plugin_name}] 依赖安装异常: {dep} - {e}")


def _get_host(url: str) -> str:
    """从镜像 URL 提取 host"""
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname
    except Exception:
        return ''


def pip_install_requirements(pip_exec, req_file, timeout=300) -> dict:
    """
    安装 requirements.txt，走清华源 + 回退
    :param pip_exec: pip 可执行路径
    :param req_file: requirements.txt 文件路径
    :param timeout: 超时（秒）
    :return: {'success': bool, 'mirror': str, 'error': str}
    """
    last_error = ''
    for mirror in _PIP_MIRRORS:
        try:
            cmd = [pip_exec, '-m', 'pip', 'install', '-r', req_file,
                   '-i', mirror, '--trusted-host', _get_host(mirror)]
            logger.info(f"pip 安装 requirements（镜像: {mirror}）: {req_file}")
            subprocess.check_call(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
            return {'success': True, 'mirror': mirror, 'error': ''}
        except subprocess.CalledProcessError as e:
            last_error = f"exit {e.returncode}"
            logger.warning(f"requirements 安装失败（镜像 {mirror}）: {last_error}")
        except subprocess.TimeoutExpired:
            last_error = f"超时({timeout}s)"
            logger.warning(f"requirements 安装超时（镜像 {mirror}）: {last_error}")
        except Exception as e:
            last_error = str(e)

    return {'success': False, 'mirror': '', 'error': last_error}


# ── 配置文件后缀定义（这些文件存放在 plugins_dat，而非 plugins） ──
# 后缀匹配（.txt 不自动归类，因为可能是数据文件/requirements.txt）
_CONFIG_FILE_EXTS = ('.yaml', '.yml', '.toml', '.cfg', '.ini', '.md')
# 明确的配置文件名（无论后缀，都归类为配置文件）
_CONFIG_FILE_NAMES = {
    'plugin.yaml', '_conf_schema.json', 'metadata.yaml',
    'README.md', 'README_zh.md', 'README_ru.md',
    'CHANGELOG.md', 'LICENSE',
}
# 排除名单：这些文件虽然后缀匹配，但属于代码/构建文件，跟代码走
_CODE_FILE_NAMES = {
    'requirements.txt', 'package.json', 'package-lock.json',
    'pyproject.toml', 'setup.cfg', 'tox.ini',
}

# ── 版本说明符解析 ────────────────────────────────────────────────

# 只提取包名部分（第一个非空格且不含运算符的连续单词）
_RE_PKG_NAME = re.compile(r'^([a-zA-Z0-9_.-]+)')
# 匹配简单的 单运算符+版本 格式，如 >=2.28, ==1.0.0
_RE_SIMPLE_SPEC = re.compile(r'\s*(>=|<=|!=|~=|==|>|<)\s*([\d.*]+)')


def _parse_version_spec(dep: str):
    """
    解析依赖版本说明符（宽松模式）
    只提取包名，版本约束如果无法解析则跳过版本检查并记录警告
    返回 (包名, 运算符, 版本号) 或 (包名, None, None)
    例: 'requests>=2.28'  → ('requests', '>=', '2.28')
        'numpy<2.0'       → ('numpy', '<', '2.0')
        'psutil'          → ('psutil', None, None)
        'requests~=2.28.0, <3.0' → ('requests', None, None)  # 复杂条件跳过
    """
    dep = dep.strip()
    m = _RE_PKG_NAME.match(dep)
    if not m:
        return (dep, None, None)
    pkg = m.group(1)

    spec_part = dep[len(pkg):].strip()
    if not spec_part:
        return (pkg, None, None)

    vm = _RE_SIMPLE_SPEC.match(spec_part)
    if vm:
        return (pkg, vm.group(1), vm.group(2))

    # 复杂格式（如 ~=3.0.0, <4 或带逗号的多条件）→ 记录警告，跳过版本检查
    logger.warning(
        f"依赖版本约束 '{dep}' 格式复杂，框架将跳过版本检查，"
        f"请手动确认兼容性：pip install '{dep}'"
    )
    return (pkg, None, None)


def _parse_ver(v: str):
    """版本字符串 → 可比较元组，如 '2.28.1' → (2, 28, 1)"""
    parts = []
    for p in v.split('.'):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(p)
    return tuple(parts)


def _check_version_compatible(installed: str, operator: str, required: str) -> bool:
    """检查已安装版本是否满足运算符要求"""
    if operator is None:
        return True
    iv = _parse_ver(installed)
    rv = _parse_ver(required.rstrip('.*'))
    rlen = len(rv)

    if operator == '==':
        return iv[:rlen] == rv
    elif operator == '>=':
        return iv >= rv
    elif operator == '<=':
        return iv <= rv
    elif operator == '>':
        return iv > rv
    elif operator == '<':
        return iv < rv
    elif operator == '!=':
        return iv[:rlen] != rv
    elif operator == '~=':
        # ~=3.0   → >=3.0, <4.0
        # ~=3.0.0 → >=3.0.0, <3.1.0
        if rlen == 1:
            return iv >= rv and iv < (rv[0] + 1,)
        elif rlen == 2:
            return iv >= rv and iv < (rv[0] + 1,)
        else:
            return iv >= rv and iv < (rv[0], rv[1] + 1)
    return True


def _parse_requirements_file(req_file: str) -> list:
    """
    解析 requirements.txt 文件，返回依赖项列表（去重、保留顺序）
    跳过空行、注释行和不规范的行
    """
    if not os.path.isfile(req_file):
        return []
    result = []
    seen = set()
    try:
        with open(req_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                # 跳过空行和注释
                if not line or line.startswith('#'):
                    continue
                # 跳过 -r / -e / --index-url 等 pip 选项行
                if line.startswith('-') or line.startswith('--'):
                    continue
                # 去掉行内注释（pkg # comment 形式）
                if ' #' in line:
                    line = line.split(' #', 1)[0].strip()
                if not line:
                    continue
                # 标准化为小写 key 做去重，保留原始写法
                norm = line.lower()
                if norm not in seen:
                    seen.add(norm)
                    result.append(line)
    except Exception as e:
        logger.warning(f"解析 requirements.txt 失败 [{req_file}]: {e}")
        return []
    return result


class PluginLoader:
    """插件加载器，管理插件生命周期"""

    def __init__(self, plugins_dir: str, framework, plugins_dat_dir: str = None):
        self.plugins_dir = plugins_dir
        self.plugins_dat_dir = plugins_dat_dir or os.path.join(
            os.path.dirname(plugins_dir.rstrip(os.sep)), 'data', 'plugins_dat'
        )
        self.framework = framework
        self.db = framework.db
        self._loaded_plugins: Dict[str, dict] = {}  # plugin_name -> {module, register_func, ...}
        self._lock = threading.Lock()
        self._missing_deps: Dict[str, list] = {}   # plugin_name -> [缺失的包列表]
        self._conflict_deps: Dict[str, list] = {}  # plugin_name -> [{name, required, installed}, ...]
        self._isolated_plugins: set = set()         # plugin_name -> 已启用隔离环境的插件
        self._memory_monitor_running = False
        self._memory_violations: Dict[str, int] = {}  # plugin_name -> 连续超限次数

        # ── 群级插件开关缓存 {group_id: {plugin_name: enabled}} ──
        self._group_plugin_cache = {}
        self._group_plugin_cache_time = 0
        self._group_cache_ttl = 30  # 缓存 30 秒

        # ── 插件文件 mtime 快照（心跳增量注册用）──
        self._plugin_mtimes: Dict[str, float] = {}

    def _read_requirements_txt(self, plugin_name: str) -> list:
        """
        读取插件代码目录下的 requirements.txt
        位置: {plugins_dir}/{plugin_name}/requirements.txt
        返回依赖列表（去重）
        """
        req_file = os.path.join(self.plugins_dir, plugin_name, 'requirements.txt')
        return _parse_requirements_file(req_file)

    def _get_merged_dependencies(self, plugin_name: str) -> list:
        """
        合并插件的所有依赖声明来源，返回去重后的依赖列表（保留顺序）
        合并顺序（优先级从低到高，同名以后续的版本约束为准）：
          1. plugins/<name>/requirements.txt           — 插件代码目录中的依赖文件
          2. plugins_dat/<name>/plugin.yaml → deps.python  — 配置目录的 yaml 声明
          3. plugins/<name>/plugin.yaml → deps.python      — 代码目录 yaml（作为 fallback）
        """
        merged = []
        seen_pkg = {}  # pkg_name(lower) → index in merged for overwrite

        # 1. 从代码目录 requirements.txt 读取
        for dep in self._read_requirements_txt(plugin_name):
            pkg, _, _ = _parse_version_spec(dep)
            key = pkg.lower()
            if key in seen_pkg:
                merged[seen_pkg[key]] = dep  # 覆盖为后续的版本约束
            else:
                seen_pkg[key] = len(merged)
                merged.append(dep)

        # 2. 从 plugins_dat 下的 plugin.yaml 读取
        yaml_dat = self.read_plugin_yaml(plugin_name)
        yaml_deps = yaml_dat.get('dependencies', {}).get('python', []) if isinstance(yaml_dat, dict) else []
        for dep in yaml_deps:
            if not isinstance(dep, str):
                continue
            dep = dep.strip()
            if not dep:
                continue
            pkg, _, _ = _parse_version_spec(dep)
            key = pkg.lower()
            if key in seen_pkg:
                merged[seen_pkg[key]] = dep
            else:
                seen_pkg[key] = len(merged)
                merged.append(dep)

        # 3. 从代码目录下的 plugin.yaml 读取（fallback，防止首次加载时 plugins_dat 没有 yaml）
        code_yaml_path = os.path.join(self.plugins_dir, plugin_name, 'plugin.yaml')
        if os.path.isfile(code_yaml_path):
            try:
                with open(code_yaml_path, 'r', encoding='utf-8') as f:
                    code_yaml = yaml.safe_load(f) or {}
                code_deps = code_yaml.get('dependencies', {}).get('python', []) if isinstance(code_yaml, dict) else []
                for dep in code_deps:
                    if not isinstance(dep, str):
                        continue
                    dep = dep.strip()
                    if not dep:
                        continue
                    pkg, _, _ = _parse_version_spec(dep)
                    key = pkg.lower()
                    if key in seen_pkg:
                        merged[seen_pkg[key]] = dep
                    else:
                        seen_pkg[key] = len(merged)
                        merged.append(dep)
            except Exception as e:
                logger.warning(f"[{plugin_name}] 读取代码目录 plugin.yaml 失败: {e}")

        return merged

    def check_dependencies(self, plugin_name: str) -> dict:
        """
        检查插件的 Python 依赖是否已安装，以及版本是否冲突
        合并所有依赖声明来源（requirements.txt + plugin.yaml）
        返回 {
            'ok': True/False,
            'missing': [缺失的包列表],
            'installed': [已安装的包列表],
            'conflicts': [{name, required, installed}],  # 版本冲突列表
            'has_conflict': True/False,
        }
        """
        deps = self._get_merged_dependencies(plugin_name)
        if not deps:
            return {'ok': True, 'missing': [], 'installed': [], 'conflicts': [], 'has_conflict': False}

        missing = []
        installed = []
        conflicts = []

        for dep in deps:
            pkg_name, operator, required_ver = _parse_version_spec(dep)
            import_name = pkg_name.replace('-', '_').replace('.', '_')

            # 检查包是否已安装
            installed_ver = None
            try:
                installed_ver = importlib.metadata.version(pkg_name)
            except importlib.metadata.PackageNotFoundError:
                try:
                    installed_ver = importlib.metadata.version(import_name)
                except importlib.metadata.PackageNotFoundError:
                    missing.append(dep)
                    continue

            # 包已安装，但版本不满足要求 → 冲突
            if operator and required_ver:
                if not _check_version_compatible(installed_ver, operator, required_ver):
                    conflicts.append({
                        'name': pkg_name,
                        'required': f'{operator}{required_ver}',
                        'installed': installed_ver,
                    })
                    continue

            installed.append(dep)

        has_conflict = len(conflicts) > 0

        return {
            'ok': len(missing) == 0 and not has_conflict,
            'missing': missing,
            'installed': installed,
            'conflicts': conflicts,
            'has_conflict': has_conflict,
        }

    def auto_install_dependencies(self, plugin_name: str) -> dict:
        """
        自动安装插件缺失的 Python 依赖
        只安装 missing 的包，版本冲突的包不自动覆盖
        返回 {'success': True/False, 'installed': [...], 'failed': [...], 'conflicts': [...]}
        """
        result = self.check_dependencies(plugin_name)
        if result['ok']:
            return {'success': True, 'installed': [], 'failed': [], 'conflicts': []}

        installed = []
        failed = []
        # 强制使用当前解释器，避免多 Python 环境安装到错误位置
        pip_exec = sys.executable
        for dep in result['missing']:
            try:
                logger.info(f"[{plugin_name}] 正在安装依赖: {dep}")
                r = pip_install_with_mirror(pip_exec, dep, timeout=120)
                if r['success']:
                    installed.append(dep)
                    logger.info(f"[{plugin_name}] 依赖安装成功: {dep}（镜像: {r['mirror']}）")
                else:
                    failed.append(dep)
                    logger.warning(f"[{plugin_name}] 依赖安装失败: {dep} - {r['error']}")
            except Exception as e:
                failed.append(dep)
                logger.warning(f"[{plugin_name}] 依赖安装失败: {dep} - {e}")

        return {
            'success': len(failed) == 0,
            'installed': installed,
            'failed': failed,
            'conflicts': result['conflicts'],
        }

    def _record_dep_status(self, plugin_name: str, missing: list, conflicts: list):
        """记录插件的依赖状态（缺失 + 冲突，供 Web UI 展示）"""
        with self._lock:
            if missing:
                self._missing_deps[plugin_name] = missing
            else:
                self._missing_deps.pop(plugin_name, None)
            if conflicts:
                self._conflict_deps[plugin_name] = conflicts
            else:
                self._conflict_deps.pop(plugin_name, None)

    def get_dep_status(self, plugin_name: str = None) -> dict:
        """获取依赖状态信息"""
        with self._lock:
            if plugin_name:
                return {
                    'missing': self._missing_deps.get(plugin_name, []),
                    'has_missing': plugin_name in self._missing_deps,
                    'conflicts': self._conflict_deps.get(plugin_name, []),
                    'has_conflict': plugin_name in self._conflict_deps,
                }
            return {
                'missing': dict(self._missing_deps),
                'conflicts': dict(self._conflict_deps),
            }

    def get_missing_deps(self, plugin_name: str) -> dict:
        """获取插件缺失依赖（Web UI 等调用）"""
        return self.get_dep_status(plugin_name)

    def install_missing_deps(self, plugin_name: str) -> dict:
        """
        一键安装插件缺失的依赖（基于全局环境），安装成功后清除缺失记录。
        版本冲突的依赖自动跳过（不覆盖全局包），冲突记录保留展示。
        """
        result = self.auto_install_dependencies(plugin_name)
        if result['success']:
            self._record_dep_status(plugin_name, [], result.get('conflicts', []))
        return result

    def _venv_dir(self, plugin_name: str) -> str:
        """插件虚拟环境目录（统一存放于 plugins_dat/<插件名>/.venv，与代码分离）"""
        return os.path.join(self._plugin_dat_dir(plugin_name), '.venv')

    def create_isolated_env(self, plugin_name: str) -> dict:
        """
        为插件创建隔离虚拟环境（手动触发，Web UI 点击「创建虚拟环境」调用）
        venv 创建在 plugins_dat/<插件名>/.venv，与插件代码目录分离。
        """
        # 确保 plugins_dat/<插件名> 目录存在
        self.ensure_plugins_dat_dir(plugin_name)
        venv_path = self._venv_dir(plugin_name)

        # 获取插件所有依赖（合并所有声明来源）
        deps = self._get_merged_dependencies(plugin_name)

        try:
            # 1. 创建虚拟环境
            logger.info(f"[{plugin_name}] 正在创建虚拟环境: {venv_path}")
            subprocess.check_call(
                [sys.executable, '-m', 'venv', venv_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )

            # 2. 安装依赖
            return self.install_deps_to_venv(plugin_name, deps, venv_path)

        except subprocess.TimeoutExpired:
            return {'success': False, 'error': '创建 venv 超时（60s）'}
        except Exception as e:
            logger.error(f"[{plugin_name}] 创建隔离环境失败: {e}")
            return {'success': False, 'error': str(e)}

    def install_deps_to_venv(self, plugin_name: str, deps: list,
                              venv_path: str = None) -> dict:
        """
        将依赖安装到插件的隔离虚拟环境中
        :param plugin_name: 插件名
        :param deps: 依赖列表
        :param venv_path: venv 路径，None 则使用 plugins_dat/<插件名>/.venv
        :return: {'success': bool, 'venv_path': str, 'python': str, 'installed': list, 'failed': list}
        """
        if venv_path is None:
            venv_path = self._venv_dir(plugin_name)

        if not os.path.isdir(venv_path):
            return {'success': False, 'error': f'venv 不存在: {venv_path}'}

        # 获取 venv 内的 pip/python
        if sys.platform == 'win32':
            pip_path = os.path.join(venv_path, 'Scripts', 'pip.exe')
            python_path = os.path.join(venv_path, 'Scripts', 'python.exe')
        else:
            pip_path = os.path.join(venv_path, 'bin', 'pip')
            python_path = os.path.join(venv_path, 'bin', 'python')

        if not os.path.isfile(python_path):
            return {'success': False, 'error': 'venv 中未找到 python'}

        # 安装依赖
        installed = []
        failed = []
        for dep in deps:
            try:
                logger.info(f"[{plugin_name}] 隔离环境安装依赖: {dep}")
                # 注意：pip_install_with_mirror 使用 {exec} -m pip install 模式，
                # 所以必须传 venv 的 python 路径，而不是 pip 路径（否则会变成 pip -m pip install 这样的错误命令）
                r = pip_install_with_mirror(python_path, dep, timeout=120)
                if r['success']:
                    installed.append(dep)
                else:
                    failed.append(dep)
                    logger.warning(f"[{plugin_name}] 隔离环境安装依赖失败: {dep} - {r.get('error')}")
            except Exception as e:
                failed.append(dep)
                logger.warning(f"[{plugin_name}] 隔离环境安装依赖失败: {dep} - {e}")

        success = len(failed) == 0
        if success:
            with self._lock:
                self._isolated_plugins.add(plugin_name)
                self._conflict_deps.pop(plugin_name, None)
                if not self._missing_deps.get(plugin_name):
                    self._missing_deps.pop(plugin_name, None)
            logger.info(f"[{plugin_name}] 隔离环境安装完成: {venv_path}")

        return {
            'success': success,
            'venv_path': venv_path,
            'python': python_path,
            'installed': installed,
            'failed': failed,
        }

    def remove_isolated_env(self, plugin_name: str) -> dict:
        """删除插件的隔离虚拟环境（plugins_dat/<插件名>/.venv）"""
        venv_path = self._venv_dir(plugin_name)
        if not os.path.isdir(venv_path):
            return {'success': True, 'msg': '无隔离环境'}
        try:
            shutil.rmtree(venv_path, ignore_errors=True)
            with self._lock:
                self._isolated_plugins.discard(plugin_name)
            logger.info(f"[{plugin_name}] 隔离环境已删除: {venv_path}")
            return {'success': True, 'msg': '隔离环境已删除'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def scan_venv_usage(self) -> dict:
        """
        扫描所有插件的 .venv 隔离环境（plugins_dat/<插件名>/.venv），返回磁盘占用信息
        用于运维监控，防止香橙派等低磁盘设备空间被虚拟环境耗尽
        """
        result = {
            'total_size_mb': 0,
            'venv_count': 0,
            'details': [],
        }
        if not os.path.isdir(self.plugins_dat_dir):
            return result

        for name in os.listdir(self.plugins_dat_dir):
            venv_path = os.path.join(self.plugins_dat_dir, name, '.venv')
            if not os.path.isdir(venv_path):
                continue

            try:
                size_bytes = 0
                for root, dirs, files in os.walk(venv_path):
                    for f in files:
                        fp = os.path.join(root, f)
                        try:
                            size_bytes += os.path.getsize(fp)
                        except OSError:
                            pass
                size_mb = round(size_bytes / 1024 / 1024, 1)
                result['total_size_mb'] += size_mb
                result['venv_count'] += 1
                result['details'].append({
                    'plugin_name': name,
                    'venv_path': venv_path,
                    'size_mb': size_mb,
                })
            except Exception as e:
                logger.warning(f"扫描 .venv 失败 [{name}]: {e}")

        result['total_size_mb'] = round(result['total_size_mb'], 1)
        return result

    def discover(self) -> list:
        """扫描插件目录，返回所有插件目录名列表"""
        plugins = []
        if not os.path.isdir(self.plugins_dir):
            os.makedirs(self.plugins_dir, exist_ok=True)
            return plugins

        for name in os.listdir(self.plugins_dir):
            main_path = os.path.join(self.plugins_dir, name, 'main.py')
            if os.path.isfile(main_path):
                plugins.append(name)
        return plugins

    def _plugin_dat_dir(self, plugin_name: str) -> str:
        """获取插件的数据/配置目录路径"""
        return os.path.join(self.plugins_dat_dir, plugin_name)

    def _plugin_code_dir(self, plugin_name: str) -> str:
        """获取插件的代码目录路径"""
        return os.path.join(self.plugins_dir, plugin_name)

    def ensure_plugins_dat_dir(self, plugin_name: str):
        """确保插件的 plugins_dat 子目录存在"""
        dat_dir = self._plugin_dat_dir(plugin_name)
        if not os.path.isdir(dat_dir):
            os.makedirs(dat_dir, exist_ok=True)
        return dat_dir

    @staticmethod
    def _is_config_file(filename: str) -> bool:
        """判断文件是否属于配置/数据文件（应存放在 plugins_dat）"""
        lower = filename.lower()
        # 排除名单优先（代码/构建文件跟代码走）
        if lower in _CODE_FILE_NAMES:
            return False
        # 明确的配置文件名
        if lower in _CONFIG_FILE_NAMES:
            return True
        # 后缀匹配
        return lower.endswith(_CONFIG_FILE_EXTS)

    def split_installed_files(self, plugin_name: str):
        """
        将 plugins/<name>/ 下的配置文件迁移到 plugins_dat/<name>/
        在插件上传/更新后调用，确保代码和配置分离
        """
        code_dir = self._plugin_code_dir(plugin_name)
        dat_dir = self.ensure_plugins_dat_dir(plugin_name)

        if not os.path.isdir(code_dir):
            return

        for name in os.listdir(code_dir):
            fpath = os.path.join(code_dir, name)
            if not os.path.isfile(fpath):
                continue
            if self._is_config_file(name):
                dest = os.path.join(dat_dir, name)
                # 如果 plugins_dat 已有同名文件（用户之前修改过），不覆盖
                if not os.path.exists(dest):
                    shutil.move(fpath, dest)
                    logger.debug(f"[{plugin_name}] 配置文件已迁移: {name}")
                else:
                    # plugins_dat 已存在，删除 plugins 下的副本
                    os.remove(fpath)
                    logger.debug(f"[{plugin_name}] 配置文件已存在于 plugins_dat，跳过: {name}")

    def migrate_legacy_configs(self):
        """
        迁移旧版插件：将 plugins/ 下所有插件的配置文件迁移到 plugins_dat/
        在框架启动时调用一次，兼容升级
        """
        if not os.path.isdir(self.plugins_dir):
            return
        migrated = 0
        for name in os.listdir(self.plugins_dir):
            plugin_dir = os.path.join(self.plugins_dir, name)
            if not os.path.isdir(plugin_dir):
                continue
            # 检查是否有配置文件需要迁移
            has_config = any(
                os.path.isfile(os.path.join(plugin_dir, f)) and self._is_config_file(f)
                for f in os.listdir(plugin_dir)
            )
            if has_config:
                self.split_installed_files(name)
                migrated += 1
        if migrated > 0:
            logger.info(f"已将 {migrated} 个插件的配置文件迁移到 plugins_dat/")

    def read_plugin_yaml(self, plugin_name: str) -> dict:
        """
        读取插件的 plugin.yaml 配置文件
        读取顺序（优先级从高到低）：
          1. plugins_dat/<name>/plugin.yaml    — 用户数据目录（配置会被迁移到此）
          2. plugins/<name>/plugin.yaml        — 代码目录（首次加载或未迁移时的 fallback）
        返回 dict，如果不存在返回空 dict
        """
        # 1. 优先读取 plugins_dat（用户可编辑版本）
        yaml_path = os.path.join(self.plugins_dat_dir, plugin_name, 'plugin.yaml')
        if os.path.isfile(yaml_path):
            try:
                with open(yaml_path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning(f"[{plugin_name}] 读取 plugins_dat/plugin.yaml 失败: {e}，回退到代码目录")

        # 2. fallback: 读取代码目录下的 plugin.yaml（首次加载未迁移时）
        yaml_path = os.path.join(self.plugins_dir, plugin_name, 'plugin.yaml')
        if os.path.isfile(yaml_path):
            try:
                with open(yaml_path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning(f"[{plugin_name}] 读取代码目录 plugin.yaml 失败: {e}")
                return {}

        return {}

    def read_config_schema(self, plugin_name: str) -> dict:
        """
        读取插件的 _conf_schema.json 配置 schema（从 plugins_dat 读取）
        返回 {key: {type, description, default, hint, options, ...}} 格式
        如果不存在返回空 dict
        """
        schema_path = os.path.join(self.plugins_dat_dir, plugin_name, '_conf_schema.json')
        if not os.path.isfile(schema_path):
            return {}
        try:
            with open(schema_path, 'r', encoding='utf-8') as f:
                return json.loads(f.read())
        except Exception as e:
            logger.warning(f"[{plugin_name}] 读取 _conf_schema.json 失败: {e}")
            return {}

    def init_plugin_configs(self, plugin_name: str):
        """
        根据 _conf_schema.json 初始化插件配置到 plugin_configs 表
        仅在配置项不存在时插入默认值，不覆盖用户已修改的配置
        """
        schema = self.read_config_schema(plugin_name)
        if not schema:
            return

        for key, spec in schema.items():
            if not isinstance(spec, dict):
                continue
            default = spec.get('default')
            # 检查是否已存在
            try:
                existing = self.db.query_one(
                    "SELECT id FROM plugin_configs WHERE plugin_name = %s AND config_key = %s",
                    (plugin_name, key)
                )
                if not existing:
                    # 插入默认值
                    config_value = json.dumps(default, ensure_ascii=False) if default is not None else 'null'
                    self.db.execute(
                        "INSERT INTO plugin_configs (plugin_name, config_key, config_value) "
                        "VALUES (%s, %s, %s)",
                        (plugin_name, key, config_value)
                    )
            except Exception as e:
                logger.error(f"[{plugin_name}] 初始化配置 {key} 失败: {e}")

    def get_plugin_config_files(self, plugin_name: str) -> list:
        """获取插件数据目录（plugins_dat）下所有配置/文档文件列表"""
        dat_dir = self._plugin_dat_dir(plugin_name)
        if not os.path.isdir(dat_dir):
            return []
        result = []
        for name in os.listdir(dat_dir):
            fpath = os.path.join(dat_dir, name)
            if os.path.isfile(fpath) and name.endswith(_CONFIG_FILE_EXTS):
                size = os.path.getsize(fpath)
                result.append({'name': name, 'size': size})
        return result

    def read_plugin_file(self, plugin_name: str, filename: str) -> str:
        """读取插件数据目录（plugins_dat）下的指定文件内容"""
        # 安全检查：防止路径穿越
        if '..' in filename or '/' in filename or '\\' in filename:
            raise ValueError('非法文件名')
        fpath = os.path.join(self.plugins_dat_dir, plugin_name, filename)
        if not os.path.isfile(fpath):
            raise FileNotFoundError(f'文件不存在: {filename}')
        with open(fpath, 'r', encoding='utf-8') as f:
            return f.read()

    def _add_venv_to_path(self, plugin_name: str) -> bool:
        """
        将插件的 venv site-packages 加入 sys.path，使其依赖在主进程中可见。
        venv 位于 plugins_dat/<插件名>/.venv。
        返回 True 表示 venv 可用，False 表示 venv 不可用。
        """
        venv_path = self._venv_dir(plugin_name)
        if not os.path.isdir(venv_path):
            return False

        # 计算 site-packages 路径
        if sys.platform == 'win32':
            site_pkg = os.path.join(venv_path, 'Lib', 'site-packages')
        else:
            # 先找 python3.x/site-packages
            import glob as _glob
            python_dirs = _glob.glob(os.path.join(venv_path, 'lib', 'python*'))
            site_pkg = os.path.join(python_dirs[0], 'site-packages') if python_dirs else None

        if not site_pkg or not os.path.isdir(site_pkg):
            return False

        if site_pkg not in sys.path:
            sys.path.insert(0, site_pkg)
        return True

    def _load_plugin_submodule(self, plugin_name: str, mod_name: str, file_path: str):
        """
        加载插件的一个顶层子模块，全名带插件前缀（plugin_<插件名>_<模块名>）
        并将短名注册到 sys.modules，保证插件 main.py 的绝对导入（import api / from ban_word import X）
        命中本插件自己的模块，避免多个插件的同名模块互相污染。
        """
        full_name = f"plugin_{plugin_name}_{mod_name}"
        try:
            spec = importlib.util.spec_from_file_location(full_name, file_path)
            if spec is None or spec.loader is None:
                return
            module = importlib.util.module_from_spec(spec)
            sys.modules[full_name] = module
            spec.loader.exec_module(module)
            # 短名覆盖：main.py 的 'import api' / 'from ban_word import X' 在导入时绑定，
            # 后续其他插件覆盖短名不影响本插件已绑定的引用
            sys.modules[mod_name] = module
        except Exception as e:
            logger.warning(f"[{plugin_name}] 子模块 {mod_name} 预加载失败（回退到全局查找）: {e}")

    def _preload_plugin_submodules(self, plugin_name: str, plugin_path: str):
        """
        预加载插件目录下的顶层子模块（.py 文件与包目录），实现同名模块短名隔离。
        解决多个插件存在同名模块（如 ban_word.py / db.py / core/）时，
        后加载插件从 sys.modules 命中其他插件模块导致的 ImportError。
        """
        try:
            entries = os.listdir(plugin_path)
        except OSError:
            return

        # 先加载顶层 .py 模块，再加载包目录（包内可能绝对导入顶层模块）
        py_files = []
        pkg_dirs = []
        for fname in entries:
            fpath = os.path.join(plugin_path, fname)
            if (os.path.isfile(fpath) and fname.endswith('.py')
                    and fname not in ('main.py', '__init__.py')):
                py_files.append((fname[:-3], fpath))
            elif (os.path.isdir(fpath)
                    and os.path.isfile(os.path.join(fpath, '__init__.py'))):
                pkg_dirs.append((fname, os.path.join(fpath, '__init__.py')))

        for mod_name, fpath in py_files:
            self._load_plugin_submodule(plugin_name, mod_name, fpath)
        for mod_name, fpath in pkg_dirs:
            self._load_plugin_submodule(plugin_name, mod_name, fpath)

    def load_plugin(self, plugin_name: str) -> bool:
        """加载单个插件，返回是否成功"""
        plugin_path = os.path.join(self.plugins_dir, plugin_name)

        # 将插件目录加入 sys.path
        if plugin_path not in sys.path:
            sys.path.insert(0, plugin_path)

        # 读取插件配置文件
        yaml_data = self.read_plugin_yaml(plugin_name)

        # ====== 依赖检查 + 自动安装（基于全局环境） ======
        # 说明：框架不再为插件自动创建/重建隔离虚拟环境；
        # 依赖自动安装到全局环境，存在版本冲突的依赖自动跳过（不覆盖全局包），
        # 如需隔离请手动点击「创建虚拟环境」。
        dep_result = self.check_dependencies(plugin_name)
        if dep_result.get('missing'):
            pip_install_all(plugin_name, dep_result['missing'])

            # 重新检查依赖
            dep_result = self.check_dependencies(plugin_name)
            if dep_result.get('missing'):
                missing = ', '.join(dep_result['missing'])
                logger.warning(f"[{plugin_name}] 仍有缺失依赖: {missing}，尝试加载...")

        # 记录依赖状态供 Web UI 展示
        self._record_dep_status(
            plugin_name,
            dep_result.get('missing', []),
            dep_result.get('conflicts', [])
        )

        # ====== 动态导入 main.py（带重试）======
        for attempt in range(2):  # 最多重试1次
            try:
                # 预加载插件顶层子模块（同名模块短名隔离，避免多插件互相污染 sys.modules）
                self._preload_plugin_submodules(plugin_name, plugin_path)

                spec = importlib.util.spec_from_file_location(
                    f"plugin_{plugin_name}",
                    os.path.join(plugin_path, 'main.py')
                )
                if spec is None or spec.loader is None:
                    logger.error(f"[{plugin_name}] 导入失败: spec 为空")
                    return False

                module = importlib.util.module_from_spec(spec)
                # 注册到 sys.modules（import 机制要求；与卸载清理 loader.py 的
                # sys.modules.pop(f"plugin_{plugin_name}") 对应）
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)

                # 检查 register 函数
                if not hasattr(module, 'register'):
                    logger.error(f"[{plugin_name}] 缺少 register(ctx) 函数")
                    return False

                register_func = getattr(module, 'register')
                if not callable(register_func):
                    logger.error(f"[{plugin_name}] register 不可调用")
                    return False

                # 读取元数据
                meta = getattr(module, '__plugin_meta__', {})
                plugin_meta = {
                    'name': meta.get('name', plugin_name),
                    'version': meta.get('version', '0.0.0'),
                    'author': meta.get('author', 'unknown'),
                    'desc': meta.get('desc', ''),
                    'priority': meta.get('priority', 50),
                }

                # 读取 plugin.yaml 覆盖元数据
                if yaml_data:
                    if 'version' in yaml_data:
                        plugin_meta['version'] = yaml_data['version']
                    if 'author' in yaml_data:
                        plugin_meta['author'] = yaml_data['author']
                    if 'description' in yaml_data:
                        plugin_meta['desc'] = yaml_data['description']
                    if 'priority' in yaml_data:
                        plugin_meta['priority'] = yaml_data['priority']

                with self._lock:
                    self._loaded_plugins[plugin_name] = {
                        'module': module,
                        'register_func': register_func,
                        'meta': plugin_meta,
                        'priority': plugin_meta['priority'],
                        'path': plugin_path,
                        'yaml': yaml_data,
                    }

                self._upsert_plugin_db(plugin_name, plugin_meta)
                self.init_plugin_configs(plugin_name)
                # 记录文件快照，避免首个心跳周期重复注册
                self._plugin_mtimes[plugin_name] = self._snapshot_mtime(plugin_name)

                logger.info(f"[{plugin_name}] 加载成功 v{plugin_meta['version']}")
                return True

            except ImportError as e:
                logger.warning(f"[{plugin_name}] 导入失败（第{attempt + 1}次）: {e}")
                if attempt == 0:
                    # 第一次失败：尝试重新安装缺失依赖（全局环境）再试一次
                    logger.info(f"[{plugin_name}] 尝试重新安装缺失依赖...")
                    dep_result2 = self.check_dependencies(plugin_name)
                    if dep_result2.get('missing'):
                        pip_install_all(plugin_name, dep_result2['missing'])
                    continue
                else:
                    logger.error(
                        f"[{plugin_name}] 加载失败，依赖可能未正确安装\n"
                        f"  请检查: pip install {' '.join(dep_result.get('missing', []))}\n"
                        f"  或在 Web UI 插件管理页查看详情"
                    )
                    # 更新 DB 状态为 error
                    try:
                        self.db.execute(
                            "UPDATE plugins SET status='error', has_register=0 WHERE plugin_name=%s",
                            (plugin_name,)
                        )
                    except Exception:
                        pass
                    return False

            except Exception as e:
                logger.error(f"[{plugin_name}] 加载失败: {e}", exc_info=True)
                # 更新 DB 状态为 error
                try:
                    self.db.execute(
                        "UPDATE plugins SET status='error', has_register=0 WHERE plugin_name=%s",
                        (plugin_name,)
                    )
                except Exception:
                    pass
                return False

    def _upsert_plugin_db(self, plugin_name: str, meta: dict):
        """写入/更新插件信息到 plugins 表"""
        try:
            existing = self.db.query_one(
                "SELECT id FROM plugins WHERE plugin_name = %s", (plugin_name,)
            )
            if existing:
                self.db.execute(
                    "UPDATE plugins SET version=%s, author=%s, description=%s, priority=%s, "
                    "has_register=1, status='running', loaded_at=NOW() WHERE plugin_name=%s",
                    (meta['version'], meta['author'], meta['desc'], meta['priority'], plugin_name)
                )
            else:
                self.db.execute(
                    "INSERT INTO plugins (plugin_name, version, author, description, priority, "
                    "has_register, status, loaded_at) VALUES (%s,%s,%s,%s,%s,1,'running',NOW())",
                    (plugin_name, meta['version'], meta['author'], meta['desc'], meta['priority'])
                )
        except Exception as e:
            logger.error(f"写入插件数据库失败 [{plugin_name}]: {e}")

    def register_commands(self, plugin_name: str) -> bool:
        """
        调用插件的 register(ctx)，收集其注册的命令
        由心跳或加载时调用
        """
        with self._lock:
            info = self._loaded_plugins.get(plugin_name)
            if not info:
                return False

        from framework.ctx import PluginContext
        ctx = PluginContext(plugin_name, self.framework)

        # 将 ctx 注入到插件模块的全局变量中
        # 这样插件的处理函数可以直接使用 ctx.api() 等
        module = info['module']
        module.ctx = ctx

        try:
            info['register_func'](ctx)
        except Exception as e:
            logger.error(f"[{plugin_name}] register(ctx) 执行异常: {e}", exc_info=True)
            return False

        # 获取注册的命令和任务
        commands = ctx._get_commands()
        tasks = ctx._get_tasks()
        dashboard_cards = ctx._get_dashboard_cards()

        # 注册原始消息处理器（收到原始消息事件，可选择性接管）
        raw_handlers = ctx._get_raw_message_handlers()
        if raw_handlers:
            _priority = info.get('priority', 50)
            for _h in raw_handlers:
                self.framework.register_raw_message_handler(plugin_name, _h, _priority)

        # 收集 WebUI 群组/用户管理页插件扩展
        group_exts = ctx._get_group_extensions()
        if group_exts:
            with self._lock:
                info['group_extensions'] = group_exts
        user_exts = ctx._get_user_extensions()
        if user_exts:
            with self._lock:
                info['user_extensions'] = user_exts

        # 写入 commands 表
        if commands:
            self._sync_commands(plugin_name, commands)

        # 注册定时任务
        if tasks:
            self._sync_tasks(plugin_name, tasks)

        # 存储仪表盘卡片
        if dashboard_cards:
            self._sync_dashboard_cards(plugin_name, dashboard_cards)

        logger.info(f"[{plugin_name}] 注册完成: {len(commands)} 命令, {len(tasks)} 定时任务")

        # 生命周期钩子：插件首次加载完成后触发一次（重载会重新触发）
        try:
            if not info.get('hook_loaded'):
                on_loaded = getattr(module, 'on_loaded', None)
                if callable(on_loaded):
                    on_loaded(ctx)
                info['hook_loaded'] = True
        except Exception as e:
            logger.error(f"[{plugin_name}] on_loaded 钩子异常: {e}")

        # 命令已写入 DB，让路由表立即重建（热路径内存快照要求一致）
        try:
            self.framework.router._invalidate_cache()
        except Exception:
            pass
        return True

    def _sync_commands(self, plugin_name: str, commands: list):
        """
        同步命令到数据库
        心跳策略：INSERT ... ON DUPLICATE KEY UPDATE 保持 ID 不变
        保留用户在 Web 端修改的 alias/description/is_active 覆盖（通过 handler_name 匹配回填）
        注意：is_dynamic 标记仅表示命令由插件以 dynamic=True 注册，不影响同步策略
              真正的动态命令（关键词回复）存储在 dynamic_commands 表，不受此处影响
        """
        try:
            # 查询当前数据库中所有命令的用户覆盖（按 handler_name 索引）
            existing_overrides = {}
            try:
                rows = self.db.query(
                    "SELECT handler, alias, description, is_active FROM commands "
                    "WHERE plugin_name = %s",
                    (plugin_name,)
                )
                for r in rows:
                    existing_overrides[r['handler']] = {
                        'alias': r.get('alias'),
                        'description': r.get('description'),
                        'is_active': r.get('is_active', 1),
                    }
            except Exception:
                pass

            # INSERT ... ON DUPLICATE KEY UPDATE 保持 ID 不变
            if commands:
                sql = (
                    "INSERT INTO commands (plugin_name, pattern, alias, description, "
                    "priority, handler, is_dynamic, require_level, is_active) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE "
                    "pattern = VALUES(pattern), "
                    "alias = VALUES(alias), "
                    "description = VALUES(description), "
                    "priority = VALUES(priority), "
                    "is_dynamic = VALUES(is_dynamic), "
                    "require_level = VALUES(require_level), "
                    "is_active = VALUES(is_active)"
                )
                params = []
                for c in commands:
                    handler_name = c['handler_name']
                    override = existing_overrides.get(handler_name, {})
                    # 优先使用用户在 Web 端设置的 alias，否则用代码注册的 alias
                    final_alias = override.get('alias') if override.get('alias') is not None else c.get('alias')
                    final_desc = override.get('description') if override.get('description') is not None else c.get('description')
                    final_active = override.get('is_active', 1)
                    params.append((
                        c['plugin_name'], c['pattern'], final_alias, final_desc,
                        c['priority'], handler_name, c.get('is_dynamic', 0),
                        c.get('require_level', ''), final_active
                    ))
                self.db.execute_many(sql, params)
        except Exception as e:
            logger.error(f"[{plugin_name}] 同步命令失败: {e}")

    def _sync_tasks(self, plugin_name: str, tasks: list):
        """同步定时任务到数据库和调度器"""
        try:
            # 先移除调度器中的旧任务（避免僵尸残留 + 重复添加报错）
            self.framework.scheduler.remove_plugin_tasks(plugin_name)

            # 删除旧任务
            self.db.execute(
                "DELETE FROM tasks WHERE plugin_name = %s", (plugin_name,)
            )
            # 插入新任务
            for t in tasks:
                task_id = self.db.insert(
                    "INSERT INTO tasks (plugin_name, cron_expression, handler, description) VALUES (%s,%s,%s,%s)",
                    (t['plugin_name'], t['cron_expression'], t['handler_name'], t['description'])
                )
                t['id'] = task_id
                # 注册到调度器
                self.framework.scheduler.add_plugin_task(t)

        except Exception as e:
            logger.error(f"[{plugin_name}] 同步任务失败: {e}")

    def _snapshot_mtime(self, plugin_name: str) -> float:
        """计算插件目录下所有 .py 文件的最大修改时间，用于变更检测"""
        plugin_path = os.path.join(self.plugins_dir, plugin_name)
        if not os.path.isdir(plugin_path):
            return -1.0
        latest = 0.0
        try:
            for root, _dirs, files in os.walk(plugin_path):
                for f in files:
                    if f.endswith('.py'):
                        try:
                            latest = max(latest, os.path.getmtime(os.path.join(root, f)))
                        except OSError:
                            pass
        except OSError:
            pass
        return latest

    def heartbeat_register(self):
        """
        心跳检查：仅对文件发生变化的插件重新 register(ctx)（增量注册）
        避免定时全量重建定时任务/命令带来的副作用与 DB 写放大
        """
        with self._lock:
            plugin_names = list(self._loaded_plugins.keys())

        changed = []
        for name in plugin_names:
            snap = self._snapshot_mtime(name)
            if snap == self._plugin_mtimes.get(name):
                continue
            try:
                self.register_commands(name)
                self._plugin_mtimes[name] = snap
                changed.append(name)
                logger.debug(f"[{name}] 心跳增量注册完成")
            except Exception as e:
                logger.error(f"[{name}] 心跳注册异常: {e}")

        # 心跳后使路由缓存失效（命令可能有变化）
        if changed:
            try:
                self.framework.router._invalidate_cache()
            except Exception:
                pass
        # 无论是否发生变化，心跳后也刷新一次路由表（兜底：DB 与内存对齐）
        try:
            self.framework.router._invalidate_cache()
        except Exception:
            pass

    def load_all(self) -> list:
        """加载所有已发现插件，返回成功列表"""
        discovered = self.discover()
        success = []
        for name in discovered:
            if self.load_plugin(name):
                success.append(name)
        # 启动内存监控
        self._start_memory_monitor()
        return success

    def _start_memory_monitor(self):
        """
        启动内存监控线程（每 3 秒采样一次）
        监控进程总内存和估算每个插件模块的内存占用
        连续两次超过阈值则自动卸载插件
        """
        if self._memory_monitor_running:
            return
        self._memory_monitor_running = True

        cfg = self.framework.config.get('plugin', {})
        max_mb = cfg.get('max_memory_mb', 64)

        def monitor():
            process = psutil.Process(os.getpid())

            while self._memory_monitor_running:
                threading.Event().wait(3)

                try:
                    # 进程级内存监控
                    proc_mem = process.memory_info().rss / 1024 / 1024
                    if proc_mem > max_mb * 1.5:  # 进程总内存超过 1.5 倍阈值
                        logger.warning(
                            f"[内存监控] 进程内存 {proc_mem:.1f}MB 超过警戒线 "
                            f"({max_mb * 1.5:.0f}MB)，可能存在插件泄漏"
                        )

                    # 逐个插件粗略估计内存（通过模块全局变量大小）
                    with self._lock:
                        for name, info in list(self._loaded_plugins.items()):
                            try:
                                module = info['module']
                                # 估算：模块的 __dict__ 里所有对象大小之和
                                module_size = sum(
                                    sys.getsizeof(v) for v in
                                    vars(module).values()
                                    if not v.__class__.__name__.startswith(('module', 'function', 'type'))
                                ) / 1024 / 1024

                                if module_size > max_mb:
                                    count = self._memory_violations.get(name, 0) + 1
                                    self._memory_violations[name] = count
                                    if count >= 2:
                                        logger.error(
                                            f"[内存监控] [{name}] 连续 {count} 次超限 "
                                            f"({module_size:.1f}MB > {max_mb}MB)，自动卸载"
                                        )
                                        # 异步卸载（不在此线程内执行耗时操作）
                                        threading.Thread(
                                            target=self.unload_plugin,
                                            args=(name,),
                                            daemon=True,
                                        ).start()
                                    else:
                                        logger.warning(
                                            f"[内存监控] [{name}] 内存使用 {module_size:.1f}MB "
                                            f"超过限制 {max_mb}MB（第 {count} 次警告）"
                                        )
                                else:
                                    # 恢复正常，清除违规计数
                                    self._memory_violations.pop(name, None)

                            except Exception:
                                pass  # 单个插件估算失败不影响其他

                except Exception:
                    pass  # 监控异常不干扰主流程

        t = threading.Thread(target=monitor, daemon=True, name="memory_monitor")
        t.start()
        logger.info(f"内存监控线程已启动 (采样间隔 3s, 单插件上限 {max_mb}MB)")

    def unload_plugin(self, plugin_name: str):
        """卸载插件"""
        with self._lock:
            info = self._loaded_plugins.pop(plugin_name, None)
            if not info:
                return

        try:
            # 调用 on_unload（如果存在）
            module = info['module']
            if hasattr(module, 'on_unload') and callable(module.on_unload):
                module.on_unload()
        except Exception as e:
            logger.warning(f"[{plugin_name}] on_unload 异常: {e}")

        # 清理调度器中的定时任务
        try:
            self.framework.scheduler.remove_plugin_tasks(plugin_name)
        except Exception as e:
            logger.warning(f"[{plugin_name}] 清理调度器任务失败: {e}")

        # 清理数据库
        try:
            self.db.execute("DELETE FROM commands WHERE plugin_name = %s", (plugin_name,))
            self.db.execute("DELETE FROM tasks WHERE plugin_name = %s", (plugin_name,))
            # 注意：不删除 plugin_configs —— 卸载/重载/更新/禁用都应保留用户配置，
            # 只有真正删除插件（delete_plugin）时才清配置
            self.db.execute("UPDATE plugins SET status='stopped', has_register=0 WHERE plugin_name=%s", (plugin_name,))
        except Exception as e:
            logger.error(f"[{plugin_name}] 卸载清理失败: {e}")

        # 清理缺失依赖记录
        with self._lock:
            self._missing_deps.pop(plugin_name, None)
            self._conflict_deps.pop(plugin_name, None)
            self._isolated_plugins.discard(plugin_name)

        # 移除事件订阅
        self.framework.event_bus.unsubscribe_plugin(plugin_name)

        # 移除原始消息处理器
        try:
            self.framework.unregister_raw_message_handlers(plugin_name)
        except Exception as e:
            logger.warning(f"[{plugin_name}] 清理原始消息处理器失败: {e}")

        # 清理 sys.modules：删除该插件目录下的所有模块（含短名模块，避免热重载污染）
        try:
            plugin_path = info.get('path') or os.path.join(self.plugins_dir, plugin_name)
            abs_plugin = os.path.abspath(plugin_path)
            for mod_name in list(sys.modules):
                mod = sys.modules.get(mod_name)
                mod_file = getattr(mod, '__file__', '') or ''
                if mod_file and os.path.abspath(mod_file).startswith(abs_plugin + os.sep):
                    sys.modules.pop(mod_name, None)
            sys.modules.pop(f"plugin_{plugin_name}", None)
        except Exception as e:
            logger.debug(f"[{plugin_name}] 清理 sys.modules 异常: {e}")

        # 清理 sys.path：移除该插件的目录（避免路径污染其他插件）
        try:
            plugin_path = info.get('path') or os.path.join(self.plugins_dir, plugin_name)
            norm = os.path.normpath(plugin_path)
            sys.path = [p for p in sys.path if os.path.normpath(p) != norm]
        except Exception:
            pass

        # 清理文件快照
        with self._lock:
            self._plugin_mtimes.pop(plugin_name, None)

        # 强制清理模块引用，触发垃圾回收
        # 防御插件未关闭的文件句柄 / socket 连接 / 长连接残留
        try:
            del module
        except NameError:
            pass
        # 连续两次 gc.collect()：第一次回收循环引用，第二次回收析构链
        collected = gc.collect()
        if collected > 0:
            logger.debug(f"[{plugin_name}] gc.collect() 回收了 {collected} 个对象")
        gc.collect()

        logger.info(f"[{plugin_name}] 已卸载")

        # 插件已从内存移除，让路由表立即重建，避免路由到已卸载插件
        try:
            self.framework.router._invalidate_cache()
        except Exception:
            pass

    def get_loaded_plugins(self) -> Dict[str, dict]:
        """获取已加载插件列表"""
        with self._lock:
            return {
                name: {
                    'meta': info['meta'],
                    'priority': info['priority'],
                    'yaml': info.get('yaml', {}),
                }
                for name, info in self._loaded_plugins.items()
            }

    def get_plugin_module(self, plugin_name: str):
        """获取插件模块引用"""
        with self._lock:
            info = self._loaded_plugins.get(plugin_name)
            return info['module'] if info else None

    def _sync_dashboard_cards(self, plugin_name: str, cards: list):
        """存储仪表盘卡片信息到插件信息中"""
        with self._lock:
            info = self._loaded_plugins.get(plugin_name)
            if info:
                info['dashboard_cards'] = cards

    def get_dashboard_cards(self) -> list:
        """
        获取所有仪表盘卡片（按优先级排序）

        每个卡片 handler 在独立线程池中执行并限时 2 秒：防止某个插件卡片的慢操作
        （如内存统计、网络请求）占满 Web 线程池导致仪表盘卡死；超时的卡片
        返回占位数据并记录告警（超时任务在后台继续跑，不阻塞 Web 线程）。
        """
        global _cards_executor
        result = []
        with self._lock:
            items = []
            for name, info in self._loaded_plugins.items():
                cards = info.get('dashboard_cards', [])
                for card in cards:
                    items.append((name, card))
        if not items:
            return result

        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
        if _cards_executor is None:
            _cards_executor = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix='zccards')
        futures = {}
        for name, card in items:
            handler = card['handler']
            fut = _cards_executor.submit(handler)
            futures[fut] = (name, card)
        for fut, (name, card) in futures.items():
            try:
                card_data = fut.result(timeout=2.0)
            except FutureTimeout:
                logger.warning(f"[{name}] 仪表盘卡片执行超时（>2s），已跳过: {card.get('title', '')}")
                card_data = {"value": "⏳ 加载超时", "label": card.get('title', ''), "timeout": True}
                fut.cancel()
            except Exception as e:
                logger.error(f"[{name}] 仪表盘卡片异常: {e}")
                card_data = None
            if card_data:
                result.append({
                    'plugin_name': name,
                    'title': card.get('title', ''),
                    'icon': card.get('icon'),
                    'priority': card.get('priority', 50),
                    'data': card_data,
                })
        result.sort(key=lambda x: x['priority'])
        return result

    # ==================================================================
    #  WebUI 群组/用户管理页插件扩展
    # ==================================================================

    def get_ui_extensions(self, scope: str) -> list:
        """
        获取群组(scope='groups')或用户(scope='users')管理页的全部插件扩展元信息
        返回: [{key, title, plugin, type}]
        """
        field = 'group_extensions' if scope == 'groups' else 'user_extensions'
        result = []
        with self._lock:
            for name, info in self._loaded_plugins.items():
                for ext in info.get(field, []):
                    result.append({
                        'key': ext['key'],
                        'title': ext['title'],
                        'plugin': name,
                        'type': ext['type'],
                    })
        result.sort(key=lambda x: (x['plugin'], x['title']))
        return result

    def _get_ext_handler(self, scope: str, key: str):
        """按 scope+key 找到扩展 handler（未找到返回 None）"""
        field = 'group_extensions' if scope == 'groups' else 'user_extensions'
        with self._lock:
            for info in self._loaded_plugins.values():
                for ext in info.get(field, []):
                    if ext['key'] == key:
                        return ext['handler']
        return None

    def call_ui_extensions(self, scope: str, target_id, keys=None) -> dict:
        """
        对单个群/用户调用扩展 handler（线程池 + 2 秒超时隔离，不卡 Web 线程）

        :param scope: 'groups' | 'users'
        :param target_id: group_id 或 user_id
        :param keys: 要调用的扩展 key 列表（None=全部）
        :return: {key: {type, data, plugin}}；超时/异常返回占位
        """
        global _cards_executor
        field = 'group_extensions' if scope == 'groups' else 'user_extensions'
        exts = []
        with self._lock:
            for name, info in self._loaded_plugins.items():
                for ext in info.get(field, []):
                    if keys is None or ext['key'] in keys:
                        exts.append((name, ext))
        if not exts:
            return {}

        from concurrent.futures import TimeoutError as FutureTimeout
        if _cards_executor is None:
            from concurrent.futures import ThreadPoolExecutor
            _cards_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='zccards')
        out = {}
        futures = {}
        for name, ext in exts:
            handler = ext['handler']
            fut = _cards_executor.submit(handler, target_id)
            futures[fut] = (name, ext)
        for fut, (name, ext) in futures.items():
            try:
                data = fut.result(timeout=2.0)
            except FutureTimeout:
                data = "⏳ 超时"
                fut.cancel()
            except Exception as e:
                logger.error(f"[{name}] UI 扩展[{ext['key']}] 异常: {e}")
                data = "-"
            out[ext['key']] = {
                'type': ext['type'],
                'plugin': name,
                'title': ext['title'],
                'data': data,
            }
        return out

    # ==================================================================
    #  插件 WebUI 内嵌
    # ==================================================================

    def register_webui(self, plugin_name: str, webui_info: dict):
        """注册插件的 WebUI 页面"""
        with self._lock:
            info = self._loaded_plugins.get(plugin_name)
            if info:
                # 避免重复注册
                webuis = info.setdefault('webuis', [])
                # 查找是否已存在同名 WebUI
                existing = next((w for w in webuis if w['title'] == webui_info['title']), None)
                if not existing:
                    webui_info['plugin_name'] = plugin_name
                    webuis.append(webui_info)

    def get_plugin_webuis(self) -> list:
        """获取所有已注册的插件 WebUI 列表（按 order 排序）"""
        result = []
        with self._lock:
            for name, info in self._loaded_plugins.items():
                for w in info.get('webuis', []):
                    result.append({
                        'plugin_name': name,
                        'title': w.get('title', ''),
                        'entry': w.get('entry', 'index.html'),
                        'icon': w.get('icon'),
                        'order': w.get('order', 50),
                    })
        result.sort(key=lambda x: x['order'])
        return result

    def get_plugin_webui_path(self, plugin_name: str) -> str:
        """获取插件 web/ 目录的绝对路径"""
        plugin_path = os.path.join(self.plugins_dir, plugin_name, 'web')
        return plugin_path if os.path.isdir(plugin_path) else None

    def get_plugin_webui_entry(self, plugin_name: str, entry: str = 'index.html') -> str:
        """获取插件 WebUI 入口文件的完整路径"""
        web_dir = self.get_plugin_webui_path(plugin_name)
        if not web_dir:
            return None
        entry_path = os.path.join(web_dir, entry)
        return entry_path if os.path.isfile(entry_path) else None

    # ==================================================================
    #  群级插件开关（在路由前检查）
    # ==================================================================

    def is_plugin_enabled_for_group(self, plugin_name: str, group_id: int) -> bool:
        """
        检查插件在指定群是否启用
        默认启用（表中无记录时视为启用）
        优先从缓存读取，30 秒刷新
        """
        if not group_id:
            return True  # 私聊不做限制

        # 刷新缓存
        self._refresh_group_plugin_cache()

        group_settings = self._group_plugin_cache.get(group_id)
        if group_settings is not None and plugin_name in group_settings:
            return group_settings[plugin_name]
        return True  # 无记录 = 启用

    def is_plugin_enabled_for_group_cached(self, plugin_name: str, group_id: int) -> bool:
        """
        检查插件在指定群是否启用（纯内存，不刷新缓存、不查库）
        供消息路由热路径使用：群开关缓存由路由表的后台刷新任务周期性维护
        默认启用（表中无记录时视为启用）
        """
        if not group_id:
            return True  # 私聊不做限制
        group_settings = self._group_plugin_cache.get(group_id)
        if group_settings is not None and plugin_name in group_settings:
            return group_settings[plugin_name]
        return True  # 无记录 = 启用

    def set_group_plugin_enabled(self, plugin_name: str, group_id: int, enabled: bool):
        """设置插件在指定群的启用/禁用状态，并立即更新缓存"""
        try:
            self.db.execute(
                "INSERT INTO group_plugin_settings (group_id, plugin_name, enabled) "
                "VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE enabled = %s",
                (group_id, plugin_name, 1 if enabled else 0, 1 if enabled else 0)
            )
        except Exception as e:
            logger.error(f"设置群级插件状态失败 [{plugin_name}][{group_id}]: {e}")
            raise

        # 立即更新缓存
        self._group_plugin_cache.setdefault(group_id, {})[plugin_name] = enabled

    def remove_group_plugin_setting(self, plugin_name: str, group_id: int):
        """删除群级插件开关记录（恢复默认=启用）"""
        try:
            self.db.execute(
                "DELETE FROM group_plugin_settings WHERE group_id = %s AND plugin_name = %s",
                (group_id, plugin_name)
            )
        except Exception as e:
            logger.error(f"删除群级插件设置失败 [{plugin_name}][{group_id}]: {e}")

        # 更新缓存
        group_settings = self._group_plugin_cache.get(group_id)
        if group_settings and plugin_name in group_settings:
            del group_settings[plugin_name]

    def get_group_plugin_settings(self, group_id: int = None) -> list:
        """获取群级插件设置列表"""
        if group_id:
            try:
                rows = self.db.query(
                    "SELECT plugin_name, enabled, updated_at "
                    "FROM group_plugin_settings WHERE group_id = %s "
                    "ORDER BY plugin_name",
                    (group_id,)
                )
                return rows
            except Exception as e:
                logger.error(f"查询群级插件设置失败 [{group_id}]: {e}")
                return []
        else:
            try:
                rows = self.db.query(
                    "SELECT group_id, plugin_name, enabled, updated_at "
                    "FROM group_plugin_settings ORDER BY group_id, plugin_name"
                )
                return rows
            except Exception as e:
                logger.error(f"查询群级插件设置失败: {e}")
                return []

    def _refresh_group_plugin_cache(self):
        """刷新群级插件开关缓存（最多 30 秒一次）"""
        now = time.time()
        if self._group_plugin_cache and (now - self._group_plugin_cache_time) < self._group_cache_ttl:
            return

        try:
            rows = self.db.query(
                "SELECT group_id, plugin_name, enabled FROM group_plugin_settings"
            )
            cache = {}
            for r in rows:
                gid = r['group_id']
                cache.setdefault(gid, {})[r['plugin_name']] = bool(r['enabled'])
            self._group_plugin_cache = cache
            self._group_plugin_cache_time = now
        except Exception as e:
            logger.warning(f"刷新群级插件缓存失败: {e}")