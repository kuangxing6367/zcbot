"""
消息统计批量写库器（自 framework/core/base.py 剥离）

框架自身的用户/群自动注册、命令命中计数等写入，统一走此队列，
由后台任务周期性批量落库（在线程中执行），不阻塞事件循环。
"""
import asyncio
import logging

logger = logging.getLogger('zcbot')


class AsyncStatsWriter:
    """
    消息统计批量写库器
    框架自身的用户/群自动注册、命令命中计数等写入，统一走此队列，
    由后台任务周期性批量落库（在线程中执行），不阻塞事件循环。
    """

    def __init__(self, framework, flush_interval: float = 5.0):
        self.framework = framework
        self.db = framework.db
        self.flush_interval = flush_interval
        # 有界队列：消息洪峰时丢弃多余注册请求（内存有上限），丢弃计数定期上报
        self._reg_queue = asyncio.Queue(maxsize=20000)   # 用户/群注册任务
        self._dropped = 0
        self._cmd_hits = {}                 # cmd_id -> count（主循环线程访问）
        self._kw_hits = {}                  # dynamic_commands.id -> count
        self._task = None

    def start(self):
        """启动后台批量写库任务（需在事件循环内调用）"""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="stats-writer")

    def command_hit(self, cmd_id: int):
        """记录命令命中（内存聚合）"""
        self._cmd_hits[cmd_id] = self._cmd_hits.get(cmd_id, 0) + 1

    def keyword_hit(self, kw_id: int):
        """记录关键词自动回复命中（内存聚合）"""
        self._kw_hits[kw_id] = self._kw_hits.get(kw_id, 0) + 1

    def register_user(self, user_id: int, sender: dict, message_type: str, group_id: int = None):
        """排队用户/群自动注册（非阻塞，队列满时丢弃并计数）"""
        try:
            self._reg_queue.put_nowait((user_id, dict(sender), message_type, group_id))
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 1000 == 1:
                logger.warning(f"用户注册队列已满，已丢弃 {self._dropped} 条注册请求（消息量过大）")

    async def _run(self):
        """后台循环：周期性 flush"""
        while True:
            try:
                await asyncio.sleep(self.flush_interval)
                await self._run_in_db_thread(self._flush)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"统计批量写库异常: {e}")
                await asyncio.sleep(1)

    async def _run_in_db_thread(self, func):
        """在数据库专用线程池中执行（与默认线程池隔离，DB 阻塞不影响消息处理）"""
        ex = getattr(self.framework, '_db_executor', None)
        if ex is not None:
            return await asyncio.get_running_loop().run_in_executor(ex, func)
        return await asyncio.to_thread(func)

    def _flush(self):
        """批量落库（在线程中执行）"""
        # 1. 命令命中计数
        hits = self._cmd_hits
        self._cmd_hits = {}
        if hits:
            for cmd_id, cnt in hits.items():
                try:
                    self.db.execute(
                        "UPDATE commands SET hit_count = hit_count + %s WHERE id = %s",
                        (cnt, cmd_id)
                    )
                except Exception as e:
                    logger.error(f"命令命中计数写库失败 [{cmd_id}]: {e}")

        # 1.5 关键词自动回复命中计数（dynamic_commands 表）
        kwhits = self._kw_hits
        self._kw_hits = {}
        if kwhits:
            for kw_id, cnt in kwhits.items():
                try:
                    self.db.execute(
                        "UPDATE dynamic_commands SET hit_count = hit_count + %s WHERE id = %s",
                        (cnt, kw_id)
                    )
                except Exception as e:
                    logger.error(f"关键词命中计数写库失败 [{kw_id}]: {e}")

        # 2. 用户/群注册
        items = []
        while True:
            try:
                items.append(self._reg_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        for item in items:
            try:
                self._register_one(*item)
            except Exception as e:
                logger.error(f"自动注册用户失败: {e}")

    def _register_one(self, user_id: int, sender: dict, message_type: str, group_id: int):
        """单条用户/群自动注册"""
        if not user_id:
            return
        nickname = sender.get('nickname', '') or sender.get('card', '') or str(user_id)
        card = sender.get('card', '')

        # INSERT ... ON DUPLICATE KEY UPDATE 实现自动注册+更新
        # （SQLite 模式下由 db 方言层自动翻译为 ON CONFLICT + CASE WHEN）
        self.db.execute(
            "INSERT INTO users (user_id, nickname, first_seen_at, last_active_at) "
            "VALUES (%s, %s, NOW(), NOW()) "
            "ON DUPLICATE KEY UPDATE "
            "nickname = IF(VALUES(nickname) != '', VALUES(nickname), nickname), "
            "last_active_at = NOW()",
            (user_id, nickname)
        )

        # 如果是群消息，自动注册群信息和群成员关系
        if group_id and message_type == 'group':
            group_name = sender.get('group_name', '')

            # 自动注册群
            self.db.execute(
                "INSERT INTO groups_info (group_id, group_name, is_active, join_at) "
                "VALUES (%s, %s, 1, NOW()) "
                "ON DUPLICATE KEY UPDATE "
                "is_active = 1, "
                "group_name = IF(VALUES(group_name) != '', VALUES(group_name), group_name)",
                (group_id, group_name)
            )

            # 自动注册群成员关系
            role = sender.get('role', 'member')
            title = sender.get('title', '')
            self.db.execute(
                "INSERT INTO group_members (group_id, user_id, card, role, title, last_active_at, message_count) "
                "VALUES (%s, %s, %s, %s, %s, NOW(), 1) "
                "ON DUPLICATE KEY UPDATE "
                "card = IF(VALUES(card) != '', VALUES(card), card), "
                "role = VALUES(role), "
                "title = IF(VALUES(title) != '', VALUES(title), title), "
                "last_active_at = NOW(), "
                "message_count = message_count + 1",
                (group_id, user_id, card, role, title)
            )

    async def stop(self):
        """停止并执行最后一次落库"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await asyncio.to_thread(self._flush)
