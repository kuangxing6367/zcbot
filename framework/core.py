"""
框架核心引擎
组装所有模块，启动生命周期（异步模型）

异步架构：
- 主事件循环驱动 WebSocket 服务端 / 定时任务 / 心跳 / 统计写库
- 消息处理全异步：async handler 直接 await，sync handler 转线程执行
- 框架自身的 DB 写入（用户注册、命中计数）由 AsyncStatsWriter 批量落库，
  避免每条消息同步写库阻塞事件循环
"""
import asyncio
import logging
import logging.handlers
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

from framework.config import load_config
from framework.db import init_db
from framework.api import ApiCaller
from framework.websocket_handler import WebSocketServer
from framework.loader import PluginLoader
from framework.scheduler import TaskScheduler
from framework.router import MessageRouter
from framework.event_bus import EventBus
from framework.apis import WebServer
from framework.log_broker import log_broker, FrameworkLogHandler

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
        self._reg_queue = asyncio.Queue()   # 用户/群注册任务
        self._cmd_hits = {}                 # cmd_id -> count（主循环线程访问）
        self._task = None

    def start(self):
        """启动后台批量写库任务（需在事件循环内调用）"""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="stats-writer")

    def command_hit(self, cmd_id: int):
        """记录命令命中（内存聚合）"""
        self._cmd_hits[cmd_id] = self._cmd_hits.get(cmd_id, 0) + 1

    def register_user(self, user_id: int, sender: dict, message_type: str, group_id: int = None):
        """排队用户/群自动注册（非阻塞）"""
        self._reg_queue.put_nowait((user_id, dict(sender), message_type, group_id))

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

    def __init__(self, config_path: str = None):
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

        # 数据库
        self.db = init_db(self.config['database'])

        # 数据库专用线程池：DB 操作与默认线程池（sync handler / 定时任务）隔离，
        # 避免某次 DB 阻塞（如连接池繁忙）把整个框架的线程池占满导致消息停摆
        self._db_executor = ThreadPoolExecutor(
            max_workers=max(8, min(32, (os.cpu_count() or 4) * 2)),
            thread_name_prefix='zcdb',
        )

        # API 调用器
        self.api_caller = ApiCaller()
        self.api_caller.on_message_sent = self._on_message_sent

        # 事件总线
        self.event_bus = EventBus()

        # 消息路由器
        self.router = MessageRouter(self)

        # 插件加载器（plugins/ 存代码，plugins_dat/ 存配置）
        self.plugin_loader = PluginLoader(
            self._get_plugins_dir(),
            self,
            self._get_plugins_dat_dir()
        )

        # 定时任务调度器
        self.scheduler = TaskScheduler(self)

        # 统计批量写库器（框架自身 DB 写入走队列，不阻塞事件循环）
        self.stats_writer = AsyncStatsWriter(self)

        # WebSocket 服务端（OneBot 客户端反向连接，运行在主事件循环）
        onebot_cfg = self.config.get('onebot', {})
        self.ws_server = WebSocketServer(
            host=onebot_cfg.get('listen_host', '0.0.0.0'),
            port=onebot_cfg.get('listen_port', 6830),
            access_token=onebot_cfg.get('access_token', ''),
            on_connect_callback=self._on_bot_connect,
            on_disconnect_callback=self._on_bot_disconnect,
            on_message_callback=self._on_ws_message,
            api_caller=self.api_caller,
        )

        # Web UI 服务器
        self.web_server = WebServer(self)

        # 心跳参数
        self._heartbeat_interval = self.config['plugin'].get('heartbeat_interval', 60)
        self._heartbeat_task = None
        self._running = False
        self.loop = None  # 主事件循环，由 start() 设置

        logger.info("框架核心引擎初始化完成")

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
        logger.info("ZCBOT OneBot QQ机器人框架 启动中...")
        logger.info("=" * 50)

        # 安全提示：Web/WS 暴露公网但 token 为空时给出警告
        self._warn_insecure_config()

        # 1. 启动定时任务调度器（绑定主事件循环）
        self.scheduler.start(loop=self.loop)

        # 1.5 确保 plugins_dat 目录存在，并迁移旧插件配置文件
        os.makedirs(self.plugin_loader.plugins_dat_dir, exist_ok=True)
        self.plugin_loader.migrate_legacy_configs()

        # 2. 加载插件（启动阶段，允许同步阻塞）
        loaded = self.plugin_loader.load_all()
        logger.info(f"已加载 {len(loaded)} 个插件: {loaded}")

        # 2.5 插件依赖自愈
        self._auto_heal_plugin_deps()

        # 自愈后可能有插件从「加载失败」转为「可加载」，再尝试一次
        if hasattr(self.plugin_loader, '_missing_deps'):
            with self.plugin_loader._lock:
                healed_candidates = list(self.plugin_loader._missing_deps.keys())
            for plugin_name in healed_candidates:
                if plugin_name not in loaded:
                    if self.plugin_loader.load_plugin(plugin_name):
                        loaded.append(plugin_name)
                        logger.info(f"[{plugin_name}] 依赖自愈后加载成功")
            if len(loaded) > 0:
                logger.info(f"自愈后共加载 {len(loaded)} 个插件: {loaded}")

        # 3. 对每个已加载的插件执行 register
        for plugin_name in loaded:
            self.plugin_loader.register_commands(plugin_name)

        # 4. 启动路由表后台刷新（构建纯内存路由表，热路径零 DB）
        self.router.start(self.loop)
        # 预热路由表：首次构建是阻塞的，确保服务端启动后即可路由
        try:
            await asyncio.to_thread(self.router._rebuild_routes)
        except Exception as e:
            logger.error(f"路由表预热失败: {e}")

        # 5. 启动统计批量写库器
        self.stats_writer.start()

        # 6. 启动插件注册心跳（异步任务）
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")

        # 7. 启动 WebSocket 服务端（运行在主事件循环）
        self.ws_server.start_async()

        # 8. 启动 Web UI（独立线程）
        self.web_server.start()

        # 9. 触发系统事件
        await self.event_bus.aemit('system.plugin.loaded', {'plugins': loaded})

        logger.info("框架启动完成，等待消息...")

    def _warn_insecure_config(self):
        """启动安全提示"""
        web_cfg = self.config.get('web', {})
        onebot_cfg = self.config.get('onebot', {})
        web_host = web_cfg.get('host', '0.0.0.0')
        token = onebot_cfg.get('access_token', '')
        if web_host in ('0.0.0.0', '::') and not token:
            logger.warning(
                "⚠ 安全提示: Web 面板监听 0.0.0.0 且 OneBot access_token 为空，"
                "公网部署存在被接管风险。请设置 config.yaml → onebot.access_token，"
                "并将 web.host 改为 127.0.0.1。"
            )

    async def _heartbeat_loop(self):
        """插件注册心跳：周期性检查插件文件变更并重新注册（不阻塞事件循环）"""
        while self._running:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._running:
                    break
                await asyncio.to_thread(self.plugin_loader.heartbeat_register)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"插件注册心跳异常: {e}")

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
            self.loop.create_task(self.event_bus.aemit('after_message_sent', {
                'bot': bot_name,
                'action': action,
                'params': params,
                'response': resp,
            }))
        except Exception as e:
            logger.debug(f"after_message_sent 事件派发失败: {e}")

    def _on_bot_connect(self, bot_name: str, ws):
        """OneBot 客户端连接时的回调"""
        # 注册 BotConnection
        conn = self.api_caller.register_connection(bot_name)
        conn.set_ws(ws)
        conn.set_ws_server(self.ws_server)
        peer = getattr(ws, 'remote_address', 'unknown')
        logger.info(f"OneBot 客户端已注册: [{bot_name}]")
        log_broker.log_connection(bot_name, 'connect', {'peer': str(peer)})
        log_broker.log_system('INFO', f'OneBot 客户端 [{bot_name}] 已连接')

    def _on_bot_disconnect(self, bot_name: str):
        """OneBot 客户端断开时的回调"""
        conn = self.api_caller.get_connection(bot_name)
        if conn:
            conn.set_ws(None)
        logger.info(f"OneBot 客户端已离线: [{bot_name}]")
        log_broker.log_connection(bot_name, 'disconnect')
        log_broker.log_system('WARN', f'OneBot 客户端 [{bot_name}] 已离线')

    async def _on_ws_message(self, data: dict, bot_name: str = 'default'):
        """收到 WebSocket 消息事件（异步处理）"""
        post_type = data.get('post_type', '')

        # 处理元事件（心跳包）
        if post_type == 'meta_event':
            return

        # 处理消息事件 → 路由
        if post_type == 'message':
            from framework.event import _extract_text
            raw_message = _extract_text(data.get('message', ''))
            message_type = data.get('message_type', 'unknown')
            user_id = data.get('user_id', 0)
            group_id = data.get('group_id')
            message_id = data.get('message_id')
            sender = data.get('sender', {})

            # 根据配置决定是否记录原始消息内容
            log_raw = self.config.get('log', {}).get('log_raw_message', True)
            if log_raw:
                log_broker.log_message(bot_name, message_type, user_id, group_id,
                                       raw_message, message_id)
            else:
                source = f"群{group_id}" if group_id else f"私聊{user_id}"
                log_broker.log('message', 'INFO',
                               f"[{bot_name}] {message_type} {source}: (原始内容未记录)",
                               {'bot': bot_name, 'message_type': message_type,
                                'user_id': user_id, 'group_id': group_id})

            # 自动注册/更新用户信息（批量写库，不阻塞事件循环）
            self.stats_writer.register_user(user_id, sender, message_type, group_id)

            await self.router.route(data, bot_name)

        # 处理通知事件
        elif post_type == 'notice':
            await self._handle_notice(data, bot_name)

        # 处理请求事件
        elif post_type == 'request':
            await self._handle_request(data, bot_name)

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

        # 停止统计批量写库器（最后一次落库）
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

        # 停止 WebSocket 服务端
        try:
            await self.ws_server.stop_async()
        except Exception as e:
            logger.warning(f"WebSocket 服务端停止异常: {e}")

        # 停止 Web UI
        self.web_server.stop()

        # 停止调度器
        self.scheduler.stop()

        # 关闭数据库专用线程池
        try:
            self._db_executor.shutdown(wait=False)
        except Exception as e:
            logger.warning(f"数据库线程池关闭异常: {e}")

        # 触发系统事件
        await self.event_bus.aemit('system.plugin.unloaded', {})

        logger.info("框架已停止")
