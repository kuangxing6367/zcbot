"""
消息统计批量写库器（自 framework/core/base.py 剥离）

框架自身的用户/群自动注册、命令命中计数等写入，统一走此队列，
由后台任务周期性批量落库（在线程中执行），不阻塞事件循环。
"""
import asyncio
import logging

from framework.messaging.adapter_registry import namespace_identity

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

    def register_user(self, user_id: int, sender: dict, message_type: str,
                      group_id: int | None = None, adapter_key: str = None):
        """排队用户/群自动注册（非阻塞，队列满时丢弃并计数）

        :param adapter_key: 事件来源适配器连接标识（onebot/qq_official/…），
            用于多平台接入身份命名空间（multi_platform.namespace 开启时生效）
        """
        # 身份键要当 dict key 和 DB 参数用：畸形/攻击型输入（dict/list 等）必须
        # 在入队前收敛，否则 _aggregate_registrations 会以 unhashable type 抛错，
        # 让整批统计写库（含正常用户的数据）一起失败。
        if not isinstance(user_id, (int, str)):
            logger.debug("忽略非法 user_id 类型: %s", type(user_id).__name__)
            return
        # 超大整数（如攻击者构造 2**200）虽是合法 int，但超出 SQL BIGINT 范围，
        # executemany 会抛 OverflowError，使同一批次里所有正常用户的注册一起失败。
        # 与上面的类型收敛同理：坏身份键必须在入队前挡掉，不能带进批量写库。
        if isinstance(user_id, int) and not (-(1 << 63) <= user_id <= (1 << 63) - 1):
            logger.debug("忽略超出 BIGINT 范围的 user_id（%d 位）", len(str(abs(user_id))))
            return
        if group_id is not None and not isinstance(group_id, (int, str)):
            group_id = None
        if isinstance(group_id, int) and not (-(1 << 63) <= group_id <= (1 << 63) - 1):
            logger.debug("忽略超出 BIGINT 范围的 group_id")
            group_id = None
        try:
            self._reg_queue.put_nowait(
                (user_id, dict(sender), message_type, group_id, adapter_key))
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 1000 == 1:
                logger.warning(f"用户注册队列已满，已丢弃 {self._dropped} 条注册请求（消息量过大）")

    async def _run(self):
        """后台循环：周期性 flush。

        各落库目标（命中计数 / users / groups / group_members）互不依赖，
        各自封装为闭包并行提交到 DB 线程池（MySQL 下真实并行；SQLite 内核
        单写者仍串行落盘，但省去三次往返的串行等待），任一失败不影响其余。
        """
        while True:
            try:
                await asyncio.sleep(self.flush_interval)
                fns = self._drain_to_fns()
                if not fns:
                    continue
                results = await asyncio.gather(
                    *[self._run_in_db_thread(fn) for fn in fns],
                    return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        logger.error(f"统计批量写库异常: {r}")
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

    def _drain_to_fns(self) -> list:
        """把内存聚合数据排空为一组互不依赖的落库闭包（供 DB 线程池并行执行）"""
        fns = []
        hits = self._cmd_hits
        self._cmd_hits = {}
        kwhits = self._kw_hits
        self._kw_hits = {}
        if hits or kwhits:
            fns.append(lambda: self._flush_hits(hits, kwhits))
        items = []
        while True:
            try:
                items.append(self._reg_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if items:
            users, groups, members = self._aggregate_registrations(items)
            if users:
                fns.append(lambda u=users: self._write_users(u))
            if groups:
                fns.append(lambda g=groups: self._write_groups(g))
            if members:
                fns.append(lambda m=members: self._write_members(m))
        return fns

    def _namespace_id(self, raw_id, adapter_key):
        """按 multi_platform.namespace 开关把裸身份改写成命名空间键（关闭时原样返回）。"""
        fw = self.framework
        try:
            enabled = fw.multi_platform_namespace()
        except Exception as _exc:
            logger.debug('[_namespace_id] 捕获异常后按降级路径继续: %s', _exc)
            enabled = False
        if not enabled:
            return raw_id
        reg = getattr(fw, 'adapter_registry', None)
        return namespace_identity(reg, adapter_key, True, raw_id)

    def _flush(self):
        """同步排空（DB 线程中执行，停机收尾 / 兜底用）"""
        for fn in self._drain_to_fns():
            try:
                fn()
            except Exception as e:
                logger.error(f"统计批量写库异常: {e}")

    def _flush_hits(self, hits: dict, kwhits: dict):
        """命令/关键词命中计数批量自增（executemany 一次往返替代逐条 UPDATE）"""
        if hits:
            try:
                self.db.execute_many(
                    "UPDATE commands SET hit_count = hit_count + %s WHERE id = %s",
                    [(cnt, cmd_id) for cmd_id, cnt in hits.items()]
                )
            except Exception as e:
                logger.error(f"命令命中计数写库失败: {e}")
        if kwhits:
            try:
                self.db.execute_many(
                    "UPDATE dynamic_commands SET hit_count = hit_count + %s WHERE id = %s",
                    [(cnt, kw_id) for kw_id, cnt in kwhits.items()]
                )
            except Exception as e:
                logger.error(f"关键词命中计数写库失败: {e}")

    def _aggregate_registrations(self, items):
        """把注册请求按 (user / group / member) 聚合（同键合并，消息数累加）

        多平台接入：namespace 开启时，user/group 身份键改写为 `<适配器uuid>@<id>`
        （由 adapter_key 关联 drive 表的稳定 uuid），不同适配器下同一 QQ 号成为不同行。
        """
        users = {}                          # namespace_user_id -> nickname（最后状态）
        groups = {}                         # namespace_group_id -> group_name（最后状态）
        members = {}                        # (namespace_group_id, namespace_user_id) -> [...]
        for user_id, sender, message_type, group_id, adapter_key in items:
            if not user_id:
                continue
            nuid = self._namespace_id(user_id, adapter_key)
            nickname = sender.get('nickname', '') or sender.get('card', '') or str(user_id)
            users[nuid] = nickname
            if group_id and message_type == 'group':
                ngid = self._namespace_id(group_id, adapter_key)
                groups[ngid] = sender.get('group_name', '')
                key = (ngid, nuid)
                m = members.get(key)
                if m is None:
                    members[key] = [sender.get('card', ''),
                                    sender.get('role', 'member'),
                                    sender.get('title', ''), 1]
                else:
                    # 同批多条消息：保留最后 card/role/title，消息数累加
                    if sender.get('card', '') != '':
                        m[0] = sender.get('card', '')
                    if sender.get('role', '') != '':
                        m[1] = sender.get('role', '')
                    if sender.get('title', '') != '':
                        m[2] = sender.get('title', '')
                    m[3] += 1
        return users, groups, members

    def _execute_many_resilient(self, sql: str, rows: list, label: str):
        """批量写入；整批失败时逐行隔离，坏行不再拖垮整批。

        旧实现是单事务 executemany：一个 user_id 超 BIGINT、一个 nickname
        含非法编码，整批几百行全部回滚（实测 492 个用户注册全丢）。
        正常路径仍是一次事务，只有失败才退化为逐行，代价可控。
        """
        if not rows:
            return
        try:
            with self.db.transaction():
                self.db.execute_many(sql, rows)
            return
        except Exception as e:
            logger.warning(f"{label} 整批写入失败（{len(rows)} 行），转为逐行隔离: {e!r}")

        ok = bad = 0
        first_error = None
        for row in rows:
            try:
                with self.db.transaction():
                    self.db.execute_many(sql, [row])
                ok += 1
            except Exception as re:
                bad += 1
                if first_error is None:
                    first_error = repr(re)
                # 连续全失败基本可判定是数据库不可用，别把每行都试一遍
                if bad >= 20 and ok == 0:
                    logger.error(f"{label} 逐行写入连续 {bad} 行失败，疑似数据库不可用，中止本批")
                    break
        if bad:
            logger.error(f"{label} 逐行写入完成：成功 {ok} 行，丢弃 {bad} 行，首个错误 {first_error}")
        else:
            logger.info(f"{label} 逐行写入完成：{ok} 行全部落库")

    def _write_users(self, users: dict):
        """用户自动注册/活跃更新（批量 + 坏行隔离）"""
        self._execute_many_resilient(
            "INSERT INTO users (user_id, nickname, first_seen_at, last_active_at) "
            "VALUES (%s, %s, NOW(), NOW()) "
            "ON DUPLICATE KEY UPDATE "
            "nickname = IF(VALUES(nickname) != '', VALUES(nickname), nickname), "
            "last_active_at = NOW()",
            [(uid, nick) for uid, nick in users.items()],
            f"用户注册（{len(users)} 用户）",
        )

    def _write_groups(self, groups: dict):
        """群信息自动注册（批量 + 坏行隔离）"""
        self._execute_many_resilient(
            "INSERT INTO groups_info (group_id, group_name, is_active, join_at) "
            "VALUES (%s, %s, 1, NOW()) "
            "ON DUPLICATE KEY UPDATE "
            "is_active = 1, "
            "group_name = IF(VALUES(group_name) != '', VALUES(group_name), group_name)",
            [(gid, gname) for gid, gname in groups.items()],
            f"群注册（{len(groups)} 群）",
        )

    def _write_members(self, members: dict):
        """群成员关系自动注册/活跃计数更新（批量 + 坏行隔离）"""
        self._execute_many_resilient(
            "INSERT INTO group_members (group_id, user_id, card, role, title, "
            "last_active_at, message_count) "
            "VALUES (%s, %s, %s, %s, %s, NOW(), %s) "
            "ON DUPLICATE KEY UPDATE "
            "card = IF(VALUES(card) != '', VALUES(card), card), "
            "role = VALUES(role), "
            "title = IF(VALUES(title) != '', VALUES(title), title), "
            "last_active_at = NOW(), "
            "message_count = message_count + VALUES(message_count)",
            [(gid, uid, m[0], m[1], m[2], m[3])
             for (gid, uid), m in members.items()],
            f"群成员注册（{len(members)} 成员）",
        )

    async def stop(self):
        """停止并执行最后一次落库"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception) as _exc:
                logger.debug('[stop] 捕获异常后按降级路径继续: %s', _exc)
            self._task = None
        await asyncio.to_thread(self._flush)
