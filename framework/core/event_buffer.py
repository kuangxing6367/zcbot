# -*- coding: utf-8 -*-
"""事件分层缓冲：内存主队列(L1) → sqlite 持久化溢出(L2) → 内存兜底(L3) → 全满告警丢弃。

对应消息流入缓冲设想（总量约 4.5MB 内存预算 + sqlite 磁盘层）：
- L1  512KB 内存队列（文字消息为主）：消息流入先入此层直接处理；字节超限即溢出
- L2  sqlite 持久化缓冲（data/event_buffer.db，独立文件独立连接，不阻塞主库）：
      L1 堆积满时承接，防内存暴涨、防事件丢失
- L3  4MB 内存兜底队列：sqlite 写入不过来（超时/失败）时的应急暂存
- 全满  日志告警并丢弃新事件（保老弃新），丢弃计数可经 stats() 查看

消费优先级：L1（最新热数据）→ L3（内存兜底，及时释放）→ L2 sqlite（批量回取，
已持久化的历史积压最后消化）。wait=True 的同步语义事件保持阻塞进 L1，不参与溢出，
以保留 done future 契约（测试 / 终端同步）。
"""
import asyncio
import json
import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger('zcbot')


class EventBuffer:
    """三层事件缓冲（单消费者语义由外部 worker 保证）"""

    def __init__(self, cfg: dict, data_dir: str):
        buf_cfg = cfg.get('buffer', {}) or {}
        self.l1_max_bytes = int(buf_cfg.get('l1_max_bytes', 512 * 1024))
        self.l1_max_items = int(cfg.get('maxsize', 2000))
        self.sqlite_enabled = bool(buf_cfg.get('sqlite_enabled', True))
        self.sqlite_path = buf_cfg.get('sqlite_path') or os.path.join(data_dir, 'event_buffer.db')
        self.sqlite_write_timeout = float(buf_cfg.get('sqlite_write_timeout', 0.5))
        self.sqlite_batch = int(buf_cfg.get('sqlite_batch', 64))
        self.l3_max_bytes = int(buf_cfg.get('l3_max_bytes', 4 * 1024 * 1024))
        self.full_action = buf_cfg.get('full_action', 'warn_drop')

        self._l1 = asyncio.Queue(maxsize=self.l1_max_items)   # (event, done)
        self._l1_bytes = 0
        self._l3 = asyncio.Queue()                            # 无条数上限，靠字节计数兜底
        self._l3_bytes = 0
        self._sqlite_pending = []                             # sqlite 批量取回、待 worker 处理
        self._dropped = 0
        self._overflow_to_sqlite = 0
        self._sqlite_conn = None
        self._sqlite_lock = threading.Lock()
        if self.sqlite_enabled:
            self._init_sqlite()

    # ---------- 大小统计 ----------
    @staticmethod
    def _size_of(event) -> int:
        try:
            return len(json.dumps(event, ensure_ascii=False, default=str).encode('utf-8'))
        except Exception:
            return 256  # 无法序列化时按保守默认估算

    # ---------- sqlite 层 ----------
    def _init_sqlite(self):
        try:
            os.makedirs(os.path.dirname(self.sqlite_path) or '.', exist_ok=True)
            self._sqlite_conn = sqlite3.connect(self.sqlite_path, check_same_thread=False)
            with self._sqlite_lock:
                self._sqlite_conn.execute(
                    "CREATE TABLE IF NOT EXISTS event_buffer ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " payload TEXT NOT NULL,"
                    " created_at REAL NOT NULL)")
                self._sqlite_conn.commit()
        except Exception as e:
            logger.error(f"事件 sqlite 缓冲初始化失败，禁用溢出层: {e}")
            self.sqlite_enabled = False

    def _write_sqlite(self, event) -> bool:
        """线程内写入一条（独立文件独立连接，不阻塞主库）"""
        payload = json.dumps([event, None], ensure_ascii=False, default=str)
        try:
            with self._sqlite_lock:
                self._sqlite_conn.execute(
                    "INSERT INTO event_buffer (payload, created_at) VALUES (?, ?)",
                    (payload, time.time()))
                self._sqlite_conn.commit()
            return True
        except Exception as e:
            logger.warning(f"sqlite 缓冲写入失败: {e}")
            return False

    def _pop_sqlite_batch(self, n: int):
        """线程内批量取出（取出即删除，语义同内存队列出队）"""
        try:
            with self._sqlite_lock:
                cur = self._sqlite_conn.execute(
                    "SELECT id, payload FROM event_buffer ORDER BY id ASC LIMIT ?", (n,))
                rows = cur.fetchall()
                if not rows:
                    return []
                ids = [r[0] for r in rows]
                self._sqlite_conn.executemany(
                    "DELETE FROM event_buffer WHERE id = ?", [(i,) for i in ids])
                self._sqlite_conn.commit()
            items = []
            for _, payload in rows:
                try:
                    data = json.loads(payload)
                    if isinstance(data, list) and len(data) == 2:
                        items.append((data[0], data[1]))  # (event, done=None)
                    else:
                        items.append((data, None))
                except Exception:
                    logger.warning("sqlite 缓冲中存在损坏事件，已跳过")
            return items
        except Exception as e:
            logger.warning(f"sqlite 缓冲读取失败: {e}")
            return []

    def sqlite_count(self) -> int:
        if not self.sqlite_enabled or self._sqlite_conn is None:
            return 0
        try:
            with self._sqlite_lock:
                cur = self._sqlite_conn.execute("SELECT COUNT(*) FROM event_buffer")
                return int(cur.fetchone()[0])
        except Exception:
            return 0

    # ---------- 入队 ----------
    async def put(self, event, done=None) -> bool:
        """事件入队。返回 True=已承接；False=全满丢弃（已告警）。

        done 非空（wait=True 同步语义）时阻塞进 L1，不参与溢出，保留背压契约。
        """
        size = self._size_of(event)
        if done is not None:
            await self._l1.put((event, done))
            self._l1_bytes += size
            return True
        # L1 内存主缓冲（字节 + 条数双限）
        if self._l1_bytes + size <= self.l1_max_bytes:
            try:
                self._l1.put_nowait((event, None))
                self._l1_bytes += size
                return True
            except asyncio.QueueFull:
                pass
        # L1 满 → sqlite 持久化溢出层（短超时；写不过来转 L3 内存兜底）
        if self.sqlite_enabled:
            try:
                ok = await asyncio.wait_for(
                    asyncio.to_thread(self._write_sqlite, event),
                    timeout=self.sqlite_write_timeout)
                if ok:
                    self._overflow_to_sqlite += 1
                    return True
            except asyncio.TimeoutError:
                logger.warning(
                    f"sqlite 缓冲写入超时（>{self.sqlite_write_timeout}s），转入内存兜底")
            except Exception as e:
                logger.warning(f"sqlite 缓冲写入异常: {e}")
        # L3 内存兜底（4MB）
        if self._l3_bytes + size <= self.l3_max_bytes:
            self._l3.put_nowait((event, None))
            self._l3_bytes += size
            return True
        # 全满 → 告警 + 丢弃（保老弃新）
        self._dropped += 1
        if self._dropped <= 3 or self._dropped % 100 == 1:
            logger.error(
                f"事件缓冲全满告警：L1={self._l1_bytes}/{self.l1_max_bytes}B "
                f"L3={self._l3_bytes}/{self.l3_max_bytes}B "
                f"sqlite={self.sqlite_count()}条，已累计丢弃 {self._dropped} 条事件")
        return False

    # ---------- 消费 ----------
    async def get_async(self):
        """取一条待处理事件，返回 (event, done, source)。

        优先级 L1 → L3 → sqlite（批量回取）；全空时阻塞等 L1（停机时由 worker cancel 中断）。
        """
        try:
            item = self._l1.get_nowait()
            self._l1_bytes -= self._size_of(item[0])
            return item[0], item[1], 'l1'
        except asyncio.QueueEmpty:
            pass
        try:
            item = self._l3.get_nowait()
            self._l3_bytes -= self._size_of(item[0])
            return item[0], item[1], 'l3'
        except asyncio.QueueEmpty:
            pass
        if self._sqlite_pending:
            item = self._sqlite_pending.pop(0)
            return item[0], item[1], 'sqlite'
        if self.sqlite_enabled:
            batch = await asyncio.to_thread(self._pop_sqlite_batch, self.sqlite_batch)
            if batch:
                self._sqlite_pending = batch
                item = self._sqlite_pending.pop(0)
                return item[0], item[1], 'sqlite'
        item = await self._l1.get()
        self._l1_bytes -= self._size_of(item[0])
        return item[0], item[1], 'l1'

    def task_done(self, source: str):
        """worker 处理完一条后回执（join_memory 依赖 L1/L3 的计数）"""
        if source == 'l1':
            self._l1.task_done()
        elif source == 'l3':
            self._l3.task_done()
        # sqlite 层取出即删，无需计数

    # ---------- 停机 / 统计 ----------
    def empty_all(self) -> bool:
        return (self._l1.empty() and self._l3.empty()
                and not self._sqlite_pending and self.sqlite_count() == 0)

    async def wait_drained(self, timeout: float) -> bool:
        """限时等待三层全部清空（供停机排空；返回是否排空完成）"""
        try:
            await asyncio.wait_for(self._l1.join(), timeout=timeout)
            await asyncio.wait_for(self._l3.join(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        deadline = time.monotonic() + timeout
        while self.sqlite_count() > 0 or self._sqlite_pending:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    def close(self):
        if self._sqlite_conn is not None:
            try:
                with self._sqlite_lock:
                    self._sqlite_conn.close()
            except Exception:
                pass
            self._sqlite_conn = None

    def stats(self) -> dict:
        return {
            'l1_items': self._l1.qsize(),
            'l1_bytes': self._l1_bytes,
            'l1_max_bytes': self.l1_max_bytes,
            'l3_items': self._l3.qsize(),
            'l3_bytes': self._l3_bytes,
            'l3_max_bytes': self.l3_max_bytes,
            'sqlite_pending': self.sqlite_count(),
            'overflow_to_sqlite': self._overflow_to_sqlite,
            'dropped': self._dropped,
        }
