#!/usr/bin/env python3
"""
ZCBOT OneBot QQ机器人框架 · 启动入口
项目地址：https://github.com/kuangxing6367/zcbot
"""
import os
import re
import subprocess
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
    # 跳过注释行和空行
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


def main():
    """启动框架"""
    # 启动前依赖自检（移机自愈）
    _check_and_install_deps()

    from framework.core import Framework, check_internal_heartbeat
    import time

    # 支持命令行参数指定配置文件路径
    config_path = sys.argv[1] if len(sys.argv) > 1 else None

    framework = Framework(config_path)

    try:
        framework.start()

        # ── 主循环 + 内部心跳检测（GIL 假死看门狗）──
        # 如果某个插件死循环/无限递归占死 GIL，check_internal_heartbeat()
        # 3 秒内没被心跳线程刷新 → 调用 os._exit(1) 强制退出，让系统看门狗重启
        _watchdog_false_alarm_count = 0  # 连续失败计数，避免单次调度抖动误杀
        while True:
            time.sleep(1)
            if not check_internal_heartbeat(timeout_s=3.0):
                _watchdog_false_alarm_count += 1
                # 必须连续 2 次（2 秒间隔）都失败才真退出，抗抖动
                if _watchdog_false_alarm_count >= 2:
                    # 直接写 stderr，因为 logging 此时可能已经被 GIL 卡死无法输出
                    sys.stderr.write(
                        f"[FATAL][{time.strftime('%H:%M:%S')}] "
                        "内部心跳超时，疑似插件死循环占死 GIL，"
                        "强制退出进程 (os._exit(1))，由系统看门狗重启...\n"
                    )
                    sys.stderr.flush()
                    try:
                        os._exit(1)
                    except Exception:
                        # 极端情况下 os._exit 也会失败，那就再暴力一点
                        os.kill(os.getpid(), 9)
            else:
                _watchdog_false_alarm_count = 0

    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在退出...")
        framework.stop()
    except Exception as e:
        print(f"框架异常退出: {e}")
        framework.stop()
        sys.exit(1)


if __name__ == '__main__':
    main()