#!/usr/bin/env python3
"""
ZCBOT OneBot QQ机器人框架 · 启动入口（异步）
项目地址：https://github.com/kuangxing6367/zcbot
"""
import asyncio
import os
import re
import signal
import sys

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _check_and_install_deps():
    """
    启动前自检：扫描 requirements.txt，自动安装缺失的依赖
    解决移机/首次部署时依赖缺失导致 import 报错的问题
    """
    import importlib.metadata

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
            try:
                importlib.metadata.version(pkg_name)
            except importlib.metadata.PackageNotFoundError:
                missing.append(pkg_name)

    if not missing:
        return

    print(f"[自检] 检测到 {len(missing)} 个缺失依赖，正在自动安装...")
    print(f"[自检] 缺失: {', '.join(missing)}")

    # 走清华源 + 自动回退
    from framework.loader import pip_install_requirements
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
        await framework.start()
        # 等待停止信号 / Ctrl+C
        await stop_event.wait()
    finally:
        await framework.stop()


def main():
    """启动入口"""
    # 启动前依赖自检（移机自愈）
    _check_and_install_deps()

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，已退出。")


if __name__ == '__main__':
    main()
