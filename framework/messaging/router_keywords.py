# -*- coding: utf-8 -*-
"""
系统关键词自动回复（dynamic_commands 表）—— 自 messaging/router.py 剥离

KeywordReplyMixin 提供：
  _load_keyword_rules   加载并预编译规则
  _try_keyword_reply    命中匹配并回复
  _call_keyword_handler plugin:func 动态生成回复
  _match_keyword        热路径纯内存匹配
  _keyword_hit          命中计数（stats_writer 批量落库）
依赖 self.framework / self.db / self._keyword_rules（由 MessageRouter 持有）。
"""
import asyncio
import logging
import re
from typing import Optional

from framework.log_broker import log_broker

logger = logging.getLogger('zcbot')


class _KeywordRule:
    """系统关键词自动回复规则（dynamic_commands 表，预编译后热路径复用）"""

    __slots__ = ('id', 'keyword', 'response', 'match_type', 'rx', 'handler', 'plugin_name')

    def __init__(self, id, keyword, response, match_type, rx, handler, plugin_name):
        self.id = id
        self.keyword = keyword
        self.response = response
        self.match_type = match_type  # exact / prefix / contains / regex
        self.rx = rx                  # regex 类型的预编译正则，其他为 None
        self.handler = handler        # 'plugin:func' 回调，命中后调用生成回复；空则用静态 response
        self.plugin_name = plugin_name


class KeywordReplyMixin:
    """关键词自动回复（dynamic_commands）"""

    def __init_keywords__(self):
        """MessageRouter.__init__ 调用：初始化关键词规则表"""
        self._keyword_rules: list = []

    def _load_keyword_rules(self) -> list:
        """从 dynamic_commands 表加载并预编译关键词自动回复规则（兼容旧表无 handler 列）"""
        try:
            rows = self.db.query(
                "SELECT id, keyword, response, match_type, handler, plugin_name "
                "FROM dynamic_commands WHERE is_active = 1 ORDER BY id ASC"
            )
        except Exception:
            # 旧表无 handler 列 → 回退基础查询，不报错
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
                    handler=r.get('handler') or '',
                    plugin_name=r.get('plugin_name') or 'system',
                ))
            except re.error as e:
                logger.warning(f"关键词正则编译失败 [id={r.get('id')}]: {e}")
            except Exception as e:
                logger.error(f"关键词规则解析失败 [id={r.get('id')}]: {e}")
        return rules


    async def _try_keyword_reply(self, ev, message: str) -> bool:
        """尝试系统关键词自动回复（命中返回 True）"""
        rule = self._match_keyword(message)
        if rule is None:
            return False
        self._keyword_hit(rule.id)

        # 优先 handler 回调生成回复内容（dynamic_commands.handler = 'plugin:func'），失败回退静态 response
        reply = rule.response
        if rule.handler:
            try:
                generated = await self._call_keyword_handler(rule, message)
                if generated:
                    reply = generated
            except Exception as e:
                logger.error(f"关键词 handler 调用失败 [{rule.handler}]: {e}")

        log_broker.log_system('INFO', f'关键词命中: "{rule.keyword}"（{rule.match_type}）', {
            'user_id': ev.user_id,
            'group_id': ev.group_id,
            'keyword': rule.keyword,
            'match_type': rule.match_type,
        })
        if not reply:
            return True  # 已匹配但无回复内容，避免重复匹配
        await self.framework.reply_text(ev, reply)
        return True


    async def _call_keyword_handler(self, rule, message: str):
        """
        调用 dynamic_commands.handler（格式 'plugin:func'）生成回复文本
        签名：func(rule, message) → 回复文本或 None
        """
        spec = (rule.handler or '').strip()
        if not spec or ':' not in spec:
            return None
        plugin_name, func_name = spec.split(':', 1)
        plugin_name = plugin_name.strip()
        func_name = func_name.strip()
        if not plugin_name or not func_name:
            return None
        module = self.framework.plugin_loader.get_plugin_module(plugin_name)
        if module is None:
            return None
        func = getattr(module, func_name, None)
        if func is None or not callable(func):
            return None
        if asyncio.iscoroutinefunction(func):
            result = await func(rule, message)
        else:
            result = await asyncio.to_thread(func, rule, message)
        return result or None


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

