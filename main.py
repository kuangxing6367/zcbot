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
  python main.py --help          显示本帮助

attach 常用参数：
  --config <path>   指定 config.yaml（据此定位数据库里的控制台凭证）
  --db <path>       直接指定 SQLite 库路径
  --port/--token    直接指定控制台端口与 token（跳过读库）
  --agent           每行都交给 AI（正常模式下也可用行首 `!` 单条交给 AI）""")


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
        print("\n收到 Ctrl+C，已退出。")


if __name__ == '__main__':
    # freeze_support：防止 spawn 的宿主子进程重复执行本入口
    multiprocessing.freeze_support()
    main()
