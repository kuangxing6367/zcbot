# -*- coding: utf-8 -*-
"""MessageRouter 匹配子系统（自 messaging/router.py 剥离的 mixin）

_simple / 正则 / 插件命令匹配与命中统计；依赖 router 中的路由数据结构。
"""
import asyncio
import logging
import re
from typing import Optional

from framework.hooks import HookPoints
from framework.log_broker import log_broker
from framework.blackbox import record as _bb_record

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


class _PluginRoute:
    """单个插件的内存路由条目

    index: 首字符倒排索引 {首字符|None: [命令]}，用于热路径剪枝候选集。
           None 键存放"无法按首字符剪枝"的命令（正则命令 / 空 pattern），
           每条消息都必须尝试。索引只做候选筛选，不改变任何匹配语义。
    """

    __slots__ = ('module', 'commands', 'index')

    def __init__(self, module, commands=None, index=None):
        self.module = module
        self.commands = commands or []
        self.index = index

    def candidates(self, message: str):
        """返回该消息的候选命令（索引命中 + 不可剪枝桶），索引缺失时退化为全量。

        索引存的是命令下标，候选按下标升序返回，保证与全量遍历的命中顺序一致
        （命中即止的语义依赖命令表原顺序，不能打乱）。
        """
        index = self.index
        if index is None:
            return self.commands
        # 每个首字符对应的候选在构建期已合并好（含不可剪枝桶）并有序，此处仅一次查表
        idxs = index.get(message[0] if message else '')
        if idxs is None:
            # 首字符无任何命令特征 → 只剩不可剪枝（正则）命令
            idxs = index.get(None)
            if not idxs:
                return []
        commands = self.commands
        return [commands[i] for i in idxs]


def _index_keys_for(pattern: str):
    """计算某个可前缀匹配字符串在哪些消息首字符下才可能命中，返回 None 表示无法剪枝。

    对齐 _match_simple 的三条分支：
      1. message 直接以 pattern 开头        → 首字符 == pattern[0]
      2. message 带 "/" 且 pattern 不带 "/" → 首字符 == "/"
      3. message 不带 "/" 且 pattern 带 "/" → 去掉 "/" 后前缀匹配 → 首字符 == pattern[1]
    """
    if not pattern:
        return None
    keys = {pattern[0]}
    if pattern[0] == '/':
        if len(pattern) > 1:
            keys.add(pattern[1])
    else:
        keys.add('/')
    return keys


def build_command_index(commands):
    """为单个插件的命令表构建首字符倒排索引（在路由表重建时调用一次）。

    返回 {首字符: [命令下标(升序), ...], None: [不可剪枝命令下标, ...]}。
    - 剪枝是保守的：只要原匹配逻辑可能命中，该命令必在候选集内；
    - 候选按命令表原顺序（下标升序），保证"命中第一个即执行"语义不变；
    - 不可剪枝命令（正则等）在构建期就并入每个首字符桶，热路径只做一次查表、
      零合并开销；首字符未命中任何桶时返回 None 桶（仍尝试正则命令）。
    """
    buckets = {}
    always = []
    for i, cmd in enumerate(commands):
        keys = None
        if cmd.rx is None:
            keys = set()
            if cmd.simple is not None:
                ks = _index_keys_for(cmd.simple)
                if ks is None:
                    keys = None
                else:
                    keys |= ks
            if keys is not None and cmd.aliases:
                for alias in cmd.aliases:
                    ks = _index_keys_for(alias)
                    if ks is None:
                        keys = None
                        break
                    keys |= ks
        if not keys:
            # 无可用首字符特征（正则 / 禁用且无别名 / 空 pattern）→ 每条消息都尝试
            always.append(i)
            continue
        for k in keys:
            buckets.setdefault(k, []).append(i)

    index = {}
    if always:
        # 预合并：每个首字符桶 = 该桶命令 + 全部不可剪枝命令（去重后升序）
        for k, idxs in buckets.items():
            merged = set(idxs)
            merged.update(always)
            index[k] = sorted(merged)
        index[None] = list(always)
    else:
        for k, idxs in buckets.items():
            index[k] = idxs
    return index


class RouterMatchMixin:
    """命令匹配 / 插件路由匹配 / 命中统计"""

    _REGEX_CHARS = set('^$.*+?()[]{}|\\')

    def _command_handler_timeout(self):
        """命令 handler 执行超时（秒，None=不限制）

        配置：framework.command_handler_timeout，默认 30.0，<=0 表示不限制。
        原始消息处理器已有独立超时（framework.raw_handler_timeout），此处补齐命令
        handler 的超时：恶意/有 bug 的插件写个 await 死循环或长时间阻塞会冻结整条
        事件管线（单事件循环 + 单 worker），故统一熔断，避免单点拖垮全部消息。
        """
        try:
            cfg = (self.framework.config or {}).get('framework', {})
            timeout = float(cfg.get('command_handler_timeout', 30.0))
        except (TypeError, ValueError, AttributeError):
            return 30.0
        return timeout if timeout > 0 else None

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

        # 首字符倒排索引剪枝：只尝试"有可能命中"的命令（语义与全量遍历等价）
        for cmd in entry.candidates(message):
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
                # ── 黑名单拦截（命中黑名单的用户直接拒绝执行命令，修复 A13）──
                if ev.role == 'blacklist':
                    self._stats_hit(cmd.id)
                    log_broker.log_plugin(plugin_name, '黑名单拦截', {
                        'handler': cmd.handler_name,
                        'user_id': ev.user_id,
                        'group_id': ev.group_id,
                        'message': message[:80],
                    })
                    return True  # 拦截并终止传播，命令不执行

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
                    await self.framework.reply_text(
                        ev, f'权限不足（需要 {require} 权限，当前身份: {ev.role}）')
                    return True
                if require == 'super' and not ev.is_superuser:
                    self._stats_hit(cmd.id)
                    log_broker.log_plugin(plugin_name, '权限不足', {
                        'handler': cmd.handler_name,
                        'user_id': ev.user_id,
                        'role': ev.role,
                    })
                    await self.framework.reply_text(ev, '权限不足（需要超级管理员权限）')
                    return True

                # ── 权限节点检查（节点式，与 require_level 并存）──
                # require_perm 为空时完全不触发权限解析，普通消息零开销
                perm_node = cmd.require_perm
                if perm_node and not ev.has_perm(perm_node):
                    self._stats_hit(cmd.id)
                    log_broker.log_plugin(plugin_name, '权限不足', {
                        'handler': cmd.handler_name,
                        'user_id': ev.user_id,
                        'group_id': ev.group_id,
                        'node': perm_node,
                        'role': ev.role,
                        'message': message[:80],
                    })
                    await self.framework.reply_text(
                        ev, f'权限不足（需要权限节点: {perm_node}）')
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
                # ── 命令执行前扩展点（返回 False 跳过本命令，继续尝试其它匹配）──
                try:
                    _cb_res = await self.framework.hooks.trigger_async(
                        HookPoints.COMMAND_BEFORE,
                        {'plugin': plugin_name, 'handler': cmd.handler_name,
                         'command_id': cmd.id, 'pattern': cmd.pattern,
                         'event': ev, 'match': match})
                    if False in _cb_res:
                        log_broker.log_plugin(plugin_name, '命令被扩展点 command.before 跳过', {
                            'handler': cmd.handler_name,
                            'user_id': ev.user_id,
                            'group_id': ev.group_id,
                            'message': message[:80],
                        })
                        continue
                except Exception as e:
                    logger.error(f"command.before 扩展点异常: {e}")

                handler = getattr(module, cmd.handler_name, None)
                if handler and callable(handler):
                    # 黑匣子：进入插件 handler（handler_enter）—— 事件流第 9 步
                    try:
                        from framework.runtime import current_event_id_var as _eid_var
                        _eid = _eid_var.get()
                    except Exception:
                        _eid = None
                    _bb_record('handler_enter', plugin=plugin_name,
                               fn=cmd.handler_name, event_id=_eid, step=9,
                               detail=cmd.pattern[:60])
                    # ── 四类事件分类回填（架构 C 项）──
                    # 命令真正执行前锁定其类别：正则 pattern 命中 → regex，否则 general。
                    # 仅在实际执行时回填（被 command.before 跳过 / 黑名单拦截 / 处理函数缺失
                    # 的情况不视为"命中命令"，保持 category 为 all）。
                    ev._category_command = ('regex'
                                            if cmd.rx is not None
                                            else 'general')
                    # 注入当前事件的 bot 到上下文变量（contextvars），确保回复走正确的
                    # OneBot 实例。协程创建与 asyncio.to_thread 均携带上下文快照，
                    # 并发消息互不干扰（原 module.ctx 插件级共享变量多 bot 时会交错错发）
                    from framework.runtime import current_source_var as current_bot_var
                    _bot_token = current_bot_var.set(ev.bot_name)
                    _handler_failed = False
                    _handler_timed_out = False
                    _hto = self._command_handler_timeout()
                    try:
                        if asyncio.iscoroutinefunction(handler):
                            _coro = handler(ev, match)
                            result = await asyncio.wait_for(_coro, timeout=_hto) if _hto else await _coro
                        else:
                            # 同步 handler 转线程执行，不阻塞事件循环
                            _thr = asyncio.to_thread(handler, ev, match)
                            result = await asyncio.wait_for(_thr, timeout=_hto) if _hto else await _thr
                    except asyncio.CancelledError:
                        raise
                    except (asyncio.TimeoutError, TimeoutError) as te:
                        # 命令 handler 执行超时：熔断，避免单点冻结整条事件管线；
                        # 视为已处理（不继续传播、不重试），并交给插件 on_error 处理。
                        logger.error(
                            f"[{plugin_name}] handler 执行超时（>{_hto}s）已熔断: {cmd.handler_name}")
                        try:
                            on_error = getattr(module, 'on_error', None)
                            if callable(on_error):
                                on_error(ev, te)
                        except Exception as he:
                            logger.error(f"[{plugin_name}] on_error 钩子异常: {he}")
                        _handler_failed = True
                        _handler_timed_out = True
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
                        _handler_failed = True
                        return True  # 视为已处理，避免半处理消息继续传播
                    finally:
                        current_bot_var.reset(_bot_token)
                        # 黑匣子：handler 执行完毕（handler_exit）—— 事件流第 11 步
                        _bb_record('handler_exit', plugin=plugin_name,
                                   fn=cmd.handler_name, event_id=_eid, step=11,
                                   msg=("handler 执行超时已熔断"
                                        if _handler_timed_out else
                                        ("handler 异常，已交 on_error 处理"
                                         if _handler_failed else "handler 执行完毕")))
                    if _handler_timed_out:
                        # 超时熔断：视为已处理（不继续传播、不重试），直接结束本插件匹配
                        return True
                    # 命令执行后扩展点（通知，不短路）
                    try:
                        await self.framework.hooks.trigger_async(
                            HookPoints.COMMAND_AFTER,
                            {'plugin': plugin_name, 'handler': cmd.handler_name,
                             'command_id': cmd.id, 'event': ev, 'match': match,
                             'result': result})
                    except Exception as e:
                        logger.error(f"command.after 扩展点异常: {e}")
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
