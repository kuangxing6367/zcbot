#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZCBOT WebUI 黑盒测试启动器（仅负责启动一个全新实例，不做任何测试）

用途：给安全审计 / AI 黑盒测试提供一个「从零开始、凭据随机、环境隔离」的 WebUI 实例。
- 随机生成管理密码、随机端口（均可用参数覆盖）
- 写入一份隔离的临时 config.yaml + 临时 SQLite 库 + 隔离的 core_plugins.yaml
  （绝不碰你真实的 data/zcbot.db 与仓库里的 core_plugins.yaml）
- 拉起框架（python main.py），待端口就绪后把随机密码注入 admin_users（bcrypt）
- 打印访问地址 + 账号密码；默认尝试打开浏览器（--no-open 关闭）
- 仅启动：Ctrl-C 停止并自动清理临时文件（--keep 保留以便复查）

跨平台：Windows / Linux / macOS 均可用（打开浏览器用对应系统命令；Linux 无桌面时加 --no-open）。

注意：本脚本只负责「拉起被测目标」，黑盒测试由你（或 AI）另行进行。
"""
import argparse
import atexit
import os
import secrets
import signal
import socket
import sqlite3
import string
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import bcrypt
except Exception:
    bcrypt = None

try:
    import yaml
except Exception:
    yaml = None

ROOT = Path(__file__).resolve().parent.parent  # test/ -> 项目根


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def log(msg):
    print(f"[blackbox] {msg}", flush=True)


def gen_secret(n=16):
    """生成一段够随机的密码 / 令牌。"""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(secrets.choice(alphabet) for _ in range(n))


def free_port():
    """找一个当前空闲的端口。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def tcp_up(host, port, timeout=60):
    """轮询 TCP 连通性，确认服务已起来。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def open_browser(url):
    """跨平台打开浏览器（失败静默忽略）。"""
    try:
        if sys.platform.startswith('win'):
            os.startfile(url)
        elif sys.platform.startswith('darwin'):
            subprocess.run(['open', url], check=False)
        else:
            subprocess.run(['xdg-open', url], check=False)
    except Exception:
        pass


def deep_merge(base, override):
    """递归合并配置字典（override 优先）。"""
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def build_config(root, args, port, api_port, db_path):
    """基于框架默认配置生成一份临时 config.yaml（只覆盖我们关心的键）。"""
    if yaml is None:
        raise RuntimeError("缺少依赖 yaml（pip install pyyaml）")
    sys.path.insert(0, str(root))
    from framework.config import _DEFAULT_CONFIG  # 框架默认配置模板
    cfg = yaml.safe_load(_DEFAULT_CONFIG) or {}
    override = {
        'web': {
            'host': args.host,
            'port': port,
            'ui_enabled': not args.api_only,
        },
        'database': {
            'type': 'sqlite',
            'path': str(db_path),
        },
        'security': {
            'level': args.security,
        },
    }
    if api_port:
        override['web']['api_port'] = api_port
    deep_merge(cfg, override)
    return cfg


def build_core_plugins(root, args, port, host):
    """生成隔离的 core_plugins.yaml：保留真实插件配置，仅覆盖 webui 端口/host。

    框架在 _autoload_core_plugins 中会把 core_plugins.yaml 的 webui 块
    merge 进 config['web']，从而覆盖 config.yaml 里的 web.port。
    因此要让黑盒实例真正监听随机端口，必须把 webui.port 也指向随机端口——
    通过 ZCBOT_CORE_PLUGINS_YAML 环境变量让框架读取这份隔离副本，
    绝不改动仓库里的真实 core_plugins.yaml。
    """
    real = root / 'core_plugins.yaml'
    data = {}
    if real.exists():
        try:
            data = yaml.safe_load(real.read_text(encoding='utf-8')) or {}
        except Exception:
            data = {}
    cps = data.get('core_plugins') if isinstance(data.get('core_plugins'), dict) else {}
    webui = cps.get('webui') if isinstance(cps.get('webui'), dict) else {}
    webui['enabled'] = webui.get('enabled', True)
    webui['host'] = host
    webui['port'] = port
    # 注意：纯 API 模式不在这里关闭 webui 插件——否则 webui 插件会注册假节点，
    # 导致 framework/api/webserver.py 的 WebServer 不被实例化，进而 API 端口也起不来。
    # 纯 API 模式由 config.yaml 的 web.ui_enabled=false 驱动 WebServer.start()
    # 只起 API 线程实现，本处仅需保证 webui.port 指向随机端口即可。
    cps['webui'] = webui
    data['core_plugins'] = cps
    return data


def inject_password(db_path, username, password, attempts=25):
    """把随机密码以 bcrypt 写入隔离库的 admin_users（服务起来后执行）。"""
    if bcrypt is None:
        log("警告: 当前环境没有 bcrypt，无法注入随机密码；默认 admin/admin123 仍然有效。")
        return False
    if not Path(db_path).exists():
        log(f"警告: 数据库文件不存在（{db_path}），跳过密码注入。")
        return False
    h = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(12)).decode('utf-8')
    last_err = None
    for i in range(attempts):
        try:
            con = sqlite3.connect(db_path, timeout=5)
            try:
                con.execute(
                    "UPDATE admin_users SET password_hash=? WHERE username=?",
                    (h, username),
                )
                con.commit()
            finally:
                con.close()
            log(f"已将随机密码写入 admin_users（{username}）")
            return True
        except Exception as e:  # 可能是建表瞬间写锁竞争，重试几次
            last_err = e
            time.sleep(0.5)
    log(f"警告: 密码注入失败（{last_err}），默认 admin/admin123 仍有效。")
    return False


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="ZCBOT WebUI 黑盒测试启动器（仅启动，不做测试）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('--host', default='127.0.0.1', help='监听地址（默认 127.0.0.1）')
    ap.add_argument('--port', type=int, default=0, help='WebUI 端口（默认随机空闲端口）')
    ap.add_argument('--api-port', type=int, default=0, help='API 独立端口（0=不隔离，与 WebUI 同端口）')
    ap.add_argument('--security', default='medium',
                    choices=['off', 'low', 'medium', 'high', 'paranoid'],
                    help='安全等级（默认 medium）')
    ap.add_argument('--username', default='admin', help='管理员账号（默认 admin）')
    ap.add_argument('--password', default='', help='管理密码（默认随机生成）')
    ap.add_argument('--api-only', action='store_true',
                    help='纯 API 模式：ui_enabled=false，不启动 WebUI 前端')
    ap.add_argument('--no-open', action='store_true', help='不尝试打开浏览器')
    ap.add_argument('--keep', action='store_true', help='退出时保留临时 config / 数据库')
    ap.add_argument('--timeout', type=int, default=90, help='等待服务就绪的秒数（默认 90）')
    ap.add_argument('--print-only', action='store_true',
                    help='只生成配置与凭据并打印，不启动服务（便于 AI 先查看）')
    ap.add_argument('--root', default=str(ROOT), help='项目根目录（默认自动推断）')
    args = ap.parse_args()

    root = Path(args.root).resolve()

    password = args.password or gen_secret(18)
    port = args.port or free_port()
    api_port = args.api_port or 0
    db_path = Path(tempfile.gettempdir()) / f"zcbot_blackbox_{os.getpid()}.db"
    run_dir = Path(tempfile.gettempdir()) / f"zcbot_blackbox_{os.getpid()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = run_dir / 'config.yaml'

    # 生成隔离配置
    cfg = build_config(root, args, port, api_port, db_path)
    cfg_path.write_text(
        "# 由 test/webui_blackbox_launcher.py 生成的隔离配置（用于黑盒测试）\n"
        + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        encoding='utf-8',
    )
    log(f"临时配置: {cfg_path}")
    log(f"临时数据库: {db_path}")

    if args.print_only:
        print("\n========== 黑盒实例配置（未启动）==========")
        print(f"  配置:   {cfg_path}")
        print(f"  数据库: {db_path}")
        print(f"  地址:   http://{args.host}:{port}/")
        if api_port:
            print(f"  API:    http://{args.host}:{api_port}/")
        print(f"  账号:   {args.username}")
        print(f"  密码:   {password}")
        print(f"  安全等级: {args.security}   纯API: {args.api_only}")
        print("============================================\n")
        if not args.keep:
            cfg_path.unlink(missing_ok=True)
            db_path.unlink(missing_ok=True)
            run_dir.rmdir() if run_dir.exists() else None
        return

    # 生成隔离的 core_plugins.yaml（仅覆盖 webui 端口，使其使用随机端口）
    cp_data = build_core_plugins(root, args, port, args.host)
    cp_path = run_dir / 'core_plugins.yaml'
    cp_path.write_text(
        "# 由 test/webui_blackbox_launcher.py 生成的隔离插件配置（用于黑盒测试）\n"
        + yaml.safe_dump(cp_data, allow_unicode=True, sort_keys=False),
        encoding='utf-8',
    )
    log(f"临时插件配置: {cp_path}")

    # 启动框架
    main_py = root / 'main.py'
    if not main_py.exists():
        log(f"错误: 找不到 {main_py}")
        sys.exit(2)
    log(f"启动框架: {sys.executable} main.py {cfg_path}")
    env = dict(os.environ)
    env['ZCBOT_CORE_PLUGINS_YAML'] = str(cp_path)
    proc = subprocess.Popen(
        [sys.executable, 'main.py', str(cfg_path)],
        cwd=str(root),
        stdout=None, stderr=None,
        env=env,
    )

    cleanup_files = [cfg_path, db_path, run_dir, cp_path]
    stopped = {'proc': proc}

    def cleanup():
        p = stopped.get('proc')
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=10)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        if not args.keep:
            for f in cleanup_files:
                try:
                    if f.is_dir():
                        import shutil
                        shutil.rmtree(f, ignore_errors=True)
                    else:
                        f.unlink(missing_ok=True)
                except Exception:
                    pass

    atexit.register(cleanup)

    def _sig(_n, _f):
        log("收到停止信号，正在清理……")
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    # 等待端口就绪
    listen_ports = [port]
    if api_port and not args.api_only:
        listen_ports.append(api_port)
    if args.api_only and api_port:
        listen_ports = [api_port]
    log(f"等待端口就绪 {listen_ports}（最多 {args.timeout}s）……")
    # 任一端口通即认为起来了
    ready = False
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if any(tcp_up('127.0.0.1', p, timeout=2) for p in listen_ports):
            ready = True
            break
        if proc.poll() is not None:
            log(f"框架进程已退出（码 {proc.returncode}），启动失败，请查看上方日志。")
            cleanup()
            sys.exit(3)
        time.sleep(0.5)
    if not ready:
        log("等待超时，服务未能在限定时间内就绪。")
        cleanup()
        sys.exit(4)

    # 注入随机密码
    inject_password(db_path, args.username, password)

    ui_port = api_port if args.api_only else port
    url = f"http://{args.host}:{ui_port}/"
    print("\n==================================================")
    print(" ZCBOT WebUI 黑盒测试实例已启动")
    print("--------------------------------------------------")
    print(f"  访问地址: {url}")
    if api_port:
        print(f"  API 地址: http://{args.host}:{api_port}/")
    print(f"  管理员账号: {args.username}")
    print(f"  管理员密码: {password}")
    print(f"  安全等级:   {args.security}")
    print(f"  纯 API 模式: {args.api_only}")
    print(f"  临时配置:   {cfg_path}")
    print(f"  临时数据库: {db_path}")
    print("  按 Ctrl-C 停止并清理（--keep 可保留临时文件）")
    print("==================================================\n")

    if not args.no_open:
        open_browser(url)

    # 前台驻留，直到被 Ctrl-C
    try:
        while True:
            if proc.poll() is not None:
                log(f"框架进程已退出（码 {proc.returncode}）。")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        log("收到 Ctrl-C，正在清理……")
    finally:
        cleanup()


if __name__ == '__main__':
    main()
