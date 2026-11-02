#!/usr/bin/env python3
"""
ZCBOT 事件驱动 IM 平台 · 启动入口（异步）
项目地址：https://github.com/kuangxing6367/zcbot
"""
import asyncio
import atexit
import logging
import multiprocessing
import os
import re
import signal
import sys

logger = logging.getLogger('zcbot')

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _apply_allocator_policy():
    """内存分配器自动策略（必须在 import framework 之前执行）。

    PYTHONMALLOC 是解释器启动前就定死的，运行时改不了；所以此函数在最早期
    根据 config 的 memory.allocator 决定分配器，并 os.execv 重启自身一次性套用
    （带 ZCBOT_ALLOC_APPLIED 防重入标记，避免无限重启）。

    allocator 三态：
      auto     （默认）生产 Linux 自动用 malloc（pymalloc arena 残留导致 RSS 地板，
                且 malloc_trim 在 Linux 最有效）；Windows/macOS 开发环境保持 pymalloc
                （SetProcessWorkingSetSize 频繁换页会影响体验）。
      malloc   强制 malloc（追求最低内存地板，小对象分配略慢）。
      pymalloc 强制 pymalloc（默认行为，最高分配吞吐）。
    """
    if os.environ.get('ZCBOT_ALLOC_APPLIED'):
        return
    alloc = 'auto'
    try:
        import yaml
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
        if os.path.isfile(cfg_path):
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}
            alloc = (cfg.get('memory') or {}).get('allocator', 'auto')
    except Exception:
        alloc = 'auto'
    alloc = str(alloc).strip().lower()
    if alloc == 'auto':
        alloc = 'malloc' if sys.platform == 'linux' else 'pymalloc'
    if alloc == 'malloc' and os.environ.get('PYTHONMALLOC') != 'malloc':
        os.environ['PYTHONMALLOC'] = 'malloc'
        os.environ['ZCBOT_ALLOC_APPLIED'] = '1'
        print('[内存策略] 已自动以 PYTHONMALLOC=malloc 重启进程'
              '（追求更低内存地板；小对象分配略慢，详见日志）')
        os.execv(sys.executable, [sys.executable] + sys.argv)


_apply_allocator_policy()


def _read_self_check_allow_missing(config_path=None):
    """读取 config.yaml 的 dependencies.allow_missing（在 import framework 之前，只用标准库 yaml）。

    返回规范名集合（小写、连字符/下划线等价）。读取失败返回空集（不影响既有行为）。
    """
    root = os.path.dirname(os.path.abspath(__file__))
    path = config_path or os.path.join(root, 'config.yaml')
    try:
        import yaml
        with open(path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
        deps = cfg.get('dependencies') or {}
        allow = deps.get('allow_missing') or []
        if isinstance(allow, str):
            allow = [allow]
        norm = lambda n: str(n).strip().lower().replace('_', '-')
        return {norm(x) for x in allow if x}
    except Exception:
        return set()


def _resolve_self_check_missing(missing, allow_missing):
    """把 requirements 解析出的缺失规格，按配置过滤出"必须处理"清单。

    :param missing: requirements 中"已缺失"的原始规格行列表（含版本约束）
    :param allow_missing: 允许缺失的规范包名集合
    :return: (to_install, skipped_allowed)
        to_install   —— 必须安装（不在 allow_missing 中）的规格
        skipped_allowed —— 被 allow_missing 豁免、跳过安装也不报错的规格
    """
    norm = lambda n: n.strip().lower().replace('_', '-')
    allowed = {norm(x) for x in allow_missing}
    to_install, skipped = [], []
    for spec in missing:
        pkg = re.match(r'^([a-zA-Z0-9_.\-]+)', spec)
        if pkg and norm(pkg.group(1)) in allowed:
            skipped.append(spec)
        else:
            to_install.append(spec)
    return to_install, skipped


def _check_and_install_deps():
    """
    启动前自检：扫描 requirements.txt，自动安装缺失的依赖
    解决移机/首次部署时依赖缺失导致 import 报错的问题。

    依赖项可被 config.yaml 的 dependencies.allow_missing 豁免：豁免的缺失包只记
    WARNING，不安装、不打断启动、不退出（详见该配置段说明）。
    """
    # 一次扫描所有已安装发行版，建立规范名集合，避免对每个依赖各做一次 metadata 查找
    import importlib.metadata as _imd

    def _norm(name: str) -> str:
        return name.strip().lower().replace('_', '-')

    installed = {_norm(d.metadata['Name']) for d in _imd.distributions()}

    project_dir = os.path.dirname(os.path.abspath(__file__))
    req_file = os.path.join(project_dir, 'requirements.txt')
    if not os.path.isfile(req_file):
        return

    # 解析 requirements.txt，提取缺失规格（保留原始写法，含版本约束）
    missing = []
    with open(req_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            m = re.match(r'^([a-zA-Z0-9_.\-]+)', line)
            if not m:
                continue
            if _norm(m.group(1)) not in installed:
                missing.append(line)

    if not missing:
        return

    allow_missing = _read_self_check_allow_missing()
    to_install, skipped = _resolve_self_check_missing(missing, allow_missing)

    if skipped:
        print(f"[自检] 以下缺失依赖在配置中标记为允许缺失，已跳过（不安装、不报错）: "
              f"{', '.join(skipped)}")

    if not to_install:
        return

    print(f"[自检] 检测到 {len(to_install)} 个缺失依赖，正在自动安装...")
    print(f"[自检] 缺失: {', '.join(to_install)}")

    # 只安装"必需子集"：把 to_install 规格写入临时 requirements，避免把 allow_missing
    # 豁免的包也塞进整文件安装（那些包即便缺失也按配置容忍）。
    # 只走 framework.deps.pip（纯标准库），不能 import framework.loader ——
    # loader/base 顶层 import yaml，缺的正是它，会让自检安装器自身崩溃
    import tempfile
    tmp_req = None
    result = {'success': False, 'mirror': '', 'error': '未执行安装'}
    try:
        fd, tmp_path = tempfile.mkstemp(prefix='zcbot_req_', suffix='.txt')
        os.close(fd)
        with open(tmp_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(to_install) + '\n')
        tmp_req = tmp_path
        from framework.deps.pip import pip_install_requirements
        result = pip_install_requirements(sys.executable, tmp_req, timeout=300)
    finally:
        if tmp_req and os.path.isfile(tmp_req):
            try:
                os.remove(tmp_req)
            except OSError:
                pass

    if result['success']:
        print(f"[自检] 依赖安装完成（镜像: {result['mirror']}）")
    else:
        print(f"[自检] 依赖安装失败: {result['error']}")
        print(f"[自检] 请手动执行: pip install -r requirements.txt")
        sys.exit(1)


# 单实例锁全局句柄（供退出清理）
_INSTANCE_LOCK = {'path': None, 'fd': None}


def _instance_lock_path(root):
    return os.path.join(root, 'data', 'zcbot.lock')


# 锁字节与 PID 字节错开：锁打在偏移 4096，PID 写在偏移 0，避免 Windows 下
# 持锁句柄锁住第 0 字节导致第二个实例读取 PID 时读到「PID 未知」。
_LOCK_BYTE_OFFSET = 4096


def _open_lock_file(lock_path):
    """跨平台打开锁文件。

    Windows 下以「读+写共享」(FILE_SHARE_READ|FILE_SHARE_WRITE) 打开，使第二个实例也能
    打开并读取记录的 PID，从而打印明确的「已有实例」提示，而不是因独占共享导致 os.open
    直接抛共享冲突、静默退出（这正是早期版本在 Windows 上「发现锁住却啥都不打印」的根因）。
    POSIX 下直接 os.open（fcntl.flock 是整文件 advisory lock，且不影响读）。
    """
    if sys.platform == 'win32':
        import ctypes
        import msvcrt
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_ALWAYS = 4  # 注意：Windows 里 3 是 OPEN_EXISTING（仅当文件已存在），4 才是 OPEN_ALWAYS（不存在则创建）
        # 用显式 c_uint32 类型对象传参，避免 ctypes 默认按有符号 c_int 转换 0xC0000000 这类高位值溢出
        h = kernel32.CreateFileW(
            ctypes.c_wchar_p(lock_path),
            ctypes.c_uint32(GENERIC_READ | GENERIC_WRITE),
            ctypes.c_uint32(FILE_SHARE_READ | FILE_SHARE_WRITE),
            None,
            ctypes.c_uint32(OPEN_ALWAYS),
            ctypes.c_uint32(0),
            None,
        )
        h_val = h.value if hasattr(h, 'value') else h
        INVALID = ctypes.c_void_p(-1).value  # 0xFFFFFFFFFFFFFFFF（INVALID_HANDLE_VALUE）
        if h_val in (None, 0, INVALID):
            raise OSError(f"CreateFileW 打开锁文件失败: {kernel32.GetLastError()}")
        try:
            # 必须带 O_BINARY：Windows CRT 的 open_osfhandle 对仅 O_RDWR 的句柄
            # 会报 [Errno 9] Bad file descriptor（缺少二进制模式标志）。
            return msvcrt.open_osfhandle(int(h_val), os.O_RDWR | os.O_BINARY)
        except Exception:
            kernel32.CloseHandle(ctypes.c_void_p(int(h_val)))
            raise
    return os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)


def _pid_is_our_instance(pid, root):
    """判断 pid 是否仍是本框架进程（避免 PID 复用误判为「已在运行」）。"""
    if pid is None or pid <= 0:
        return False
    if sys.platform == 'win32':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            PROCESS_QUERY_INFORMATION = 0x0400
            h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, int(pid))
            alive = bool(h)
            if h:
                kernel32.CloseHandle(h)
            return alive
        except Exception:
            return False
    # POSIX：先确认进程存在，再比对 /proc/<pid>/cmdline 是否指向本 main.py
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except Exception:
        # 存在但无权限探活 -> 保守视为存活
        return True
    try:
        with open('/proc/%d/cmdline' % int(pid), 'rb') as f:
            cmd = f.read().decode('utf-8', 'replace')
        for seg in cmd.split('\x00'):
            if 'main.py' in seg or 'zcbot' in seg.lower():
                return True
        return False
    except (FileNotFoundError, PermissionError):
        return False


def _read_lock_pid(fd):
    """从本进程已打开的锁 fd 读取记录的 PID（lseek+read，不涉及跨句柄共享，
    跨平台稳定；读不到或非数字返回 None，不阻断主流程）。
    """
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        data = os.read(fd, 64)
        txt = data.decode('utf-8', 'replace').strip()
        return int(txt) if txt.isdigit() else None
    except Exception:
        return None


def _write_lock_pid(fd):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode('utf-8'))
    except Exception:
        pass


def _try_lock(fd):
    if sys.platform == 'win32':
        import msvcrt
        try:
            os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _remove_stale_lock(lock_path, fd):
    try:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except Exception:
        pass


def _acquire_instance_lock(root):
    """单实例文件锁：防止守护进程无脑拉起导致多实例（抢端口 / 抢 DB / 双写）。

    跨平台：Linux 用 fcntl.flock（内核级 advisory lock，进程退出自动释放）；
    Windows 用 msvcrt.locking。拿到锁则持有 fd 直到进程退出（fd 关闭即释放）。

    退出时由 _cleanup_instance_lock()（atexit + finally 双保险）关闭 fd 并删除锁文件，
    避免残留锁文件干扰运维判断。若进程被强杀导致锁文件残留，下次启动会通过 PID 探活
    识别 stale 锁并清理后重新获取，不会误判「已在运行」。

    返回 fd（持有锁）或 None（获取失败，调用方应退出；具体原因已打印提示）。
    """
    lock_path = _instance_lock_path(root)
    lock_dir = os.path.dirname(lock_path)
    try:
        os.makedirs(lock_dir, exist_ok=True)
    except Exception:
        pass

    try:
        fd = _open_lock_file(lock_path)
    except Exception as e:
        logger.warning(f"实例锁文件创建失败，跳过单实例保护: {e}")
        return None

    if _try_lock(fd):
        _INSTANCE_LOCK['path'] = lock_path
        _INSTANCE_LOCK['fd'] = fd
        _write_lock_pid(fd)
        return fd

    # 拿不到锁：可能是真有另一个实例，也可能是残留 stale 锁（持有进程已死）
    holder_pid = _read_lock_pid(fd)
    if holder_pid is not None and _pid_is_our_instance(holder_pid, root):
        # 确属本框架仍在运行的实例 -> 明确提示并拒绝
        try:
            os.close(fd)
        except Exception:
            pass
        print("\n" + "=" * 60)
        print(f"[实例锁] 检测到另一个 ZCBOT 实例正在运行（PID {holder_pid}），")
        print(f"          本进程立即退出，避免多实例抢端口 / 抢 DB / 双写。")
        print(f"          如需强制启动，请先结束该进程或删除锁文件：")
        print(f"          {lock_path}")
        print("=" * 60)
        logger.warning(f"实例锁冲突：检测到另一个 ZCBOT 实例（PID {holder_pid}），本进程退出")
        return None
    if holder_pid is None:
        # 锁被占用但读不到 PID（极少见，如权限/共享拦截）-> 保守拒绝并提示
        try:
            os.close(fd)
        except Exception:
            pass
        print("\n" + "=" * 60)
        print("[实例锁] 检测到实例锁已被占用（PID 未知），本进程立即退出，")
        print(f"          避免多实例抢端口 / 抢 DB / 双写。如需强制启动，")
        print(f"          请先结束占用进程或删除锁文件：")
        print(f"          {lock_path}")
        print("=" * 60)
        logger.warning("实例锁冲突：锁被占用但 PID 未知，本进程退出")
        return None

    # stale 锁：持有者已死或无法确认 -> 清理残留并重新获取
    print(f"\n[实例锁] 发现残留锁文件（记录的 PID {holder_pid} 已不存在或无效），"
          f"判定为 stale，清理后继续启动。")
    logger.info(f"实例锁：清理 stale 锁文件（原 PID {holder_pid}），路径 {lock_path}")
    _remove_stale_lock(lock_path, fd)
    try:
        fd = _open_lock_file(lock_path)
    except Exception as e:
        logger.warning(f"实例锁文件重建失败，跳过单实例保护: {e}")
        return None
    if _try_lock(fd):
        _INSTANCE_LOCK['path'] = lock_path
        _INSTANCE_LOCK['fd'] = fd
        _write_lock_pid(fd)
        return fd
    # 重建后仍失败（极少见，可能真有并发启动）-> 保守拒绝并提示
    try:
        os.close(fd)
    except Exception:
        pass
    print("\n" + "=" * 60)
    print("[实例锁] 清理残留锁后仍无法获取实例锁，疑似并发启动，本进程退出。")
    print("=" * 60)
    logger.warning("实例锁：清理 stale 后仍无法获取，疑似并发启动，本进程退出")
    return None


def _cleanup_instance_lock():
    """退出时释放并删除实例锁文件（atexit + finally 双保险）。

    仅清理本进程持有的锁（_INSTANCE_LOCK 记录当前 fd / 路径），不会误删
    其他存活实例的锁；清理前先解锁再关闭再删除，跨平台安全。
    """
    info = _INSTANCE_LOCK
    fd = info.get('fd')
    path = info.get('path')
    if fd is not None:
        try:
            if sys.platform == 'win32':
                try:
                    import msvcrt
                    os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass
    _INSTANCE_LOCK['fd'] = None
    _INSTANCE_LOCK['path'] = None


async def _enter_safe_mode(stop_event, crash_cfg, root):
    """安全模式（假启动）：启动崩溃达上限且守护进程持续重启时，不退出进程、不做事，
    仅挂起，让 systemd/pm2（Type=simple 只看进程存在）误以为已启动、停止反复拉起，
    从而消除无限冷启动性能消耗。运维修复根因后，需先删除 error.log* 崩溃计数文件，
    再 kill 本进程（由守护进程拉起干净实例；新实例正常启动会自行清零计数）。
    """
    crash = (crash_cfg.get('framework', {}) or {}).get('crash', {}) or {}
    path = crash.get('path') or os.path.join(root, 'error.log')
    print("\n" + "=" * 60)
    print("[安全模式] 启动崩溃次数已达上限，且检测到守护进程持续重启策略。")
    print("          为避免无限冷启动消耗性能，框架进入安全模式（假启动）：")
    print("          进程保持运行但不执行任何工作，使守护进程误以为已启动、停止拉起。")
    print(f"          请排查崩溃日志（{path} 及其 .N 轮转副本）修复根因，")
    print("          确认无误后先删除这些 error.log* 文件，再手动 kill 本进程")
    print("          （由守护进程拉起干净实例；新实例正常启动后会自行清零崩溃计数）。")
    print("=" * 60)
    try:
        from framework.crash_guard import fire_alert
        fire_alert(crash_cfg)
    except Exception:
        pass
    # 持有实例锁挂起，直到收到 SIGTERM/SIGINT 优雅退出
    await stop_event.wait()


async def amain():
    """异步主流程"""
    from framework.core import Framework
    from framework.crash_guard import (should_abort_boot, write_crash_log,
                                        clear_crash_markers, fire_alert)

    # 支持命令行参数指定配置文件路径
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    root = os.path.dirname(os.path.abspath(__file__))

    # 单实例文件锁：防止守护进程无脑拉起导致多实例（抢端口 / 抢 DB / 双写）。
    # 拿不到锁说明已有实例（含安全模式假启动进程）在跑，本进程立即退出、不做任何重活。
    lock_fd = _acquire_instance_lock(root)
    if lock_fd is None:
        sys.exit(1)
    # 注册退出清理（双保险：atexit + 顶层 finally），确保锁文件被释放并删除
    atexit.register(_cleanup_instance_lock)

    # 注册停止信号（提前到崩溃守卫之前，使安全模式也能优雅响应 SIGTERM/SIGINT）
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    # 读取崩溃配置（仅用标准库 yaml，避免提前 import 重型依赖）
    crash_cfg = {}
    try:
        import yaml
        cp = config_path or os.path.join(root, 'config.yaml')
        if os.path.isfile(cp):
            crash_cfg = yaml.safe_load(open(cp, encoding='utf-8')) or {}
    except Exception:
        crash_cfg = {}

    # 崩溃守卫：达重启上限则进入安全模式（假启动）或明确拒绝，打破守护进程无限拉起。
    # safe_mode（默认开）：不退出进程、仅挂起，让 systemd/pm2（Type=simple 只看进程
    # 存在）误以为已启动而停止反复拉起，消除无限冷启动性能消耗；运维修复后 kill 重启。
    if should_abort_boot(crash_cfg, root):
        safe_mode = (crash_cfg.get('framework', {}).get('crash', {}) or {}).get('safe_mode', True)
        if safe_mode:
            # 安全模式挂起期间继续保持实例锁，避免运维误启第二个实例；
            # 进程真正退出时由 _cleanup_instance_lock()（atexit）释放并删除锁文件。
            await _enter_safe_mode(stop_event, crash_cfg, root)
            return
        crash = (crash_cfg.get('framework', {}) or {}).get('crash', {}) or {}
        path = crash.get('path') or os.path.join(root, 'error.log')
        print("\n" + "=" * 60)
        print(f"[崩溃守卫] 检测到已累计多次启动崩溃（≥ 上限），拒绝继续启动，")
        print(f"          以避免与守护进程(systemd/pm2)形成无限重启死循环。")
        print(f"          请排查并修复根因（崩溃日志: {path} 及其 .N 轮转副本），")
        print(f"          确认无误后删除这些 error.log* 文件再启动。")
        print("=" * 60)
        sys.exit(1)

    framework = None
    try:
        framework = Framework(config_path)
        # 异步启动模式：config.yaml → startup.wait_ready: false 时不等待插件加载完成，
        # 核心/用户插件在后台异步加载，启动立即返回（可后续 await framework.wait_ready()）
        _startup_cfg = framework.config.get('startup', {}) or {}
        await framework.start(wait_ready=_startup_cfg.get('wait_ready', True))
        # 启动成功：复位崩溃计数（健康运行即清零，避免误杀正常重启）
        clear_crash_markers(crash_cfg, root)
        # 等待停止信号 / Ctrl+C
        await stop_event.wait()
    except Exception as e:
        # 启动期致命异常：写崩溃日志（含 traceback）+ 触发可选外部告警，再退出
        write_crash_log(crash_cfg, root, exc=e)
        fire_alert(crash_cfg, exc=e)
        logger.exception("启动期致命异常（已写入崩溃日志）")
        sys.exit(1)
    finally:
        if framework is not None:
            try:
                await framework.stop()
            except Exception:
                pass
        if lock_fd is not None:
            _cleanup_instance_lock()


def _read_version():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'VERSION'),
                  'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return 'unknown'


def _print_usage():
    print("""ZCBOT 用法：
  python main.py [config.yaml]   启动 bot（缺省用 config.yaml；文件不存在则自动生成默认配置）
  python main.py code            打开 AI 智能体界面（不启动 bot，需要 textual）
  python main.py attach          接入运行中的 bot 调试控制台（无需 TTY / 无需重启）
  python main.py -a              同上（attach 的简写）
  python main.py -a --agent      智能体对话模式（纯文本通道里跟 AI 对话）
  python main.py code            全屏 AI 界面（需要真正的终端 / TTY）
  python main.py --language LANG 提示文案语言：zh（默认）/ ciallo（活泼版）/ en
  python main.py -l LANG         同上（简写）。也可用环境变量 ZCBOT_LANG=ciallo
  python main.py config          配置控制台：显示当前配置摘要
  python main.py config get K    查看配置项（如 language）
  python main.py config set K V  修改配置项（写回 config.yaml，重启生效）
  python main.py --iipc          走 IIPC 内核链路（Go 内核 + Rust 协议端，插件独立进程）
  python main.py --help          显示本帮助

attach 常用参数：
  --config <path>   指定 config.yaml（据此定位数据库里的控制台凭证）
  --db <path>       直接指定 SQLite 库路径
  --port/--token    直接指定控制台端口与 token（跳过读库）
  --agent           每行都交给 AI（正常模式下也可用行首 `!` 单条交给 AI）

--iipc 常用参数（其余见 framework/iipc/README.md；端口等也可在 config.yaml 的 iipc: 段配置）：
  --ipc-addr ADDR      内核 IPC 监听 [ip:]port（默认 127.0.0.1:18730）
  --plugins-dir DIR    插件目录（默认 framework/iipc/plugins）
  --admin IDS          管理员 user_id（逗号分隔，供 require_admin 鉴权）
  --onebot-url URL     正向 WS：适配器主动连 ws://host:port
  --onebot-listen ADDR 反向 WS：监听 [ip:]port 等 OneBot 实现连入（默认 127.0.0.1:6701）
  --no-rust            不启动 Rust 协议端（send 类动作不可用，事件注入仍可）""")


def _resolve_db_config(config_path=None):
    """从 config.yaml 解析 database 段（dict 或 sqlite 路径字符串）。"""
    root = os.path.dirname(os.path.abspath(__file__))
    path = config_path or os.path.join(root, 'config.yaml')
    db = None
    try:
        import yaml
        with open(path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
        db = cfg.get('database')
    except Exception:
        pass
    if not db:
        db = {'type': 'sqlite', 'path': 'data/zcbot.db'}
    return db, root


def _load_console_creds(config_path=None, db_override=None):
    """读取控制台凭证（支持 sqlite / mysql）。返回 (port, token, 描述)。"""
    from framework.console_server import load_credentials_any
    if db_override:
        port, token = load_credentials_any({'type': 'sqlite', 'path': db_override})
        return port, token, db_override
    db, root = _resolve_db_config(config_path)
    typ = str((db or {}).get('type') or 'sqlite').lower()
    if isinstance(db, dict) and typ == 'mysql':
        desc = 'mysql %s@%s:%s/%s' % (db.get('user', ''), db.get('host', '127.0.0.1'),
                                      db.get('port', 3306), db.get('database', 'zcbot'))
    else:
        p = db if isinstance(db, str) else (db or {}).get('path') or 'data/zcbot.db'
        desc = p if os.path.isabs(p) else os.path.join(root, p)
    return load_credentials_any(db, project_root=root) + (desc,)


def _run_attach(argv):
    """接入运行中的框架调试控制台（端口 + 超长 token 存于数据库，无需 TTY/重启）。"""
    import argparse

    from framework.console_server import ConsoleClient

    ap = argparse.ArgumentParser(
        prog='python main.py attach',
        description='接入运行中的 ZCBOT 调试控制台（无需 TTY / 无需重启）')
    ap.add_argument('--config', default=None, help='config.yaml 路径')
    ap.add_argument('--db', default=None, help='SQLite 库路径（覆盖配置）')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--token', default='')
    ap.add_argument('--agent', action='store_true',
                    help='智能体对话模式：每行都交给 AI（等价于每行加 ! 前缀）')
    ap.add_argument('command', nargs='*', help='可选：只执行一条命令后退出')
    args = ap.parse_args(argv)

    port, token = args.port, args.token
    desc = '（命令行指定）'
    if not port or not token:
        p, t, desc = _load_console_creds(args.config, args.db)
        port = port or p
        token = token or t
    if not port or not token:
        print('未找到控制台凭证。请确认框架正在运行，且配置的数据库可读：%s\n'
              '（框架启动时会把端口与 token 写入该库的 console_access 表）\n'
              '也可用 --port/--token 直接指定。' % desc)
        return 1

    client = ConsoleClient(args.host, port, token)
    try:
        client.connect()
    except Exception as e:  # noqa: BLE001
        print('连接失败：%s（%s:%s）' % (e, args.host, port))
        return 1

    try:
        if args.command:
            text = ' '.join(args.command)
            print(client.run_agent(text) if args.agent else client.run(text), end='')
            return 0
        if args.agent:
            print('已接入 ZCBOT 智能体（%s:%s）。直接输入需求，exit 退出。' % (args.host, port))
            prompt = 'ai> '
        else:
            print('已接入 ZCBOT 调试控制台（%s:%s）。输入终端命令，exit 退出。' % (args.host, port))
            print('常用：help / status / plugins / reload <名> / log；'
                  '行首加 ! 交给 AI（如 `!写个 hello.py`）。')
            prompt = 'zcbot> '
        while True:
            try:
                line = input(prompt)
            except (EOFError, KeyboardInterrupt):
                print()
                break
            line = (line or '').strip()
            if not line:
                continue
            if line in ('exit', 'quit', ':q'):
                break
            try:
                if args.agent:
                    out = client.run_agent(line)
                elif line.startswith('!'):
                    out = client.run_agent(line[1:].strip())
                else:
                    out = client.run(line)
                print(out, end='')
            except Exception as e:  # noqa: BLE001
                print('[错误] %s' % e)
                break
    finally:
        client.close()
    return 0


def _pop_language_arg(argv):
    """从 argv 中摘出语言选项：--language LANG / -l LANG / --language=LANG。

    摘掉后剩余参数继续走原有的「子命令 / 配置文件路径」解析，避免被后面的
    「未知选项」检查误杀。返回 (新 argv, 语言代码或 None)。
    """
    out = []
    lang = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith('--language='):
            lang = a.split('=', 1)[1].strip()
        elif a in ('--language', '-l'):
            if i + 1 < len(argv):
                lang = argv[i + 1].strip()
                i += 1
        else:
            out.append(a)
        i += 1
    return out, (lang or None)


def main():
    """启动入口"""
    # 终端编码对齐：Python UTF-8 模式 vs Windows 控制台代码页不匹配会导致中文乱码
    try:
        from framework.terminal.encoding import ensure_utf8_console
        ensure_utf8_console()
    except Exception:
        pass

    # 启动前依赖自检（移机自愈）
    _check_and_install_deps()

    # 解析参数：子命令 → 未知选项报错 → 可选的一个配置文件路径
    argv = sys.argv[1:]
    # 语言选项（--language / -l）：优先于 config.yaml 的 language
    argv, _lang = _pop_language_arg(argv)
    if _lang:
        os.environ['ZCBOT_LANG'] = _lang
    # 同步回 sys.argv：amain() 等下游直接读 sys.argv[1] 取配置文件路径，
    # 不同步会把摘掉的语言选项误当成配置文件路径（历史坑的同类问题：
    # `python main.py argparse` 曾在根目录生成一个叫 argparse 的默认配置）
    sys.argv = [sys.argv[0]] + argv
    if argv[:1] and argv[0] in ('-h', '--help', 'help'):
        _print_usage()
        return
    if argv[:1] and argv[0] in ('-v', '--version'):
        print(_read_version())
        return
    if argv[:1] and argv[0] in ('code', 'cc'):
        try:
            from core_plugins.aiwriter.main import run_cli
        except Exception as e:  # noqa: BLE001
            print(f"[code] 无法加载 AI 智能体界面: {e}")
            sys.exit(1)
        sys.exit(run_cli(argv[1] if len(argv) > 1 else None))

    # 子命令：attach（别名 -a）→ 接入运行中的框架调试控制台（无需 TTY / 无需重启）
    if argv[:1] and argv[0] in ('attach', '-a'):
        sys.exit(_run_attach(argv[1:]))

    # 脚手架子命令 → 统一交给 framework.cli（等价于 `zb <cmd>`）：
    #   create / init / run / generate / plugin / adapter / driver / self / clean / config
    # 这些子命令不与「配置文件路径」语义冲突，优先识别并路由；其余位置参数仍当配置路径。
    _CLI_GROUPS = ('create', 'init', 'run', 'generate', 'plugin',
                   'adapter', 'driver', 'self', 'clean', 'config')
    if argv[:1] and argv[0] in _CLI_GROUPS:
        try:
            from framework.cli import main as _cli_main
        except Exception as e:  # noqa: BLE001
            print('[cli] 无法加载 CLI 模块: %s' % e)
            sys.exit(1)
        sys.exit(_cli_main(argv))

    # 子命令：--iipc / -iipc → 走 IIPC 内核链路（Go 内核 + Rust 协议端 + 独立进程插件）
    #   与上面的 asyncio 框架是并列的两条启动路径：插件、DB、事件语义 1:1 对齐，
    #   但进程拓扑不同（插件在独立进程、DB 在 Go 侧执行，不占 Python GIL）。
    #   不带此开关时框架行为完全不变。
    if argv[:1] and argv[0] in ('--iipc', '-iipc'):
        try:
            from framework.iipc import run as _iipc_run
        except Exception as e:  # noqa: BLE001
            print('[iipc] 无法加载 IIPC 链路: %s' % e)
            print('[iipc] 确认 framework/iipc/ 源码完整；IIPC 模式需要 go / cargo 工具链'
                  '（也可设 ZCBOT_GO_BIN / ZCBOT_CARGO 指定路径）。'
                  '注意：go/cargo 仅 --iipc 模式需要，基础 `python main.py`（asyncio 引擎）不需要。')
            sys.exit(1)
        sys.exit(_iipc_run(argv[1:]) or 0)

    # 未知选项：不要静默忽略（历史坑：`main.py -a` 被当无事发生、`main.py argparse`
    # 被当成配置文件路径并在根目录生成一个叫 argparse 的默认配置）
    unknown = [a for a in argv if a.startswith('-')]
    if unknown:
        print('错误：未知选项 %s\n' % ' '.join(unknown))
        _print_usage()
        sys.exit(2)
    positional = [a for a in argv if not a.startswith('-')]
    if len(positional) > 1:
        print('错误：参数过多 %s\n' % ' '.join(positional))
        _print_usage()
        sys.exit(2)
    config_path = positional[0] if positional else None

    # 显式传入的配置文件必须存在——不要因为拼错参数就静默生成一个同名默认配置
    # （历史坑：`python main.py argparse` 会在根目录生成一个叫 argparse 的配置文件）
    if config_path is not None and not os.path.isfile(config_path):
        print(f"错误：配置文件不存在，也不是已知子命令: {config_path}\n")
        _print_usage()
        sys.exit(2)

    # 双进程模式：由 config.yaml 的 dual_process.enabled 门控（默认关闭，行为不变）
    try:
        from framework.config import load_config
        cfg = load_config(config_path)
    except Exception:
        cfg = {}
    dual = cfg.get('dual_process', {}) or {}
    if dual.get('enabled'):
        from framework.ipc.core_runtime import CoreRuntime
        CoreRuntime(config_path).run()
        return

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        from framework import i18n
        print("\n" + i18n.t('app.ctrl_c'))


if __name__ == '__main__':
    # freeze_support：防止 spawn 的宿主子进程重复执行本入口
    multiprocessing.freeze_support()
    main()
