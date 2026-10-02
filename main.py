#!/usr/bin/env python3
"""
ZCBOT 事件驱动 IM 平台 · 启动入口（异步）
项目地址：https://github.com/kuangxing6367/zcbot
"""
import asyncio
import multiprocessing
import os
import re
import signal
import sys

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


def _check_and_install_deps():
    """
    启动前自检：扫描 requirements.txt，自动安装缺失的依赖
    解决移机/首次部署时依赖缺失导致 import 报错的问题
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

    # 解析 requirements.txt，提取需要检查的包名
    missing = []
    with open(req_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # 提取包名（去掉版本约束和注释）
            m = re.match(r'^([a-zA-Z0-9_.\-]+)', line)
            if not m:
                continue
            pkg_name = m.group(1)
            if _norm(pkg_name) not in installed:
                missing.append(pkg_name)

    if not missing:
        return

    print(f"[自检] 检测到 {len(missing)} 个缺失依赖，正在自动安装...")
    print(f"[自检] 缺失: {', '.join(missing)}")

    # 只走 framework.deps.pip（纯标准库），不能 import framework.loader ——
    # loader/base 顶层 import yaml，缺的正是它，会让自检安装器自身崩溃
    from framework.deps.pip import pip_install_requirements
    result = pip_install_requirements(sys.executable, req_file, timeout=300)
    if result['success']:
        print(f"[自检] 依赖安装完成（镜像: {result['mirror']}）")
    else:
        print(f"[自检] 依赖安装失败: {result['error']}")
        print(f"[自检] 请手动执行: pip install -r requirements.txt")
        sys.exit(1)


async def amain():
    """异步主流程"""
    from framework.core import Framework

    # 支持命令行参数指定配置文件路径
    config_path = sys.argv[1] if len(sys.argv) > 1 else None

    framework = Framework(config_path)

    # 注册停止信号（Windows 上 add_signal_handler 可能不支持，忽略即可）
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        # 异步启动模式：config.yaml → startup.wait_ready: false 时不等待插件加载完成，
        # 核心/用户插件在后台异步加载，启动立即返回（可后续 await framework.wait_ready()）
        _startup_cfg = framework.config.get('startup', {}) or {}
        await framework.start(wait_ready=_startup_cfg.get('wait_ready', True))
        # 等待停止信号 / Ctrl+C
        await stop_event.wait()
    finally:
        await framework.stop()


def main():
    """启动入口"""
    # 启动前依赖自检（移机自愈）
    _check_and_install_deps()

    # 解析配置文件路径（首个非 '-' 开头的参数）
    config_path = None
    for arg in sys.argv[1:]:
        if not arg.startswith('-'):
            config_path = arg
            break

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
        print("\n收到 Ctrl+C，已退出。")


if __name__ == '__main__':
    # freeze_support：防止 spawn 的宿主子进程重复执行本入口
    multiprocessing.freeze_support()
    main()
