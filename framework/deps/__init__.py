"""
依赖管理模块（从 framework/loader/ 剥离，史山拆解）

两层职责：
1. 自由函数：pip 镜像安装（清华源 + 自动回退）、requirements.txt 解析、
   版本说明符解析与兼容性判定 —— 供 main.py 启动自检、Web 后台、加载器共用；
2. PluginDepsMixin：插件依赖检查/自动安装/隔离 venv 的实现，
   以混入方式挂到 PluginLoader 上（self 即加载器实例）。

兼容性：framework.loader 继续 re-export 本模块的公开函数，
旧导入路径 from framework.loader import pip_install_* 仍然有效。
"""
import importlib.metadata
import logging
import os
import shutil
import subprocess
import sys
import yaml

logger = logging.getLogger('zcbot')


# ── 插件依赖 / 隔离 venv（混入 PluginLoader）─────────────────────

from framework.deps.pip import (  # noqa: F401  向后兼容 re-export
    _check_version_compatible, _get_host, _parse_requirements_file,
    _parse_ver, _parse_version_spec, pip_install_all,
    pip_install_requirements, pip_install_with_mirror,
)


class PluginDepsMixin:
    """
    插件依赖检查、自动安装与隔离虚拟环境。
    以混入方式提供给 PluginLoader（依赖 self.plugins_dir / self._lock 等加载器状态）。
    """

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
        log = logger
        result = self.check_dependencies(plugin_name)
        if result['ok']:
            return {'success': True, 'installed': [], 'failed': [], 'conflicts': []}

        installed = []
        failed = []
        # 强制使用当前解释器，避免多 Python 环境安装到错误位置
        pip_exec = sys.executable
        for dep in result['missing']:
            try:
                log.info(f"[{plugin_name}] 正在安装依赖: {dep}")
                r = pip_install_with_mirror(pip_exec, dep, timeout=120)
                if r['success']:
                    installed.append(dep)
                    log.info(f"[{plugin_name}] 依赖安装成功: {dep}（镜像: {r['mirror']}）")
                else:
                    failed.append(dep)
                    log.warning(f"[{plugin_name}] 依赖安装失败: {dep} - {r['error']}")
            except Exception as e:
                failed.append(dep)
                log.warning(f"[{plugin_name}] 依赖安装失败: {dep} - {e}")

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
        log = logger
        # 确保 plugins_dat/<插件名> 目录存在
        self.ensure_plugins_dat_dir(plugin_name)
        venv_path = self._venv_dir(plugin_name)

        # 获取插件所有依赖（合并所有声明来源）
        deps = self._get_merged_dependencies(plugin_name)

        try:
            # 1. 创建虚拟环境
            log.info(f"[{plugin_name}] 正在创建虚拟环境: {venv_path}")
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
            log.error(f"[{plugin_name}] 创建隔离环境失败: {e}")
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
        log = logger
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
                log.info(f"[{plugin_name}] 隔离环境安装依赖: {dep}")
                # 注意：pip_install_with_mirror 使用 {exec} -m pip install 模式，
                # 所以必须传 venv 的 python 路径，而不是 pip 路径（否则会变成 pip -m pip install 这样的错误命令）
                r = pip_install_with_mirror(python_path, dep, timeout=120)
                if r['success']:
                    installed.append(dep)
                else:
                    failed.append(dep)
                    log.warning(f"[{plugin_name}] 隔离环境安装依赖失败: {dep} - {r.get('error')}")
            except Exception as e:
                failed.append(dep)
                log.warning(f"[{plugin_name}] 隔离环境安装依赖失败: {dep} - {e}")

        success = len(failed) == 0
        if success:
            with self._lock:
                self._isolated_plugins.add(plugin_name)
                self._conflict_deps.pop(plugin_name, None)
                if not self._missing_deps.get(plugin_name):
                    self._missing_deps.pop(plugin_name, None)
            log.info(f"[{plugin_name}] 隔离环境安装完成: {venv_path}")

        return {
            'success': success,
            'venv_path': venv_path,
            'python': python_path,
            'installed': installed,
            'failed': failed,
        }

    def remove_isolated_env(self, plugin_name: str) -> dict:
        """删除插件的隔离虚拟环境（plugins_dat/<插件名>/.venv）"""
        log = logger
        venv_path = self._venv_dir(plugin_name)
        if not os.path.isdir(venv_path):
            return {'success': True, 'msg': '无隔离环境'}
        try:
            shutil.rmtree(venv_path, ignore_errors=True)
            with self._lock:
                self._isolated_plugins.discard(plugin_name)
            log.info(f"[{plugin_name}] 隔离环境已删除: {venv_path}")
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
