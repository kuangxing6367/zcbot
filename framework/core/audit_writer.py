"""操作审计批量写库器（audit_logs 表）

和运行日志的分工：

* 运行日志（``logger`` / log_broker）回答「系统发生了什么」，供排障看；
* 审计日志回答「**谁、在什么时候、对什么、做了什么、结果如何**」，供追责与合规查。

两者的关键差别是**必须落库、必须可查询、不能随日志轮转丢失**。Web 管理面的
操作（登录、改配置、加管理员、封禁 IP）早已写入 ``audit_logs``；这里补齐此前
完全没有留痕的那一半——**消息侧的操作**：

===================  ============================================
action               含义
===================  ============================================
command_exec         命令被真正执行（含哪个插件、哪个 handler）
command_denied       命中命令但权限不足，被拒绝
command_blocked      命中命令但用户在黑名单里，被拦截
message_unmatched    消息没命中任何命令/关键词（默认关闭，噪音大）
===================  ============================================

设计约束：

* **热路径零阻塞**：``record()`` 只做一次 deque.append + 截断，不做 IO、不 await；
  落库交给后台任务按 ``flush_interval`` 批量 INSERT。
* **审计不能拖垮业务**：队列满直接丢弃并计数，写库失败只告警，绝不向调用方抛。
* **脱敏**：消息正文按 ``record_message_text`` 决定是否入库，且统一截断；
  审计表不是聊天记录表。
"""
import asyncio
import json
import logging
import time
from collections import deque

logger = logging.getLogger('zcbot')

#: 单条 detail 里消息正文的截断长度（审计只留够定位的片段）
_MAX_TEXT = 200


class AuditWriter:
    """操作审计批量写库器（由 Framework 持有，生命周期与 stats_writer 一致）"""

    def __init__(self, framework, flush_interval: float = None):
        cfg = ((framework.config or {}).get('framework') or {}).get('audit') or {}
        self.framework = framework
        self.db = framework.db
        self.enabled = bool(cfg.get('enabled', True))
        self.command_events = bool(cfg.get('command_events', True))
        self.unmatched_events = bool(cfg.get('unmatched_events', False))
        self.record_text = bool(cfg.get('record_message_text', True))
        self.flush_interval = float(flush_interval or cfg.get('flush_interval', 5.0))
        self._max_queue = int(cfg.get('max_queue', 5000))

        self._pending = deque()
        self._dropped = 0
        self._written = 0
        self._last_warn = 0.0
        self._task = None

    # ── 生命周期 ──────────────────────────────────────────

    def start(self):
        """启动后台批量写库任务（需在事件循环内调用）"""
        if self._task is None and self.enabled:
            self._task = asyncio.create_task(self._run(), name='audit-writer')

    async def stop(self):
        """停止后台任务并做最后一次 flush（停机前的操作不能丢）"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f'审计写库任务停止异常: {e}')
            self._task = None
        try:
            await self._flush_once()
        except Exception as e:
            logger.warning(f'停机前审计收尾写入失败: {e}')

    # ── 记录入口（同步、非阻塞）────────────────────────────

    def record(self, action: str, actor: str = None, target_type: str = None,
               target_name: str = None, detail: dict = None,
               result: str = 'success', error: str = None,
               ip: str = None) -> None:
        """记录一条操作审计。

        :param action: 操作类型（见模块文档的常量表）
        :param actor: 操作者标识，消息侧用 ``qq:<user_id>`` / ``group:<group_id>``
        :param target_type: 操作对象类型（command / plugin / user / group）
        :param target_name: 操作对象名（命令 pattern、插件名等）
        :param detail: 附加信息（会被序列化为 JSON，正文按需截断）
        :param result: success / failure
        :param error: 失败原因（``audit_logs.error_message``）
        :param ip: 来源 IP（消息侧通常为空，Web 侧由调用方传）
        """
        if not self.enabled:
            return
        if len(self._pending) >= self._max_queue:
            self._dropped += 1
            # 丢弃计数只在幂次上打日志，避免队列打满时把日志也打满
            if self._dropped == 1 or self._dropped % 1000 == 0:
                logger.warning(f'审计队列已满，已丢弃 {self._dropped} 条（消息量过大）')
            return
        self._pending.append((
            None,                                   # admin_id：消息侧无 Web 管理员
            _clip(actor, 50),
            _clip(action, 50),
            _clip(target_type, 50),
            _clip(target_name, 200),
            _detail_json(detail),
            _clip(ip, 45),
            'failure' if result == 'failure' else 'success',
            error,
        ))

    # ── 消息侧便捷方法（router 直接调用）────────────────────

    def command_executed(self, ev, plugin: str, handler: str, pattern: str,
                         matched_by: str = '', message: str = '',
                         result: str = 'success', error: str = None) -> None:
        if not self.command_events:
            return
        self.record(
            'command_exec',
            actor=_actor_of(ev),
            target_type='command',
            target_name=pattern,
            detail=self._msg_detail(ev, {
                'plugin': plugin,
                'handler': handler,
                'matched_by': matched_by,
                'message': message,
            }),
            result=result,
            error=error,
        )

    def command_denied(self, ev, plugin: str, handler: str, pattern: str,
                       reason: str, require: str = '', message: str = '') -> None:
        if not self.command_events:
            return
        self.record(
            'command_denied',
            actor=_actor_of(ev),
            target_type='command',
            target_name=pattern,
            detail=self._msg_detail(ev, {
                'plugin': plugin,
                'handler': handler,
                'require': require,
                'role': getattr(ev, 'role', ''),
                'message': message,
            }),
            result='failure',
            error=reason,
        )

    def command_blocked(self, ev, plugin: str, handler: str, pattern: str,
                        message: str = '') -> None:
        if not self.command_events:
            return
        self.record(
            'command_blocked',
            actor=_actor_of(ev),
            target_type='command',
            target_name=pattern,
            detail=self._msg_detail(ev, {
                'plugin': plugin,
                'handler': handler,
                'message': message,
            }),
            result='failure',
            error='用户在黑名单中',
        )

    def message_unmatched(self, ev, message: str = '') -> None:
        if not self.unmatched_events:
            return
        self.record(
            'message_unmatched',
            actor=_actor_of(ev),
            target_type='message',
            detail=self._msg_detail(ev, {'message': message}),
            result='failure',
            error='未命中任何命令/关键词',
        )

    # ── 内部 ─────────────────────────────────────────────

    def _msg_detail(self, ev, extra: dict) -> dict:
        """消息侧的公共维度：平台、会话、身份；正文按配置决定是否保留"""
        detail = {
            'adapter': getattr(ev, 'adapter', '') or '',
            'bot': getattr(ev, 'bot_name', '') or '',
            'user_id': getattr(ev, 'user_id', None),
            'group_id': getattr(ev, 'group_id', None),
        }
        detail.update(extra)
        if not self.record_text:
            detail.pop('message', None)
        return {k: v for k, v in detail.items() if v not in ('', None)}

    async def _run(self):
        """后台循环：周期性 flush"""
        while True:
            try:
                await asyncio.sleep(self.flush_interval)
                await self._flush_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f'审计批量写库异常: {e}')
                await asyncio.sleep(1)

    async def _flush_once(self):
        if not self._pending:
            return
        rows = []
        while self._pending:
            try:
                rows.append(self._pending.popleft())
            except IndexError:
                break
        if not rows:
            return
        try:
            await self._run_in_db_thread(lambda: self._write(rows))
            self._written += len(rows)
        except Exception as e:
            self._warn_slow(f'审计写入失败（{len(rows)} 条）: {e!r}')

    async def _run_in_db_thread(self, func):
        ex = getattr(self.framework, '_db_executor', None)
        if ex is not None:
            return await asyncio.get_running_loop().run_in_executor(ex, func)
        return await asyncio.to_thread(func)

    def _write(self, rows: list):
        sql = ("INSERT INTO audit_logs (admin_id, admin_name, action, target_type, "
               "target_name, detail, ip_address, result, error_message) "
               "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)")
        try:
            with self.db.transaction():
                self.db.execute_many(sql, rows)
            return
        except Exception as e:
            # 整批失败（列缺失 / 单行超长）时逐行隔离：丢一条坏记录，
            # 不能让整批审计一起消失
            self._warn_slow(f'审计整批写入失败（{len(rows)} 条），转为逐行: {e!r}')
        ok = bad = 0
        for row in rows:
            try:
                with self.db.transaction():
                    self.db.execute_many(sql, [row])
                ok += 1
            except Exception:
                bad += 1
        if bad:
            self._warn_slow(f'审计逐行写入完成：成功 {ok} 行，丢弃 {bad} 行')

    def _warn_slow(self, msg: str):
        """写入异常按分钟限频告警：审计故障不能每次都刷屏"""
        now = time.monotonic()
        if now - self._last_warn < 60:
            logger.debug(msg)
            return
        self._last_warn = now
        logger.warning(msg)

    def stats(self) -> dict:
        return {'pending': len(self._pending), 'written': self._written,
                'dropped': self._dropped, 'enabled': self.enabled}


def _actor_of(ev) -> str:
    """消息侧操作者标识：群聊记 ``group:<gid>/user:<uid>``，私聊记 ``user:<uid>``"""
    uid = getattr(ev, 'user_id', None)
    gid = getattr(ev, 'group_id', None)
    if gid:
        return f'group:{gid}/user:{uid}'
    return f'user:{uid}'


def _clip(value, limit: int):
    if value is None:
        return None
    text = str(value)
    return text[:limit]


def _detail_json(detail) -> str | None:
    if not detail:
        return None
    safe = {}
    for k, v in detail.items():
        if isinstance(v, str):
            safe[k] = v[:_MAX_TEXT]
        else:
            safe[k] = v
    try:
        return json.dumps(safe, ensure_ascii=False)
    except Exception:
        return None
