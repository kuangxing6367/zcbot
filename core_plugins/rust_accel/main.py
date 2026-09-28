# -*- coding: utf-8 -*-
"""
Rust 加速层（官方插件）

将 OneBot 反向 WS 的「接收 + 广播」链路由 Python 移植到 Rust 二进制
（core_plugins/rust_accel/rust_accel），Python 侧只保留三件事：

  1. 进程生命周期：随框架启停拉起 / 优雅关闭 rust_accel.exe；
  2. IPC 桥接：事件（event）注入框架 dispatch，广播（call/call_resp）转发出站；
  3. 监控观测：rust 周期性推送 stats（吞吐 / 延迟 / 连接状态 / 丢包计数），
     本插件落日志并提供可查询快照（services['rust_accel'].get_stats()）。

启用方式（core_plugins.yaml 是唯一权威）：
  python tools/scan_core_plugins.py --enable rust_accel

前置：先构建 Rust 二进制（一次性，见 rust_accel/README.md）：
  cd core_plugins/rust_accel/rust_accel && cargo build --release

与 onebot_adapter 的关系：两者都监听反向 WS 并注入事件，同时启用会重复
事件、竞争端口。建议启用 rust_accel 时禁用 onebot_adapter（本插件启动时
会检测并告警，不擅自改动配置）。
"""
import asyncio
import itertools
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "Rust 加速层",
    "version": "0.1.0",
    "author": "ZCBOT",
    "desc": "OneBot 反向 WS 接收+广播链路的 Rust 加速实现（进程/IPC/监控由本插件桥接）",
    "priority": 0,
    "official": True,
    # 协议接入基础设施：与 onebot_adapter 同侧，只在核心进程与单进程加载
    "process": "core",
}

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_EXE_SUFFIX = '.exe' if os.name == 'nt' else ''
_HELLO_TIMEOUT = 15.0          # 子进程 HELLO 握手超时（秒）
_SHUTDOWN_GRACE = 3.0          # 停机等待秒数，超时强杀
_IPC_RECONNECT_BASE = 1.0      # IPC 断线重连基础间隔（秒）
_IPC_RECONNECT_MAX = 10.0      # 重连最大间隔（秒）
_CALL_PY_TIMEOUT = 15.0        # Python 侧 call 应答超时（略大于 rust CALL_TIMEOUT=10）
_BOOT_WAIT = 60.0              # 等待框架事件循环就绪的上限（秒）

# 与 framework/config.py _CORE_PLUGIN_SCHEMA['rust_accel'] 保持一致
_DEFAULTS = {
    'enabled': False,
    'ws_host': '0.0.0.0',
    'ws_port': 6831,
    'access_token': '',
    'max_frame_size': 16777216,
    'max_pending_events': 4096,
    'max_pending_bytes': 67108864,
    'stats_interval_secs': 5,
    'ipc_strip_raw': False,
    'inherit_onebot': False,
    'binary_path': '',
    'auto_build': False,
}

_RA_CONFIG_KEYS = (
    'ws_host', 'ws_port', 'access_token', 'max_frame_size',
    'max_pending_events', 'max_pending_bytes',
    'stats_interval_secs', 'ipc_strip_raw',
)
_ONE_AC = ('listen_host', 'listen_port', 'access_token',
           'max_frame_size', 'max_pending_events', 'max_pending_bytes')


def _as_bool(value) -> bool:
    """与 framework.config._as_bool 相同的严格布尔解析"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ('1', 'true', 'yes', 'on', 'y'):
            return True
        if s in ('0', 'false', 'no', 'off', 'n'):
            return False
    return bool(value)


class RustAccelService:
    """Rust 加速层服务：子进程管理 + IPC 桥接 + 监控观测"""

    def __init__(self, framework, config: Optional[dict] = None):
        self.framework = framework
        self.cfg: dict = {**_DEFAULTS, **(config or {})}
        self.proc: Optional[subprocess.Popen] = None
        self.ipc_port: Optional[int] = None
        self.ws_host: str = str(self.cfg.get('ws_host') or '0.0.0.0')
        self.ws_port: Optional[int] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._next_id = itertools.count(1)
        self._pending: Dict[str, asyncio.Future] = {}
        self._bots: Dict[str, float] = {}
        self._last_stats: Optional[dict] = None
        self._started_at: Optional[float] = None
        self._stopping = False
        self._reconnects = 0
        self._dispatch_tasks = set()

    # ── 进程生命周期 ──────────────────────────────────────────

    def _locate_binary(self) -> Optional[str]:
        p = str(self.cfg.get('binary_path') or '').strip()
        if p and os.path.isfile(p):
            return p
        env = os.environ.get('RUST_ACCEL_BIN', '').strip()
        if env and os.path.isfile(env):
            return env
        for tag in ('release', 'debug'):
            cand = os.path.join(_PLUGIN_DIR, 'rust_accel', 'target', tag,
                                'rust_accel' + _EXE_SUFFIX)
            if os.path.isfile(cand):
                return cand
        return None

    def _try_build(self) -> bool:
        cargo = shutil.which('cargo')
        if not cargo:
            logger.error("[rust_accel] auto_build 需要 cargo，但 PATH 中未找到")
            return False
        crate = os.path.join(_PLUGIN_DIR, 'rust_accel')
        try:
            r = subprocess.run([cargo, 'build', '--release'], cwd=crate,
                               capture_output=True, timeout=600)
        except Exception as e:
            logger.error(f"[rust_accel] auto_build 异常: {e}")
            return False
        if r.returncode == 0:
            logger.info("[rust_accel] auto_build 完成")
            return True
        tail = r.stderr.decode('utf-8', 'replace')[-2000:]
        logger.error(f"[rust_accel] auto_build 失败:\n{tail}")
        return False

    def _build_ra_config(self) -> dict:
        ra = {}
        for k in _RA_CONFIG_KEYS:
            v = self.cfg.get(k)
            if v is not None:
                ra[k] = v
        if _as_bool(self.cfg.get('inherit_onebot')):
            one = self.framework.config.get('onebot', {}) or {}
            for ra_k, one_k in zip(_RA_CONFIG_KEYS[:6], _ONE_AC):
                if self.cfg.get(ra_k) is None and one.get(one_k) is not None:
                    ra[ra_k] = one[one_k]
        # 缺省对齐 rust 侧缺省值，避免 None 注入 RA_CONFIG
        ra.setdefault('ws_host', '0.0.0.0')
        ra.setdefault('ws_port', 6831)
        ra.setdefault('access_token', '')
        ra.setdefault('max_frame_size', 16 * 1024 * 1024)
        ra.setdefault('max_pending_events', 4096)
        ra.setdefault('max_pending_bytes', 64 * 1024 * 1024)
        ra.setdefault('stats_interval_secs', 5)
        ra.setdefault('ipc_strip_raw', False)
        return ra

    def start(self) -> bool:
        """拉起子进程并完成 HELLO 握手（在插件注册线程调用）。

        事件循环未就绪时按 scheduler 同款策略：等待框架 loop 运行后派发
        IPC 桥接任务；等待超时则终止子进程并返回 False。
        """
        if self._stopping:
            return False
        exe = self._locate_binary()
        if exe is None:
            hint = (f"未找到 rust_accel 二进制。请先构建: cd {os.path.join(_PLUGIN_DIR, 'rust_accel')} "
                    "&& cargo build --release；或配置 binary_path / 环境变量 RUST_ACCEL_BIN")
            logger.error(f"[rust_accel] {hint}")
            if _as_bool(self.cfg.get('auto_build')) and self._try_build():
                exe = self._locate_binary()
            if exe is None:
                return False

        env = dict(os.environ, RA_CONFIG=json.dumps(self._build_ra_config()))
        try:
            proc = subprocess.Popen([exe], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env)
        except OSError as e:
            logger.error(f"[rust_accel] 进程启动失败: {e}")
            return False
        self.proc = proc

        # 先独占读 HELLO 首行，再让 drain 线程接管后续 stdout/stderr 输出
        # （否则 drain 与 HELLO 读取并发抢读同一管道，可能吞掉握手行）
        hello = self._read_hello(proc)
        threading.Thread(target=self._drain, args=(proc.stdout, 'stdout'),
                         daemon=True, name='rust-stdout').start()
        threading.Thread(target=self._drain, args=(proc.stderr, 'stderr'),
                         daemon=True, name='rust-stderr').start()
        if hello is None:
            self._kill_proc()
            self.proc = None
            logger.error("[rust_accel] HELLO 握手失败：子进程未就绪（检查 ws_port 是否被占用 / "
                         "二进制是否匹配）")
            return False
        self.ipc_port = hello.get('ipc_port')
        self.ws_port = hello.get('ws_port')
        self._started_at = time.time()
        logger.info(f"[rust_accel] 子进程就绪 ws={self.ws_host}:{self.ws_port} "
                    f"ipc=127.0.0.1:{self.ipc_port} ({exe})")

        # 等待框架事件循环就绪后再建 IPC 链路
        loop = getattr(self.framework, 'loop', None)
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._boot)
            return True
        deadline = time.time() + _BOOT_WAIT
        while time.time() < deadline:
            loop = getattr(self.framework, 'loop', None)
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._boot)
                return True
            if self.proc is None or self.proc.poll() is not None:
                logger.error("[rust_accel] 子进程提前退出")
                return False
            time.sleep(0.5)
        logger.error("[rust_accel] 等待框架事件循环超时, 终止子进程")
        self._kill_proc()
        self.proc = None
        return False

    def _boot(self):
        """事件循环线程：创建 IPC 主循环任务"""
        if self._stopping or self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._ipc_loop(), name='rust-ipc')

    def _read_hello(self, proc: subprocess.Popen) -> Optional[dict]:
        result = {}

        def _reader():
            try:
                line = proc.stdout.readline()
                if line:
                    result['line'] = line.decode('utf-8', 'replace').strip()
            except Exception as e:
                result['err'] = str(e)

        th = threading.Thread(target=_reader, daemon=True, name='rust-hello')
        th.start()
        th.join(timeout=_HELLO_TIMEOUT)
        line = result.get('line')
        if not line:
            return None
        try:
            hello = json.loads(line)
        except ValueError:
            logger.error(f"[rust_accel] HELLO 行非 JSON: {line[:200]}")
            return None
        if hello.get('type') != 'hello':
            logger.error(f"[rust_accel] 意外首行: {line[:200]}")
            return None
        return hello

    def _drain(self, stream, tag: str):
        """子进程 stdout/stderr 逐行转日志，防管道写满阻塞"""
        try:
            for raw in iter(lambda: stream.readline(), b''):
                line = raw.decode('utf-8', 'replace').rstrip()
                if line:
                    logger.debug("[rust_accel:%s] %s", tag, line)
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _kill_proc(self):
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    async def stop(self):
        """优雅关闭：先发 shutdown -> 取消 IPC 循环 -> 清 pending -> 等待退出"""
        # 先等 IPC 初次连接就绪（如 register 后立即 unregister、或连接还在重连退避），
        # 确保 shutdown 真能发出；此等待必须在置 _stopping 之前——一旦置位，
        # _ipc_loop 连接成功后检测到 stopping 会立即退出并把 writer 置 None，
        # shutdown 将无从发出，只能等超时强杀。
        if (self._writer is None and self.proc is not None
                and self.proc.poll() is None):
            for _ in range(10):
                if self._writer is not None:
                    break
                await asyncio.sleep(0.1)
        self._stopping = True
        # 先发 shutdown（此时 writer 尚存活；若先 cancel 读循环，finally 会把
        # writer 置 None，shutdown 将无从发出，只能等超时强杀）
        if self._writer is not None:
            try:
                self._writer.write(b'{"type":"shutdown"}\n')
                await self._writer.drain()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result({'status': 'failed', 'retcode': -4,
                                'msg': 'Rust 加速层已停止'})
        self._pending.clear()
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, proc.wait),
                    _SHUTDOWN_GRACE)
                logger.info("[rust_accel] 子进程已优雅退出")
            except asyncio.TimeoutError:
                logger.warning("[rust_accel] shutdown 超时，强制终止子进程")
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait()
                except Exception:
                    pass
        # 进程已确认退出后（或本就无进程）才清空引用，保证
        # running_proc=False 意味着子进程已真正退出、无句柄残留
        self.proc = None

    # ── IPC 桥接 ──────────────────────────────────────────────

    async def _ipc_loop(self):
        """IPC 主循环：连接 127.0.0.1:ipc_port，断线自动重连（指数退避）"""
        delay = _IPC_RECONNECT_BASE
        while not self._stopping:
            reader, writer = None, None
            try:
                reader, writer = await asyncio.open_connection('127.0.0.1', self.ipc_port)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._stopping:
                    logger.warning(f"[rust_accel] IPC 连接失败: {e}，{delay:.0f}s 后重试")
                await asyncio.sleep(delay)
                delay = min(delay * 2, _IPC_RECONNECT_MAX)
                continue
            delay = _IPC_RECONNECT_BASE
            self._writer = writer
            logger.info(f"[rust_accel] IPC 已连接 (127.0.0.1:{self.ipc_port})")
            try:
                while not self._stopping:
                    line = await reader.readline()
                    if not line:
                        break  # EOF：对端关闭
                    text = line.decode('utf-8', 'replace').strip()
                    if text:
                        self._handle_line(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[rust_accel] IPC 读循环异常: {e}")
            finally:
                self._writer = None
                self._reconnects += 1
                if writer is not None:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

    def _handle_line(self, line: str):
        """IPC 行分发（事件循环线程执行）"""
        try:
            msg = json.loads(line)
        except ValueError:
            logger.warning(f"[rust_accel] IPC 非 JSON 行: {line[:200]}")
            return
        t = msg.get('type')
        if t == 'event':
            self._spawn_dispatch(msg.get('event'))
        elif t == 'call_resp':
            fut = self._pending.pop(str(msg.get('id')), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif t == 'conn':
            name, state = msg.get('name'), msg.get('state')
            if state == 'up':
                self._bots[name] = time.time()
                logger.info(f"[rust_accel] 客户端已连接: {name}")
            else:
                self._bots.pop(name, None)
                logger.info(f"[rust_accel] 客户端已断开: {name}")
        elif t == 'stats':
            self._on_stats(msg)
        elif t == 'alert':
            logger.warning(f"[rust_accel] {msg.get('reason', msg)}")
        # hello / conn_list 由启动与查询路径处理，此处忽略

    def _spawn_dispatch(self, event: Any):
        if not isinstance(event, dict):
            return
        fw = self.framework
        try:
            task = asyncio.get_running_loop().create_task(fw.dispatch_event(event))
        except Exception as e:
            logger.error(f"[rust_accel] 事件入队失败: {e}")
            return
        pending = getattr(fw, '_pending_tasks', None)
        if pending is not None:
            pending.add(task)
            task.add_done_callback(pending.discard)
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)

    def _on_stats(self, msg: dict):
        st = msg.get('stats') or {}
        self._last_stats = st
        conn = st.get('conn') or {}
        api = st.get('api') or {}
        fwd = st.get('fwd') or {}
        drop = st.get('drop') or {}
        logger.info(
            "[rust_accel] 监控 conn=%s(peak %s) api_ok=%s fail=%s timeout=%s qfull=%s "
            "rt_avg=%.2fms rt_max=%.2fms events=%s bytes=%s drop(gate/norm)=%s/%s ipc=%s",
            conn.get('now', 0), conn.get('peak', 0),
            api.get('ok', 0), api.get('fail', 0), api.get('timeout', 0),
            api.get('qfull', 0),
            api.get('rt_avg_ms', 0.0), api.get('rt_max_ms', 0.0),
            fwd.get('events', 0), fwd.get('bytes', 0),
            drop.get('gate', 0), drop.get('norm', 0),
            msg.get('ipc_connected', True))

    # ── 对外接口（services['rust_accel']）──────────────────────

    async def acall(self, action: str, bot: str = None, **params) -> dict:
        """异步广播：经 IPC 交给 rust 选择连接发送并匹配 echo 回执"""
        if self._writer is None or self.proc is None or self.proc.poll() is not None:
            return {'status': 'failed', 'retcode': -1, 'msg': 'Rust 加速层未运行'}
        rid = next(self._next_id)
        msg = {'type': 'call', 'id': rid, 'action': action, 'params': params or {}}
        if bot:
            msg['bot'] = bot
        fut = self._loop.create_future()
        self._pending[str(rid)] = fut
        try:
            try:
                self._writer.write((json.dumps(msg, ensure_ascii=False) + '\n').encode())
                await self._writer.drain()
            except Exception as e:
                return {'status': 'failed', 'retcode': -3, 'msg': f'发送失败: {e}'}
            try:
                return await asyncio.wait_for(fut, timeout=_CALL_PY_TIMEOUT)
            except asyncio.TimeoutError:
                return {'status': 'failed', 'retcode': -2, 'msg': '请求超时'}
        finally:
            self._pending.pop(str(rid), None)

    def call(self, action: str, bot: str = None, **params) -> dict:
        """同步广播：桥接到事件循环线程，供旧式调用面使用"""
        if self._loop is None or not self._loop.is_running():
            return {'status': 'failed', 'retcode': -1, 'msg': '主事件循环未运行'}
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self.acall(action, bot=bot, **params), self._loop)
            return fut.result(timeout=_CALL_PY_TIMEOUT + 5)
        except Exception as e:
            return {'status': 'failed', 'retcode': -3, 'msg': str(e)}

    async def query_connections(self) -> list:
        """向 rust 查询当前已连接的 OneBot 客户端名"""
        fut = self._loop.create_future()
        self._pending['__connlist__'] = fut
        try:
            self._writer.write(b'{"type":"query","kind":"connections"}\n')
            await self._writer.drain()
            msg = await asyncio.wait_for(fut, timeout=5)
            return msg.get('connections', [])
        except Exception:
            return []
        finally:
            self._pending.pop('__connlist__', None)

    def get_connected_bots(self) -> list:
        return list(self._bots.keys())

    def get_stats(self) -> Optional[dict]:
        return self._last_stats

    @property
    def status(self) -> dict:
        running = (self.proc is not None and self.proc.poll() is None)
        return {
            'running_proc': running,
            'ipc_port': self.ipc_port,
            'ws_host': self.ws_host,
            'ws_port': self.ws_port,
            'connected_bots': list(self._bots.keys()),
            'ipc_reconnects': self._reconnects,
            'uptime_secs': round(time.time() - self._started_at, 1)
            if self._started_at and running else 0.0,
            'last_stats': self._last_stats,
        }


# ── 插件注册入口 ──────────────────────────────────────────────

_service = None


def register(ctx):
    """注册 Rust 加速层为官方插件"""
    global _service
    fw = ctx._framework
    cfg = fw.config.get('rust_accel', {}) or {}
    if _as_bool(cfg.get('enabled')) is False:
        ctx.log("Rust 加速层已禁用 (rust_accel.enabled: false)")
        return

    # 与 onebot_adapter 互斥提示（重复事件 + 端口竞争风险）
    core_cfg = fw.config.get('core_plugins', {}) or {}
    ob = core_cfg.get('onebot_adapter')
    if _as_bool(ob if not isinstance(ob, dict) else ob.get('enabled', False)):
        ctx.log("警告：onebot_adapter 仍处于启用状态，与 rust_accel 同时启用会导致"
                "重复事件与端口竞争；建议执行 "
                "python tools/scan_core_plugins.py --disable onebot_adapter")

    service = RustAccelService(fw, cfg)
    if not service.start():
        ctx.log("Rust 加速层启动失败，请查看日志")
        # 启动失败仍注册服务，让状态面板可查询失败原因（status.running_proc=False）
        fw.services.register('rust_accel', service)
        return
    fw.services.register('rust_accel', service)
    _service = service
    ctx.log(f"Rust 加速层已启动 (ws://{service.ws_host}:{service.ws_port})")


def _close_service(svc, loop):
    """关闭服务：优先经事件循环优雅停机；循环不可用时同步兜底强杀"""
    try:
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(
                svc.stop(), loop).result(timeout=_SHUTDOWN_GRACE + 5)
            return
        # 事件循环未运行：直接终止进程
        svc._stopping = True
        if svc.proc is not None and svc.proc.poll() is None:
            try:
                svc.proc.kill()
            except Exception:
                pass
        svc.proc = None
    except Exception as e:
        logger.error(f"[rust_accel] unregister 关闭异常: {e}")


def unregister():
    """卸载时优雅关闭子进程（兼容任意调用线程，不阻塞事件循环线程）"""
    global _service
    if _service is None:
        return
    svc = _service
    _service = None
    loop = getattr(svc.framework, 'loop', None)
    try:
        in_loop_thread = asyncio.get_running_loop() is loop
    except RuntimeError:
        in_loop_thread = False
    if in_loop_thread:
        # 事件循环线程内卸载（热重载/停机流程在 loop 内触发）：同步等待 .result()
        # 会占死事件循环导致 stop 协程无法执行（死锁），转后台线程收尾
        threading.Thread(target=_close_service, args=(svc, loop),
                         daemon=True, name='rust_accel-unregister').start()
    else:
        # 常规卸载（loader/停机线程）：同步收尾，保证进程清理完成后才返回
        _close_service(svc, loop)