"""异步主流程：装配框架、崩溃守卫、信号、实例锁，启动并等待停止。"""
import asyncio
import atexit
import logging
import os
import signal
import sys

from framework.bootstrap.instance_lock import acquire_instance_lock, cleanup_instance_lock
from framework.bootstrap.safe_mode import enter_safe_mode

logger = logging.getLogger('zcbot')

# framework/bootstrap/app.py 上溯三级即项目根：app -> bootstrap -> framework -> 根
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def amain():
    """异步主流程"""
    from framework.core import Framework
    from framework.crash_guard import (should_abort_boot, write_crash_log,
                                        clear_crash_markers, fire_alert)

    # 支持命令行参数指定配置文件路径
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    root = _PROJECT_ROOT

    # 单实例文件锁：防止守护进程无脑拉起导致多实例（抢端口 / 抢 DB / 双写）。
    # None = 确实已有实例在跑 → 立即退出；False = 锁文件不可用（只读目录等），
    # 只跳过保护、照常启动，不能把「没有保护」误判成「有另一个实例」。
    lock_fd = acquire_instance_lock(root)
    if lock_fd is None:
        sys.exit(1)
    # 注册退出清理（双保险：atexit + 顶层 finally），确保锁文件被释放并删除
    atexit.register(cleanup_instance_lock)

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
            # 进程真正退出时由 cleanup_instance_lock()（atexit）释放并删除锁文件。
            await enter_safe_mode(stop_event, crash_cfg, root)
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

    # 事件循环卡顿看门狗：尽早启动，覆盖启动期（插件 import、建表、大配置解析
    # 同样会阻塞循环，而那正是「启动卡住但没日志」最难查的阶段）。
    # 配置段：framework.loop_watchdog.{enabled,interval,threshold,stall_wait}
    watchdog = None
    try:
        from framework.core import loop_watchdog as _loop_watchdog
        _wd_cfg = ((crash_cfg.get('framework') or {}).get('loop_watchdog') or {})
        if _wd_cfg.get('enabled', True):
            watchdog = _loop_watchdog.configure(
                interval=_wd_cfg.get('interval'),
                threshold=_wd_cfg.get('threshold'),
                stall_wait=_wd_cfg.get('stall_wait'),
            ).start(loop=loop)
    except Exception as e:  # noqa: BLE001 - 诊断设施失败不能挡住启动
        logger.debug(f"事件循环看门狗启动失败: {e}")
        watchdog = None

    framework = None
    # 启动期暂停告警：装依赖 / 建表 / 同步加载全部插件必然阻塞循环，
    # 这段时间报警等于每次启动刷几条 warning，噪音会让真卡顿被忽略。
    # 启动期延迟仍会记录，resume() 返回峰值。
    # 注意：必须在 Framework() 构造**之前**暂停 —— 构造里就要装 MySQL 驱动、
    # 初始化连接池、建表，那是最容易触发「阻塞 ≥500ms」的一段（此前暂停
    # 放在构造之后，于是每次首启都先刷一条无意义的阻塞告警）。
    if watchdog is not None:
        try:
            watchdog.suspend()
        except Exception:
            pass
    try:
        framework = Framework(config_path)
        # 异步启动模式：config.yaml → startup.wait_ready: false 时不等待插件加载完成，
        # 核心/用户插件在后台异步加载，启动立即返回（可后续 await framework.wait_ready()）
        _startup_cfg = framework.config.get('startup', {}) or {}
        try:
            await framework.start(wait_ready=_startup_cfg.get('wait_ready', True))
        finally:
            if watchdog is not None:
                try:
                    watchdog.resume()
                except Exception:
                    pass
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
        if watchdog is not None:
            try:
                watchdog.stop()
            except Exception:
                pass
        if framework is not None:
            try:
                await framework.stop()
            except Exception:
                pass
        if lock_fd is not None:
            cleanup_instance_lock()
