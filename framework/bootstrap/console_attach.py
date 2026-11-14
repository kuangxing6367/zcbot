"""调试控制台接入（attach）：连运行中的框架，无需 TTY / 无需重启。"""
import argparse
import os

from framework.paths import code_root, resolve_config_path

_PROJECT_ROOT = code_root()


def resolve_db_config(config_path=None):
    """从 config.yaml 解析 database 段（dict 或 sqlite 路径字符串）。"""
    path = resolve_config_path(config_path)
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
    return db, _PROJECT_ROOT


def load_console_creds(config_path=None, db_override=None):
    """读取控制台凭证（支持 sqlite / mysql）。返回 (port, token, 描述)。"""
    from framework.console_server import load_credentials_any
    if db_override:
        port, token = load_credentials_any({'type': 'sqlite', 'path': db_override})
        return port, token, db_override
    db, root = resolve_db_config(config_path)
    typ = str((db or {}).get('type') or 'sqlite').lower()
    if isinstance(db, dict) and typ == 'mysql':
        desc = 'mysql %s@%s:%s/%s' % (db.get('user', ''), db.get('host', '127.0.0.1'),
                                      db.get('port', 3306), db.get('database', 'zcbot'))
    else:
        p = db if isinstance(db, str) else (db or {}).get('path') or 'data/zcbot.db'
        desc = p if os.path.isabs(p) else os.path.join(root, p)
    return load_credentials_any(db, project_root=root) + (desc,)


def run_attach(argv):
    """接入运行中的框架调试控制台（端口 + 超长 token 存于数据库，无需 TTY/重启）。"""
    from framework.console_server import ConsoleClient

    ap = argparse.ArgumentParser(
        prog='python startup.py attach',
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
        p, t, desc = load_console_creds(args.config, args.db)
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
    except Exception as e:
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
            except Exception as e:
                print('[错误] %s' % e)
                break
    finally:
        client.close()
    return 0
