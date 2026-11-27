# 第 10 轮侦察材料：framework/core/event_buffer.py 第 1~130 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
   1 """事件分层缓冲：内存主队列(L1) → 写缓冲(L4) → sqlite 持久化溢出(L2) → 内存兜底(L3) → 全满告警丢弃。
   2 
   3 对应消息流入缓冲设想（总量约 8.5MB 内存预算 + sqlite 磁盘层）：
   4 - L1  512KB 内存队列（文字消息为主）：消息流入先入此层直接处理；字节超限即溢出
   5 - L4  4MB 写缓冲（L1 与 L2 之间的批量写聚合层）：L1 满时小包先进 L4 攒批，后台任务
   6       周期 / 阈值触发后 executemany 一次性落 L2，把「逐条 INSERT+commit」降为
   7       「攒一批一次 commit」，缓解 sqlite 单写者瓶颈。大包（size > l4_max_bytes，装不下
   8       攒批）绕过 L4 直写 L2；L4 满（缓不过来）直接塞 L2
   9 - L2  sqlite 持久化缓冲（data/event_buffer.db，独立文件独立连接，不阻塞主库）：
  10       L4 批量承接 / 大包直写，防内存暴涨、防事件丢失
  11 - L3  4MB 内存兜底队列：L2 写入不过来（超时/失败）时的应急暂存
  12 - 全满  日志告警并丢弃新事件（保老弃新），丢弃计数可经 stats() 查看
  13 
  14 消费优先级：L1（最新热数据）→ L3（内存兜底）→ L4（写缓冲，未及 flush 时消费者直接取走，
  15 不丢事件）→ L2 sqlite（flush 落盘后的历史积压最后回取）。L4 常态由后台 flush 任务批量落 L2
  16 （攒批 commit 缓解 sqlite 写压力），消费者空闲或单测未启动 flush 时也可直接取走 L4 中事件。
  17 wait=True 的同步语义事件保持阻塞进 L1，不参与溢出，以保留 done future 契约（测试 / 终端同步）。
  18 """
  19 import asyncio
  20 import json
  21 import logging
  22 import os
  23 import sqlite3
  24 import threading
  25 import time
  26 from contextlib import suppress
  27 
  28 logger = logging.getLogger('zcbot')
  29 
  30 
  31 def _l2_serialize(event) -> str:
  32     """L2 落盘序列化：剔除冗余 raw（完整原始 payload 副本，event 已有 message /
  33     sender / user_id 等结构化字段，raw 仅为镜像备份），减小落盘体积与磁盘 IO。
  34 
  35     不修改入参 event（L1/L3 内存事件仍需保留 raw，供 session 备份等消费），
  36     而是浅拷贝后剥离。回读侧（_pop_sqlite_batch / _FileL2Backend.pop_batch）
  37     会补 raw 占位，保持回读事件形态与内存一致。
  38     """
  39     if isinstance(event, dict) and 'raw' in event:
  40         ev = dict(event)
  41         del ev['raw']
  42         return json.dumps([ev, None], ensure_ascii=False, default=str)
  43     return json.dumps([event, None], ensure_ascii=False, default=str)
  44 
  45 
  46 class _FileL2Backend:
  47     """自研 L2 溢出后端：append-only 日志文件，按行存取事件（不依赖 sqlite）。
  48 
  49     - 写入：一条事件一行 JSON（与 sqlite payload 同构），追加 + flush；
  50     - 读取：从 _read_offset 顺序读 n 行后推进偏移；读到文件尾即 truncate 复位
  51       （语义同 sqlite 取出即删）；读指针越过一半时压缩一次，避免头部空洞无限膨胀；
  52     - 内存记账：仅 _read_offset 与剩余行数两个整数，不缓存事件本体——O(1)，
  53       不为内存而内存（不把积压事件全量加载进内存）。
  54     """
  55 
  56     def __init__(self, path: str):
  57         self._path = path
  58         self._lock = threading.Lock()
  59         self._fh = None
  60         self._read_offset = 0          # 已消费字节偏移
  61         self._rows = 0                 # 剩余未消费行数
  62         os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
  63         try:
  64             self._fh = open(path, 'a+', encoding='utf-8', newline='')
  65             self._rows = self._count_lines()
  66         except Exception as e:
  67             logger.error(f"L2 文件缓冲初始化失败，回落 L2 不可用: {e}")
  68             self._fh = None
  69             self._rows = 0
  70 
  71     def _count_lines(self) -> int:
  72         """启动时清点遗留行数（仅初始化一次，非热路径）"""
  73         try:
  74             self._fh.seek(0)
  75             n = 0
  76             for line in self._fh:
  77                 if line.strip():
  78                     n += 1
  79             self._fh.seek(0, os.SEEK_END)
  80             return n
  81         except Exception as _exc:
  82             logger.debug('L2 行数统计失败，按 0 处理: %s', _exc)
  83             return 0
  84 
  85     def _file_size(self) -> int:
  86         try:
  87             self._fh.flush()
  88             return os.path.getsize(self._path)
  89         except Exception as _exc:
  90             logger.debug('事件缓冲文件大小获取失败，按 0 处理: %s', _exc)
  91             return 0
  92 
  93     def _compact(self):
  94         """把未读部分（read_offset..EOF）重写进临时文件并原子替换，归还已读空洞"""
  95         try:
  96             tmp = self._path + '.tmp'
  97             self._fh.flush()
  98             with open(tmp, 'w', encoding='utf-8', newline='') as out:
  99                 self._fh.seek(self._read_offset)
 100                 while True:
 101                     line = self._fh.readline()
 102                     if not line:
 103                         break
 104                     out.write(line)
 105             self._fh.close()
 106             os.replace(tmp, self._path)
 107             self._fh = open(self._path, 'a+', encoding='utf-8', newline='')
 108             self._read_offset = 0
 109         except Exception as e:
 110             logger.warning(f"L2 文件缓冲压缩失败（忽略，继续顺序读）: {e}")
 111 
 112     def write(self, event) -> bool:
 113         """线程内追加一条（独立文件句柄 + 锁，不阻塞主库）"""
 114         payload = _l2_serialize(event)
 115         try:
 116             with self._lock:
 117                 if self._fh is None:
 118                     return False
 119                 self._fh.write(payload + '\n')
 120                 self._fh.flush()
 121                 self._rows += 1
 122             return True
 123         except Exception as e:
 124             logger.warning(f"L2 文件缓冲写入失败: {e}")
 125             return False
 126 
 127     def write_batch(self, events: list) -> bool:
 128         """线程内批量追加多条（一次 flush，L4 flush 路径用）"""
 129         if not events:
 130             return True
```
