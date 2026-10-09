# -*- coding: utf-8 -*-
"""框架内置的本地调试控制台 —— 无 TTY 也能接进运行中的框架。

解决的问题：框架由 systemd 之类托管时没有交互终端，想调试只能重启。这里在
127.0.0.1 上监听一个**自动分配**的本地端口，端口与**超长随机 token** 在首次
启动时生成并**存入数据库**（`console_access` 表），之后每次启动复用。本地执行

    python main.py attach

即可接入，直接使用框架的终端命令（help / status / plugins / reload / …），
无需重启、无需 TTY。

协议：JSON Lines（一行一个 JSON 对象，UTF-8）
    连接后服务端先发 {"banner": ..., "version": ...}
    客户端发 {"token": "..."}  → {"ok": true, "commands": [...]} 或 {"ok": false} 后断开
    客户端发 {"cmd": "plugins"} → {"ok": true, "output": "..."}
    客户端发 {"close": true} 或直接断开 → 结束会话

安全：只绑定回环地址；token 长度可配（默认 16384 bit = 2048 字节），
比较用 `hmac.compare_digest` 常量时间，避免时序侧信道。
"""
import hmac
import json
import logging
import os
import secrets
import socket
import threading
import time

logger = logging.getLogger('zcbot')

DEFAULT_TOKEN_BITS = 16384

_CREATE_SQL = (
    "CREATE TABLE IF NOT EXISTS console_access ("
    " id INTEGER PRIMARY KEY,"
    " port INTEGER NOT NULL,"
    " token TEXT NOT NULL,"
    " token_bits INTEGER,"
    " created_at INTEGER,"
    " updated_at INTEGER)"
)


def _brief(obj, limit=200):
    s = obj if isinstance(obj, str) else str(obj)
    s = s.replace('\n', ' ')
    return s if len(s) <= limit else s[:limit] + '…'


class ConsoleServer:
    """本地调试控制台服务端（随框架启动/停止）。"""

    def __init__(self, framework, host='127.0.0.1', port=0, token_bits=DEFAULT_TOKEN_BITS,
                 backlog=8, read_timeout=3600):
        self.fw = framework
        self.host = host or '127.0.0.1'
        self.port = int(port or 0)
        self.token_bits = max(256, int(token_bits or DEFAULT_TOKEN_BITS))
        self.backlog = max(1, int(backlog or 8))
        self.read_timeout = max(0, int(read_timeout or 0))
        self.token = ''
        self._sock = None
        self._thread = None
        self._stop = False

    # ── 凭证（端口 + token）────────────────────────────────────────────
    def _db(self):
        return getattr(self.fw, 'db', None)

    def _ensure_table(self, db):
        try:
            db.execute(_CREATE_SQL)
        except Exception as e:  # noqa: BLE001
            logger.debug("console_access 建表失败: %s", e)

    def _load_or_create(self):
        db = self._db()
        row = None
        if db is not None:
            self._ensure_table(db)
            # 读凭证：短暂失败（库被占用等）重试几次，避免误判为「没有凭证」而重新生成
            for attempt in range(3):
                try:
                    row = db.query_one(
                        "SELECT port, token, token_bits FROM console_access WHERE id=1")
                    break
                except Exception as e:  # noqa: BLE001
                    logger.debug("读取控制台凭证失败(第 %d 次): %s", attempt + 1, e)
                    time.sleep(0.2)
        if row:
            try:
                self.port = int(row['port']) or self.port
                self.token = str(row['token'])
                self.token_bits = int(row['token_bits'] or self.token_bits)
                return
            except Exception:  # noqa: BLE001
                pass
        # 首次：生成超长 token 并落库
        self.token = secrets.token_hex(max(1, self.token_bits // 8))
        self._persist()

    def _persist(self):
        db = self._db()
        if db is None:
            return
        now = int(time.time())
        try:
            self._ensure_table(db)
            # 占位符用 %s：框架按方言翻译（SQLite → ?，MySQL 保持 %s）。
            # 用 ? 会在 MySQL 下报错（曾导致凭证静默落库失败）。
            n = db.execute(
                "UPDATE console_access SET port=%s, token=%s, token_bits=%s, updated_at=%s"
                " WHERE id=1",
                (self.port, self.token, self.token_bits, now))
            if not n:
                db.execute(
                    "INSERT INTO console_access"
                    " (id, port, token, token_bits, created_at, updated_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (1, self.port, self.token, self.token_bits, now, now))
            logger.info("调试控制台凭证已写入数据库（port=%s, token %s 位）",
                        self.port, self.token_bits)
        except Exception as e:  # noqa: BLE001
            logger.warning("控制台凭证落库失败（端口/token 仅本次有效）: %s", e)

    # ── 生命周期 ───────────────────────────────────────────────────────
    def start(self) -> bool:
        self._load_or_create()
        try:
            self._sock = self._bind()
        except Exception as e:  # noqa: BLE001
            logger.warning("调试控制台启动失败: %s", e)
            return False
        self._stop = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name='console-server')
        self._thread.start()
        logger.info(
            "调试控制台已就绪：%s:%s（token %s 位，凭证存于数据库 console_access；"
            "本地执行 `python main.py attach` 接入）",
            self.host, self.port, self.token_bits)
        return True

    def _is_our_console(self, port) -> bool:
        """探测该端口上是否已有 zcbot 调试控制台（避免多实例互相覆盖凭证）。"""
        try:
            with socket.create_connection((self.host, int(port)), timeout=0.5) as s:
                # 给读 banner 加超时：连上「幽灵端口」（TIME_WAIT 等）但无进程发
                # banner 时不能无限阻塞，否则会误判为「已有控制台在跑」
                s.settimeout(0.5)
                f = s.makefile('rb')
                line = f.readline()
                if not line:
                    return False
                obj = json.loads(line.decode('utf-8'))
                return isinstance(obj, dict) and obj.get('banner') == 'ZCBOT console'
        except Exception:
            return False

    def _bind(self):
        """优先复用库里的端口（短暂占用时重试）。

        若该端口上已有**本框架的控制台**在跑（典型：又启动了一个实例），
        直接放弃并保留原有凭证——绝不覆盖，否则先跑那个实例的 attach 入口会失效。
        端口被无关服务占用时，才让系统分配新端口并落库。
        """
        if self.port and self._is_our_console(self.port):
            raise RuntimeError(
                "已有调试控制台在 %s:%s 上运行（保留原凭证，本实例不重复监听）"
                % (self.host, self.port))
        attempts = ([self.port] * 3 if self.port else []) + [0]
        last_err = None
        for cand in attempts:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if os.name != 'nt':
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((self.host, cand))
                s.listen(self.backlog)
            except OSError as e:
                last_err = e
                try:
                    s.close()
                except Exception:
                    pass
                # 复用旧端口失败（可能是上一个进程还没释放）——稍等再试
                if cand != 0:
                    time.sleep(0.4)
                continue
            new_port = s.getsockname()[1]
            if new_port != self.port:
                if self.port:      # 首次（port=0）由系统分配，不算「原端口不可用」
                    logger.warning("控制台原端口 %s 不可用（%s），改用 %s",
                                   self.port, last_err, new_port)
                self.port = new_port
                self._persist()
            return s
        raise last_err or RuntimeError("端口绑定失败")

    def stop(self):
        self._stop = True
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        # 等 accept 线程真正退出，确保端口释放后再允许复用探测。
        # 否则 Linux 上 close 后线程仍在 accept，复用探测会误判「已有控制台在跑」
        # （test_console_reuses_port_and_token 在 CI/Linux 上因此失败）。
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)

    # ── 会话 ───────────────────────────────────────────────────────────
    def _accept_loop(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            except Exception:  # noqa: BLE001
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True,
                             name='console-client').start()

    @staticmethod
    def _send(f, obj):
        f.write((json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8'))
        f.flush()

    def _serve(self, conn):
        try:
            conn.settimeout(self.read_timeout or None)
            f = conn.makefile('rwb')
            self._send(f, {'banner': 'ZCBOT console', 'port': self.port})
            line = f.readline()
            if not line:
                return
            try:
                msg = json.loads(line.decode('utf-8'))
            except Exception:  # noqa: BLE001
                self._send(f, {'ok': False, 'error': '协议错误'})
                return
            if not hmac.compare_digest(str(msg.get('token', '')), self.token):
                self._send(f, {'ok': False, 'error': 'token 无效'})
                logger.warning("调试控制台：token 校验失败，已拒绝连接")
                return
            self._send(f, {'ok': True, 'commands': self._command_names()})
            while not self._stop:
                line = f.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode('utf-8'))
                except Exception:  # noqa: BLE001
                    self._send(f, {'ok': False, 'error': 'JSON 解析失败'})
                    continue
                if msg.get('close'):
                    break
                if msg.get('agent') is not None:
                    self._send(f, {'ok': True, 'output': self.run_agent(msg.get('agent') or '')})
                    continue
                self._send(f, {'ok': True, 'output': self.run_command(msg.get('cmd') or '')})
        except Exception as e:  # noqa: BLE001
            logger.debug("调试控制台连接结束: %s", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ── 命令执行 ───────────────────────────────────────────────────────
    @staticmethod
    def _command_names():
        try:
            from framework.terminal import terminal_commands
            return sorted(terminal_commands.list_commands().keys())
        except Exception:  # noqa: BLE001
            return []

    def run_command(self, command: str) -> str:
        """执行一条终端命令并捕获其 stdout（与 HTTP/终端面板同一套命令表）。"""
        command = (command or '').strip()
        if not command:
            return ''
        import contextlib
        import io
        try:
            from framework.terminal import terminal_commands
        except Exception as e:  # noqa: BLE001
            return '终端命令不可用：%s' % e
        name = command.split()[0].lower()
        handler = terminal_commands.get(name)
        if handler is None:
            return '未知命令: %s（输入 help 查看可用命令）' % name
        args = command[len(name):].strip()
        buf = io.StringIO()
        try:
            from framework.terminal.context import remote_session
            with contextlib.redirect_stdout(buf), remote_session():
                handler(args)
        except Exception as e:  # noqa: BLE001
            buf.write('\n执行异常: %s' % e)
        return buf.getvalue()

    def run_agent(self, text: str) -> str:
        """把一句话交给智能体（aiwriter 服务），返回「工具过程 + 最终答复」。

        无 TTY 的文本通道也能用智能体——不必起全屏界面。
        """
        text = (text or '').strip()
        if not text:
            return '（空）'
        svc = None
        try:
            svc = self.fw.services.get('aiwriter')
        except Exception:  # noqa: BLE001
            svc = None
        if svc is None or not hasattr(svc, 'run'):
            return ('[错误] 未启用 aiwriter 插件（或未提供 agent 服务）：'
                    '在 core_plugins.yaml 里把 aiwriter.enabled 设为 true 并重启')
        events = []
        try:
            from framework.terminal.context import remote_session
            with remote_session():
                final = svc.run(text, on_event=lambda k, p: events.append((k, p)))
        except Exception as e:  # noqa: BLE001
            return '[错误] %s' % e
        out = []
        for k, p in events:
            if k == 'tool_start':
                out.append('[tool] %s(%s)' % (p.get('name'), _brief(p.get('args'))))
            elif k == 'tool_end':
                out.append('  -> %s: %s' % ('ok' if p.get('ok') else 'fail',
                                            _brief(p.get('result'))))
        if final:
            out.append(final)
        return '\n'.join(out) or '（无输出）'


# ── 客户端 ────────────────────────────────────────────────────────────
class ConsoleClient:
    """接入调试控制台的客户端（供 `main.py attach` 使用）。"""

    def __init__(self, host, port, token, timeout=3600):
        self.host = host
        self.port = int(port)
        self.token = token
        self.timeout = timeout
        self._sock = None
        self._f = None
        self.banner = {}

    def connect(self):
        self._sock = socket.create_connection((self.host, self.port), timeout=10)
        self._sock.settimeout(self.timeout)
        self._f = self._sock.makefile('rwb')
        self.banner = self._recv()
        self._send({'token': self.token})
        resp = self._recv()
        if not resp.get('ok'):
            raise RuntimeError(resp.get('error') or '认证失败')
        self.banner.update(resp)
        return resp

    def _send(self, obj):
        self._f.write((json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8'))
        self._f.flush()

    def _recv(self):
        line = self._f.readline()
        if not line:
            raise ConnectionError('连接已断开')
        return json.loads(line.decode('utf-8'))

    def run(self, command: str) -> str:
        self._send({'cmd': command})
        resp = self._recv()
        if not resp.get('ok'):
            return '[错误] %s' % resp.get('error')
        return resp.get('output', '')

    def run_agent(self, text: str) -> str:
        """把一句话交给智能体（服务端 aiwriter 服务）。"""
        self._send({'agent': text})
        resp = self._recv()
        if not resp.get('ok'):
            return '[错误] %s' % resp.get('error')
        return resp.get('output', '')

    def close(self):
        try:
            self._send({'close': True})
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


def load_credentials(db_path):
    """从 SQLite 读控制台凭证（客户端用）。返回 (port, token) 或 (None, None)。"""
    import sqlite3
    try:
        con = sqlite3.connect('file:%s?mode=ro' % db_path.replace('\\', '/'), uri=True, timeout=5)
    except Exception:
        try:
            con = sqlite3.connect(db_path, timeout=5)
        except Exception:
            return None, None
    try:
        cur = con.execute("SELECT port, token FROM console_access WHERE id=1")
        row = cur.fetchone()
        return (int(row[0]), str(row[1])) if row else (None, None)
    except Exception:
        return None, None
    finally:
        try:
            con.close()
        except Exception:
            pass


def load_credentials_mysql(db_cfg):
    """从 MySQL 读控制台凭证（客户端用）。返回 (port, token) 或 (None, None)。"""
    try:
        import pymysql
    except ImportError:
        return None, None
    db_cfg = db_cfg or {}
    try:
        conn = pymysql.connect(
            host=str(db_cfg.get('host') or '127.0.0.1'),
            port=int(db_cfg.get('port') or 3306),
            user=str(db_cfg.get('user') or 'root'),
            password=str(db_cfg.get('password') or ''),
            database=str(db_cfg.get('database') or 'zcbot'),
            charset='utf8mb4', connect_timeout=5, read_timeout=6)
    except Exception:
        return None, None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT port, token FROM console_access WHERE id=1")
            row = cur.fetchone()
        return (int(row[0]), str(row[1])) if row else (None, None)
    except Exception:
        return None, None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_credentials_any(db_cfg, project_root=None):
    """按 database 配置（dict 或 sqlite 路径字符串）读控制台凭证。

    支持 sqlite 与 mysql 两种后端——`python main.py attach` 在两种部署下都能用。
    """
    if db_cfg is None:
        db_cfg = {'type': 'sqlite', 'path': 'data/zcbot.db'}
    if isinstance(db_cfg, str):
        db_cfg = {'type': 'sqlite', 'path': db_cfg}
    typ = str(db_cfg.get('type') or 'sqlite').lower()
    if typ == 'mysql':
        return load_credentials_mysql(db_cfg)
    path = db_cfg.get('path') or 'data/zcbot.db'
    if project_root and not os.path.isabs(path):
        path = os.path.join(project_root, path)
    return load_credentials(path)
