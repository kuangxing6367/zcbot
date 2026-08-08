"""
消息路由器（高性能版）
按插件优先级顺序分发消息，匹配静态命令
- 插件注册的动态命令（is_dynamic=1）仅用于展示，不参与命令匹配
- 系统级动态命令（dynamic_commands 表，关键词自动回复）在插件未命中时兜底匹配

异步模型：
- 后台刷新任务周期性（默认 5s）从 DB 构建纯内存路由表（插件序 + 预编译命令 + 关键词规则），
  路由热路径零 DB 查询、零线程切换（参考 AstrBot 内存路由思路）
- 命令/关键词命中计数交给 framework.stats_writer 批量落库，不阻塞事件循环
- handler 支持 async def（直接 await）和普通 def（转线程执行）
"""
import asyncio
import logging
import re
import threading
import time
from typing import Callable, Optional

from framework.log_broker import log_broker
from framework.event import _extract_text

logger = logging.getLogger('zcbot')


class SimpleMatch:
    """
    纯字符串匹配结果，模拟 re.Match 的常用接口
    group(0) → 匹配的完整文本
    group(1) → 命令后面的参数（无参数时为空字符串）
    让 handler 无需区分正则/简单匹配，统一用 match.group(1) 取参数
    """
    __slots__ = ('_full', '_args')

    def __init__(self, full: str, args: str = ''):
        self._full = full
        self._args = args

    def group(self, n=0):
        if n == 0:
            return self._full
        if n == 1:
            return self._args
        return None

    def groups(self):
        return (self._args,)


class _RouteCommand:
    """预编译后的路由命令（构建一次，热路径直接复用）"""

    __slots__ = ('id', 'pattern', 'handler_name', 'require_level',
                 'rx', 'simple', 'aliases')

    def __init__(self, id, pattern, handler_name, require_level,
                 rx, simple, aliases):
        self.id = id
        self.pattern = pattern
        self.handler_name = handler_name
        self.require_level = require_level
        self.rx = rx          # 编译后的正则，或 None
        self.simple = simple  # 简单前缀匹配 pattern，或 None
        self.aliases = aliases


class _PluginRoute:
    """单个插件的内存路由条目"""

    __slots__ = ('module', 'commands')

    def __init__(self, module, commands=None):
        self.module = module
        self.commands = commands or []


class _KeywordRule:
    """系统关键词自动回复规则（dynamic_commands 表，预编译后热路径复用）"""

    __slots__ = ('id', 'keyword', 'response', 'match_type', 'rx', 'plugin_name')

    def __init__(self, id, keyword, response, match_type, rx, plugin_name):
        self.id = id
        self.keyword = keyword
        self.response = response
        self.match_type = match_type  # exact / prefix / contains / regex
        self.rx = rx                  # regex 类型的预编译正则，其他为 None
        self.plugin_name = plugin_name


class MessageRouter:
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
            try:
                cmds = self.db.query(
                    "SELECT id, plugin_name, pattern, alias, handler, "
                    "require_level, is_active FROM commands "
                    "WHERE is_dynamic = 0 "
                    "AND (is_active = 1 OR (is_active = 0 AND alias IS NOT NULL AND alias != '')) "
                    "ORDER BY priority ASC, created_at ASC"
                )
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

    def _load_keyword_rules(self) -> list:
        """从 dynamic_commands 表加载并预编译关键词自动回复规则"""
        try:
            rows = self.db.query(
                "SELECT id, keyword, response, match_type, plugin_name "
                "FROM dynamic_commands WHERE is_active = 1 ORDER BY id ASC"
            )
        except Exception as e:
            logger.error(f"加载关键词回复失败: {e}")
            return []

        rules = []
        for r in rows:
            try:
                mt = (r.get('match_type') or 'exact').strip().lower()
                if mt not in ('exact', 'prefix', 'contains', 'regex'):
                    mt = 'exact'
                rx = None
                if mt == 'regex':
                    rx = re.compile(r.get('keyword') or '')
                rules.append(_KeywordRule(
                    id=r['id'],
                    keyword=r.get('keyword') or '',
                    response=r.get('response') or '',
                    match_type=mt,
                    rx=rx,
                    plugin_name=r.get('plugin_name') or 'system',
                ))
            except re.error as e:
                logger.warning(f"关键词正则编译失败 [id={r.get('id')}]: {e}")
            except Exception as e:
                logger.error(f"关键词规则解析失败 [id={r.get('id')}]: {e}")
        return rules

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
        post_type = event.get('post_type')
        if post_type != 'message':
            return

        message = _extract_text(event.get('message', ''))
        if not message:
            # 无文本消息（纯分享卡片/图片/视频等）：不走命令匹配，
            # 直接广播到事件总线，供插件通过 ctx.on('message.share') 等订阅处理
            await self._broadcast_non_text(event, bot_name)
            return

        from framework.event import Event
        ev = Event(event, bot_name)
        ev._framework = self.framework

        routes = self._routes
        plugin_order = self._plugin_order
        if not plugin_order:
            # 无可用插件时仍尝试系统关键词自动回复
            if await self._try_keyword_reply(ev, message):
                return
            log_broker.log_system('WARN', f'无可用插件处理消息: "{message[:50]}"')
            return

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

        # 没有任何插件命中 → 系统级关键词自动回复（dynamic_commands 表，动态命令）
        # 插件已处理的场景不触发，避免插件回复 + 关键词回复双重应答
        if not matched_any:
            if await self._try_keyword_reply(ev, message):
                return

        log_broker.log_system('DEBUG', f'消息未匹配任何命令: "{message[:80]}"')

    async def _try_keyword_reply(self, ev, message: str) -> bool:
        """尝试系统关键词自动回复（命中返回 True）"""
        rule = self._match_keyword(message)
        if rule is None:
            return False
        self._keyword_hit(rule.id)
        log_broker.log_system('INFO', f'关键词命中: "{rule.keyword}"（{rule.match_type}）', {
            'user_id': ev.user_id,
            'group_id': ev.group_id,
            'keyword': rule.keyword,
            'match_type': rule.match_type,
        })
        target = {'group_id': ev.group_id} if ev.is_group else {'user_id': ev.user_id}
        await self.framework.api_caller.acall(
            'send_msg', **target, message=rule.response)
        return True

    def _match_keyword(self, message: str) -> Optional[_KeywordRule]:
        """系统关键词自动回复匹配（动态命令，热路径纯内存）"""
        for r in self._keyword_rules:
            mt = r.match_type
            if mt == 'exact':
                if message == r.keyword:
                    return r
            elif mt == 'prefix':
                if r.keyword and message.startswith(r.keyword):
                    return r
            elif mt == 'contains':
                if r.keyword and r.keyword in message:
                    return r
            elif mt == 'regex':
                if r.rx is not None and r.rx.search(message):
                    return r
        return None

    def _keyword_hit(self, kw_id: int):
        """记录关键词命中（交给 stats_writer 批量落库）"""
        writer = getattr(self.framework, 'stats_writer', None)
        if writer is not None:
            writer.keyword_hit(kw_id)

    async def _broadcast_non_text(self, event: dict, bot_name: str = 'default'):
        """
        广播无文本消息到事件总线（供插件订阅，绕开命令匹配）
        事件名：message.<消息段类型>（如 message.share）+ 通用 message.media
        载荷：Event 对象，插件可通过 ev.segments / ev.share 等访问富媒体数据
        """
        from framework.event import Event
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

    # 正则特殊字符，用于判断 pattern 是命令名还是正则
    _REGEX_CHARS = set('^$.*+?()[]{}|\\')

    def _is_regex(self, pattern: str) -> bool:
        """判断 pattern 是否包含正则特殊字符"""
        return any(c in self._REGEX_CHARS for c in pattern)

    @staticmethod
    def _match_simple(pattern: str, message: str) -> Optional[SimpleMatch]:
        """
        纯字符串前缀匹配（不使用 re）
        支持：
        - pattern 带 /（如 "/echo"）匹配 "/echo arg"
        - pattern 不带 /（如 "mc-command"）同时匹配 "mc-command arg" 和 "/mc-command arg"
        返回 SimpleMatch 或 None；SimpleMatch.group(0) 永远是原始消息全文
        """
        def _try(msg: str) -> Optional[SimpleMatch]:
            """尝试用 msg 匹配 pattern，返回 SimpleMatch（_full 用原始 message）"""
            if msg == pattern:
                return SimpleMatch(message, '')
            if len(msg) > len(pattern) and msg.startswith(pattern):
                sep = msg[len(pattern)]
                if sep == ' ' or sep == '\t':
                    args = msg[len(pattern) + 1:].strip()
                    return SimpleMatch(message, args)
            return None

        # 1. 先试原始消息
        result = _try(message)
        if result:
            return result
        # 2. 消息以 / 开头 且 pattern 不以 / 开头 → 去掉 / 再试
        if message.startswith("/") and not pattern.startswith("/"):
            result = _try(message[1:])
            if result:
                return result
        # 3. 消息不以 / 开头 且 pattern 以 / 开头 → 加上 / 再试
        if not message.startswith("/") and pattern.startswith("/"):
            result = _try("/" + message)
            if result:
                return result
        return None

    @staticmethod
    def _regex_search(rx: re.Pattern, message: str) -> Optional[re.Match]:
        """编译后的正则搜索，自动处理 / 前缀"""
        match = rx.search(message)
        if match:
            return match
        if message.startswith("/"):
            match = rx.search(message[1:])
            if match:
                return match
        if not message.startswith("/") and rx.pattern.startswith("^/"):
            match = rx.search("/" + message)
        return match

    async def _match_plugin_commands(self, entry: _PluginRoute, ev, message: str,
                                     plugin_name: str) -> bool:
        """在指定插件的内存命令表中匹配消息（零 DB）"""
        module = entry.module
        if module is None:
            log_broker.log_plugin(plugin_name, '模块未加载，跳过')
            return False

        for cmd in entry.commands:
            match = None
            matched_by = ''

            # ---- 主 pattern 匹配（仅启用状态时匹配）----
            if cmd.rx is not None:
                match = self._regex_search(cmd.rx, message)
                if match:
                    matched_by = cmd.pattern
            elif cmd.simple is not None:
                match = self._match_simple(cmd.simple, message)
                if match:
                    matched_by = cmd.pattern

            # ---- 别名匹配（无论启用/禁用，只要设置别名就匹配）----
            if not match and cmd.aliases:
                for alias in cmd.aliases:
                    match = self._match_simple(alias, message)
                    if match:
                        matched_by = f"别名:{alias}"
                        break

            if match:
                # ── 权限检查 ──
                require = cmd.require_level  # 'admin' | 'super' | ''
                if require == 'admin' and not ev.is_admin:
                    self._stats_hit(cmd.id)
                    log_broker.log_plugin(plugin_name, '权限不足', {
                        'handler': cmd.handler_name,
                        'user_id': ev.user_id,
                        'role': ev.role,
                        'message': message[:80],
                    })
                    target = {'group_id': ev.group_id} if ev.is_group else {'user_id': ev.user_id}
                    await self.framework.api_caller.acall(
                        'send_msg',
                        **target,
                        message=f'权限不足（需要 {require} 权限，当前身份: {ev.role}）'
                    )
                    return True
                if require == 'super' and not ev.is_superuser:
                    self._stats_hit(cmd.id)
                    log_broker.log_plugin(plugin_name, '权限不足', {
                        'handler': cmd.handler_name,
                        'user_id': ev.user_id,
                        'role': ev.role,
                    })
                    target = {'group_id': ev.group_id} if ev.is_group else {'user_id': ev.user_id}
                    await self.framework.api_caller.acall(
                        'send_msg',
                        **target,
                        message=f'权限不足（需要超级管理员权限）'
                    )
                    return True

                # 命中计数（异步批量落库，不阻塞路由）
                self._stats_hit(cmd.id)
                log_broker.log_plugin(plugin_name, '命令命中', {
                    'matched_by': matched_by,
                    'handler': cmd.handler_name,
                    'message': message[:100],
                    'user_id': ev.user_id,
                    'group_id': ev.group_id,
                })
                handler = getattr(module, cmd.handler_name, None)
                if handler and callable(handler):
                    # 注入当前事件的 bot 到 ctx，确保回复走正确的 OneBot 实例
                    if hasattr(module, 'ctx'):
                        module.ctx._current_bot = ev.bot_name
                    try:
                        if asyncio.iscoroutinefunction(handler):
                            result = await handler(ev, match)
                        else:
                            # 同步 handler 转线程执行，不阻塞事件循环
                            result = await asyncio.to_thread(handler, ev, match)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error(
                            f"[{plugin_name}] handler 异常: {cmd.handler_name} - {e}",
                            exc_info=True)
                        # 生命周期钩子：插件 on_error(event, error) 处理自己的错误
                        try:
                            on_error = getattr(module, 'on_error', None)
                            if callable(on_error):
                                on_error(ev, e)
                        except Exception as he:
                            logger.error(f"[{plugin_name}] on_error 钩子异常: {he}")
                        return True  # 视为已处理，避免半处理消息继续传播
                    # handler 返回 False 表示"未实际处理，继续路由"
                    if result is False:
                        continue
                else:
                    log_broker.log_plugin(plugin_name, '处理函数不存在', {
                        'handler': cmd.handler_name
                    })
                    continue
                return True  # 匹配成功，由 route() 检查 is_stopped()

        return False

    def _stats_hit(self, cmd_id: int):
        """记录命令命中（交给 stats_writer 批量落库）"""
        writer = getattr(self.framework, 'stats_writer', None)
        if writer is not None:
            writer.command_hit(cmd_id)
