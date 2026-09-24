"""
消息路由器（高性能版）
按插件优先级顺序分发消息，匹配静态命令
- 插件注册的动态命令（is_dynamic=1）仅用于展示，不参与命令匹配
- 系统级动态命令（dynamic_commands 表，关键词自动回复）在插件未命中时兜底匹配
- 文本消息统一监听通道：插件命令未命中时广播 `message` 事件（内容监听型插件可用 ctx.on('message')）

异步模型：
- 后台刷新任务周期性（默认 5s）从 DB 构建纯内存路由表（插件序 + 预编译命令 + 关键词规则），
  路由热路径零 DB 查询、零线程切换（内存路由思路）
- 命令/关键词命中计数交给 framework.core.stats_writer 批量落库，不阻塞事件循环
- handler 支持 async def（直接 await）和普通 def（转线程执行）
"""
import asyncio
import logging
import re
import threading
from typing import Optional

from framework.log_broker import log_broker
from framework.messaging.event import _has_text_segment
from framework.messaging.router_keywords import KeywordReplyMixin, _KeywordRule  # noqa: F401

logger = logging.getLogger('zcbot')



class _RouteCommand:
    """预编译后的路由命令（构建一次，热路径直接复用）"""

    __slots__ = ('id', 'pattern', 'handler_name', 'require_level', 'require_perm',
                 'rx', 'simple', 'aliases')

    def __init__(self, id, pattern, handler_name, require_level,
                 rx, simple, aliases, require_perm=''):
        self.id = id
        self.pattern = pattern
        self.handler_name = handler_name
        self.require_level = require_level
        self.require_perm = require_perm  # 权限节点要求（LuckPerms 风格），空=不限制
        self.rx = rx          # 编译后的正则，或 None
        self.simple = simple  # 简单前缀匹配 pattern，或 None


from framework.messaging.router_match import (  # noqa: E402
    RouterMatchMixin, SimpleMatch, _PluginRoute,
)


class MessageRouter(RouterMatchMixin, KeywordReplyMixin):
    """消息路由分发器（纯内存路由表）"""

    def __init__(self, framework):
        self.framework = framework
        self.db = framework.db

        # ── 内存路由表（后台任务构建，热路径只读）──
        self._routes: dict = {}      # plugin_name -> _PluginRoute
        self._plugin_order: list = []  # 有序插件名列表
        self._keyword_rules: list = []  # 系统关键词自动回复规则（dynamic_commands 表）
        self._routes_lock = threading.Lock()  # 兜底锁（保护快照交换）
        self._refresh_interval = 5.0  # 路由表刷新间隔（秒）
        self._refresh_task = None
        self._force_refresh = False   # 外部置位后立即重建（插件变更等）

    def start(self, loop):
        """启动后台路由表刷新任务（在主事件循环内调用）"""
        if self._refresh_task is None:
            self._refresh_task = loop.create_task(
                self._refresh_loop(), name="router-refresh")

    async def stop(self):
        """停止后台刷新任务"""
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except (asyncio.CancelledError, Exception):
                pass
            self._refresh_task = None

    async def _refresh_loop(self):
        """周期性重建内存路由表（DB 访问在线程中，不阻塞事件循环）"""
        while True:
            try:
                await asyncio.to_thread(self._rebuild_routes)
                if self._force_refresh:
                    self._force_refresh = False
                    continue  # 外部有变更，跳过休眠立即再建一次
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"路由表刷新异常: {e}")
                await asyncio.sleep(1)
                continue
            await asyncio.sleep(self._refresh_interval)

    def _rebuild_routes(self):
        """从 DB 构建纯内存路由表（在 to_thread 中执行）"""
        # 1. 群级插件开关缓存刷新（最多 30s 一次，热路径无需再查库）
        try:
            self.framework.plugin_loader._refresh_group_plugin_cache()
        except Exception:
            pass

        loaded = self.framework.plugin_loader.get_loaded_plugins()

        try:
            rows = self.db.query(
                "SELECT plugin_name, priority, created_at FROM plugins "
                "WHERE is_active = 1 AND has_register = 1 AND status = 'running' "
                "ORDER BY priority ASC, created_at ASC"
            )
        except Exception as e:
            logger.error(f"构建路由表失败（插件排序）: {e}")
            return

        table = {}
        order = []
        for r in rows:
            name = r['plugin_name']
            # 过滤掉内存中未加载的插件（防止 DB 残留导致路由到已卸载/加载失败的插件）
            if name not in loaded:
                continue
            module = self.framework.plugin_loader.get_plugin_module(name)
            if module is None:
                continue
            table[name] = _PluginRoute(module=module)
            order.append(name)

        if table:
            # 2. 一次性加载全部已启用命令（is_dynamic 仅展示，不路由）
            base_where = (
                "WHERE is_dynamic = 0 "
                "AND (is_active = 1 OR (is_active = 0 AND alias IS NOT NULL AND alias != '')) "
                "ORDER BY priority ASC, created_at ASC"
            )
            try:
                cmds = self.db.query(
                    "SELECT id, plugin_name, pattern, alias, handler, "
                    "require_level, require_perm, is_active FROM commands " + base_where
                )
            except Exception:
                # 极老库没有 require_perm 列 → 回退基础查询并补空值
                try:
                    cmds = self.db.query(
                        "SELECT id, plugin_name, pattern, alias, handler, "
                        "require_level, is_active FROM commands " + base_where
                    )
                    for _c in cmds:
                        _c['require_perm'] = ''
                except Exception as e:
                    logger.error(f"构建路由表失败（命令查询）: {e}")
                    cmds = []

            by_plugin = {}
            for c in cmds:
                compiled = self._compile_command(c)
                if compiled is not None:
                    by_plugin.setdefault(c['plugin_name'], []).append(compiled)
            for name in order:
                if name in by_plugin:
                    table[name].commands = by_plugin[name]

        # 3. 加载系统关键词自动回复（dynamic_commands 表，插件未命中时兜底）
        keyword_rules = self._load_keyword_rules()

        with self._routes_lock:
            self._routes = table
            self._plugin_order = order
            self._keyword_rules = keyword_rules

    def _compile_command(self, c: dict) -> Optional[_RouteCommand]:
        """预编译单条命令：正则编译 + 别名预解析"""
        try:
            pattern = c['pattern'] or ''
            is_active = c.get('is_active', 1)
            rx = None
            simple = None
            if is_active:
                if self._is_regex(pattern):
                    try:
                        rx = re.compile(pattern)
                    except re.error as e:
                        logger.warning(
                            f"正则错误 [{c['plugin_name']}]: {pattern} - {e}")
                        return None
                else:
                    simple = pattern

            aliases = []
            alias_raw = c.get('alias') or ''
            if alias_raw:
                aliases = [a.strip() for a in alias_raw.split(',') if a.strip()]

            return _RouteCommand(
                id=c['id'],
                pattern=pattern,
                handler_name=c.get('handler', ''),
                require_level=c.get('require_level', '') or '',
                rx=rx,
                simple=simple,
                aliases=aliases,
                require_perm=(c.get('require_perm') or '').strip().lower(),
            )
        except Exception as e:
            logger.error(f"命令预编译失败 [{c.get('plugin_name')}]: {e}")
            return None

    def _invalidate_cache(self):
        """使路由表立即重建（插件重载 / Web 修改命令后调用）"""
        self._force_refresh = True

    async def route(self, event: dict, bot_name: str = 'default'):
        """
        路由一条消息事件（异步，热路径零 DB / 零线程切换）
        1. 读取内存路由表（原子快照） → 遍历插件
        2. 每个插件内按 commands.priority 匹配命令
        3. 未命中 → 记录未匹配日志
        """
        post_type = event.get('post_type') or event.get('type')
        if post_type != 'message':
            return

        # 纯富媒体消息（无文本段：纯分享卡片/图片/视频等）：
        # 不走命令匹配，直接广播 message.<类型> 事件（与 message 事件互不干扰）
        if not _has_text_segment(event.get('message', '')):
            await self._broadcast_non_text(event, bot_name)
            return

        from framework.messaging.event import Event
        ev = Event(event, bot_name)
        ev._framework = self.framework
        # 复用 Event 构造时已做 @机器人 前缀剥离的匹配文本（见 Event.__init__），
        # 使 "@bot 命令" 能正常命中，而非被 [@bot] 前缀污染导致命令失效。
        message = ev.message
        if not message:
            return  # 防御：存在文本段时提取结果必非空

        routes = self._routes
        plugin_order = self._plugin_order

        logger.debug(
            f'路由消息: "{message[:80]}" → 插件队列: {plugin_order}')

        matched_any = False
        for plugin_name in plugin_order:
            entry = routes.get(plugin_name)
            if entry is None:
                continue
            # 群级插件开关检查（私聊不限制，纯内存缓存）
            if ev.is_group:
                if not self.framework.plugin_loader.is_plugin_enabled_for_group_cached(
                        plugin_name, ev.group_id):
                    logger.debug(f"跳过 [{plugin_name}]：已在群 {ev.group_id} 中禁用")
                    continue
            matched = await self._match_plugin_commands(entry, ev, message, plugin_name)
            if not matched:
                continue
            matched_any = True
            # 插件处理了消息，记录 info 日志
            log_broker.log_plugin(plugin_name, '处理消息', {
                'user_id': ev.user_id,
                'group_id': ev.group_id,
                'message': message[:100],
            })
            # handler 已返回，检查事件传播控制
            if ev.is_stopped():
                log_broker.log_plugin(plugin_name, '终止传播', {
                    'reason': '事件传播被终止',
                })
                return
            logger.debug(f"消息由 [{plugin_name}] 处理，事件继续传播给下一插件")

        # ── 插件命令未命中 → 广播 message 事件（文本消息统一监听通道）──
        if not matched_any:
            if await self._broadcast_message_event(ev, message):
                return
            if not plugin_order:
                log_broker.log_system('WARN', f'无可用插件处理消息: "{message[:50]}"')

        # ── 系统级关键词自动回复（dynamic_commands 表，动态命令）──
        # 插件未命中时触发；插件命中但 ev.continue_route() 声明"允许继续"时同样触发
        if not matched_any or ev.is_continue_route():
            if await self._try_keyword_reply(ev, message):
                return

        log_broker.log_system('DEBUG', f'消息未匹配任何命令: "{message[:80]}"')

    async def _broadcast_message_event(self, ev, message: str) -> bool:
        """
        广播 message 事件（文本消息统一监听通道）
        内容监听型插件（关键词回复/违禁词/自动应答等）通过 ctx.on('message', handler) 订阅，
        handler 返回 True 视为已处理（路由终止）
        """
        try:
            result = await self.framework.event_bus.aemit('message', ev)
            return result is True
        except Exception as e:
            logger.error(f"message 事件广播异常: {e}")
            return False
    async def _broadcast_non_text(self, event: dict, bot_name: str = 'default'):
        """
        广播无文本消息到事件总线（供插件订阅，绕开命令匹配）
        事件名：message.<消息段类型>（如 message.share）+ 通用 message.media
        载荷：Event 对象，插件可通过 ev.segments / ev.share 等访问富媒体数据
        """
        from framework.messaging.event import Event
        ev = Event(event, bot_name)
        ev._framework = self.framework
        # 提取非文本/非回复消息段类型（text/at/reply 不参与广播）
        types = {
            s.get('type') for s in ev.segments
            if s.get('type') not in ('text', 'at', 'reply')
        }
        if not types:
            return
        for t in types:
            await self.framework.event_bus.aemit(f'message.{t}', ev)
        await self.framework.event_bus.aemit('message.media', ev)
