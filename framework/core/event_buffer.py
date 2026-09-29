# -*- coding: utf-8 -*-
"""事件分层缓冲：内存主队列(L1) → 写缓冲(L4) → sqlite 持久化溢出(L2) → 内存兜底(L3) → 全满告警丢弃。

对应消息流入缓冲设想（总量约 8.5MB 内存预算 + sqlite 磁盘层）：
- L1  512KB 内存队列（文字消息为主）：消息流入先入此层直接处理；字节超限即溢出
- L4  4MB 写缓冲（L1 与 L2 之间的批量写聚合层）：L1 满时小包先进 L4 攒批，后台任务
      周期 / 阈值触发后 executemany 一次性落 L2，把「逐条 INSERT+commit」降为
      「攒一批一次 commit」，缓解 sqlite 单写者瓶颈。大包（size > l4_max_bytes，装不下
      攒批）绕过 L4 直写 L2；L4 满（缓不过来）直接塞 L2
- L2  sqlite 持久化缓冲（data/event_buffer.db，独立文件独立连接，不阻塞主库）：
      L4 批量承接 / 大包直写，防内存暴涨、防事件丢失
- L3  4MB 内存兜底队列：L2 写入不过来（超时/失败）时的应急暂存
- 全满  日志告警并丢弃新事件（保老弃新），丢弃计数可经 stats() 查看

消费优先级：L1（最新热数据）→ L3（内存兜底）→ L4（写缓冲，未及 flush 时消费者直接取走，
不丢事件）→ L2 sqlite（flush 落盘后的历史积压最后回取）。L4 常态由后台 flush 任务批量落 L2
（攒批 commit 缓解 sqlite 写压力），消费者空闲或单测未启动 flush 时也可直接取走 L4 中事件。
wait=True 的同步语义事件保持阻塞进 L1，不参与溢出，以保留 done future 契约（测试 / 终端同步）。
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

    def write_batch(self, events: list) -> bool:
        """线程内批量追加多条（一次 flush，L4 flush 路径用）"""
        if not events:
            return True
        try:
            lines = []
            for ev, _ in events:
                lines.append(_l2_serialize(ev) + '\n')
            with self._lock:
                if self._fh is None:
                    return False
                self._fh.write(''.join(lines))
                self._fh.flush()
                self._rows += len(lines)
            return True
        except Exception as e:
            logger.warning(f"L2 文件缓冲批量写入失败: {e}")
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
        # L4 写缓冲（4MB）：L1 与 L2 之间的批量写聚合层。L1 满时小包先进 L4 攒批，
        # 后台 flush 任务周期 / 阈值触发后 executemany 一次性落 L2，把「逐条 commit」
        # 降为「攒一批一次 commit」，缓解 sqlite 单写者瓶颈。
        #   大包(size > l4_max_bytes，攒批无意义) 绕过 L4 直写 L2；
        #   L4 满（缓不过来）直接塞 L2；
        #   L2 写失败 / 超时 再回落 L3 兜底（见 _l4_do_flush）。
        self.l4_max_bytes = int(buf_cfg.get('l4_max_bytes', 4 * 1024 * 1024))
        self.l4_flush_bytes = int(buf_cfg.get('l4_flush_bytes', max(1, int(self.l4_max_bytes * 0.5))))
        self.l4_flush_items = int(buf_cfg.get('l4_flush_items', 256))
        self.l4_max_idle = float(buf_cfg.get('l4_max_idle', 0.1))   # 最长空闲（秒）也触发 flush，保低延迟
        self._l4 = []                                  # 攒批列表 [(event, size), ...]
        self._l4_bytes = 0
        self._l4_lock = threading.Lock()
        self._l4_task = None
        self._l4_stop = asyncio.Event()
        self._l4_flushing = False
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

    async def _l2_write_batch(self, events: list) -> bool:
        """批量写多条到 L2 溢出层（统一超时与失败计数语义，L4 flush 路径用）"""
        if not events:
            return True
        try:
            if self.l2_mode == 'sqlite':
                ok = await asyncio.wait_for(
                    asyncio.to_thread(self._write_sqlite_batch, events),
                    timeout=self.sqlite_write_timeout)
            elif self.l2_mode == 'file':
                ok = await asyncio.wait_for(
                    asyncio.to_thread(self._l2_file.write_batch, events),
                    timeout=self.sqlite_write_timeout)
            else:
                return False
            if ok:
                self._overflow_to_sqlite += len(events)
                return True
            return False
        except asyncio.TimeoutError:
            logger.warning(
                f"L2 缓冲批量写入超时（>{self.sqlite_write_timeout}s），转下一层")
            return False
        except Exception as e:
            logger.warning(f"L2 缓冲批量写入异常: {e}")
            return False

    def _l4_append(self, event, size: int) -> bool:
        """入队一条到 L4 攒批缓冲（线程安全）。L4 装不下返回 False（调用方改直写 L2）。"""
        with self._l4_lock:
            if self._l4_bytes + size > self.l4_max_bytes:
                return False
            self._l4.append((event, size))
            self._l4_bytes += size
            return True

    async def _l4_do_flush(self) -> int:
        """把 L4 攒批内容一次性批量落 L2（executemany 一次 commit）。

        返回本次 flush 的条数；L2 写失败则回落 L3 兜底，L3 满则丢弃（告警）。
        """
        with self._l4_lock:
            if not self._l4:
                return 0
            batch = self._l4
            self._l4 = []
            self._l4_bytes = 0
        if await self._l2_write_batch(batch):
            self._wakeup.set()          # 落 L2 也要唤醒消费者取回
            return len(batch)
        # L2 写失败 → 回落 L3 兜底（内存），L3 也满才丢弃（压力给适配器）
        spilled = 0
        for ev, size in batch:
            if self._l3_bytes + size <= self.l3_max_bytes:
                self._l3.put_nowait((ev, None, size))
                self._l3_bytes += size
                spilled += 1
            else:
                self._dropped += 1
                if self._dropped <= 3 or self._dropped % 100 == 1:
                    logger.error(
                        f"L4 flush 回落 L3 仍满，事件丢弃："
                        f"L3({self._l3_bytes}/{self.l3_max_bytes}B) "
                        f"已累计丢弃 {self._dropped} 条")
        if spilled:
            self._wakeup.set()
        return 0

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
    def _fallback_to_file(self, reason: str):
        """sqlite 后端不可用/持续失败时自动降级到自研 file 后端。

        降级只发生在 sqlite 路径异常（初始化失败或连续写失败达阈值），
        保证任何环境下（含 debug 模式、只读磁盘、sqlite 驱动异常）L2 溢出层
        仍可用，而不是静默失效让事件直接冲进 L3/丢弃。
        降级前尽力把 sqlite 中尚未消费的遗留行迁移到 file，避免丢事件；
        迁移失败只告警、不阻塞切换。
        """
        if self.l2_mode != 'sqlite':
            return
        migrated = 0
        bak = None
        # 1) 尽力迁移 sqlite 遗留行到 file 后端（取出即删语义由 file 继承）
        migrated_ids = []
        try:
            if self._sqlite_conn is not None:
                with self._sqlite_lock:
                    cur = self._sqlite_conn.execute(
                        "SELECT id, payload FROM event_buffer ORDER BY id ASC")
                    rows = cur.fetchall()
                if rows:
                    bak = _FileL2Backend(self.l2_path)
                    for row_id, payload in rows:
                        try:
                            data = json.loads(payload) if isinstance(payload, str) else payload
                            ev = data[0] if (isinstance(data, list) and len(data) == 2) else data
                            if isinstance(ev, dict) and 'raw' not in ev:
                                ev = dict(ev)
                                ev['raw'] = {}   # 回读补占位：保持事件形态与内存一致
                            if bak.write(ev):
                                migrated += 1
                                migrated_ids.append(row_id)
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"L2 sqlite→file 降级迁移异常（忽略，继续降级）: {e}")
        # 1.5) 删除已迁移成功的行，避免 sqlite 恢复后与 file 重复消费
        if migrated_ids:
            try:
                with self._sqlite_lock:
                    self._sqlite_conn.executemany(
                        "DELETE FROM event_buffer WHERE id = ?",
                        [(rid,) for rid in migrated_ids])
                    self._sqlite_conn.commit()
            except Exception as e:
                logger.warning(
                    f"降级后清理已迁移 sqlite 行失败（可能重复消费）: {e}")
        # 2) 关闭 sqlite 连接并切换到 file 后端
        try:
            if self._sqlite_conn is not None:
                with self._sqlite_lock:
                    self._sqlite_conn.close()
        except Exception:
            pass
        self._sqlite_conn = None
        if bak is not None:
            self._l2_file = bak
        elif self._l2_file is None:
            self._l2_file = _FileL2Backend(self.l2_path)
        self.l2_mode = 'file'
        self.sqlite_enabled = False
        self._sqlite_rows = 0
        logger.error(
            f"L2 sqlite 后端自动降级为自研 file（原因: {reason}）"
            + (f"，已迁移 {migrated} 条遗留事件到 file" if migrated else ""))

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
            logger.error(f"事件 sqlite 缓冲初始化失败，自动降级为 file 后端: {e}")
            self._fallback_to_file(f"sqlite 初始化失败（{type(e).__name__}）")

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
                    f"自动降级为自研 file 后端（事件不再丢失，重启后仍优先恢复 sqlite）")
                self._fallback_to_file(
                    f"连续写失败 {self._sqlite_fail_streak} 次（{type(e).__name__}）")
            return False

    def _write_sqlite_batch(self, events: list) -> bool:
        """线程内批量写入多条（executemany 一次 commit，L4 flush 路径用）。

        把 N 条事件的「N 次 INSERT + N 次 commit」合并为「1 次 executemany + 1 次
        commit」，显著降低 sqlite 单写者瓶颈下的磁盘 fsync 次数，是高并发溢出落盘的
        关键提速点（L4 攒批 → 此处一次性落 L2）。
        """
        if not events:
            return True
        try:
            payloads = [(_l2_serialize(ev), time.time()) for ev, _ in events]
            with self._sqlite_lock:
                self._sqlite_conn.executemany(
                    "INSERT INTO event_buffer (payload, created_at) VALUES (?, ?)",
                    payloads)
                self._sqlite_conn.commit()
            self._sqlite_rows += len(payloads)
            self._sqlite_fail_streak = 0
            return True
        except Exception as e:
            logger.warning(f"sqlite 缓冲批量写入失败: {e}")
            self._sqlite_fail_streak += 1
            if self._sqlite_fail_streak >= self._sqlite_fail_threshold:
                logger.error(
                    f"sqlite 缓冲连续失败 {self._sqlite_fail_streak} 次，"
                    f"自动降级为自研 file 后端")
                self._fallback_to_file(
                    f"连续写失败 {self._sqlite_fail_streak} 次（{type(e).__name__}）")
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
            # 缓冲彻底清空时收缩 sqlite 文件（见 _try_vacuum_sqlite）。
            # 只在「刚消费完最后一批、剩余归零」时触发，避免热路径每次重写大文件。
            if self._sqlite_rows == 0 and rows:
                self._try_vacuum_sqlite()
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

    def _try_vacuum_sqlite(self):
        """事件缓冲清空后收缩 sqlite 文件（防磁盘空占）。

        L2 用 DELETE 消费事件，DELETE 只把行标记为空闲页、文件大小不变；
        一次 50w 突发灌入后文件可膨胀到数百 MB，排空后若不 VACUUM 则磁盘
        居高不下。此处事件已消费完（_sqlite_rows==0）才重写文件缩容，
        频率极低（仅清空瞬间），且经 db 线程执行不阻塞事件循环。
        """
        try:
            with self._sqlite_lock:
                cur = self._sqlite_conn.execute(
                    "SELECT COUNT(*) FROM event_buffer")
                if int(cur.fetchone()[0]) != 0:
                    return  # 并发取回下又有新事件写入，跳过本次
                self._sqlite_conn.execute("VACUUM")
                self._sqlite_conn.commit()
            logger.debug("L2 sqlite 已 VACUUM 收缩（事件缓冲清空）")
            # 趁事件缓冲刚清空、内存已释放，尽力把空闲块归还 OS，
            # 压低大峰值后的 RSS 地板（见 framework/memory.trim_memory）。
            from framework.memory import trim_memory
            trim_memory()
        except Exception as e:
            logger.warning(f"L2 sqlite VACUUM 收缩失败（忽略，下次清空再试）: {e}")

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
        # 大包（size > 整个 L4 容量，如 16MB）装不下攒批缓冲，跳过 L1/L4 直接走
        # L3→L2 兜底链（攒批无意义）；其余事件先试 L1，L1 满落 L4 攒批，L4 满直写 L2。
        if size > self.l4_max_bytes:
            if self._l3_bytes + size <= self.l3_max_bytes:
                self._l3.put_nowait((event, None, size))
                self._l3_bytes += size
                self._wakeup.set()
                return True
            if self.l2_mode != 'off':
                if await self._l2_write(event):
                    self._wakeup.set()
                    return True
                logger.warning(
                    f"超大事件 L3 满且 L2 写失败：size={size}B，事件改走丢弃")
            self._dropped += 1
            logger.error(
                f"超大事件丢弃：size={size}B 超 L4({self.l4_max_bytes}B)，"
                f"且 L3({self._l3_bytes}/{self.l3_max_bytes}B) 已满"
                f"{'、L2 不可用' if self.l2_mode == 'off' else ''}，累计丢弃 {self._dropped} 条")
            return False
        # L1 内存主缓冲（字节 + 条数双限）。L1 一满就溢出到 L4 攒批（而非直写 L2），
        # 由后台 flush 任务批量落 L2，把逐条 commit 降为攒批 commit，缓解 sqlite 写压力。
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
        # L1 满 → L4 攒批写缓冲（纯内存追加，极快，不阻塞 put）
        if self._l4_append(event, size):
            self._wakeup.set()
            return True
        # L4 满（缓不过来）→ 直接塞 L2（sqlite / file，短超时；写不过来转 L3 内存兜底）
        if self.l2_mode != 'off':
            if await self._l2_write(event):
                self._wakeup.set()
                return True
            logger.warning(
                f"L4 满且 L2 缓冲写入超时/失败（>{self.sqlite_write_timeout}s），转入内存兜底")
        # L3 内存兜底（4MB）
        if self._l3_bytes + size <= self.l3_max_bytes:
            self._l3.put_nowait((event, None, size))
            self._l3_bytes += size
            self._wakeup.set()
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
                f"L4={self._l4_bytes}/{self.l4_max_bytes}B "
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
            # L4 写缓冲：若后台 flush 尚未落 L2，消费者直接取走（防事件滞留 L4 丢失）；
            # 常态由 flush 批量落 L2 后走统一回取路径，此处分支仅作安全回退与单测友好路径。
            with self._l4_lock:
                if self._l4:
                    ev, sz = self._l4.pop(0)
                    return ev, None, 'l4'
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
            with self._l4_lock:
                l4_nonempty = bool(self._l4)
            if (not self._l1.empty() or not self._l3.empty()
                    or self._l2_pending or self._sqlite_rows > 0 or l4_nonempty):
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
        with self._l4_lock:
            l4_empty = not self._l4
        return (self._l1.empty() and self._l3.empty()
                and not self._l2_pending and self._l2_rows() == 0 and l4_empty)

    async def wait_drained(self, timeout: float) -> bool:
        """限时等待全部清空（供停机排空；返回是否排空完成）。

        先确保 L4 攒批全部落 L2（否则 L2 永远有残留、循环不退），再等三层排空。
        """
        try:
            await asyncio.wait_for(self._l1.join(), timeout=timeout)
            await asyncio.wait_for(self._l3.join(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        try:
            await asyncio.wait_for(self._l4_do_flush(), timeout=timeout)
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
                    # 已无遗留事件则收缩文件，避免关机后磁盘仍空占峰值体积
                    cur = self._sqlite_conn.execute(
                        "SELECT COUNT(*) FROM event_buffer")
                    if int(cur.fetchone()[0]) == 0:
                        self._sqlite_conn.execute("VACUUM")
                        self._sqlite_conn.commit()
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
            'l4_items': len(self._l4),
            'l4_bytes': self._l4_bytes,
            'l4_max_bytes': self.l4_max_bytes,
            'l2_mode': self.l2_mode,
            'l2_pending': self._l2_rows() + len(self._l2_pending),
            'sqlite_pending': self._sqlite_rows + len(self._l2_pending),
            'overflow_to_sqlite': self._overflow_to_sqlite,
            'dropped': self._dropped,
        }

    # ---------- L4 flush 后台任务 ----------
    async def _l4_flush_loop(self):
        """后台周期 flush：每 0.05s 检查，达到阈值（字节/条数）或空闲上限即批量落 L2。

        触发条件（覆盖「满了写入 + 自动写入 + 定时尝试」）：
          - L4 字节 ≥ l4_flush_bytes（默认一半容量）或 条数 ≥ l4_flush_items → 立即 flush
          - 否则空闲持续 ≥ l4_max_idle（默认 0.1s）也兜底 flush，避免小数据长期滞留内存
        """
        try:
            while not self._l4_stop.is_set():
                try:
                    await asyncio.wait_for(self._l4_stop.wait(), timeout=0.05)
                    break  # 被显式 stop，退出前由 stop_l4 做一次收尾 flush
                except asyncio.TimeoutError:
                    pass
                with self._l4_lock:
                    n = len(self._l4)
                    b = self._l4_bytes
                if n == 0:
                    continue
                flush = (b >= self.l4_flush_bytes or n >= self.l4_flush_items)
                if not flush:
                    idle = time.monotonic() - getattr(self, '_l4_last_flush', time.monotonic())
                    if idle >= self.l4_max_idle:
                        flush = True
                if flush:
                    self._l4_last_flush = time.monotonic()
                    await self._l4_do_flush()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"L4 flush 后台任务异常: {e}")

    def start_l4(self, loop=None):
        """启动 L4 后台 flush 任务（在事件循环内调用，如 base.start）"""
        if self._l4_task is not None:
            return
        self._l4_stop.clear()
        self._l4_last_flush = time.monotonic()
        self._l4_task = asyncio.get_running_loop().create_task(
            self._l4_flush_loop(), name="event-buffer-l4-flush")

    async def stop_l4(self):
        """停止 L4 后台任务并收尾 flush 剩余数据到 L2（供停机排空）"""
        if self._l4_task is None:
            await self._l4_do_flush()   # 未启动也要保证残留 L4 落盘
            return
        self._l4_stop.set()
        try:
            await asyncio.wait_for(self._l4_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._l4_task.cancel()
        self._l4_task = None
        # 收尾：把剩余（可能 stop 后刚入队的）全部 flush 到 L2
        await self._l4_do_flush()
