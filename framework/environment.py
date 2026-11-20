"""运行环境快照：Python / 虚拟环境 / 容器 / 特权身份 / 机器资源。

启动横幅、崩溃日志页脚、数据库恢复记录共用这里的一份实现；
只读取进程与系统事实，不修改任何文件，探测失败一律退化成「未检测到」。
"""
import os
import platform
import shutil
import socket
import sys

PROC_MEMINFO = '/proc/meminfo'
CGROUP_V2_MEM_MAX = '/sys/fs/cgroup/memory.max'
CGROUP_V1_MEM_LIMIT = '/sys/fs/cgroup/memory/memory.limit_in_bytes'
_CGROUP_UNLIMITED = 1 << 62

_UNKNOWN = '未检测到'


def python_runtime() -> dict:
    """当前 Python 解释器事实。"""
    return {
        'version': platform.python_version(),
        'implementation': platform.python_implementation(),
        'executable': sys.executable or _UNKNOWN,
        'prefix': sys.prefix or _UNKNOWN,
        'base_prefix': getattr(sys, 'base_prefix', sys.prefix) or _UNKNOWN,
    }


def _prefix() -> str:
    return str(getattr(sys, 'prefix', '') or '')


def env_kind() -> str:
    """按确定性顺序判定环境类型，返回短名；识别不了返回 system。"""
    prefix = _prefix().lower()
    if os.environ.get('CONDA_PREFIX') or any(
            token in prefix for token in ('conda', 'miniconda', 'anaconda')):
        return 'conda'
    if os.environ.get('PIXI_ENVIRONMENT_NAME'):
        return 'pixi'
    if os.environ.get('UV_PROJECT_ENVIRONMENT'):
        return 'uv'
    if '.venv' in prefix:
        cwd = os.getcwd()
        for path in (cwd, os.path.dirname(cwd), os.path.dirname(os.path.dirname(cwd))):
            if os.path.isfile(os.path.join(path, 'uv.lock')):
                return 'uv'
    if os.environ.get('PYENV_VERSION'):
        return 'pyenv'
    pyenv_root = os.path.expanduser('~/.pyenv')
    if pyenv_root and prefix.startswith(pyenv_root.lower()):
        return 'pyenv'
    if sys.prefix != getattr(sys, 'base_prefix', sys.prefix):
        return 'venv'
    return 'system'


def env_hint(kind: str) -> str:
    """给环境短名补一句可核对的依据，日志里能看出为什么这么判。"""
    prefix = _prefix().lower()
    if kind == 'conda' and os.environ.get('CONDA_PREFIX'):
        return 'CONDA_PREFIX'
    if kind == 'pixi':
        return 'PIXI_ENVIRONMENT_NAME'
    if kind == 'uv':
        return 'UV_PROJECT_ENVIRONMENT 或 uv.lock' if os.environ.get(
            'UV_PROJECT_ENVIRONMENT') else '检测到 .venv 前缀与 uv.lock'
    if kind == 'pyenv':
        return 'PYENV_VERSION 或 ~/.pyenv 前缀'
    if kind == 'venv':
        return '检测到 .venv 前缀' if '.venv' in prefix else 'sys.prefix != base_prefix'
    if kind == 'conda':
        return '解释器路径含 conda'
    return ''


def container_kind() -> str | None:
    """容器类型：docker / podman / kubernetes / lxc，未检测到返回 None。"""
    try:
        if os.path.exists('/.dockerenv'):
            return 'docker'
    except OSError:
        pass
    try:
        if os.path.exists('/run/.containerenv'):
            return 'podman'
    except OSError:
        pass
    if os.environ.get('KUBERNETES_SERVICE_HOST'):
        return 'kubernetes'
    try:
        with open('/proc/1/cgroup', encoding='utf-8', errors='ignore') as f:
            cgroup = f.read().lower()
        if 'lxc' in cgroup:
            return 'lxc'
    except OSError:
        pass
    return None


def is_privileged() -> tuple[bool, str]:
    """是否以 root / Windows 管理员身份运行。绝不抛异常。"""
    if hasattr(os, 'geteuid'):
        try:
            if os.geteuid() == 0:
                return True, 'uid=0 (root)'
            return False, f'uid={os.geteuid()}'
        except Exception:
            return False, _UNKNOWN
    if os.name == 'nt':
        try:
            import ctypes

            # IsUserAnAdmin 无参数、返回 BOOL，避免手工构造 SID 缓冲区时因
            # 参数/长度不匹配导致 ctypes 触发访问违规（探测绝不能拖垮进程）。
            is_admin = ctypes.windll.shell32.IsUserAnAdmin
            is_admin.argtypes = ()
            is_admin.restype = ctypes.c_int
            return bool(is_admin()), 'Windows Administrators'
        except Exception:
            return False, _UNKNOWN
    return False, _UNKNOWN


def _read_first_line(path):
    try:
        with open(path, encoding='utf-8', errors='ignore') as f:
            return f.readline().strip()
    except OSError:
        return ''


def _cgroup_memory_limit(v2=None, v1=None) -> int | None:
    """容器内存上限（字节）；无限制或读不到返回 None。"""
    for path in (v2 or CGROUP_V2_MEM_MAX, v1 or CGROUP_V1_MEM_LIMIT):
        raw = _read_first_line(path)
        if not raw or raw == 'max':
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if 0 < value < _CGROUP_UNLIMITED:
            return value
    return None


def _memory_usage(platform_name=None, proc_meminfo=None) -> tuple[int | None, int | None]:
    """(总内存, 可用内存)，字节；平台不识别返回 (None, None)。"""
    system = platform_name or platform.system()
    if system == 'Linux':
        total = available = None
        try:
            with open(proc_meminfo or PROC_MEMINFO, encoding='utf-8',
                      errors='ignore') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        total = int(line.split()[1]) * 1024
                    elif line.startswith('MemAvailable:'):
                        available = int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None, None
        return total, available
    if system == 'Windows':
        try:
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ('dwLength', ctypes.c_ulong),
                    ('dwMemoryLoad', ctypes.c_ulong),
                    ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
                ]

            stat = _MemoryStatus()
            stat.dwLength = ctypes.sizeof(_MemoryStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return None, None
            return stat.ullTotalPhys, stat.ullAvailPhys
        except Exception:
            return None, None
    return None, None


def _size_text(value) -> str:
    if not isinstance(value, (int, float)) or value < 0:
        return _UNKNOWN
    size = float(value)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:.1f}{unit}' if unit != 'B' else f'{int(size)}B'
        size /= 1024
    return _UNKNOWN


def snapshot(config: dict | None = None) -> dict:
    """一次性收集全部环境事实；任何单项失败都退化成空值。

    ``config`` 与 ``framework.config.load_config`` 写入的命令行路径标记一起传入时，
    可让启动横幅显示实际使用的配置路径，而不是当前工作目录的默认猜测。
    """
    info = python_runtime()
    kind = env_kind()
    info.update({
        'env': kind,
        'env_detail': env_hint(kind),
        'container': container_kind(),
        'os': platform.system() or _UNKNOWN,
        'kernel': platform.release() or _UNKNOWN,
        'machine': platform.machine() or _UNKNOWN,
        'hostname': socket.gethostname() or _UNKNOWN,
        'pid': os.getpid(),
        'cwd': os.getcwd(),
        'config_path': _UNKNOWN,
        'db_type': _UNKNOWN,
        'db_path': _UNKNOWN,
    })
    privileged, reason = is_privileged()
    info['privileged'] = privileged
    info['privileged_reason'] = reason or _UNKNOWN

    try:
        from framework.paths import resolve_config_path

        explicit = None
        if isinstance(config, dict):
            explicit = config.get('_config_path')
        info['config_path'] = os.path.abspath(resolve_config_path(explicit))
    except Exception:
        pass
    try:
        from framework.config import get_config

        cfg = config if isinstance(config, dict) else get_config()
        db = cfg.get('database') or {}
        info['db_type'] = str(db.get('type') or _UNKNOWN)
        info['db_path'] = str(db.get('path') or _UNKNOWN)
    except Exception:
        pass

    mem_total, mem_available = _memory_usage()
    info['mem_total'] = mem_total
    info['mem_available'] = mem_available
    info['cgroup_mem_limit'] = _cgroup_memory_limit()
    target = info['db_path'] if os.path.isabs(info['db_path']) and info['db_path'] != _UNKNOWN else info['cwd']
    try:
        usage = shutil.disk_usage(target)
        info['disk_free'] = usage.free
        info['disk_total'] = usage.total
        info['disk_target'] = target
    except OSError:
        info['disk_free'] = None
        info['disk_total'] = None
        info['disk_target'] = target
    return info


def _display_width(text: str) -> int:
    """终端显示宽度：东亚宽字符按 2 列算，否则中文标签永远差半截。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
               for ch in str(text))


def format_block(info: dict, title: str = '[运行环境]') -> str:
    """固定标签列宽的多行可读块；空值统一写「未检测到」，绝不出现 None。"""
    def val(key, fallback=_UNKNOWN):
        value = info.get(key)
        if value is None or value == '':
            return fallback
        return str(value)

    runtime = (f"{val('version')} {val('implementation')} ({val('executable')})")
    env_text = val('env')
    if info.get('env_detail'):
        env_text += f"（{info['env_detail']}）"
    privileged = '是 —— ' + val('privileged_reason') if info.get('privileged') else '否'
    mem = (f"可用 {_size_text(info.get('mem_available'))} / "
           f"总计 {_size_text(info.get('mem_total'))}")
    disk = f"{val('disk_target')} 剩余 {_size_text(info.get('disk_free'))}"
    rows = [
        (title, None),
        ('Python', runtime),
        ('环境类型', env_text),
        ('容器', val('container')),
        ('特权身份', privileged),
        ('系统', f"{val('os')} {val('kernel')} {val('machine')}"),
        ('主机 / PID', f"{val('hostname')} / {val('pid')}"),
        ('工作目录', val('cwd')),
        ('配置文件', val('config_path')),
        ('数据库', f"{val('db_type')} {val('db_path')}"),
        ('内存', mem),
        ('磁盘', disk),
    ]
    label_width = max(_display_width(label) for label, _ in rows[1:])
    lines = []
    for label, value in rows:
        if value is None:
            lines.append(str(label))
            continue
        pad = ' ' * max(0, label_width - _display_width(label))
        lines.append(f'  {label}{pad} : {value}')
    return '\n'.join(lines).replace('None', _UNKNOWN)
