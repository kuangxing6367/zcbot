# -*- coding: utf-8 -*-
"""Framework 事件分发 / 回复 / 原始消息 / notice·request / 群成员同步（自 core.py 剥离的 mixin）"""
import asyncio
import logging

from framework.hooks import HookPoints
from framework.log_broker import log_broker
from framework.messaging.protocol import ProtocolAdapter

logger = logging.getLogger('zcbot')

class FrameworkDispatchMixin:
    """事件分发 / 回复 / 原始消息 / notice·request / 群成员同步"""

    # 以下方法依赖 Framework.__init__ 建立的实例字段

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

        payload = {'text': text, 'group_id': group_id,
                   'user_id': user_id, 'source': source}
        # 发送前扩展点（返回 False 取消本次发送）
        try:
            if False in (await self.hooks.trigger_async(
                    HookPoints.MESSAGE_BEFORE_SEND, payload)):
                logger.debug("消息发送被扩展点 message.before_send 取消")
                return None
        except Exception as e:
            logger.error(f"message.before_send 扩展点异常: {e}")

        adapter = self.services.get('protocol_adapter')
        result = None
        # 仅当接入端确实覆写了 send_text（而非基类 unsupported 默认）时走适配器
        if adapter is not None and type(adapter).send_text is not ProtocolAdapter.send_text:
            try:
                result = await adapter.send_text(
                    text,
                    group_id=group_id if is_group else None,
                    user_id=None if is_group else user_id,
                    source=source,
                )
            except Exception as e:
                logger.warning(f"接入端 send_text 失败，回退通用调用: {e}")

        # 适配器未返回（不支持/失败）→ 回退到通用 API 调用
        if result is None:
            # 向后兼容：未实现中立 send_text 的旧接入端
            caller = self.services.get('api_caller')
            if caller is None:
                logger.debug("无可用接入端，框架自动回复被跳过")
                return None
            tgt = {'group_id': group_id} if is_group else {'user_id': user_id}
            result = await caller.acall('send_msg', **tgt, message=text)

        # 发送后扩展点
        try:
            await self.hooks.trigger_async(
                HookPoints.MESSAGE_AFTER_SEND, {**payload, 'result': result})
        except Exception as e:
            logger.error(f"message.after_send 扩展点异常: {e}")
        return result

    async def dispatch_event(self, event: dict):
        """
        协议适配器入口：将转换后的内部事件分发到框架
        适配器（如 onebot_adapter）调用此方法，框架处理路由/事件总线
        """
        event_type = event.get('type', '')
        bot_name = event.get('bot_name', 'default')

        # 事件进入内核前的扩展点（任何 handler 返回 False 即丢弃该事件）
        try:
            if False in (await self.hooks.trigger_async(
                    HookPoints.EVENT_BEFORE_DISPATCH, event, bot_name)):
                logger.debug("事件被扩展点 event.before_dispatch 拦截丢弃")
                return
        except Exception as e:
            logger.error(f"event.before_dispatch 扩展点异常: {e}")

        try:
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
        finally:
            # 事件处理后扩展点（无论是否提前返回都会触发）
            try:
                await self.hooks.trigger_async(
                    HookPoints.EVENT_AFTER_DISPATCH, event, bot_name)
            except Exception as e:
                logger.error(f"event.after_dispatch 扩展点异常: {e}")

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
