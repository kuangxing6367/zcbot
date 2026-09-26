"""
框架核心引擎
组装所有模块，启动生命周期（异步模型）

插件化架构：
- 核心壳：插件加载器 + 事件总线 + 消息路由 + ctx + 数据库
- 官方插件：OneBot适配、WebUI、会话管理、定时调度（config 开关控制）
- 用户插件：业务逻辑
"""
import asyncio
import logging
import logging.handlers
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

from framework.config import load_config
from framework.database.db import init_db
from framework.loader import PluginLoader
from framework.messaging.router import MessageRouter
from framework.messaging.event_bus import EventBus
from framework.log_broker import log_broker, FrameworkLogHandler
from framework.messaging.protocol import ServiceRegistry
from framework.terminal import TerminalInput, terminal_commands, register_builtins
from framework.hooks import HookRegistry, HookPoints

logger = logging.getLogger('zcbot')


from framework.core.stats_writer import AsyncStatsWriter  # noqa: F401  向后兼容 re-export


from framework.core.dispatch import FrameworkDispatchMixin
from framework.core.event_buffer import EventBuffer
from framework.core.runtime import FrameworkRuntimeMixin

class Framework(FrameworkDispatchMixin, FrameworkRuntimeMixin):
    """框架核心引擎"""

    def __init__(self, config_path: str = None, role: str = 'standard', ipc_client=None):
        # 运行角色：standard=单进程（默认）/ core=双进程核心 / host=双进程宿主
        # ipc_client：宿主模式下注入的 IPC 客户端（供 RemoteDatabase 使用）
        self._role = role
        self._ipc_client = ipc_client
        # 记录实际使用的配置文件路径（供 Web API 读写 config.yaml 使用）
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'config.yaml')
        self.config_path = os.path.abspath(config_path)
        self.config = load_config(config_path)
        # 数据目录统一迁移（logs / plugins_dat → data/ 下），必须在日志与插件加载前执行
        self._migrate_legacy_data_dirs()
        self._setup_logging()

        # 初始化各个模块
        logger.info("正在初始化框架核心引擎...")

        # 服务注册表（官方插件注册自身为核心能力）
        self.services = ServiceRegistry()

        # 扩展点注册表（内核契约：插件可挂载到几乎每个运行环节）
        self.hooks = HookRegistry(self)

        # 数据库：宿主模式下用 RemoteDatabase（经 IPC RPC 到核心进程执行）；否则真实数据库
        if role == 'host' and ipc_client is not None:
            from framework.ipc.remote_db import RemoteDatabase
            self.db = RemoteDatabase(ipc_client)
            logger.info("数据库已切换为 RemoteDatabase（双进程宿主模式，RPC 到核心）")
        else:
            self.db = init_db(self.config['database'])

        # 数据库专用线程池
        self._db_executor = ThreadPoolExecutor(
            max_workers=max(8, min(32, (os.cpu_count() or 4) * 2)),
            thread_name_prefix='zcdb',
        )

        # 事件总线
        self.event_bus = EventBus()

        # 消息路由器
        self.router = MessageRouter(self)

        # 插件加载器（支持 core_plugins/ + plugins/）
        self.plugin_loader = PluginLoader(
            self._get_plugins_dir(),
            self,
            self._get_plugins_dat_dir()
        )

        # 终端交互
        self.terminal = TerminalInput(self)

        # 统计批量写库器
        self.stats_writer = AsyncStatsWriter(self)

        # 原始消息处理器注册表
        self._raw_message_handlers = []
        # 后台事件任务引用集
        self._pending_tasks = set()

        # 事件分层缓冲（生产者-消费者：适配器入队即返，后台 worker 异步处理）
        # 配置节 event_queue: maxsize / workers / drain_timeout / buffer
        #  buffer: L1 512KB 内存主缓冲 → sqlite 持久化溢出 → L3 4MB 内存兜底 → 全满告警丢弃
        eq_cfg = self.config.get('event_queue', {}) or {}
        self._event_queue_maxsize = int(eq_cfg.get('maxsize', 2000))
        self._event_workers_count = max(1, int(eq_cfg.get('workers', 1)))
        self._event_drain_timeout = float(eq_cfg.get('drain_timeout', 5))
        _project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self._event_buffer = EventBuffer(eq_cfg, os.path.join(_project_root, 'data'))
        self._event_workers = []   # worker task 引用集（start() 填充）

        # 群成员同步批量写库队列（notice 高频时合并落库，避免每条 to_thread + 单条 SQL）
        self._member_sync_queue = asyncio.Queue(maxsize=20000)
        self._member_sync_dropped = 0
        self._member_sync_task = None
        self._member_sync_interval = float(
            (self.config.get('event_queue', {}) or {}).get('member_sync_interval', 2))

        # 心跳参数
        self._heartbeat_interval = self.config.get('plugin', {}).get('heartbeat_interval', 60)
        self._heartbeat_task = None
        self._running = False
        self.loop = None
        # 双进程核心进程注入的 IPC 服务端（供终端把宿主侧命令转发过去）
        self.ipc_server = None

        # 内存看门狗参数
        mem_cfg = self.config.get('memory', {})
        self._memory_limit_mb = mem_cfg.get('limit_mb', 120)
        self._memory_check_interval = mem_cfg.get('check_interval', 30)
        self._memory_watchdog_task = None

        # 启动时间
        import time
        self._start_time = time.time()

        # 启动就绪标记（start(wait_ready=False) 异步加载插件时，可 await wait_ready 等待就绪）
        self._ready = False
        self._ready_event = asyncio.Event()
        self._startup_load_task = None

        logger.info("框架核心引擎初始化完成")

    def _format_uptime(self):
        """格式化运行时间"""
        import time
        seconds = time.time() - self._start_time if hasattr(self, '_start_time') else 0
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        mins = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        parts = []
        if days > 0:
            parts.append(f"{days}天")
        if hours > 0:
            parts.append(f"{hours}小时")
        if mins > 0:
            parts.append(f"{mins}分钟")
        if secs > 0 or not parts:
            parts.append(f"{secs}秒")
        return "".join(parts)
    @property
    def api_caller(self):
        return self.services.get('api_caller')
    @property
    def protocol_adapter(self):
        """当前主协议接入端（services['protocol_adapter']）"""
        primary = self.services.get('protocol_adapter')
        return primary if primary is not None and hasattr(primary, 'get_connected_bots') else None
    @property
    def ws_server(self):
        """兼容旧调用：优先取接入端自报的 ws_server，否则回落服务注册表"""
        primary = self.services.get('protocol_adapter')
        ws = getattr(primary, 'ws_server', None)
        if ws is not None:
            return ws
        return self.services.get('ws_server')
    @property
    def scheduler(self):
        return self.services.get('scheduler')
    @property
    def web_server(self):
        return self.services.get('web_server')

    def _migrate_legacy_data_dirs(self):
        """
        数据目录统一迁移：将旧版分散在项目根的 logs/、plugins_dat/ 迁移到 data/ 下。
        仅当目标目录不存在时执行一次，避免覆盖新数据。
        """
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        data_dir = os.path.join(project_root, 'data')
        os.makedirs(data_dir, exist_ok=True)

        for old_name, new_name in (('logs', 'logs'), ('plugins_dat', 'plugins_dat')):
            old_path = os.path.join(project_root, old_name)
            new_path = os.path.join(data_dir, new_name)
            if os.path.isdir(old_path) and not os.path.exists(new_path):
                try:
                    shutil.move(old_path, new_path)
                    logger.info(f"数据目录迁移: {old_path} → {new_path}")
                except Exception as e:
                    logger.warning(f"数据目录迁移失败 [{old_name}]: {e}")

    def _setup_logging(self):
        """配置日志（统一存放于 data/logs/ 下）"""
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        log_level = self.config.get('log', {}).get('level', 'INFO')

        # 日志文件路径：优先配置 log.file，默认 data/logs/zcbot.log
        log_file = self.config.get('log', {}).get('file') or os.path.join('data', 'logs', 'zcbot.log')
        if not os.path.isabs(log_file):
            log_file = os.path.join(project_root, log_file)
        log_file = os.path.abspath(log_file)
        os.makedirs(os.path.dirname(log_file), exist_ok=True)

        # 控制台日志
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter(
            '[%(asctime)s] %(levelname)s %(message)s',
            datefmt='%H:%M:%S'
        ))

        # 文件日志（按大小轮转，保留历史，不手动删除旧日志）
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding='utf-8',
            delay=True  # 延迟打开文件，避免初始化时文件锁问题
        )
        file_handler.setFormatter(logging.Formatter(
            '[%(asctime)s] %(levelname)s [%(name)s] %(message)s'
        ))

        root = logging.getLogger()
        root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        root.addHandler(console)
        root.addHandler(file_handler)

        # 桥接框架日志到 LogBroker
        root.addHandler(FrameworkLogHandler(log_broker))

        # 降低第三方库日志级别
        logging.getLogger('apscheduler').setLevel(logging.WARNING)
        logging.getLogger('websocket').setLevel(logging.WARNING)

    def _get_plugins_dir(self) -> str:
        """获取插件代码目录路径（优先使用配置）"""
        plugin_dir = self.config.get('plugin', {}).get('dir', '')
        if plugin_dir:
            if os.path.isabs(plugin_dir):
                return plugin_dir
            return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), plugin_dir)
        return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'plugins')

    def _get_plugins_dat_dir(self) -> str:
        """获取插件数据/配置目录路径（与 plugins 同级，统一存放于 data/ 下）"""
        dat_dir = self.config.get('plugin', {}).get('dat_dir', '')
        if dat_dir:
            if os.path.isabs(dat_dir):
                return dat_dir
            return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), dat_dir)
        return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'data', 'plugins_dat')

    async def start(self, wait_ready: bool = True):
        """启动框架（异步）

        wait_ready=True ：等待核心/用户插件全部加载完成、路由表预热后才返回
                         （默认，保持原同步启动行为）。
        wait_ready=False：立即返回，不等待插件加载；核心/用户插件加载与路由
                         预热在后台异步执行（事件队列/心跳/终端等基础服务
                         已先就绪），可后续 await self.wait_ready(timeout=...)
                         等待全部就绪。
        """
        self.loop = asyncio.get_running_loop()
        self._running = True

        logger.info("=" * 50)
        logger.info("ZCBOT 框架 启动中...")
        logger.info("=" * 50)

        # 安全提示
        self._warn_insecure_config()

        # 1. 启动路由表周期刷新任务（首次预热在插件加载完成后进行）
        self.router.start(self.loop)

        # 2. 启动事件队列消费者（接收入队事件，后台 worker 串行处理）
        for i in range(self._event_workers_count):
            self._event_workers.append(
                asyncio.create_task(
                    self._event_worker_loop(i), name=f"event-worker-{i}")
            )

        # 3. 启动统计批量写库器
        self.stats_writer.start()

        # 4. 启动群成员同步批量写库任务
        self._member_sync_task = asyncio.create_task(
            self._member_sync_loop(), name="member-sync")

        # 5. 启动心跳
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")

        # 6. 启动内存看门狗
        self._memory_watchdog_task = asyncio.create_task(
            self._memory_watchdog_loop(), name="memory-watchdog"
        )

        # 7. 终端命令注册（核心/宿主两个进程都注册，命令绑定到本进程的 fw，供跨进程转发执行）；
        #    交互输入只在非宿主进程启动（宿主子进程无交互 stdin，避免与核心抢控制台）
        register_builtins(self)
        if getattr(self, '_role', 'standard') != 'host':
            self.terminal.start()

        # 8. 插件加载：wait_ready=True 同步等待（原行为）；False 后台异步执行不阻塞启动
        if wait_ready:
            loaded = await asyncio.to_thread(self._load_plugins_sync)
            await self._finalize_startup(loaded)
        else:
            self._startup_load_task = asyncio.create_task(
                self._background_startup_load(), name="startup-plugin-load")
            logger.info(
                "异步启动模式：插件加载已在后台执行，"
                "可 await framework.wait_ready() 等待全部就绪")

    def _load_plugins_sync(self) -> list:
        """同步加载全部插件（core_plugins + 用户插件 + 依赖自愈 + register）。

        独立成同步函数，供 start(wait_ready=False) 放入后台线程执行，
        避免长时间 import/register 阻塞事件循环与启动返回。
        """
        import time
        t0 = time.perf_counter()

        # 1. 加载官方插件（core_plugins/）— 必须最先加载，提供基础服务
        self._load_core_plugins()
        t_core = time.perf_counter()

        # 2. 确保 plugins_dat 目录存在
        os.makedirs(self.plugin_loader.plugins_dat_dir, exist_ok=True)
        self.plugin_loader.migrate_legacy_configs()

        # 3. 加载用户插件（plugins/）
        loaded = self.plugin_loader.load_all()
        logger.info(f"已加载 {len(loaded)} 个用户插件: {loaded}")
        t_user = time.perf_counter()

        # 4. 插件依赖自愈
        self._auto_heal_plugin_deps()
        if hasattr(self.plugin_loader, '_missing_deps'):
            with self.plugin_loader._lock:
                healed_candidates = list(self.plugin_loader._missing_deps.keys())
            for plugin_name in healed_candidates:
                if plugin_name not in loaded and self.plugin_loader.is_plugin_active_in_db(plugin_name):
                    if self.plugin_loader.load_plugin(plugin_name):
                        loaded.append(plugin_name)
            if loaded:
                logger.info(f"自愈后共加载 {len(loaded)} 个插件: {loaded}")
        t_heal = time.perf_counter()

        # 5. 对每个已加载的插件执行 register
        for plugin_name in loaded:
            self.plugin_loader.register_commands(plugin_name)
        t_reg = time.perf_counter()

        logger.info(
            f"插件加载耗时: core {t_core - t0:.2f}s / user {t_user - t_core:.2f}s "
            f"/ 自愈 {t_heal - t_user:.2f}s / register {t_reg - t_heal:.2f}s / 合计 {t_reg - t0:.2f}s")
        return loaded

    async def _finalize_startup(self, loaded: list):
        """插件加载完成后收尾：路由预热 + 系统事件 + 启动扩展点 + 置就绪标记"""
        # 1. 路由表预热（重建热路径内存快照）
        try:
            await asyncio.to_thread(self.router._rebuild_routes)
        except Exception as e:
            logger.error(f"路由表预热失败: {e}")

        # 2. 触发系统事件
        await self.event_bus.aemit('system.plugin.loaded', {'plugins': loaded})

        # 3. 触发启动扩展点（插件可在此预热/注册后台任务/挂载资源）
        try:
            await self.hooks.trigger_async(HookPoints.LIFECYCLE_STARTUP)
        except Exception as e:
            logger.error(f"启动扩展点异常: {e}", exc_info=True)

        # 4. 就绪标记
        self._ready = True
        self._ready_event.set()
        import time
        logger.info(f"框架启动完成，等待消息...（总耗时 {time.time() - self._start_time:.2f}s）")

    async def _background_startup_load(self):
        """异步启动模式的后台插件加载任务"""
        try:
            loaded = await asyncio.to_thread(self._load_plugins_sync)
            await self._finalize_startup(loaded)
        except asyncio.CancelledError:
            logger.warning("后台插件加载被取消（框架停机）")
            raise
        except Exception as e:
            logger.error(f"后台插件加载异常: {e}", exc_info=True)
            self._ready = False
            self._ready_event.set()  # 唤醒 wait_ready，避免等待方永久挂起

    async def wait_ready(self, timeout: float | None = None) -> bool:
        """等待框架完全就绪（插件加载 + 路由预热完成）。

        start(wait_ready=False) 后调用；timeout 为 None 时无限等待。
        返回 True 表示就绪，False 表示超时或后台加载失败。
        """
        if self._ready:
            return True
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return self._ready

    async def terminal_exec(self, name: str, args: str = '') -> str:
        """执行一条终端命令并捕获其输出（供双进程另一侧经 IPC 调用）。

        终端命令 handler 用 print 输出；这里把 stdout 重定向后把文本回传，
        让核心进程的终端能展示宿主侧命令（plugins / tasks 等）的真实结果。
        """
        import io
        import contextlib
        handler = terminal_commands.get(name)
        if handler is None:
            return f"未知命令: {name}"
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                if asyncio.iscoroutinefunction(handler):
                    await handler(args)
                else:
                    await asyncio.to_thread(handler, args)
        except Exception as e:
            return f"命令 [{name}] 执行失败: {e}"
        out = buf.getvalue()
        return out if out.strip() else f"[{name}] 已执行"

    def build_ssl_context(self):
        """构建服务端 SSLContext（Web HTTPS 与 OneBot WSS 共用 config['ssl']）。

        cert/key 支持绝对路径或相对项目根目录（config.yaml 所在目录）。
        未启用 ssl 时返回 None；已启用但证书缺失会抛异常（调用方负责降级/报错）。
        """
        from framework.tls import build_server_ssl_context
        return build_server_ssl_context(
            self.config.get('ssl', {}), os.path.dirname(self.config_path))

    async def stop(self):
        """停止框架（异步）"""
        logger.info("正在停止框架...")
        self._running = False

        # 取消异步启动模式下仍在进行的后台插件加载任务
        if self._startup_load_task:
            self._startup_load_task.cancel()
            try:
                await self._startup_load_task
            except (asyncio.CancelledError, Exception):
                pass
            self._startup_load_task = None

        # 触发关闭扩展点（插件可在此释放资源/落盘/断开外部连接）
        try:
            await self.hooks.trigger_async(HookPoints.LIFECYCLE_SHUTDOWN)
        except Exception as e:
            logger.warning(f"关闭扩展点异常: {e}")

        # 停止终端交互
        self.terminal.stop()

        # 停止事件缓冲消费者（限时排空三层缓冲，超时丢弃并取消 worker）
        if self._event_workers:
            if not await self._event_buffer.wait_drained(self._event_drain_timeout):
                logger.warning(
                    f"事件缓冲排空超时，丢弃剩余 "
                    f"L1={self._event_buffer.stats()['l1_items']}条 "
                    f"sqlite={self._event_buffer.sqlite_count()}条")
            for w in self._event_workers:
                w.cancel()
            await asyncio.gather(*self._event_workers, return_exceptions=True)
            self._event_workers = []
            self._event_buffer.close()

        # 停止统计批量写库器
        try:
            await self.stats_writer.stop()
        except Exception as e:
            logger.warning(f"统计写库器停止异常: {e}")

        # 停止群成员同步批量写库任务（最后一次 flush 收尾）
        if self._member_sync_task:
            self._member_sync_task.cancel()
            try:
                await self._member_sync_task
            except (asyncio.CancelledError, Exception):
                pass
            self._member_sync_task = None
        try:
            await asyncio.to_thread(self._flush_member_sync)
        except Exception as e:
            logger.warning(f"群成员同步收尾落库异常: {e}")

        # 停止路由表刷新任务
        try:
            await self.router.stop()
        except Exception as e:
            logger.warning(f"路由表刷新任务停止异常: {e}")

        # 停止插件心跳任务
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat_task = None

        # 停止内存看门狗
        if self._memory_watchdog_task:
            self._memory_watchdog_task.cancel()
            try:
                await self._memory_watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._memory_watchdog_task = None

        # 停止官方插件（通过服务注册表；协议接入端优先，兼容旧 ws_server 键）
        for name in ('protocol_adapter', 'ws_server', 'web_server', 'scheduler'):
            svc = self.services.get(name)
            if svc is not None:
                try:
                    if asyncio.iscoroutinefunction(svc.stop):
                        await svc.stop()
                    else:
                        svc.stop()
                except Exception as e:
                    logger.warning(f"服务 [{name}] 停止异常: {e}")

        # 关闭数据库专用线程池
        try:
            self._db_executor.shutdown(wait=False)
        except Exception as e:
            logger.warning(f"数据库线程池关闭异常: {e}")

        # 触发系统事件
        await self.event_bus.aemit('system.plugin.unloaded', {})

        logger.info("框架已停止")
