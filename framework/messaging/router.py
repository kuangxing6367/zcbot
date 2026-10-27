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
import time
from typing import Optional

from framework.log_broker import log_broker
from framework.hooks import HookPoints
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
        self.require_perm = require_perm  # 权限节点要求（节点式），空=不限制
        self.rx = rx          # 编译后的正则，或 None
        self.simple = simple  # 简单前缀匹配 pattern，或 None
        self.aliases = aliases


from framework.messaging.router_match import (  # noqa: E402
    RouterMatchMixin, SimpleMatch, _PluginRoute, build_command_index,
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
        self._routes_signature = None  # 路由表轻量签名，未变动则跳过重建
        self._last_group_cache_refresh = 0.0  # 群级插件开关缓存刷新时间戳（30s 节流）

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

    def _compute_signature(self):
        """轻量签名：捕获路由表变化的几种来源（避免每 5s 全量重编译正则）。

        commands 表无 updated_at 列，故用 COUNT(*)/SUM(is_active)/MAX(id) 组合判定；
        插件启用集用 COUNT + MAX(created_at)；关键词规则同理。三者任一变化即触发重建。
        """
        try:
            p = self.db.query_one(
                "SELECT COUNT(*) AS n, COALESCE(MAX(created_at), 0) AS m FROM plugins "
                "WHERE is_active = 1 AND has_register = 1 AND status = 'running'")
            c = self.db.query_one(
                "SELECT COUNT(*) AS n, COALESCE(SUM(is_active), 0) AS a, "
                "COALESCE(MAX(id), 0) AS m FROM commands WHERE is_dynamic = 0")
            k = self.db.query_one(
                "SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m FROM dynamic_commands")
            return (p['n'], p['m'], c['n'], c['a'], c['m'], k['n'], k['m'])
        except Exception:
            return None

    def _rebuild_routes(self):
        """从 DB 构建纯内存路由表（在 to_thread 中执行）"""
        # 1. 群级插件开关缓存刷新（最多 30s 一次，热路径无需再查库）
        now = time.monotonic()
        if now - self._last_group_cache_refresh >= 30.0:
            try:
                self.framework.plugin_loader._refresh_group_plugin_cache()
            except Exception:
                pass
            self._last_group_cache_refresh = now

        # 2. 轻量签名：路由表未变动则跳过完整重建（省去命令全量加载 + 正则重编译）。
        #    外部变更经 _invalidate_cache() 置 _force_refresh，仍会强制重建。
        sig = self._compute_signature()
        if (sig is not None and sig == self._routes_signature
                and not self._force_refresh):
            return
        self._routes_signature = sig

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

        # 合并排序来源：
        #  - 用户插件：plugins 表（含 is_active/has_register/status 过滤），带真实 priority/created_at
        #  - 核心插件：已加载但未入 plugins 表（core_plugins/），用其 meta priority
        #    此前核心插件命令漏同步、也不进路由排序，需纳入使其命令可路由。
        plugin_order_meta = {}
        for r in rows:
            # created_at 在库里是字符串时间戳，统一转 str 以保证跨来源可比较排序
            plugin_order_meta[r['plugin_name']] = (
                r.get('priority') or 50, str(r.get('created_at') or 0))
        for _n in loaded:
            if _n not in plugin_order_meta:
                plugin_order_meta[_n] = ((loaded[_n].get('priority') or 50), "0")

        table = {}
        order = []
        for name in sorted(plugin_order_meta,
                           key=lambda n: (plugin_order_meta[n][0],
                                          plugin_order_meta[n][1])):
            # 过滤掉内存中未加载的插件（防止 DB 残留导致路由到已卸载/加载失败的插件）
            if name not in loaded:
                continue
            module = self.framework.plugin_loader.get_plugin_module(name)
            if module is None:
                continue
            table[name] = _PluginRoute(module=module)
            order.append(name)

        if table:
            # 3. 一次性加载全部已启用命令（is_dynamic 仅展示，不路由）
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
                cmds = by_plugin.get(name)
                if cmds:
                    table[name].commands = cmds
                    # 首字符倒排索引（一次性构建，热路径剪枝用）
                    table[name].index = build_command_index(cmds)

        # 4. 加载系统关键词自动回复（dynamic_commands 表，插件未命中时兜底）
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
        扩展点：
        - router.before_route  路由开始前（返回 False 跳过本次路由）
        - router.after_route   路由完成后通知（携带命中结果，不短路）
        - router.message_unmatched  未命中任何命令/关键词时兜底（返回 True 视为接管）
        """
        post_type = event.get('post_type') or event.get('type')
        if post_type != 'message':
            return

        # 路由前扩展点（返回 False 跳过本次路由）
        try:
            if False in (await self.framework.hooks.trigger_async(
                    HookPoints.ROUTER_BEFORE_ROUTE, {'event': event, 'bot_name': bot_name})):
                # 原实现此处无日志：扩展点否决路由后消息既不处理也不报错，
                # 运维侧完全无痕迹（隐性吞）。升级为可见告警。
                logger.warning(
                    f"消息被扩展点 router.before_route 跳过路由（bot={bot_name}）："
                    f"若怀疑消息未进入命令匹配，请排查该扩展点")
                return
        except Exception as e:
            logger.error(f"router.before_route 扩展点异常: {e}")

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

        logger.debug('路由消息: "%s" → 插件队列: %s', message[:80], plugin_order)

        matched_any = False
        handled_plugin = None   # 路由结果（供 router.after_route 通知）
        try:
            for plugin_name in plugin_order:
                entry = routes.get(plugin_name)
                if entry is None:
                    continue
                # 多平台接入：适配器↔插件双向绑定白名单（不受当前适配器允许的插件直接跳过）
                # 用 getattr 取：老版本/测试桩 framework 没有该方法时不得阻断路由
                _allowed = getattr(self.framework, 'adapter_plugin_allowed', None)
                if _allowed is not None and not _allowed(plugin_name, ev.adapter or bot_name):
                    continue
                # 群级插件开关检查（私聊不限制，纯内存缓存）
                if ev.is_group:
                    if not self.framework.plugin_loader.is_plugin_enabled_for_group_cached(
                            plugin_name, ev.group_id):
                        logger.debug("跳过 [%s]：已在群 %s 中禁用", plugin_name, ev.group_id)
                        continue
                matched = await self._match_plugin_commands(entry, ev, message, plugin_name)
                if not matched:
                    continue
                matched_any = True
                handled_plugin = plugin_name
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
                logger.debug("消息由 [%s] 处理，事件继续传播给下一插件", plugin_name)

            # ── 插件命令未命中 → 广播 message 事件（文本消息统一监听通道）──
            if not matched_any:
                if await self._broadcast_message_event(ev, message):
                    handled_plugin = 'message'
                    return
                if not plugin_order and logger.isEnabledFor(logging.WARNING):
                    log_broker.log_system('WARN', f'无可用插件处理消息: "{message[:50]}"')

            # ── 系统级关键词自动回复（dynamic_commands 表，动态命令）──
            # 插件未命中时触发；插件命中但 ev.continue_route() 声明"允许继续"时同样触发
            if not matched_any or ev.is_continue_route():
                if await self._try_keyword_reply(ev, message):
                    handled_plugin = 'keyword'
                    return

            # ── 未命中兜底扩展点（插件可在此接管未匹配消息）──
            if not matched_any and handled_plugin is None:
                try:
                    res = await self.framework.hooks.trigger_async(
                        HookPoints.ROUTER_MESSAGE_UNMATCHED,
                        {'event': ev, 'message': message, 'bot_name': bot_name})
                    if any(r is True for r in res):
                        handled_plugin = 'unmatched_handler'
                        return
                except Exception as e:
                    logger.error(f"router.message_unmatched 扩展点异常: {e}")

            # 热路径：闲聊消息每条都会走到这里，日志级别未开启时不做字符串格式化
            if logger.isEnabledFor(logging.DEBUG):
                log_broker.log_system('DEBUG', f'消息未匹配任何命令: "{message[:80]}"')
        finally:
            # ── 四类事件分类完成扩展点（架构 C 项）──
            # 必须发在这里：ev 由本函数构造，且 try 内所有提前 return 都会汇流到
            # finally，保证「命中命令 / 未命中 / 被终止传播」各路径分类都能送达。
            # 分类此时已最终确定（命中命令=general/regex，否则 all；raw 横切恒真）。
            # 无订阅者时 trigger_async 直接短路返回，热路径零开销。
            try:
                await self.framework.hooks.trigger_async(
                    HookPoints.MESSAGE_CLASSIFIED,
                    {'event': ev, 'category': ev.category,
                     'categories': ev.categories, 'bot_name': bot_name})
            except Exception as e:
                logger.error(f"message.classified 扩展点异常: {e}")
            # 路由完成通知扩展点（无论命中与否、是否提前返回）
            try:
                await self.framework.hooks.trigger_async(
                    HookPoints.ROUTER_AFTER_ROUTE,
                    {'event': ev, 'message': message, 'bot_name': bot_name,
                     'matched': matched_any, 'handler': handled_plugin})
            except Exception as e:
                logger.error(f"router.after_route 扩展点异常: {e}")

    async def _broadcast_message_event(self, ev, message: str) -> bool:
        """
        广播 message 事件（文本消息统一监听通道）
        内容监听型插件（关键词回复/违禁词/自动应答等）通过 ctx.on('message', handler) 订阅，
        handler 返回 True 视为已处理（路由终止）

        多平台接入：同样按「适配器↔插件绑定」过滤订阅者——插件声明只服务某些
        适配器（或适配器配了 plugins 白名单）时，不该收到其它适配器的消息广播。
        """
        try:
            result = await self.framework.event_bus.aemit(
                'message', ev, allow=self._plugin_allowed_fn(ev))
            return result is True
        except Exception as e:
            logger.error(f"message 事件广播异常: {e}")
            return False

    def _plugin_allowed_fn(self, ev):
        """构造本事件来源下的插件允许判定函数（供事件广播过滤订阅者）。

        framework 未提供 adapter_plugin_allowed（老版本/测试桩）时返回 None=不过滤。
        """
        allowed = getattr(self.framework, 'adapter_plugin_allowed', None)
        if allowed is None:
            return None
        adapter_key = getattr(ev, 'adapter', '') or getattr(ev, 'bot_name', '') or ''

        def _fn(plugin_name):
            try:
                return bool(allowed(plugin_name, adapter_key))
            except Exception:
                return True   # 判定异常不阻断广播
        return _fn
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
        allow = self._plugin_allowed_fn(ev)
        for t in types:
            await self.framework.event_bus.aemit(f'message.{t}', ev, allow=allow)
        await self.framework.event_bus.aemit('message.media', ev, allow=allow)
