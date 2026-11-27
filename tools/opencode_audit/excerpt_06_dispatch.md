# 第 6 轮侦察材料：framework/core/dispatch.py 第 490~619 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
 490         return True
 491 
 492     async def _process_event(self, event: dict):
 493         """
 494         事件处理主体：按类型分流 meta / message / notice / request。
 495         （原 dispatch_event 核心逻辑，入队改造后由 worker 调用）
 496         """
 497         event_type = event.get('type', '') or event.get('post_type', '')
 498         bot_name = event.get('bot_name', 'default')
 499 
 500         # 事件关联 ID：把事件的稳定标识注入 contextvar，使本次事件处理跨度内
 501         # （router 路由、event_bus 派发、插件 handler 内的日志）都能按 event_id 串联。
 502         # _bb_eid 在 dispatch_event 入队时生成；为空时（极早期事件）不注入。
 503         _eid = event.get('_bb_eid')
 504         _eid_token = current_event_id_var.set(_eid) if _eid else None
 505         try:
 506             # 黑匣子：事件分类（classify）—— 事件流第 4 步，记录类型与文本预览
 507             _bb_record('classify', plugin='dispatch', fn='_process_event',
 508                        event_id=event.get('_bb_eid'),
 509                        msg=f"type={event_type} msg_type={event.get('message_type','')}")
 510             logger.debug("dispatch_event: type=%s bot=%s msg_type=%s",
 511                          event_type, bot_name, event.get('message_type', ''))
 512 
 513             # 元事件 → 广播
 514             if event_type == 'meta_event':
 515                 meta_type = event.get('sub_type', 'unknown')
 516                 await self.event_bus.aemit(f'meta.{meta_type}', event)
 517                 return
 518 
 519             # 消息事件 → 路由
 520             if event_type == 'message':
 521                 if await self._dispatch_raw_message_handlers(event, bot_name):
 522                     return
 523 
 524                 from framework.messaging.event import _extract_text
 525                 raw_message = _extract_text(event.get('message', ''))
 526                 # 预提取文本随事件携带：router 构造 Event 时直接复用，
 527                 # 避免同一消息段数组被遍历两次（热路径优化）
 528                 event['_msg_text'] = raw_message
 529                 message_type = event.get('message_type', 'unknown')
 530                 user_id = event.get('user_id', 0)
 531                 group_id = event.get('group_id')
 532                 sender = event.get('sender', {})
 533 
 534                 log_raw = self.config.get('log', {}).get('log_raw_message', True)
 535                 if log_raw:
 536                     log_broker.log_message(bot_name, message_type, user_id, group_id,
 537                                            raw_message, event.get('message_id'))
 538                 else:
 539                     source = f"群{group_id}" if group_id else f"私聊{user_id}"
 540                     log_broker.log('message', 'INFO',
 541                                    f"[{bot_name}] {message_type} {source}: (原始内容未记录)",
 542                                    {'bot': bot_name, 'message_type': message_type,
 543                                     'user_id': user_id, 'group_id': group_id})
 544 
 545                 adapter_key = event.get('adapter') or bot_name
 546                 # 群名回填：事件里没有就问适配器要一次（带缓存），否则群组管理
 547                 # 的群名一列永远是空。回填后同步写回 event，路由层也能读到。
 548                 if message_type == 'group' and not sender.get('group_name'):
 549                     gname = await self._resolve_group_name(bot_name, group_id, adapter_key)
 550                     if gname:
 551                         sender = {**sender, 'group_name': gname}
 552                         event['sender'] = sender
 553                 self.stats_writer.register_user(
 554                     user_id, sender, message_type, group_id, adapter_key=adapter_key)
 555                 # 注：message.classified 扩展点由 router.route() 内部发出——
 556                 # Event 对象在路由内构造、分类也只在那里回填，本层拿不到 ev
 557                 # （此处 event 仍是原始 dict，直接读 .category 会 AttributeError）。
 558                 await self.router.route(event, bot_name)
 559 
 560             elif event_type == 'notice':
 561                 await self._handle_notice(event, bot_name)
 562             elif event_type == 'request':
 563                 await self._handle_request(event, bot_name)
 564         finally:
 565             # 事件处理后扩展点（无论是否提前返回都会触发）
 566             try:
 567                 await self.hooks.trigger_async(
 568                     HookPoints.EVENT_AFTER_DISPATCH, event, bot_name)
 569             except Exception as e:
 570                 logger.error(f"event.after_dispatch 扩展点异常: {e}")
 571             finally:
 572                 if _eid_token is not None:
 573                     current_event_id_var.reset(_eid_token)
 574 
 575     def register_raw_message_handler(self, plugin_name: str, handler, priority: int = 50):
 576         """注册插件原始消息处理器（同插件同 handler 去重，按优先级升序）"""
 577         for item in self._raw_message_handlers:
 578             if item['plugin_name'] == plugin_name and item['handler'] == handler:
 579                 return
 580         self._raw_message_handlers.append({
 581             'plugin_name': plugin_name,
 582             'priority': priority,
 583             'handler': handler,
 584         })
 585         self._raw_message_handlers.sort(key=lambda x: x['priority'])
 586 
 587     def unregister_raw_message_handlers(self, plugin_name: str):
 588         """移除某插件的全部原始消息处理器（插件卸载时调用）"""
 589         self._raw_message_handlers = [
 590             item for item in self._raw_message_handlers
 591             if item['plugin_name'] != plugin_name
 592         ]
 593 
 594     def _raw_handler_timeout(self):
 595         """原始消息处理器超时（秒，None=不限制）
 596 
 597         配置：framework.raw_handler_timeout，默认 10s，<=0 表示不限制。
 598         这些 handler 是串行按优先级调用的，任一卡住会阻塞整条消息链，
 599         故统一加超时保护（超时后跳过该插件，继续下一个）。
 600         """
 601         try:
 602             cfg = (self.config or {}).get('framework', {})
 603             timeout = float(cfg.get('raw_handler_timeout', 10.0))
 604         except (TypeError, ValueError, AttributeError):
 605             return 10.0
 606         return timeout if timeout > 0 else None
 607 
 608     async def _dispatch_raw_message_handlers(self, data: dict, bot_name: str) -> bool:
 609         """
 610         按插件优先级分发原始消息事件（未提取纯文本、含全部消息段）
 611         任一 handler 返回 True 表示"已接管"（框架跳过该消息的后续处理，选择性使用）；
 612         返回 None/False 表示未处理，消息继续走正常流程。单个 handler 异常不影响其他。
 613         单个 handler 超时（默认 10s）同样只跳过该插件，不拖垮后续 handler。
 614         """
 615         if not self._raw_message_handlers:
 616             return False
 617         # 黑匣子：原始消息处理器评估（raw_handlers）—— 列出订阅了原始消息的插件
 618         _bb_record('raw_handlers', plugin='dispatch', fn='_dispatch_raw_message_handlers',
 619                    event_id=data.get('_bb_eid'),
```
