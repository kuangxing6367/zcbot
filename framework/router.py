"""
消息路由器
按插件优先级顺序分发消息，匹配静态命令
动态命令（is_dynamic=1）仅用于展示，不参与路由匹配
"""
import logging
import re
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


class MessageRouter:
    """消息路由分发器"""

    def __init__(self, framework):
        self.framework = framework
        self.db = framework.db

        # ── 路由缓存（避免每条消息都查库）──
        self._plugin_order_cache = []
        self._plugin_order_cache_time = 0
        self._commands_cache = {}       # plugin_name -> [cmd, ...]
        self._commands_cache_time = {}  # plugin_name -> timestamp
        self._cache_ttl = 5  # 缓存有效期（秒）

    def _invalidate_cache(self):
        """使路由缓存失效（外部调用，如插件重载后）"""
        self._plugin_order_cache = []
        self._plugin_order_cache_time = 0
        self._commands_cache = {}
        self._commands_cache_time = {}

    def route(self, event: dict, bot_name: str = 'default'):
        """
        路由一条消息事件
        1. 按插件优先级排序 → 遍历插件
        2. 每个插件内按 commands.priority 匹配命令
        3. 未命中 → 记录未匹配日志
        """
        post_type = event.get('post_type')
        if post_type != 'message':
            return

        message = _extract_text(event.get('message', ''))
        if not message:
            return

        from framework.event import Event
        ev = Event(event, bot_name)
        ev._framework = self.framework

        plugin_order = self._get_plugin_order()
        if not plugin_order:
            log_broker.log_system('WARN', f'无可用插件处理消息: "{message[:50]}"')
            return

        log_broker.log_system('INFO',
            f'路由消息: "{message[:80]}" → 插件队列: {[p[0] for p in plugin_order]}')

        for plugin_name, _ in plugin_order:
            # 群级插件开关检查（私聊不限制）
            if ev.is_group and not self.framework.plugin_loader.is_plugin_enabled_for_group(
                plugin_name, ev.group_id
            ):
                logger.debug(f"跳过 [{plugin_name}]：已在群 {ev.group_id} 中禁用")
                continue
            matched = self._match_plugin_commands(plugin_name, ev, message)
            if not matched:
                continue
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

        log_broker.log_system('DEBUG', f'消息未匹配任何命令: "{message[:80]}"')

    def _get_plugin_order(self) -> list:
        """获取插件优先级排序列表（带 5 秒缓存）"""
        now = time.time()
        if self._plugin_order_cache and (now - self._plugin_order_cache_time) < self._cache_ttl:
            return self._plugin_order_cache

        try:
            rows = self.db.query(
                "SELECT plugin_name, priority, created_at FROM plugins "
                "WHERE is_active = 1 AND has_register = 1 AND status = 'running' "
                "ORDER BY priority ASC, created_at ASC"
            )
            # 过滤掉内存中未加载的插件（防止 DB 残留导致路由到已卸载/加载失败的插件）
            loaded = self.framework.plugin_loader.get_loaded_plugins()
            self._plugin_order_cache = [
                (r['plugin_name'], r['priority'])
                for r in rows if r['plugin_name'] in loaded
            ]
            self._plugin_order_cache_time = now
            return self._plugin_order_cache
        except Exception as e:
            logger.error(f"获取插件排序失败: {e}")
            return []

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

    def _match_plugin_commands(self, plugin_name: str, ev, message: str) -> bool:
        """
        在指定插件的命令中匹配消息（带 5 秒命令缓存）
        """
        commands = self._get_cached_commands(plugin_name)
        if not commands:
            return False

        module = self.framework.plugin_loader.get_plugin_module(plugin_name)
        if module is None:
            log_broker.log_plugin(plugin_name, '模块未加载，跳过')
            return False

        for cmd in commands:
            try:
                pattern = cmd['pattern']
                is_active = cmd.get('is_active', 1)
                match = None
                matched_by = ''

                # ---- 主 pattern 匹配（仅启用状态时匹配）----
                if is_active:
                    if self._is_regex(pattern):
                        # 先匹配原始消息
                        match = re.search(pattern, message)
                        if match:
                            matched_by = pattern
                        # 正则没匹配到，尝试自动处理 / 前缀
                        # （和 _match_simple 的 / 前缀剥离逻辑保持一致）
                        if not match:
                            if message.startswith("/"):
                                # 消息带 / 但正则要求不带 → 去掉 / 再试
                                match = re.search(pattern, message[1:])
                            elif not message.startswith("/") and pattern.startswith("^/"):
                                # 消息不带 / 但正则要求带 / → 加上 / 再试
                                match = re.search(pattern, "/" + message)
                            if match:
                                matched_by = pattern
                    else:
                        match = self._match_simple(pattern, message)
                        if match:
                            matched_by = pattern

                # ---- 别名匹配（无论启用/禁用，只要设置别名就匹配）----
                if not match and cmd.get('alias'):
                    aliases = [a.strip() for a in cmd['alias'].split(',') if a.strip()]
                    for alias in aliases:
                        match = self._match_simple(alias, message)
                        if match:
                            matched_by = f"别名:{alias}"
                            break

                if match:
                    # ── 权限检查（参考 AstrBot PermissionTypeFilter）──
                    require = cmd.get('require_level', '')  # 'admin' | 'super' | ''
                    if require == 'admin' and not ev.is_admin:
                        self.db.execute(
                            "UPDATE commands SET hit_count = hit_count + 1 WHERE id = %s",
                            (cmd['id'],)
                        )
                        ev._framework = self.framework  # 确保 framework 可用
                        log_broker.log_plugin(plugin_name, '权限不足', {
                            'handler': cmd['handler'],
                            'user_id': ev.user_id,
                            'role': ev.role,
                            'message': message[:80],
                        })
                        # 发送提示
                        from framework.event import Event
                        ev_type = 'group' if ev.is_group else 'private'
                        target = {'group_id': ev.group_id} if ev.is_group else {'user_id': ev.user_id}
                        self.framework.api_caller.call(
                            'send_msg',
                            **target,
                            message=f'权限不足（需要 {require} 权限，当前身份: {ev.role}）'
                        )
                        return True
                    if require == 'super' and not ev.is_superuser:
                        ev._framework = self.framework
                        log_broker.log_plugin(plugin_name, '权限不足', {
                            'handler': cmd['handler'],
                            'user_id': ev.user_id,
                            'role': ev.role,
                        })
                        target = {'group_id': ev.group_id} if ev.is_group else {'user_id': ev.user_id}
                        self.framework.api_caller.call(
                            'send_msg',
                            **target,
                            message=f'权限不足（需要超级管理员权限）'
                        )
                        return True

                    # 命中计数
                    self.db.execute(
                        "UPDATE commands SET hit_count = hit_count + 1 WHERE id = %s",
                        (cmd['id'],)
                    )
                    log_broker.log_plugin(plugin_name, '命令命中', {
                        'matched_by': matched_by,
                        'handler': cmd['handler'],
                        'message': message[:100],
                        'user_id': ev.user_id,
                        'group_id': ev.group_id,
                    })
                    handler = getattr(module, cmd['handler'], None)
                    if handler and callable(handler):
                        # 注入当前事件的 bot 到 ctx，确保回复走正确的 OneBot 实例
                        if hasattr(module, 'ctx'):
                            module.ctx._current_bot = ev.bot_name
                        result = handler(ev, match)
                        # handler 返回 False 表示"未实际处理，继续路由"
                        if result is False:
                            continue
                    else:
                        log_broker.log_plugin(plugin_name, '处理函数不存在', {
                            'handler': cmd['handler']
                        })
                        continue
                    return True  # 匹配成功，由 route() 检查 is_stopped()
            except re.error as e:
                logger.warning(f"正则错误 [{plugin_name}]: {cmd['pattern']} - {e}")
                continue

        return False

    def _get_cached_commands(self, plugin_name: str) -> list:
        """获取插件命令列表（带 5 秒缓存）"""
        now = time.time()
        cached_time = self._commands_cache_time.get(plugin_name, 0)
        if plugin_name in self._commands_cache and (now - cached_time) < self._cache_ttl:
            return self._commands_cache[plugin_name]

        try:
            commands = self.db.query(
                "SELECT id, pattern, alias, handler, is_dynamic, require_level, is_active FROM commands "
                "WHERE plugin_name = %s AND (is_active = 1 OR (is_active = 0 AND alias IS NOT NULL AND alias != '')) "
                "ORDER BY priority ASC, created_at ASC",
                (plugin_name,)
            )
            self._commands_cache[plugin_name] = commands
            self._commands_cache_time[plugin_name] = now
            return commands
        except Exception as e:
            logger.error(f"查询命令失败 [{plugin_name}]: {e}")
            log_broker.log_plugin(plugin_name, '查询命令失败', {'error': str(e)})
            return []
