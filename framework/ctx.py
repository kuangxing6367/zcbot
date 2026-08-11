"""
插件上下文对象 (ctx)
向插件暴露：命令注册、API调用、数据库、日志、配置读取、事件、定时任务

异步模型：
- async handler 中优先使用 aapi() / asend_msg() 等 async 方法，不阻塞事件循环
- 旧插件继续使用 api() / send_msg() 等同步方法（内部桥接到主事件循环，自动兼容）
"""
import asyncio
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from framework.onebot_api import OneBotAPI

logger = logging.getLogger('zcbot')

# 全局线程池，用于异步执行耗时操作（如图片渲染），不阻塞主消息处理流程
_async_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix='async_plugin'
)


class PluginContext:
    """插件上下文，传递给 register(ctx) 函数"""

    class _PluginLogger:
        """插件日志适配器：提供 .info/.warning/.error/.debug 接口，自动加插件名前缀"""
        def __init__(self, plugin_name: str):
            self._name = plugin_name

        def info(self, msg, *args, **kwargs):
            logger.info(f"[{self._name}] {msg}", *args, **kwargs)

        def warning(self, msg, *args, **kwargs):
            logger.warning(f"[{self._name}] {msg}", *args, **kwargs)

        def error(self, msg, *args, **kwargs):
            logger.error(f"[{self._name}] {msg}", *args, **kwargs)

        def debug(self, msg, *args, **kwargs):
            logger.debug(f"[{self._name}] {msg}", *args, **kwargs)

        def warn(self, msg, *args, **kwargs):
            logger.warning(f"[{self._name}] {msg}", *args, **kwargs)

        def exception(self, msg, *args, **kwargs):
            logger.exception(f"[{self._name}] {msg}", *args, **kwargs)

    def __init__(self, plugin_name: str, framework):
        self._plugin_name = plugin_name
        self._framework = framework  # 框架引擎引用
        self._db = framework.db
        self._commands = []  # 本次注册周期收集的命令
        self._tasks = []     # 本次注册周期收集的任务
        self._dashboard_cards = []  # 仪表盘卡片
        self._logger = self._PluginLogger(plugin_name)

        # OneBot 11 标准 API 封装（全量 38 个方法）
        self.onebot = OneBotAPI(framework.api_caller)

    @property
    def logger(self):
        """获取插件日志记录器（标准 logger 接口）"""
        return self._logger

    @property
    def plugin_name(self) -> str:
        """获取当前插件名"""
        return self._plugin_name

    def get_data_dir(self) -> str:
        """
        获取当前插件的数据/配置目录绝对路径（plugins_dat/<plugin_name>/）
        插件应在此目录下读写自己的配置文件、缓存数据等，而非 plugins/ 代码目录
        """
        import os
        dat_dir = os.path.join(
            self._framework.plugin_loader.plugins_dat_dir,
            self._plugin_name
        )
        if not os.path.isdir(dat_dir):
            os.makedirs(dat_dir, exist_ok=True)
        return dat_dir

    # ---- 命令注册 ----

    def command(self, pattern: str, handler: Callable, priority: int = 50,
                dynamic: bool = False, alias: str = None, description: str = None,
                require_admin: bool = False, require_superuser: bool = False):
        """
        注册一个命令
        :param pattern: 正则表达式或命令名（主匹配模式）
        :param handler: 处理函数 (event, match) -> None
        :param priority: 优先级，越小越优先
        :param dynamic: 是否为动态命令（dynamic=True 表示该命令在动态命令 tab 展示，仅标记用）
        :param alias: 命令别名，逗号分隔的字符串或列表（如 "/help,/h" 或 ["/help", "/h"]）
        :param description: 命令描述文本
        :param require_admin: 需要管理员/群主/超管权限
        :param require_superuser: 需要超级管理员权限（高于 require_admin）
        """
        if require_superuser:
            require_admin = False  # super 优先级更高

        if not callable(handler):
            raise TypeError(f"handler '{handler.__name__ if hasattr(handler, '__name__') else handler}' 不可调用")

        # 规范化 alias 为字符串
        if alias is not None:
            if isinstance(alias, (list, tuple)):
                alias = ','.join(str(a).strip() for a in alias)
            else:
                alias = str(alias).strip()

        # 从 handler 的 docstring 自动提取描述（如果未显式传入）
        if description is None and handler.__doc__:
            description = handler.__doc__.strip().split('\n')[0].strip()

        # 所有命令统一收集到列表中，由框架批量写入 commands 表
        self._commands.append({
            'plugin_name': self._plugin_name,
            'pattern': pattern,
            'alias': alias,
            'description': description,
            'priority': priority,
            'handler': handler,
            'handler_name': handler.__name__,
            'is_dynamic': 1 if dynamic else 0,
            'require_level': 'super' if require_superuser else ('admin' if require_admin else ''),
        })

    # ---- 插件配置读取 ----

    def get_config(self, key: str, default=None):
        """
        读取插件配置项
        配置值由 Web UI 通过 _conf_schema.json 定义并存储在 plugin_configs 表中
        :param key: 配置键名
        :param default: 默认值（配置不存在时返回）
        :return: 配置值
        """
        try:
            row = self._db.query_one(
                "SELECT config_value FROM plugin_configs WHERE plugin_name = %s AND config_key = %s",
                (self._plugin_name, key)
            )
            if row:
                value = row['config_value']
                # 数据库值为 NULL 时返回 default
                if value is None:
                    return default
                # 尝试 JSON 解码（非字符串类型）
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    return value
            return default
        except Exception as e:
            logger.error(f"[{self._plugin_name}] 读取配置 {key} 失败: {e}")
            return default

    def get_all_config(self) -> dict:
        """读取插件所有配置项，返回 {key: value} 字典"""
        try:
            rows = self._db.query(
                "SELECT config_key, config_value FROM plugin_configs WHERE plugin_name = %s",
                (self._plugin_name,)
            )
            result = {}
            for r in rows:
                try:
                    result[r['config_key']] = json.loads(r['config_value'])
                except (json.JSONDecodeError, TypeError):
                    result[r['config_key']] = r['config_value']
            return result
        except Exception as e:
            logger.error(f"[{self._plugin_name}] 读取全部配置失败: {e}")
            return {}

    # ---- 定时任务 ----

    def task(self, cron_expr: str, executor: Callable, description: str = None):
        """注册定时任务"""
        if not callable(executor):
            raise TypeError(f"executor '{executor.__name__ if hasattr(executor, '__name__') else executor}' 不可调用")

        self._tasks.append({
            'plugin_name': self._plugin_name,
            'cron_expression': cron_expr,
            'handler': executor,
            'handler_name': executor.__name__,
            'description': description or f"{self._plugin_name} 定时任务",
        })

    # ---- 事件订阅/发布 ----

    def on(self, event_name: str, handler: Callable):
        """订阅系统事件（handler 支持 async def 和普通 def）"""
        self._framework.event_bus.subscribe(event_name, self._plugin_name, handler)

    def emit(self, event_name: str, payload: dict = None):
        """发布事件（同步桥接，供旧插件使用）"""
        self._framework.event_bus.emit(event_name, payload)

    async def aemit(self, event_name: str, payload: dict = None):
        """异步发布事件（推荐 async handler 使用，不阻塞事件循环）"""
        await self._framework.event_bus.aemit(event_name, payload)

    # ---- 统一 API 入口 ----

    def api(self, action: str, bot: str = None, **params):
        """
        调用 OneBot 11 API（同步桥接，自动兼容旧插件）
        推荐 async handler 使用 aapi()，避免阻塞事件循环
        :param action: OneBot 11 标准动作名，如 send_msg, set_group_ban 等
        :param bot: 指定 OneBot 实例名称（None=默认实例）
        :param params: 对应动作的参数
        """
        if bot is None:
            bot = getattr(self, '_current_bot', None)
        return self._framework.api_caller.call(action, bot=bot, **params)

    async def aapi(self, action: str, bot: str = None, **params):
        """
        异步调用 OneBot 11 API（不阻塞事件循环）
        :param action: OneBot 11 标准动作名，如 send_msg, set_group_ban 等
        :param bot: 指定 OneBot 实例名称（None=默认实例）
        :param params: 对应动作的参数
        """
        if bot is None:
            bot = getattr(self, '_current_bot', None)
        return await self._framework.api_caller.acall(action, bot=bot, **params)

    # ---- 最常用 API 快捷方法（省得每次都拼 params）----

    def send_msg(self, user_id: int = None, group_id: int = None,
                 message=None, auto_escape: bool = False, bot: str = None):
        """快捷发送消息，支持通过 user_id/group_id 自动判断私聊/群聊（同步桥接）"""
        # 未指定 bot 时，自动使用当前消息来源的 bot（由 router 注入 _current_bot）
        if bot is None:
            bot = getattr(self, '_current_bot', None)
        return self.onebot.send_msg(
            user_id=user_id, group_id=group_id,
            message=message, auto_escape=auto_escape, bot=bot
        )

    async def asend_msg(self, user_id: int = None, group_id: int = None,
                        message=None, auto_escape: bool = False, bot: str = None):
        """异步快捷发送消息（推荐 async handler 使用，不阻塞事件循环）"""
        if bot is None:
            bot = getattr(self, '_current_bot', None)
        return await self.onebot.acall(
            'send_msg', user_id=user_id, group_id=group_id,
            message=message, auto_escape=auto_escape, bot=bot
        )

    def ban(self, group_id: int, user_id: int, duration: int = 600, bot: str = None):
        """快捷禁言群成员（duration=0 解禁）（同步桥接）"""
        return self.onebot.set_group_ban(group_id, user_id, duration=duration, bot=bot)

    async def aban(self, group_id: int, user_id: int, duration: int = 600, bot: str = None):
        """异步快捷禁言群成员"""
        return await self.onebot.acall(
            'set_group_ban', group_id=group_id, user_id=user_id,
            duration=duration, bot=bot
        )

    def kick(self, group_id: int, user_id: int, reject_add_request: bool = False, bot: str = None):
        """快捷踢出群成员（同步桥接）"""
        return self.onebot.set_group_kick(group_id, user_id, reject_add_request=reject_add_request, bot=bot)

    async def akick(self, group_id: int, user_id: int,
                    reject_add_request: bool = False, bot: str = None):
        """异步快捷踢出群成员"""
        return await self.onebot.acall(
            'set_group_kick', group_id=group_id, user_id=user_id,
            reject_add_request=reject_add_request, bot=bot
        )

    def mute_all(self, group_id: int, enable: bool = True, bot: str = None):
        """快捷全员禁言/解禁（同步桥接）"""
        return self.onebot.set_group_whole_ban(group_id, enable=enable, bot=bot)

    async def amute_all(self, group_id: int, enable: bool = True, bot: str = None):
        """异步快捷全员禁言/解禁"""
        return await self.onebot.acall(
            'set_group_whole_ban', group_id=group_id, enable=enable, bot=bot
        )

    def set_card(self, group_id: int, user_id: int, card: str, bot: str = None):
        """快捷设置群名片（空字符串清除名片）（同步桥接）"""
        return self.onebot.set_group_card(group_id, user_id, card=card, bot=bot)

    async def aset_card(self, group_id: int, user_id: int, card: str, bot: str = None):
        """异步快捷设置群名片"""
        return await self.onebot.acall(
            'set_group_card', group_id=group_id, user_id=user_id, card=card, bot=bot
        )

    def get_member_list(self, group_id: int, bot: str = None):
        """快捷获取群成员列表（同步桥接）"""
        return self.onebot.get_group_member_list(group_id=group_id, bot=bot)

    async def aget_member_list(self, group_id: int, bot: str = None):
        """异步快捷获取群成员列表"""
        return await self.onebot.acall('get_group_member_list', group_id=group_id, bot=bot)

    def get_member_info(self, group_id: int, user_id: int, bot: str = None):
        """快捷获取群成员信息（同步桥接）"""
        return self.onebot.get_group_member_info(group_id, user_id, bot=bot)

    async def aget_member_info(self, group_id: int, user_id: int, bot: str = None):
        """异步快捷获取群成员信息"""
        return await self.onebot.acall(
            'get_group_member_info', group_id=group_id, user_id=user_id, bot=bot
        )

    # ---- 权限判断快捷方法 ----

    def is_group_admin(self, group_id: int, user_id: int) -> bool:
        """
        判断用户是否为群管理员或群主
        :return: True=管理员/群主, False=普通成员或不存在
        """
        try:
            row = self._db.query_one(
                "SELECT role FROM group_members WHERE group_id=%s AND user_id=%s",
                (group_id, user_id)
            )
            return row and row['role'] in ('owner', 'admin')
        except Exception:
            return False

    def is_group_owner(self, group_id: int, user_id: int) -> bool:
        """判断用户是否为群主"""
        try:
            row = self._db.query_one(
                "SELECT role FROM group_members WHERE group_id=%s AND user_id=%s",
                (group_id, user_id)
            )
            return row and row['role'] == 'owner'
        except Exception:
            return False

    def is_superuser(self, user_id: int) -> bool:
        """
        判断用户是否为框架超管（从 users 表的 role 字段判断）
        超管在 config.yaml 中配置，自动同步到 users.role
        """
        try:
            row = self._db.query_one(
                "SELECT role FROM users WHERE user_id=%s", (user_id,)
            )
            return row and row.get('role') == 'super'
        except Exception:
            return False

    def is_blacklisted(self, user_id: int) -> bool:
        """判断用户是否在黑名单中"""
        try:
            row = self._db.query_one(
                "SELECT is_blacklist FROM users WHERE user_id=%s", (user_id,)
            )
            return row and row.get('is_blacklist') == 1
        except Exception:
            return False

    def get_user_role(self, group_id: int, user_id: int) -> str:
        """
        获取用户在群内的完整身份
        :return: "super"（超管）> "owner"（群主）> "admin"（管理员）> "member"（成员）> "blacklist"（黑名单）
        """
        if self.is_superuser(user_id):
            return "super"
        if self.is_blacklisted(user_id):
            return "blacklist"
        if self.is_group_owner(group_id, user_id):
            return "owner"
        if self.is_group_admin(group_id, user_id):
            return "admin"
        return "member"

    # ---- 群级插件开关 ----

    def enable_plugin_in_group(self, plugin_name: str, group_id: int):
        """在指定群启用某个插件（仅管理员/群主可用）"""
        self._framework.plugin_loader.set_group_plugin_enabled(plugin_name, group_id, True)

    def disable_plugin_in_group(self, plugin_name: str, group_id: int):
        """在指定群禁用某个插件（仅管理员/群主可用）"""
        self._framework.plugin_loader.set_group_plugin_enabled(plugin_name, group_id, False)

    def is_plugin_enabled_in_group(self, plugin_name: str, group_id: int) -> bool:
        """检查插件在指定群是否启用"""
        return self._framework.plugin_loader.is_plugin_enabled_for_group(plugin_name, group_id)

    def get_plugin_status_list(self, group_id: int) -> dict:
        """获取指定群所有插件的启用状态"""
        settings = self._framework.plugin_loader.get_group_plugin_settings(group_id)
        disabled_plugins = {r['plugin_name'] for r in settings if not r['enabled']}
        result = {}
        for name in self._framework.plugin_loader.get_loaded_plugins():
            result[name] = name not in disabled_plugins
        return result

    # ---- 数据库操作（连接池） ----

    def db_query(self, sql: str, params: tuple = None) -> list:
        """查询数据库，返回 list[dict]"""
        return self._db.query(sql, params)

    def db_query_one(self, sql: str, params: tuple = None) -> dict:
        """查询单条，返回 dict 或 None"""
        return self._db.query_one(sql, params)

    def db_execute(self, sql: str, params: tuple = None) -> int:
        """执行插入/更新/删除，返回受影响行数"""
        return self._db.execute(sql, params)

    def db_execute_many(self, sql: str, params_list: list) -> int:
        """批量执行，返回受影响行数"""
        return self._db.execute_many(sql, params_list)

    def db_insert(self, sql: str, params: tuple = None) -> int:
        """插入并返回自增 ID"""
        return self._db.insert(sql, params)

    def create_table(self, ddl: str):
        """
        插件建表统一入口（自动适配方言，无需判断数据库类型）
        - SQLite：自动翻译 MySQL 风格 DDL（ENUM→TEXT、AUTO_INCREMENT→AUTOINCREMENT、INDEX 移除等）
        - MySQL：自动将长列（TEXT / VARCHAR>191）索引改写为前缀索引 `col`(191)，避免错误 1170/1064
        """
        try:
            self._db.execute(ddl)
        except Exception as e:
            logger.error(f"[{self._plugin_name}] 建表失败: {e}")
            raise

    def db_connection(self):
        """
        获取一个数据库连接（高级用法）
        池模式下调用方 close() 会自动归还连接到池中
        适用于事务或多条连续操作：

            conn = ctx.db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("INSERT ...")
                cursor.execute("UPDATE ...")
                conn.commit()
            except:
                conn.rollback()
            finally:
                cursor.close()
                conn.close()  # 归还到池
        """
        return self._db.get_connection()

    # ---- 异步数据库操作（不阻塞事件循环，async handler 推荐使用）----

    async def _db_thread(self, func, *args):
        """在数据库专用线程池执行（与默认线程池隔离；DB 繁忙时不影响消息处理）"""
        ex = getattr(self._framework, '_db_executor', None)
        if ex is not None:
            return await asyncio.get_running_loop().run_in_executor(ex, func, *args)
        return await asyncio.to_thread(func, *args)

    async def db_query_async(self, sql: str, params: tuple = None) -> list:
        """异步查询数据库，返回 list[dict]"""
        return await self._db_thread(self._db.query, sql, params)

    async def db_query_one_async(self, sql: str, params: tuple = None) -> dict:
        """异步查询单条，返回 dict 或 None"""
        return await self._db_thread(self._db.query_one, sql, params)

    async def db_execute_async(self, sql: str, params: tuple = None) -> int:
        """异步执行插入/更新/删除，返回受影响行数"""
        return await self._db_thread(self._db.execute, sql, params)

    async def db_execute_many_async(self, sql: str, params_list: list) -> int:
        """异步批量执行，返回受影响行数"""
        return await self._db_thread(self._db.execute_many, sql, params_list)

    async def db_insert_async(self, sql: str, params: tuple = None) -> int:
        """异步插入并返回自增 ID"""
        return await self._db_thread(self._db.insert, sql, params)

    @property
    def db_pool_status(self) -> dict:
        """获取连接池状态信息"""
        return self._db.pool_status

    # ---- 日志 ----

    def log(self, msg: str, level: str = 'info'):
        """输出日志"""
        level = level.upper()
        log_method = getattr(logger, level.lower(), logger.info)
        log_method(f"[{self._plugin_name}] {msg}")

    # ---- 异步执行 ----

    def run_async(self, func: Callable, *args, **kwargs):
        """
        异步执行耗时操作（如图片渲染、网络请求），不阻塞主消息处理流程。

        提交的任务在线程池中执行，返回 concurrent.futures.Future 对象。
        适合用于图片渲染、文件处理等不需要立即返回的耗时操作。

        示例：
            def render_and_send():
                path = renderer.render_card(...)
                ctx.send_msg(..., message=f"[CQ:image,file=file:///{path}]")

            ctx.run_async(render_and_send)
        """
        return _async_executor.submit(func, *args, **kwargs)

    def audit_log(self, action: str, target_type: str = None,
                  target_name: str = None, detail: dict = None,
                  result: str = 'success', error_message: str = None):
        """
        记录插件操作审计日志
        无需管理员上下文，插件可以记录自己的操作（如数据修改、配置变更等）
        :param action: 操作名，如 'sign_in', 'update_data', 'create_record'
        :param target_type: 操作对象类型，如 'user', 'data', 'record'
        :param target_name: 操作对象名称
        :param detail: 详情字典（将被 JSON 序列化）
        :param result: 结果 'success' 或 'failure'
        :param error_message: 错误信息（result='failure' 时填写）
        """
        try:
            self._db.execute(
                "INSERT INTO audit_logs (admin_id, admin_name, action, target_type, target_name, "
                "detail, result, error_message) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (0, f"plugin:{self._plugin_name}", f"plugin.{action}",
                 target_type, target_name,
                 json.dumps(detail, ensure_ascii=False) if detail else None,
                 result, error_message)
            )
        except Exception as e:
            logger.warning(f"[{self._plugin_name}] 审计日志写入失败: {e}")

    # ---- 仪表盘卡片 ----

    def dashboard_card(self, title: str, handler: Callable, icon: str = None, priority: int = 50):
        """
        注册一个仪表盘卡片
        handler 返回 dict: {title, value, label, icon, color}
        """
        self._dashboard_cards.append({
            'plugin_name': self._plugin_name,
            'title': title,
            'handler': handler,
            'icon': icon,
            'priority': priority,
        })

    # ---- WebUI 内嵌 ----

    def webui(self, title: str, entry: str = 'index.html', icon: str = None, order: int = 50):
        """
        注册插件 WebUI 页面
        插件目录下的 web/ 子目录中的 HTML/JS/CSS 文件将被框架内嵌展示

        :param title: 页面标题（显示在导航栏）
        :param entry: 入口文件名（默认 index.html）
        :param icon: 图标（HTML 实体或 emoji）
        :param order: 排序权重（越小越靠前）
        """
        self._framework.plugin_loader.register_webui(self._plugin_name, {
            'title': title,
            'entry': entry,
            'icon': icon,
            'order': order,
        })

    # ---- 内部方法 ----

    def _get_commands(self) -> list:
        return self._commands

    def _get_tasks(self) -> list:
        return self._tasks

    def _get_dashboard_cards(self) -> list:
        return self._dashboard_cards
