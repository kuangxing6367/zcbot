#!/usr/bin/env python3
"""
ZCBOT 事件驱动 IM 平台 · 启动入口（异步）

本文件只作「薄派发层」，所有启动期细节下沉到 framework/bootstrap/：
- 内存分配器策略（可能 execv 自重启）        framework/bootstrap/allocator.py
- 依赖自检与自动补装                      framework/bootstrap/deps_check.py
- 单实例文件锁（跨平台）                    framework/bootstrap/instance_lock.py
- 安全模式（崩溃守卫的假启动）              framework/bootstrap/safe_mode.py
- 异步主流程                              framework/bootstrap/app.py
- 调试控制台接入（attach）                 framework/bootstrap/console_attach.py
- 版本 / 帮助 / 语言参数解析                framework/bootstrap/cli_dispatch.py

对外兼容：cli 子命令（generate / project）与回归测试仍从本模块取符号，
故在此再导出（带原下划线名）。
"""
import asyncio
import multiprocessing
import os
import sys

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── 启动期引导（必须在 import 重型 framework 之前；可能 execv 自重启）──
from framework.bootstrap.allocator import apply_allocator_policy
apply_allocator_policy()

# 对外兼容再导出（cli 子命令与回归测试直接 import 这些符号）
from framework.bootstrap.deps_check import (
    read_self_check_allow_missing as _read_self_check_allow_missing,
    resolve_self_check_missing as _resolve_self_check_missing,
    check_and_install_deps as _check_and_install_deps,
)
from framework.bootstrap.cli_dispatch import (
    print_usage as _print_usage,
    read_version as _read_version,
    pop_language_arg as _pop_language_arg,
)
from framework.bootstrap.console_attach import run_attach as _run_attach
from framework.bootstrap.app import amain


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
