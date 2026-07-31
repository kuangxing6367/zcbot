"""
框架核心引擎
组装所有模块，启动生命周期
"""
import logging
import logging.handlers
import os
import sys
import threading
import time

from framework.config import load_config, get_config
from framework.db import init_db, db
from framework.api import ApiCaller
from framework.websocket_handler import WebSocketServer
from framework.loader import PluginLoader
from framework.scheduler import TaskScheduler
from framework.router import MessageRouter
from framework.event_bus import EventBus
from framework.apis import WebServer
from framework.log_broker import log_broker, FrameworkLogHandler

logger = logging.getLogger('zcbot')

# ── 内部心跳：检测 GIL 被插件死循环占死导致的进程假死 ──
# 独立守护线程定期写时间戳；主循环（或 watchdog）读，如果 3s 没更新就 os._exit(1)
# 注意：这个时间戳必须在主循环或能被主循环读取的地方使用
_INTERNAL_HEARTBEAT_TS = time.monotonic()
_INTERNAL_HEARTBEAT_LOCK = threading.Lock()


def _internal_heartbeat_worker():
    """独立守护线程：每隔 500ms 更新一次内部心跳时间戳"""
    global _INTERNAL_HEARTBEAT_TS
    while True:
        with _INTERNAL_HEARTBEAT_LOCK:
            _INTERNAL_HEARTBEAT_TS = time.monotonic()
        time.sleep(0.5)


def check_internal_heartbeat(timeout_s: float = 3.0) -> bool:
    """
    检查内部心跳是否超时（返回 True 表示正常）
    如果返回 False → 说明某个插件把 GIL 占死了，进程假死
    """
    with _INTERNAL_HEARTBEAT_LOCK:
        last_ts = _INTERNAL_HEARTBEAT_TS
    return (time.monotonic() - last_ts) <= timeout_s


class Framework:
    """框架核心引擎"""

    def __init__(self, config_path: str = None):
        self.config = load_config(config_path)
        self._setup_logging()

        # 初始化各个模块
        logger.info("正在初始化框架核心引擎...")

        # 数据库
        self.db = init_db(self.config['database'])

        # API 调用器
        self.api_caller = ApiCaller()

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

        # WebSocket 服务端（OneBot 客户端反向连接）
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

        # 心跳定时器
        self._heartbeat_timer = None
        self._heartbeat_interval = self.config['plugin'].get('heartbeat_interval', 60)
        self._running = False

        # ── 启动内部心跳守护线程（检测 GIL 假死）──
        # 必须放在 __init__ 末尾，确保 start() 之前就能跑起来
        t = threading.Thread(
            target=_internal_heartbeat_worker,
            daemon=True,
            name="internal_heartbeat"
        )
        t.start()

        logger.info("框架核心引擎初始化完成")

    def _setup_logging(self):
        """配置日志"""
        log_level = self.config.get('log', {}).get('level', 'INFO')
        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')
        os.makedirs(log_dir, exist_ok=True)

        # 清理旧日志文件（防止 Windows 文件锁导致轮转失败）
        log_file = os.path.join(log_dir, 'framework.log')
        try:
            for i in range(5, 0, -1):
                old = f"{log_file}.{i}"
                if os.path.exists(old):
                    os.remove(old)
            if os.path.exists(log_file):
                os.remove(log_file)
        except PermissionError:
            pass  # 文件被占用则跳过

        # 控制台日志
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter(
            '[%(asctime)s] %(levelname)s %(message)s',
            datefmt='%H:%M:%S'
        ))

        # 文件日志（按大小轮转，防止日志爆炸）
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

        # 桥接框架日志到 LogBroker（参考 AstrBot LogQueueHandler）
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
        """获取插件数据/配置目录路径（与 plugins 同级）"""
        dat_dir = self.config.get('plugin', {}).get('dat_dir', '')
        if dat_dir:
            if os.path.isabs(dat_dir):
                return dat_dir
            return os.path.join(os.path.dirname(os.path.dirname(__file__)), dat_dir)
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'plugins_dat')

    def start(self):
        """启动框架"""
        logger.info("=" * 50)
        logger.info("ZCBOT OneBot QQ机器人框架 启动中...")
        logger.info("=" * 50)

        self._running = True

        # 1. 启动定时任务调度器
        self.scheduler.start()

        # 1.5 确保 plugins_dat 目录存在，并迁移旧插件配置文件
        os.makedirs(self.plugin_loader.plugins_dat_dir, exist_ok=True)
        self.plugin_loader.migrate_legacy_configs()

        # 2. 加载插件
        loaded = self.plugin_loader.load_all()
        logger.info(f"已加载 {len(loaded)} 个插件: {loaded}")

        # 2.5 插件依赖自愈：启动时自动为缺失依赖的插件尝试安装
        # 参考 main.py 自检思路，解决移机/迁移后插件依赖断档问题
        # 默认开启，可通过 config.yaml → plugin.auto_install_deps_on_startup: false 关闭
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

        # 4. 启动心跳定时器
        self._start_heartbeat()

        # 5. 启动 WebSocket 服务端，等待 OneBot 客户端连接
        self.ws_server.start()

        # 6. 启动 Web UI
        self.web_server.start()

        # 7. 触发系统事件
        self.event_bus.emit('system.plugin.loaded', {
            'plugins': loaded
        })

        logger.info("框架启动完成，等待消息...")

    def _start_heartbeat(self):
        """启动1分钟注册心跳"""
        def heartbeat_loop():
            while self._running:
                threading.Event().wait(self._heartbeat_interval)
                if not self._running:
                    break
                try:
                    self.plugin_loader.heartbeat_register()
                    logger.debug("插件注册心跳完成")
                except Exception as e:
                    logger.error(f"插件注册心跳异常: {e}")

        self._heartbeat_timer = threading.Thread(
            target=heartbeat_loop,
            daemon=True,
            name="heartbeat"
        )
        self._heartbeat_timer.start()
        logger.info(f"插件注册心跳已启动 (间隔: {self._heartbeat_interval}s)")

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

        # 快照缺失依赖列表，避免迭代时被 install_missing_deps 修改
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
                logger.info(
                    f"[{plugin_name}] 自愈：尝试自动安装缺失依赖: {deps}"
                )
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

    def _on_ws_message(self, data: dict, bot_name: str = 'default'):
        """收到 WebSocket 消息事件"""
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
                # 仅记录消息来源，不记录具体内容
                source = f"群{group_id}" if group_id else f"私聊{user_id}"
                log_broker.log('message', 'INFO',
                               f"[{bot_name}] {message_type} {source}: (原始内容未记录)",
                               {'bot': bot_name, 'message_type': message_type,
                                'user_id': user_id, 'group_id': group_id})

            # 自动注册/更新用户信息（用户发消息就自动注册）
            self._auto_register_user(user_id, sender, message_type, group_id)

            self.router.route(data, bot_name)

        # 处理通知事件
        elif post_type == 'notice':
            self._handle_notice(data, bot_name)

        # 处理请求事件
        elif post_type == 'request':
            self._handle_request(data, bot_name)

    def _auto_register_user(self, user_id: int, sender: dict, message_type: str, group_id: int = None):
        """
        自动注册/更新用户信息（用户发消息就自动注册）
        参考 AstrBot 的用户自动注册机制
        """
        if not user_id:
            return

        try:
            nickname = sender.get('nickname', '') or sender.get('card', '') or str(user_id)
            card = sender.get('card', '')

            # 使用 INSERT ... ON DUPLICATE KEY UPDATE 实现自动注册+更新
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

        except Exception as e:
            logger.error(f"自动注册用户失败: {e}")

    def _handle_notice(self, data: dict, bot_name: str = 'default'):
        """处理通知事件"""
        notice_type = data.get('notice_type', '')
        self.event_bus.emit(f'notice.{notice_type}', data)

        # 群成员增加/减少时更新数据库
        if notice_type == 'group_increase':
            self._sync_group_member_join(data)
        elif notice_type == 'group_decrease':
            self._sync_group_member_leave(data)

    def _handle_request(self, data: dict, bot_name: str = 'default'):
        """处理请求事件"""
        request_type = data.get('request_type', '')
        self.event_bus.emit(f'request.{request_type}', data)

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

    def stop(self):
        """停止框架"""
        logger.info("正在停止框架...")
        self._running = False

        # 停止 WebSocket 服务端
        self.ws_server.stop()

        # 停止 Web UI
        self.web_server.stop()

        # 停止心跳
        self._heartbeat_timer = None

        # 停止调度器
        self.scheduler.stop()

        # 触发系统事件
        self.event_bus.emit('system.plugin.unloaded', {})

        logger.info("框架已停止")

    def on_message(self, event, bot_name: str = 'default'):
        """供外部调用的消息处理入口"""
        self.router.route(event._raw, bot_name)