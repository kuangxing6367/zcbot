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


def _l2_serialize(event) -> str:
    """L2 落盘序列化：剔除冗余 raw（完整原始 payload 副本，event 已有 message /
    sender / user_id 等结构化字段，raw 仅为镜像备份），减小落盘体积与磁盘 IO。

    不修改入参 event（L1/L3 内存事件仍需保留 raw，供 session 备份等消费），
    而是浅拷贝后剥离。回读侧（_pop_sqlite_batch / _FileL2Backend.pop_batch）
    会补 raw 占位，保持回读事件形态与内存一致。
    """
    if isinstance(event, dict) and 'raw' in event:
        ev = dict(event)
        del ev['raw']
        return json.dumps([ev, None], ensure_ascii=False, default=str)
    return json.dumps([event, None], ensure_ascii=False, default=str)


class _FileL2Backend:
    """自研 L2 溢出后端：append-only 日志文件，按行存取事件（不依赖 sqlite）。

    - 写入：一条事件一行 JSON（与 sqlite payload 同构），追加 + flush；
    - 读取：从 _read_offset 顺序读 n 行后推进偏移；读到文件尾即 truncate 复位
      （语义同 sqlite 取出即删）；读指针越过一半时压缩一次，避免头部空洞无限膨胀；
    - 内存记账：仅 _read_offset 与剩余行数两个整数，不缓存事件本体——O(1)，
      不为内存而内存（不把积压事件全量加载进内存）。
    """

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._fh = None
        self._read_offset = 0          # 已消费字节偏移
        self._rows = 0                 # 剩余未消费行数
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        try:
            self._fh = open(path, 'a+', encoding='utf-8', newline='')
            self._rows = self._count_lines()
        except Exception as e:
            logger.error(f"L2 文件缓冲初始化失败，回落 L2 不可用: {e}")
            self._fh = None
            self._rows = 0

    def _count_lines(self) -> int:
        """启动时清点遗留行数（仅初始化一次，非热路径）"""
        try:
            self._fh.seek(0)
            n = 0
            for line in self._fh:
                if line.strip():
                    n += 1
            self._fh.seek(0, os.SEEK_END)
            return n
        except Exception:
            return 0

    def _file_size(self) -> int:
        try:
            self._fh.flush()
            return os.path.getsize(self._path)
        except Exception:
            return 0

    def _compact(self):
        """把未读部分（read_offset..EOF）重写进临时文件并原子替换，归还已读空洞"""
        try:
            tmp = self._path + '.tmp'
            self._fh.flush()
            with open(tmp, 'w', encoding='utf-8', newline='') as out:
                self._fh.seek(self._read_offset)
                while True:
                    line = self._fh.readline()
                    if not line:
                        break
                    out.write(line)
            self._fh.close()
            os.replace(tmp, self._path)
            self._fh = open(self._path, 'a+', encoding='utf-8', newline='')
            self._read_offset = 0
        except Exception as e:
            logger.warning(f"L2 文件缓冲压缩失败（忽略，继续顺序读）: {e}")

    def write(self, event) -> bool:
        """线程内追加一条（独立文件句柄 + 锁，不阻塞主库）"""
        payload = _l2_serialize(event)
        try:
            with self._lock:
                if self._fh is None:
                    return False
                self._fh.write(payload + '\n')
                self._fh.flush()
                self._rows += 1
            return True
        except Exception as e:
            logger.warning(f"L2 文件缓冲写入失败: {e}")
            return False

    def pop_batch(self, n: int):
        """线程内批量取出（取出即删语义，同 sqlite 批量取回）。

        返回 (event, done, size) 三元组；size 用 payload 字节近似（仅水位记账用）。
        """
        try:
            with self._lock:
                if self._fh is None:
                    return []
                self._fh.seek(self._read_offset)
                items = []
                for _ in range(n):
                    line = self._fh.readline()
                    if not line:
                        break
                    line = line.rstrip('\n')
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        if isinstance(data, list) and len(data) == 2:
                            ev = data[0]
                            if isinstance(ev, dict) and 'raw' not in ev:
                                ev['raw'] = {}   # 回读补占位：保持事件形态与内存一致
                            items.append((ev, data[1], len(line) + 64))
                        else:
                            ev = data
                            if isinstance(ev, dict) and 'raw' not in ev:
                                ev['raw'] = {}
                            items.append((ev, None, len(line) + 64))
                    except Exception:
                        logger.warning("L2 文件缓冲中存在损坏行，已跳过")
                self._read_offset = self._fh.tell()
                if self._read_offset >= self._file_size():
                    # 已全部消化：截断复位，归还磁盘空间
                    self._fh.truncate(0)
                    self._fh.seek(0)
                    self._read_offset = 0
                elif self._read_offset > self._file_size() / 2:
                    self._compact()
                self._rows = max(0, self._rows - len(items))
            return items
        except Exception as e:
            logger.warning(f"L2 文件缓冲读取失败: {e}")
            return []

    def rows(self) -> int:
        return self._rows

    def close(self):
        try:
            if self._fh is not None:
                with self._lock:
                    self._fh.close()
                self._fh = None
        except Exception:
            pass


class EventBuffer:
    """三层事件缓冲（单消费者语义由外部 worker 保证）"""

    def __init__(self, cfg: dict, data_dir: str):
        buf_cfg = cfg.get('buffer', {}) or {}
        self.l1_max_bytes = int(buf_cfg.get('l1_max_bytes', 512 * 1024))
        self.l1_max_items = int(cfg.get('maxsize', 2000))
        # L2 溢出后端：'sqlite'（原持久化层）| 'file'（自研 append 文件）| 'off'（禁用）。
        # 显式给值则按值，未给时 sqlite_enabled=True 用 sqlite、否则自动用自研 file——
        # 保证 sqlite 关闭时 L3 满仍能回落 L2，而不是直接丢弃。
        self.l2_mode = buf_cfg.get('l2_backend')
        if self.l2_mode not in ('sqlite', 'file', 'off'):
            self.l2_mode = 'sqlite' if buf_cfg.get('sqlite_enabled', True) else 'file'
        self.sqlite_enabled = self.l2_mode == 'sqlite'
        self.sqlite_path = buf_cfg.get('sqlite_path') or os.path.join(data_dir, 'event_buffer.db')
        self.l2_path = buf_cfg.get('l2_path') or os.path.join(data_dir, 'event_buffer.l2.log')
        self.sqlite_write_timeout = float(buf_cfg.get('sqlite_write_timeout', 0.5))
        self.sqlite_batch = int(buf_cfg.get('sqlite_batch', 64))
        self.l3_max_bytes = int(buf_cfg.get('l3_max_bytes', 4 * 1024 * 1024))
        self.full_action = buf_cfg.get('full_action', 'warn_drop')

        self._l1 = asyncio.Queue(maxsize=self.l1_max_items)   # (event, done, size)
        self._l1_bytes = 0
        self._l3 = asyncio.Queue()                            # 无条数上限，靠字节计数兜底
        self._l3_bytes = 0
        self._wakeup = asyncio.Event()                        # 任一层入队信号（修溢出不唤醒）
        self._l2_pending = []                             # L2 批量取回、待 worker 处理
        self._dropped = 0
        self._overflow_to_sqlite = 0
        self._sqlite_conn = None
        self._sqlite_lock = threading.Lock()
        self._sqlite_fail_streak = 0                      # 连续写失败计数（自愈降级用）
        self._sqlite_fail_threshold = int(buf_cfg.get('sqlite_fail_threshold', 8))
        self._sqlite_rows = 0                             # sqlite 表内待消化行数（免热路径查库）
        self._l2_file = None                              # 自研 file 后端实例
        if self.l2_mode == 'sqlite':
            self._init_sqlite()
            self._sqlite_rows = self._count_rows()   # 承接上次进程遗留的持久化事件
        elif self.l2_mode == 'file':
            self._l2_file = _FileL2Backend(self.l2_path)

    # ---------- L2 统一入口（sqlite / 自研 file 一致语义） ----------
    def _l2_rows(self) -> int:
        """L2 待消化条数（免热路径查库）"""
        if self.l2_mode == 'sqlite':
            return self._sqlite_rows
        if self.l2_mode == 'file':
            return self._l2_file.rows() if self._l2_file else 0
        return 0

    async def _l2_write(self, event) -> bool:
        """写一条到 L2 溢出层（统一超时与失败计数语义）"""
        try:
            if self.l2_mode == 'sqlite':
                ok = await asyncio.wait_for(
                    asyncio.to_thread(self._write_sqlite, event),
                    timeout=self.sqlite_write_timeout)
            elif self.l2_mode == 'file':
                ok = await asyncio.wait_for(
                    asyncio.to_thread(self._l2_file.write, event),
                    timeout=self.sqlite_write_timeout)
            else:
                return False
            if ok:
                self._overflow_to_sqlite += 1
                return True
            return False
        except asyncio.TimeoutError:
            logger.warning(
                f"L2 缓冲写入超时（>{self.sqlite_write_timeout}s），转下一层")
            return False
        except Exception as e:
            logger.warning(f"L2 缓冲写入异常: {e}")
            return False

    def _l2_pop_batch(self, n: int):
        """从 L2 批量取回（取出即删语义）"""
        if self.l2_mode == 'sqlite':
            return self._pop_sqlite_batch(n)
        if self.l2_mode == 'file':
            return self._l2_file.pop_batch(n) if self._l2_file else []
        return []

    # ---------- 大小统计 ----------
    @staticmethod
    def _size_of(event) -> int:
        """事件字节估算（入队时算一次，随条目携带，出队复用——不再每事件两次全量序列化）。

        字节记账是软水位：±50% 误差不影响 512KB/4MB 预算的正确性，但要便宜且能抓住
        重尾（base64 图片 / 超长文本等超大字段）。

        旧实现用 len(repr(event))：C 层把整个 dict（含大段 base64 的 raw_message / message
        段数组）递归拼成一条长字符串——中小结构 C 层尚可，超大事件（图片等）被迫拷贝几百 KB
        字符串，直接拖垮入队热路径。

        现改为内联浅层估算：只遍历一次顶层字段；对 message 段数组只 peek 各段
        data.text 长度（不深递归、不拼字符串），对 sender 等只数字符串值。中小事件
        因免去 C 层字符串构建而更快；超大事件免去大块内存拷贝，提速 1~2 个数量级。
        命中 1MB 重尾立即截断，防止超大事件拖慢估算本身。
        """
        try:
            # 快路径：上游归一化已随事件携带预预算尺寸 → O(1) 字典查表。这是入队热路径
            # 在纯 Python 下可达的最优（单次哈希查表 ~几十 ns，已逼近 C 层量级）；无预存
            # 值（测试 fixture / 其它协议来源）则走下方浅层回退。不写回 event，避免改动
            # 事件内容与落盘语义。
            if isinstance(event, dict) and '_est_size' in event:
                v = event['_est_size']
                if isinstance(v, int) and v >= 0:
                    return v
            if not isinstance(event, dict):
                return len(repr(event)) + 64  # 非 dict（理论上不会走到）兜底
            total = 64  # 结构体固定开销
            raw = event.get('raw_message')
            if isinstance(raw, str):
                total += len(raw)
            msg = event.get('message')
            if isinstance(msg, (list, tuple)):
                # 段数组：逐段取 data 内所有字符串值（text/file/url……），抓长文本与
                # base64 大图（data.file）重尾；不递归嵌套、不拼字符串。
                for seg in msg:
                    if isinstance(seg, dict):
                        d = seg.get('data')
                        if isinstance(d, dict):
                            for dv in d.values():
                                if isinstance(dv, str):
                                    total += len(dv)
                                if total > (1 << 20):   # 抓到 1MB 重尾直接停
                                    break
                        else:
                            total += 24  # at/face 等非文本段的经验值
                    else:
                        total += 24
                    if total > (1 << 20):
                        break
            elif isinstance(msg, str):
                total += len(msg)            # 归一化事件把 message 直接存为字符串的情形
            elif isinstance(msg, dict):
                for sv in msg.values():      # 极少见的 dict 形态，只数字符串值
                    total += len(sv) if isinstance(sv, str) else 8
            else:
                total += 64                  # 未知形状的经验值，避免漏记
            for k, v in event.items():
                # 跳过 message（上方已精确计入）/ raw_message（无段数组协议的主内容，
                # 但 message 段已覆盖其正文）。raw 保留：仅数顶层字符串值（不深入 message
                # 段，避免与 message 双计），与 normalize_event._est_size 口径一致。
                if k in ('message', 'raw_message'):
                    continue
                total += len(str(k))
                if isinstance(v, str):
                    total += len(v)
                elif isinstance(v, dict):
                    # sender / raw 等嵌套 dict：只数字符串值，不深递归
                    for sv in v.values():
                        total += len(sv) if isinstance(sv, str) else 8
                else:
                    total += 8
            return total
        except Exception:
            return 4096  # 无法估算时按保守值记账

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
        payload = _l2_serialize(event)
        try:
            with self._sqlite_lock:
                self._sqlite_conn.execute(
                    "INSERT INTO event_buffer (payload, created_at) VALUES (?, ?)",
                    (payload, time.time()))
                self._sqlite_conn.commit()
            self._sqlite_rows += 1
            self._sqlite_fail_streak = 0                 # 写成功：重置连续失败计数
            return True
        except Exception as e:
            logger.warning(f"sqlite 缓冲写入失败: {e}")
            self._sqlite_fail_streak += 1
            if self._sqlite_fail_streak >= self._sqlite_fail_threshold:
                logger.error(
                    f"sqlite 缓冲连续失败 {self._sqlite_fail_streak} 次，"
                    f"自动禁用溢出层，事件改走内存兜底（重启后自动恢复重试）")
                self.sqlite_enabled = False
                try:
                    if self._sqlite_conn is not None:
                        self._sqlite_conn.close()
                except Exception:
                    pass
                self._sqlite_conn = None
            return False

    def _pop_sqlite_batch(self, n: int):
        """线程内批量取出（取出即删除，语义同内存队列出队）。

        返回 (event, done, size) 三元组；size 用持久化 payload 长度近似（仅水位记账用）。
        """
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
            self._sqlite_rows = max(0, self._sqlite_rows - len(rows))
            items = []
            for _, payload in rows:
                try:
                    data = json.loads(payload)
                    if isinstance(data, list) and len(data) == 2:
                        ev = data[0]
                        if isinstance(ev, dict) and 'raw' not in ev:
                            ev['raw'] = {}   # 回读补占位：保持事件形态与内存一致
                        items.append((ev, data[1], len(payload) + 64))
                    else:
                        ev = data
                        if isinstance(ev, dict) and 'raw' not in ev:
                            ev['raw'] = {}
                        items.append((ev, None, len(payload) + 64))
                except Exception:
                    logger.warning("sqlite 缓冲中存在损坏事件，已跳过")
            return items
        except Exception as e:
            logger.warning(f"sqlite 缓冲读取失败: {e}")
            return []

    def _count_rows(self) -> int:
        """启动时清点 sqlite 表内遗留行数（仅初始化调用一次，非热路径）"""
        if not self.sqlite_enabled or self._sqlite_conn is None:
            return 0
        try:
            with self._sqlite_lock:
                cur = self._sqlite_conn.execute("SELECT COUNT(*) FROM event_buffer")
                return int(cur.fetchone()[0])
        except Exception:
            return 0

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
            await self._l1.put((event, done, size))
            self._l1_bytes += size
            self._wakeup.set()
            return True
        # 单条超 L1 容量的超大事件（如 1MB 图片包）：L1 天然放不下，不浪费两次
        # sleep(0) 重试，直接优先 L3 内存兜底，其次才落 L2（自研 file / sqlite）——
        # 避免超大块每次都触发磁盘 IO（sqlite 单条 INSERT+commit 约 15ms/条）。
        if size > self.l1_max_bytes:
            if self._l3_bytes + size <= self.l3_max_bytes:
                self._l3.put_nowait((event, None, size))
                self._l3_bytes += size
                self._wakeup.set()
                return True
            # L3 满 → 回落 L2（内存 O(1) 的自研 file 或 sqlite），不直接丢弃
            if self.l2_mode != 'off':
                if await self._l2_write(event):
                    self._wakeup.set()          # L2 落盘也要唤醒阻塞中的消费者
                    return True
                logger.warning(
                    f"L3 满且 L2 写失败：size={size}B，事件改走丢弃")
            self._dropped += 1
            logger.error(
                f"超大事件丢弃：size={size}B 超 L1({self.l1_max_bytes}B)，"
                f"且 L3({self._l3_bytes}/{self.l3_max_bytes}B) 已满"
                f"{'、L2 不可用' if self.l2_mode == 'off' else ''}，累计丢弃 {self._dropped} 条")
            return False
        # L1 内存主缓冲（字节 + 条数双限）。突发注入（风暴 bench / 批量回调）不逐事件
        # 让出事件循环，L1 一满就会把本可留在内存的事件全压进 sqlite 磁盘层——
        # 所以满时先 sleep(0) 让消费者排空一次再重试，仍满才真正溢出
        for attempt in (0, 1):
            if self._l1_bytes + size <= self.l1_max_bytes:
                try:
                    self._l1.put_nowait((event, None, size))
                    self._l1_bytes += size
                    self._wakeup.set()
                    return True
                except asyncio.QueueFull:
                    pass
            if attempt == 0:
                await asyncio.sleep(0)
# L1 满 → L2 溢出层（自研 file / sqlite，短超时；写不过来转 L3 内存兜底）
        if self.l2_mode != 'off':
            if await self._l2_write(event):
                self._wakeup.set()          # L2 落盘也要唤醒阻塞中的消费者
                return True
            logger.warning(
                f"L2 缓冲写入超时/失败（>{self.sqlite_write_timeout}s），转入内存兜底")
        # L3 内存兜底（4MB）
        if self._l3_bytes + size <= self.l3_max_bytes:
            self._l3.put_nowait((event, None, size))
            self._l3_bytes += size
            self._wakeup.set()                  # 任一层入队都必须唤醒消费者
            return True
        # L3 满 → 再试一次回落 L2（上游瞬时抖动重试，避免直接丢弃）
        if self.l2_mode != 'off':
            if await self._l2_write(event):
                self._wakeup.set()
                return True
        # 全满 → 告警 + 丢弃（保老弃新）
        self._dropped += 1
        if self._dropped <= 3 or self._dropped % 100 == 1:
            logger.error(
                f"事件缓冲全满告警：L1={self._l1_bytes}/{self.l1_max_bytes}B "
                f"L3={self._l3_bytes}/{self.l3_max_bytes}B "
                f"L2={self._l2_rows()}条，已累计丢弃 {self._dropped} 条事件")
        return False

# ---------- 消费 ----------
    async def get_async(self):
        """取一条待处理事件，返回 (event, done, source)。

        优先级 L1 → L3 → sqlite。L1 为空时不会直接从 L2 逐条吐出，而是把 L2 批量
        取回并回填进 L1（字节 + 条数双限内尽量回填），再走 L1 正常路径消费——
        保证事件始终经内存热层消化，sqlite 只作为持久化暂存；回填放不下（超 L1
        上限的遗留大块）才直接取出。全空时阻塞等待「任一层入队」信号。
        """
        while True:
            try:
                event, done, size = self._l1.get_nowait()
                self._l1_bytes -= size
                return event, done, 'l1'
            except asyncio.QueueEmpty:
                pass
            try:
                event, done, size = self._l3.get_nowait()
                self._l3_bytes -= size
                return event, done, 'l3'
            except asyncio.QueueEmpty:
                pass
            # L2 → L1 回填：批量取回（取出即删），在 L1 剩余容量内回填；
            # 填不下的（单条超 L1 上限的历史大块）暂存 _l2_pending 依次直出。
            if self.l2_mode != 'off' and self._l2_rows() > 0:
                batch = await asyncio.to_thread(self._l2_pop_batch, self.sqlite_batch)
                if batch:
                    for item in batch:
                        _e, _d, _s = item
                        if self._l1_bytes + _s <= self.l1_max_bytes:
                            try:
                                self._l1.put_nowait(item)
                                self._l1_bytes += _s
                            except asyncio.QueueFull:
                                self._l2_pending.append(item)
                        else:
                            self._l2_pending.append(item)
                    self._wakeup.set()          # 回填 L1 也要唤醒消费者
                    continue                    # 回填后回到顶部走 L1 正常路径
            if self._l2_pending:
                item = self._l2_pending.pop(0)
                _src = 'sqlite' if self.l2_mode == 'sqlite' else 'l2'
                return item[0], item[1], _src
            # 全空：清信号后复查一遍（闭合「清信号与入队 set 之间」的丢失唤醒竞态），
            # 仍空才阻塞等信号
            self._wakeup.clear()
            if (not self._l1.empty() or not self._l3.empty()
                    or self._l2_pending or self._sqlite_rows > 0):
                continue
            await self._wakeup.wait()

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
                and not self._l2_pending and self._l2_rows() == 0)

    async def wait_drained(self, timeout: float) -> bool:
        """限时等待三层全部清空（供停机排空；返回是否排空完成）"""
        try:
            await asyncio.wait_for(self._l1.join(), timeout=timeout)
            await asyncio.wait_for(self._l3.join(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        deadline = time.monotonic() + timeout
        while self._l2_rows() > 0 or self._l2_pending:
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
        if self._l2_file is not None:
            self._l2_file.close()
            self._l2_file = None

    def stats(self) -> dict:
        return {
            'l1_items': self._l1.qsize(),
            'l1_bytes': self._l1_bytes,
            'l1_max_bytes': self.l1_max_bytes,
            'l3_items': self._l3.qsize(),
            'l3_bytes': self._l3_bytes,
            'l3_max_bytes': self.l3_max_bytes,
            'l2_mode': self.l2_mode,
            'l2_pending': self._l2_rows() + len(self._l2_pending),
            'sqlite_pending': self._sqlite_rows + len(self._l2_pending),
            'overflow_to_sqlite': self._overflow_to_sqlite,
            'dropped': self._dropped,
        }
