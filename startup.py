#!/usr/bin/env python3

#                    _ooOoo_
#                   o8888888o
#                   88" . "88
#                   (| -_- |)
#                    O\ = /O
#                ____/`---'\____
#              .   ' \\| |// `.
#               / \\||| : |||// \
#             / _||||| -:- |||||- \
#               | | \\\ - /// | |
#             | \_| ''\---/'' | |
#              \ .-\__ `-` ___/-. /
#           ___`. .' /--.--\ `. . __
#        ."" '< `.___\_<|>_/___.' >'"".
#       | | : `- \`.;`\ _ /`;.`/ - ` : | |
#         \ \ `-. \_ __\ /__ _/ .-` / /
# ======`-.____`-.___\_____/___.-`____.-'======
#                    `=---='
#
# .............................................
#          佛祖保佑             永无BUG

"""ZCBOT 事件驱动 IM 平台 · 唯一启动入口。

所有启动链路（源码运行 / pip 安装态 / `zb generate` 生成的脚本 / 打包脚本）
统一从本模块进入；根目录 ``main.py`` 只保留为向后兼容的薄 shim，不再承载
启动逻辑。启动期细节按职责分派到 ``framework/bootstrap/``：

- 内存分配器策略（可能 execv 自重启）        framework/bootstrap/allocator.py
- 依赖自检与自动补装                      framework/bootstrap/deps_check.py
- 单实例文件锁（跨平台）                    framework/bootstrap/instance_lock.py
- 安全模式（崩溃守卫的假启动）              framework/bootstrap/safe_mode.py
- 异步主流程                              framework/bootstrap/app.py
- 调试控制台接入（attach）                 framework/bootstrap/console_attach.py
- 版本 / 帮助 / 语言参数解析                framework/bootstrap/cli_dispatch.py

对外兼容：cli 子命令（generate / project）与回归测试仍从 ``main`` 模块取符号，
本模块把这些符号按原名再导出，``main.py`` 从本模块转出，保持单一实现。
"""
import asyncio
import multiprocessing
import os
import sys

# 确保代码根目录在 sys.path 中（pip 安装态下 code_root 可能不在 cwd）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── 启动期引导（必须在 import 重型 framework 之前；可能 execv 自重启）──
# 只有「作为入口直接运行」才允许套用分配器策略并 execv 重启自身。
# 被 import 时（pytest 收集、打包脚本、插件复用本模块符号）绝不能替换调用方
# 进程：pytest 会把 fd 1/2 重定向到捕获临时文件，execv 出的新进程会继承那对
# fd，把用例跑进捕获文件里 —— 外部表现是「零输出 + 退出码 0」的假绿。
from framework.bootstrap.allocator import apply_allocator_policy

if __name__ == '__main__':
    apply_allocator_policy()

# 对外兼容再导出（cli 子命令与回归测试直接 import 这些符号）
from framework.bootstrap.app import amain
from framework.bootstrap.cli_dispatch import (
    pop_language_arg as _pop_language_arg,
)
from framework.bootstrap.cli_dispatch import (
    print_usage as _print_usage,
)
from framework.bootstrap.cli_dispatch import (
    read_version as _read_version,
)
from framework.bootstrap.console_attach import run_attach as _run_attach
from framework.bootstrap.deps_check import (
    check_and_install_deps as _check_and_install_deps,
)
from framework.bootstrap.deps_check import (
    ensure_optional_driver as _ensure_optional_driver,
)
from framework.bootstrap.deps_check import (
    read_self_check_allow_missing as _read_self_check_allow_missing,
)
from framework.bootstrap.deps_check import (
    resolve_self_check_missing as _resolve_self_check_missing,
)


def _config_syntax_ok(path):
    """只判断 YAML 语法能否解析：不校验语义、不写文件、不生成默认配置。"""
    try:
        import yaml
        with open(path, encoding='utf-8') as f:
            yaml.safe_load(f)
        return True
    except Exception:
        return False


def _run_auto_migration(config_path=None):
    """启动期状态目录迁移：默认开启，config.yaml 可手动关闭。

    必须在 load_config() 之前执行——迁移会改变主配置的物理路径；
    失败不阻断启动，framework.paths 仍兼容旧根目录布局。
    """
    try:
        from framework.migration import ensure_auto_migrated
        result = ensure_auto_migrated(config_path)
    except Exception as e:
        print('[migration] 迁移模块不可用，按旧布局兼容运行: %s' % e)
        return
    if result.get('status') in ('applied', 'partial', 'manual-required',
                                'locked-skip', 'error'):
        print('[migration] %s: %s' % (result.get('status'), result.get('detail', '')))


def main():
    """启动入口：解析参数 → 选择执行链路 → 运行框架。"""
    # 终端编码对齐：Python UTF-8 模式 vs Windows 控制台代码页不匹配会导致中文乱码
    try:
        from framework.terminal.encoding import ensure_utf8_console
        ensure_utf8_console()
    except Exception:
        pass

    # 运行时下限：pyproject 声明 requires-python >= 3.12。绕过 pip 直接跑源码时
    # 给一条明确提示，免得后面在 asyncio / AST 上撞见难懂的报错。
    if sys.version_info < (3, 12):
        print('[startup] 本版本要求 Python 3.12 及以上，当前 %s；'
              '请升级解释器，或改用支持旧解释器的发行包。' % sys.version.split()[0])

    # 启动前依赖自检（移机自愈）
    _check_and_install_deps()

    # 解析参数：子命令 → 未知选项报错 → 可选的一个配置文件路径
    argv = sys.argv[1:]
    # 语言选项（--language / -l）：优先于 config.yaml 的 language
    argv, _lang = _pop_language_arg(argv)
    if _lang:
        os.environ['ZCBOT_LANG'] = _lang
    # 显式降级开关：等价于 framework.config.on_error: default，只影响语法坏掉的配置。
    if '--allow-default-config' in argv:
        os.environ['ZCBOT_ALLOW_DEFAULT_CONFIG'] = '1'
        argv = [a for a in argv if a != '--allow-default-config']
    # 同步回 sys.argv：amain() 等下游直接读 sys.argv[1] 取配置文件路径，
    # 不同步会把摘掉的语言选项误当成配置文件路径（历史坑的同类问题：
    # `python startup.py argparse` 曾会在根目录生成一个叫 argparse 的配置）
    sys.argv = [sys.argv[0]] + argv
    # 配置只读检查：转发到 framework.config --check，不启动框架、不写文件。
    if '--check-config' in argv:
        from framework.config import check_config

        rest = [a for a in argv if a != '--check-config']
        if len(rest) > 1:
            print('错误：--check-config 最多接受一个配置文件路径\n')
            sys.exit(2)
        sys.exit(check_config(rest[0] if rest else None))
    if argv[:1] and argv[0] in ('-h', '--help', 'help'):
        _print_usage()
        return
    if argv[:1] and argv[0] in ('-v', '--version'):
        print(_read_version())
        return
    if argv[:1] and argv[0] in ('code', 'cc'):
        try:
            from core_plugins.aiwriter.main import run_cli
        except Exception as e:
            print(f"[code] 无法加载 AI 智能体界面: {e}")
            sys.exit(1)
        sys.exit(run_cli(argv[1] if len(argv) > 1 else None))

    # 子命令：attach（别名 -a）→ 接入运行中的框架调试控制台（无需 TTY / 无需重启）
    if argv[:1] and argv[0] in ('attach', '-a'):
        sys.exit(_run_attach(argv[1:]))

    # 脚手架子命令 → 统一交给 framework.cli（等价于 `zb <cmd>`）：
    #   create / init / run / generate / plugin / adapter / driver / self / clean / config / debug
    # 这些子命令不与「配置文件路径」语义冲突，优先识别并路由；其余位置参数仍当配置路径。
    _CLI_GROUPS = ('create', 'init', 'run', 'generate', 'plugin',
                   'adapter', 'driver', 'self', 'clean', 'config', 'debug')
    if argv[:1] and argv[0] in _CLI_GROUPS:
        try:
            from framework.cli import main as _cli_main
        except Exception as e:
            print('[cli] 无法加载 CLI 模块: %s' % e)
            sys.exit(1)
        sys.exit(_cli_main(argv))

    # 已移除的第二条内核链路：给出明确说明，而不是当成未知选项一笔带过。
    if argv[:1] and argv[0] in ('--iipc', '-iipc', 'iipc'):
        print('错误：--iipc 启动方式已移除（framework/iipc 整体下线）。')
        print('      直接运行 `python startup.py` 即走 asyncio 主链路；')
        print('      插件进程隔离请使用双进程模式（config.yaml 的 dual_process 段）。')
        sys.exit(2)

    # 未知选项：不要静默忽略（历史坑：`-a` 被当无事发生、`argparse`
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
    from framework.paths import resolve_config_path
    if config_path is not None and not os.path.isfile(resolve_config_path(config_path)):
        print(f"错误：配置文件不存在，也不是已知子命令: {config_path}\n")
        _print_usage()
        sys.exit(2)

    # 状态目录自动迁移（默认开启；framework.auto_migrate: false 可关闭）
    # 必须在 load_config() 之前：迁移后主配置物理路径会变。
    # 因此这里先做一次「纯语法」预检：配置根本解析不了时不要搬动 data/ 目录，
    # 否则用户只是少写一个冒号，就会被顺带迁移一次数据目录（不可接受的副作用）。
    if _config_syntax_ok(resolve_config_path(config_path)):
        _run_auto_migration(config_path)

    # 双进程模式：由 config.yaml 的 dual_process.enabled 门控（默认关闭，行为不变）
    from framework.config import (
        ConfigLoadError,
        ConfigValidationError,
        load_config,
        report_config_load_error,
        report_config_validation_error,
    )

    _root = os.path.dirname(os.path.abspath(__file__))
    try:
        cfg = load_config(config_path)
    except ConfigLoadError as config_exc:
        report_config_load_error(config_exc, _root, config_path)
        sys.exit(2)
    except ConfigValidationError as config_exc:
        report_config_validation_error(config_exc, _root, config_path)
        sys.exit(2)
    except Exception as exc:
        # 其他读取失败也不能用内置默认值假装启动成功：宁可拒绝启动。
        print(f'[配置加载失败] 框架未启动，请检查配置文件与权限: {exc}', file=sys.stderr)
        sys.exit(2)
    # 可选数据库驱动的补装必须在 asyncio.run(amain()) 之前完成：
    # 放到 Framework() 构造里会同步阻塞事件循环最长 120s。
    _ensure_optional_driver(config_path)
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
