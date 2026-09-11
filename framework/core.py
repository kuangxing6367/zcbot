"""
框架核心引擎
组装所有模块，启动生命周期（异步模型）

插件化架构：
- 核心壳：插件加载器 + 事件总线 + 消息路由 + ctx + 数据库
- 官方插件：OneBot适配、WebUI、会话管理、定时调度（config 开关控制）
- 用户插件：业务逻辑
"""
import asyncio
import ast
import importlib
import gc
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
from framework.messaging.protocol import ServiceRegistry, ProtocolAdapter
from framework.terminal import TerminalInput, terminal_commands, register_builtins

logger = logging.getLogger('zcbot')


class AsyncStatsWriter:
    """
    消息统计批量写库器
    框架自身的用户/群自动注册、命令命中计数等写入，统一走此队列，
    由后台任务周期性批量落库（在线程中执行），不阻塞事件循环。
    """

    def __init__(self, framework, flush_interval: float = 5.0):
        self.framework = framework
        self.db = framework.db
        self.flush_interval = flush_interval
        # 有界队列：消息洪峰时丢弃多余注册请求（内存有上限），丢弃计数定期上报
        self._reg_queue = asyncio.Queue(maxsize=20000)   # 用户/群注册任务
        self._dropped = 0
        self._cmd_hits = {}                 # cmd_id -> count（主循环线程访问）
        self._kw_hits = {}                  # dynamic_commands.id -> count
        self._task = None

    def start(self):
        """启动后台批量写库任务（需在事件循环内调用）"""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="stats-writer")

    def command_hit(self, cmd_id: int):
        """记录命令命中（内存聚合）"""
        self._cmd_hits[cmd_id] = self._cmd_hits.get(cmd_id, 0) + 1

    def keyword_hit(self, kw_id: int):
        """记录关键词自动回复命中（内存聚合）"""
        self._kw_hits[kw_id] = self._kw_hits.get(kw_id, 0) + 1

    def register_user(self, user_id: int, sender: dict, message_type: str, group_id: int = None):
        """排队用户/群自动注册（非阻塞，队列满时丢弃并计数）"""
        try:
            self._reg_queue.put_nowait((user_id, dict(sender), message_type, group_id))
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 1000 == 1:
                logger.warning(f"用户注册队列已满，已丢弃 {self._dropped} 条注册请求（消息量过大）")

    async def _run(self):
        """后台循环：周期性 flush"""
        while True:
            try:
                await asyncio.sleep(self.flush_interval)
                await self._run_in_db_thread(self._flush)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"统计批量写库异常: {e}")
                await asyncio.sleep(1)

    async def _run_in_db_thread(self, func):
        """在数据库专用线程池中执行（与默认线程池隔离，DB 阻塞不影响消息处理）"""
        ex = getattr(self.framework, '_db_executor', None)
        if ex is not None:
            return await asyncio.get_running_loop().run_in_executor(ex, func)
        return await asyncio.to_thread(func)

    def _flush(self):
        """批量落库（在线程中执行）"""
        # 1. 命令命中计数
        hits = self._cmd_hits
        self._cmd_hits = {}
        if hits:
            for cmd_id, cnt in hits.items():
                try:
                    self.db.execute(
                        "UPDATE commands SET hit_count = hit_count + %s WHERE id = %s",
                        (cnt, cmd_id)
                    )
                except Exception as e:
                    logger.error(f"命令命中计数写库失败 [{cmd_id}]: {e}")

        # 1.5 关键词自动回复命中计数（dynamic_commands 表）
        kwhits = self._kw_hits
        self._kw_hits = {}
        if kwhits:
            for kw_id, cnt in kwhits.items():
                try:
                    self.db.execute(
                        "UPDATE dynamic_commands SET hit_count = hit_count + %s WHERE id = %s",
                        (cnt, kw_id)
                    )
                except Exception as e:
                    logger.error(f"关键词命中计数写库失败 [{kw_id}]: {e}")

        # 2. 用户/群注册
        items = []
        while True:
            try:
                items.append(self._reg_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        for item in items:
            try:
                self._register_one(*item)
            except Exception as e:
                logger.error(f"自动注册用户失败: {e}")

    def _register_one(self, user_id: int, sender: dict, message_type: str, group_id: int):
        """单条用户/群自动注册（原 core._auto_register_user）"""
        if not user_id:
            return
        nickname = sender.get('nickname', '') or sender.get('card', '') or str(user_id)
        card = sender.get('card', '')

        # INSERT ... ON DUPLICATE KEY UPDATE 实现自动注册+更新
        # （SQLite 模式下由 db.py 自动翻译为 ON CONFLICT + CASE WHEN）
        self.db.execute(
            "INSERT INTO users (user_id, nickname, first_seen_at, last_active_at) "
            "VALUES (%s, %s, NOW(), NOW()) "
            "ON DUPLICATE KEY UPDATE "
            "nickname = IF(VALUES(nickname) != '', VALUES(nickname), nickname), "
            "last_active_at = NOW()",
            (user_id, nickname)
        )

        # 如果是群消息，自动注册群信息和群成员关系
        if group_id and message_type == 'group':
            group_name = sender.get('group_name', '')

            # 自动注册群
            self.db.execute(
                "INSERT INTO groups_info (group_id, group_name, is_active, join_at) "
                "VALUES (%s, %s, 1, NOW()) "
                "ON DUPLICATE KEY UPDATE "
                "is_active = 1, "
                "group_name = IF(VALUES(group_name) != '', VALUES(group_name), group_name)",
                (group_id, group_name)
            )

            # 自动注册群成员关系
            role = sender.get('role', 'member')
            title = sender.get('title', '')
            self.db.execute(
                "INSERT INTO group_members (group_id, user_id, card, role, title, last_active_at, message_count) "
                "VALUES (%s, %s, %s, %s, %s, NOW(), 1) "
                "ON DUPLICATE KEY UPDATE "
                "card = IF(VALUES(card) != '', VALUES(card), card), "
                "role = VALUES(role), "
                "title = IF(VALUES(title) != '', VALUES(title), title), "
                "last_active_at = NOW(), "
                "message_count = message_count + 1",
                (group_id, user_id, card, role, title)
            )

    async def stop(self):
        """停止并执行最后一次落库"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await asyncio.to_thread(self._flush)


class Framework:
    """框架核心引擎"""

    def __init__(self, config_path: str = None, role: str = 'standard', ipc_client=None):
        # 运行角色：standard=单进程（默认）/ core=双进程核心 / host=双进程宿主
        # ipc_client：宿主模式下注入的 IPC 客户端（供 RemoteDatabase 使用）
        self._role = role
        self._ipc_client = ipc_client
        # 记录实际使用的配置文件路径（供 Web API 读写 config.yaml 使用）
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.yaml')
        self.config_path = os.path.abspath(config_path)
        self.config = load_config(config_path)
        # 数据目录统一迁移（logs / plugins_dat → data/ 下），必须在日志与插件加载前执行
        self._migrate_legacy_data_dirs()
        self._setup_logging()

        # 初始化各个模块
        logger.info("正在初始化框架核心引擎...")

        # 服务注册表（官方插件注册自身为核心能力）
        self.services = ServiceRegistry()

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

        # 心跳参数
        self._heartbeat_interval = self.config['plugin'].get('heartbeat_interval', 60)
        self._heartbeat_task = None
        self._running = False
        self.loop = None

        # 内存看门狗参数
        mem_cfg = self.config.get('memory', {})
        self._memory_limit_mb = mem_cfg.get('limit_mb', 120)
        self._memory_check_interval = mem_cfg.get('check_interval', 30)
        self._memory_watchdog_task = None

        # 启动时间
        import time
        self._start_time = time.time()

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

    # ── 服务别名（兼容旧代码，指向 service registry）──

    @property
    def api_caller(self):
        return self.services.get('api_caller')

    @property
    def ws_server(self):
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
        project_root = os.path.dirname(os.path.dirname(__file__))
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
        project_root = os.path.dirname(os.path.dirname(__file__))
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
            return os.path.join(os.path.dirname(os.path.dirname(__file__)), plugin_dir)
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'plugins')

    def _get_plugins_dat_dir(self) -> str:
        """获取插件数据/配置目录路径（与 plugins 同级，统一存放于 data/ 下）"""
        dat_dir = self.config.get('plugin', {}).get('dat_dir', '')
        if dat_dir:
            if os.path.isabs(dat_dir):
                return dat_dir
            return os.path.join(os.path.dirname(os.path.dirname(__file__)), dat_dir)
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'plugins_dat')

    async def start(self):
        """启动框架（异步）"""
        self.loop = asyncio.get_running_loop()
        self._running = True

        logger.info("=" * 50)
        logger.info("ZCBOT 框架 启动中...")
        logger.info("=" * 50)

        # 安全提示
        self._warn_insecure_config()

        # 1. 加载官方插件（core_plugins/）— 必须最先加载，提供基础服务
        self._load_core_plugins()

        # 2. 确保 plugins_dat 目录存在
        os.makedirs(self.plugin_loader.plugins_dat_dir, exist_ok=True)
        self.plugin_loader.migrate_legacy_configs()

        # 3. 加载用户插件（plugins/）
        loaded = self.plugin_loader.load_all()
        logger.info(f"已加载 {len(loaded)} 个用户插件: {loaded}")

        # 3.5 插件依赖自愈
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

        # 4. 对每个已加载的插件执行 register
        for plugin_name in loaded:
            self.plugin_loader.register_commands(plugin_name)

        # 5. 启动路由表后台刷新
        self.router.start(self.loop)
        try:
            await asyncio.to_thread(self.router._rebuild_routes)
        except Exception as e:
            logger.error(f"路由表预热失败: {e}")

        # 6. 启动统计批量写库器
        self.stats_writer.start()

        # 7. 启动心跳
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")

        # 8. 启动内存看门狗
        self._memory_watchdog_task = asyncio.create_task(
            self._memory_watchdog_loop(), name="memory-watchdog"
        )

        # 9. 触发系统事件
        await self.event_bus.aemit('system.plugin.loaded', {'plugins': loaded})

        # 10. 启动终端交互（宿主子进程无交互 stdin，跳过）
        if getattr(self, '_role', 'standard') != 'host':
            register_builtins(self)
            self.terminal.start()

        logger.info("框架启动完成，等待消息...")

    def _load_core_plugins(self):
        """加载官方插件（core_plugins/ 目录）"""
        core_plugins_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 'core_plugins')
        if not os.path.isdir(core_plugins_dir):
            logger.warning(f"core_plugins 目录不存在: {core_plugins_dir}")
            return

        core_cfg = self.config.get('core_plugins', {})

        # 双进程角色过滤（按插件 __plugin_meta__['process'] 自动分派，不再硬编码名单）：
        #  - process='core'：协议/Web 基础设施，只在核心进程与单进程加载，宿主进程排除
        #  - 其余（插件侧能力）：单进程与宿主进程加载，纯核心进程排除
        # 仍兼容 dual_process.core_plugins 显式名单覆盖（配置了就以配置为准）。
        dual = self.config.get('dual_process', {})
        _explicit_core = dual.get('core_plugins')
        if isinstance(_explicit_core, list) and _explicit_core:
            _explicit_core = set(_explicit_core)
        else:
            _explicit_core = None
        _role = getattr(self, '_role', 'standard')

        for name in os.listdir(core_plugins_dir):
            if name.startswith('_'):
                continue
            plugin_dir = os.path.join(core_plugins_dir, name)
            main_file = os.path.join(plugin_dir, 'main.py')
            if not os.path.isfile(main_file):
                continue

            # 双进程角色分派（单进程 standard 不过滤，全部加载）
            if _role in ('core', 'host'):
                _core_side = self._core_plugin_is_core_side(name, main_file, _explicit_core)
                if _role == 'core' and not _core_side:
                    continue
                if _role == 'host' and _core_side:
                    continue

            # 检查配置开关（默认启用）
            enabled = core_cfg.get(name, True)
            if enabled is False:
                logger.info(f"官方插件 [{name}] 已禁用 (core_plugins.{name}: false)")
                continue

            try:
                spec = importlib.util.spec_from_file_location(
                    f"core_plugin_{name}", main_file)
                module = importlib.util.module_from_spec(spec)
                sys.modules[f"core_plugin_{name}"] = module
                spec.loader.exec_module(module)

                # 调用 register(ctx)
                from framework.ctx import PluginContext
                ctx = PluginContext(f"core:{name}", self)
                module.ctx = ctx

                if hasattr(module, 'register'):
                    module.register(ctx)
                    logger.info(f"官方插件 [{name}] 已加载")

                    # 存入 plugin_loader，使调度器能通过 get_plugin_module 获取模块
                    with self.plugin_loader._lock:
                        meta = getattr(module, '__plugin_meta__', {})
                        self.plugin_loader._loaded_plugins[name] = {
                            'module': module,
                            'path': plugin_dir,
                            'meta': meta,
                            'priority': meta.get('priority', 50),
                            'yaml': {},
                        }
                else:
                    logger.warning(f"官方插件 [{name}] 无 register 函数")
            except Exception as e:
                logger.error(f"官方插件 [{name}] 加载失败: {e}", exc_info=True)

    @staticmethod
    def _read_plugin_process_tag(main_file: str):
        """不执行插件，静态解析 __plugin_meta__ 里的 process 进程归属标记。
        解析失败返回 None（按插件侧能力处理）。"""
        try:
            with open(main_file, 'r', encoding='utf-8') as f:
                tree = ast.parse(f.read(), filename=main_file)
        except Exception:
            return None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == '__plugin_meta__':
                        try:
                            meta = ast.literal_eval(node.value)
                        except Exception:
                            return None
                        if isinstance(meta, dict):
                            return meta.get('process')
        return None

    def _core_plugin_is_core_side(self, name: str, main_file: str, explicit_core) -> bool:
        """判断官方插件是否属于核心进程侧（协议/Web 基础设施）"""
        if explicit_core is not None:
            return name in explicit_core
        return self._read_plugin_process_tag(main_file) == 'core'

    def _register_builtin_jobs(self):
        """注册框架内置定时任务（与插件任务互不干扰）"""
        try:
            from framework import perm as perm_mod

            def _cleanup_expired_perms():
                try:
                    perm_mod.cleanup_expired(self.db)
                except Exception as e:
                    logger.warning(f"权限过期清理任务异常: {e}")

            sched = getattr(self.scheduler, '_scheduler', None)
            if sched is None:
                return
            sched.add_job(
                _cleanup_expired_perms,
                'cron', minute=17, id='builtin_perm_cleanup',
                replace_existing=True, misfire_grace_time=600,
            )
            logger.debug("内置定时任务已注册: 权限过期清理（每小时第 17 分钟）")
        except Exception as e:
            logger.warning(f"注册内置定时任务失败: {e}")

    def _warn_insecure_config(self):
        """启动安全提示（协议中立；各接入端自身的令牌提示由适配器注册时给出）"""
        web_cfg = self.config.get('web', {})
        web_host = web_cfg.get('host', '0.0.0.0')
        if web_host in ('0.0.0.0', '::'):
            logger.warning(
                "⚠ 安全提示: Web 面板监听 0.0.0.0，公网部署请确认已设置访问凭据，"
                "并按需将 web.host 改为 127.0.0.1。"
            )

    async def _heartbeat_loop(self):
        """插件注册心跳：周期性检查插件文件变更并重新注册（不阻塞事件循环）"""
        while self._running:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._running:
                    break
                await asyncio.to_thread(self.plugin_loader.heartbeat_register)
                # 每分钟自检：清理不应存在的孤儿任务/命令（插件已删除时自动校正）
                await asyncio.to_thread(self.plugin_loader.self_check_orphans)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"插件注册心跳异常: {e}")

    async def _memory_watchdog_loop(self):
        """内存看门狗：周期检查 RSS，超限时清理框架级缓存并强制 GC"""
        import psutil  # 延迟导入：psutil 导入较慢，仅在启用看门狗时加载
        process = psutil.Process()
        while self._running:
            try:
                await asyncio.sleep(self._memory_check_interval)
                if not self._running:
                    break
                rss_mb = process.memory_info().rss / 1024 / 1024
                if rss_mb > self._memory_limit_mb:
                    logger.warning(
                        f"[内存看门狗] RSS {rss_mb:.1f}MB 超过限制 {self._memory_limit_mb}MB，触发清理"
                    )
                    # 1. 清理框架级角色缓存
                    from framework.messaging.event import _user_role_cache, _group_role_cache
                    cache_before = len(_user_role_cache) + len(_group_role_cache)
                    _user_role_cache.clear()
                    _group_role_cache.clear()
                    # 2. 清理 stats_writer 聚合计数（高频但不关键）
                    if hasattr(self, 'stats_writer'):
                        self.stats_writer._cmd_hits.clear()
                        self.stats_writer._kw_hits.clear()
                    # 3. 强制 GC 回收循环引用
                    collected = gc.collect()
                    after_rss = process.memory_info().rss / 1024 / 1024
                    logger.info(
                        f"[内存看门狗] 清理完成：缓存 {cache_before} 条，GC 回收 {collected} 对象，"
                        f"RSS {rss_mb:.1f}→{after_rss:.1f}MB（节省 {rss_mb - after_rss:.1f}MB）"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[内存看门狗] 异常: {e}")

    def _auto_heal_plugin_deps(self):
        """
        插件依赖自愈：启动时为缺失依赖的插件尝试自动安装
        - 默认开启，可通过 config.yaml → plugin.auto_install_deps_on_startup: false 关闭
        - 只处理 missing 依赖，不处理版本冲突（避免覆盖全局包）
        - 安装失败不影响框架启动，仅记录警告
        """
        cfg = self.config.get('plugin', {})
        auto_install = cfg.get('auto_install_deps_on_startup', True)
        if not auto_install:
            logger.info("插件依赖自愈已关闭 (plugin.auto_install_deps_on_startup: false)")
            return

        with self.plugin_loader._lock:
            missing_snapshot = {
                name: list(deps)
                for name, deps in self.plugin_loader._missing_deps.items()
                if deps
            }

        if not missing_snapshot:
            return

        logger.info(
            f"检测到 {len(missing_snapshot)} 个插件依赖缺失，"
            f"启动自愈流程: {list(missing_snapshot.keys())}"
        )

        for plugin_name, deps in missing_snapshot.items():
            try:
                logger.info(f"[{plugin_name}] 自愈：尝试自动安装缺失依赖: {deps}")
                result = self.plugin_loader.install_missing_deps(plugin_name)
                if result['success']:
                    if result.get('installed'):
                        logger.info(
                            f"[{plugin_name}] 自愈完成，已安装: "
                            f"{', '.join(result['installed'])}"
                        )
                    else:
                        logger.info(f"[{plugin_name}] 自愈完成，依赖已满足")
                else:
                    failed = result.get('failed', [])
                    conflicts = result.get('conflicts', [])
                    if failed:
                        logger.warning(
                            f"[{plugin_name}] 自愈部分失败，未能安装: "
                            f"{', '.join(failed)}。请在 Web UI 手动处理。"
                        )
                    if conflicts:
                        logger.warning(
                            f"[{plugin_name}] 存在版本冲突（不会自动覆盖全局包），"
                            f"请在 Web UI 创建隔离虚拟环境: "
                            f"{', '.join(c['name'] + ' ' + c['required'] + ' (已安装 ' + c['installed'] + ')' for c in conflicts)}"
                        )
            except Exception as e:
                logger.error(f"[{plugin_name}] 依赖自愈异常: {e}")

    def _on_message_sent(self, bot_name: str, action: str, params: dict, resp: dict):
        """消息发送成功后的生命周期钩子（转发为 after_message_sent 事件）"""
        try:
            task = self.loop.create_task(self.event_bus.aemit('after_message_sent', {
                'bot': bot_name,
                'action': action,
                'params': params,
                'response': resp,
            }))
            # 保存任务引用防止被 GC 提前回收（完成后自动移除）
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
        except Exception as e:
            logger.debug(f"after_message_sent 事件派发失败: {e}")

    async def reply_text(self, target, text: str):
        """
        协议中立的"框架自动回复一条文本"统一入口（权限不足提示、关键词回复等）。
        - target 可以是内部 Event 对象或归一化事件 dict；
        - 优先调用当前接入端实现的 ProtocolAdapter.send_text（协议翻译在适配器内）；
        - 向后兼容：仅注册 api_caller、未覆写 send_text 的旧接入端，退回通用 send_msg。
        """
        def _g(key):
            if isinstance(target, dict):
                return target.get(key)
            return getattr(target, key, None)

        group_id = _g('group_id')
        user_id = _g('user_id')
        is_group = _g('is_group')
        if is_group is None:
            is_group = bool(group_id)
        source = _g('bot_name')

        adapter = self.services.get('protocol_adapter')
        # 仅当接入端确实覆写了 send_text（而非基类 unsupported 默认）时走适配器
        if adapter is not None and type(adapter).send_text is not ProtocolAdapter.send_text:
            try:
                return await adapter.send_text(
                    text,
                    group_id=group_id if is_group else None,
                    user_id=None if is_group else user_id,
                    source=source,
                )
            except Exception as e:
                logger.warning(f"接入端 send_text 失败，回退通用调用: {e}")

        # 向后兼容：未实现中立 send_text 的旧接入端
        caller = self.services.get('api_caller')
        if caller is None:
            logger.debug("无可用接入端，框架自动回复被跳过")
            return None
        tgt = {'group_id': group_id} if is_group else {'user_id': user_id}
        return await caller.acall('send_msg', **tgt, message=text)

    async def dispatch_event(self, event: dict):
        """
        协议适配器入口：将转换后的内部事件分发到框架
        适配器（如 onebot_adapter）调用此方法，框架处理路由/事件总线
        """
        event_type = event.get('type', '')
        bot_name = event.get('bot_name', 'default')

        logger.debug(f"dispatch_event: type={event_type} bot={bot_name} msg_type={event.get('message_type','')}")

        # 元事件 → 广播
        if event_type == 'meta_event':
            meta_type = event.get('sub_type', 'unknown')
            await self.event_bus.aemit(f'meta.{meta_type}', event)
            return

        # 消息事件 → 路由
        if event_type == 'message':
            if await self._dispatch_raw_message_handlers(event, bot_name):
                return

            from framework.messaging.event import _extract_text
            raw_message = _extract_text(event.get('message', ''))
            message_type = event.get('message_type', 'unknown')
            user_id = event.get('user_id', 0)
            group_id = event.get('group_id')
            sender = event.get('sender', {})

            log_raw = self.config.get('log', {}).get('log_raw_message', True)
            if log_raw:
                log_broker.log_message(bot_name, message_type, user_id, group_id,
                                       raw_message, event.get('message_id'))
            else:
                source = f"群{group_id}" if group_id else f"私聊{user_id}"
                log_broker.log('message', 'INFO',
                               f"[{bot_name}] {message_type} {source}: (原始内容未记录)",
                               {'bot': bot_name, 'message_type': message_type,
                                'user_id': user_id, 'group_id': group_id})

            self.stats_writer.register_user(user_id, sender, message_type, group_id)
            await self.router.route(event, bot_name)

        elif event_type == 'notice':
            await self._handle_notice(event, bot_name)
        elif event_type == 'request':
            await self._handle_request(event, bot_name)

    def register_raw_message_handler(self, plugin_name: str, handler, priority: int = 50):
        """注册插件原始消息处理器（同插件同 handler 去重，按优先级升序）"""
        for item in self._raw_message_handlers:
            if item['plugin_name'] == plugin_name and item['handler'] == handler:
                return
        self._raw_message_handlers.append({
            'plugin_name': plugin_name,
            'priority': priority,
            'handler': handler,
        })
        self._raw_message_handlers.sort(key=lambda x: x['priority'])

    def unregister_raw_message_handlers(self, plugin_name: str):
        """移除某插件的全部原始消息处理器（插件卸载时调用）"""
        self._raw_message_handlers = [
            item for item in self._raw_message_handlers
            if item['plugin_name'] != plugin_name
        ]

    async def _dispatch_raw_message_handlers(self, data: dict, bot_name: str) -> bool:
        """
        按插件优先级分发原始消息事件（未提取纯文本、含全部消息段）
        任一 handler 返回 True 表示"已接管"（框架跳过该消息的后续处理，选择性使用）；
        返回 None/False 表示未处理，消息继续走正常流程。单个 handler 异常不影响其他。
        """
        if not self._raw_message_handlers:
            return False
        for item in list(self._raw_message_handlers):
            handler = item['handler']
            try:
                if asyncio.iscoroutinefunction(handler):
                    result = await handler(data, bot_name)
                else:
                    result = await asyncio.to_thread(handler, data, bot_name)
                if result is True:
                    log_broker.log_plugin(item['plugin_name'], '原始消息接管', {
                        'bot': bot_name,
                        'message_type': data.get('message_type', ''),
                        'user_id': data.get('user_id', 0),
                        'group_id': data.get('group_id'),
                    })
                    return True
            except Exception as e:
                logger.error(f"[{item['plugin_name']}] 原始消息处理器异常: {e}", exc_info=True)
        return False

    async def _handle_notice(self, data: dict, bot_name: str = 'default'):
        """处理通知事件"""
        notice_type = data.get('notice_type', '')
        await self.event_bus.aemit(f'notice.{notice_type}', data)

        # 群成员增加/减少时更新数据库（在线程中执行）
        if notice_type == 'group_increase':
            await asyncio.to_thread(self._sync_group_member_join, data)
        elif notice_type == 'group_decrease':
            await asyncio.to_thread(self._sync_group_member_leave, data)

    async def _handle_request(self, data: dict, bot_name: str = 'default'):
        """处理请求事件"""
        request_type = data.get('request_type', '')
        await self.event_bus.aemit(f'request.{request_type}', data)

    def _sync_group_member_join(self, data: dict):
        """同步群成员加入"""
        try:
            group_id = data.get('group_id')
            user_id = data.get('user_id')
            self.db.execute(
                "INSERT IGNORE INTO group_members (group_id, user_id) VALUES (%s, %s)",
                (group_id, user_id)
            )
        except Exception as e:
            logger.error(f"同步群成员加入失败: {e}")

    def _sync_group_member_leave(self, data: dict):
        """同步群成员离开"""
        try:
            group_id = data.get('group_id')
            user_id = data.get('user_id')
            self.db.execute(
                "DELETE FROM group_members WHERE group_id = %s AND user_id = %s",
                (group_id, user_id)
            )
        except Exception as e:
            logger.error(f"同步群成员离开失败: {e}")

    async def stop(self):
        """停止框架（异步）"""
        logger.info("正在停止框架...")
        self._running = False

        # 停止终端交互
        self.terminal.stop()

        # 停止统计批量写库器
        try:
            await self.stats_writer.stop()
        except Exception as e:
            logger.warning(f"统计写库器停止异常: {e}")

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

        # 停止官方插件（通过服务注册表）
        for name in ('ws_server', 'web_server', 'scheduler'):
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
