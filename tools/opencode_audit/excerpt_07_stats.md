# 第 7 轮侦察材料：framework/core/stats_writer.py 第 1~130 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
   1 """
   2 消息统计批量写库器（自 framework/core/base.py 剥离）
   3 
   4 框架自身的用户/群自动注册、命令命中计数等写入，统一走此队列，
   5 由后台任务周期性批量落库（在线程中执行），不阻塞事件循环。
   6 """
   7 import asyncio
   8 import logging
   9 import time
  10 
  11 from framework.messaging.adapter_registry import namespace_identity
  12 
  13 logger = logging.getLogger('zcbot')
  14 
  15 
  16 class AsyncStatsWriter:
  17     """
  18     消息统计批量写库器
  19     框架自身的用户/群自动注册、命令命中计数等写入，统一走此队列，
  20     由后台任务周期性批量落库（在线程中执行），不阻塞事件循环。
  21     """
  22 
  23     def __init__(self, framework, flush_interval: float = 5.0):
  24         self.framework = framework
  25         self.db = framework.db
  26         self.flush_interval = flush_interval
  27         # 有界队列：消息洪峰时丢弃多余注册请求（内存有上限），丢弃计数定期上报
  28         self._reg_queue = asyncio.Queue(maxsize=20000)   # 用户/群注册任务
  29         self._dropped = 0
  30         self._last_drop_warn = 0.0   # 丢弃告警节流：首次立即报，之后每隔 60s 报一次
  31         self._cmd_hits = {}                 # cmd_id -> count（主循环线程访问）
  32         self._kw_hits = {}                  # dynamic_commands.id -> count
  33         self._task = None
  34 
  35     def start(self):
  36         """启动后台批量写库任务（需在事件循环内调用）"""
  37         if self._task is None:
  38             self._task = asyncio.create_task(self._run(), name="stats-writer")
  39 
  40     def command_hit(self, cmd_id: int):
  41         """记录命令命中（内存聚合）"""
  42         self._cmd_hits[cmd_id] = self._cmd_hits.get(cmd_id, 0) + 1
  43 
  44     def keyword_hit(self, kw_id: int):
  45         """记录关键词自动回复命中（内存聚合）"""
  46         self._kw_hits[kw_id] = self._kw_hits.get(kw_id, 0) + 1
  47 
  48     def register_user(self, user_id: int, sender: dict, message_type: str,
  49                       group_id: int | None = None, adapter_key: str = None):
  50         """排队用户/群自动注册（非阻塞，队列满时丢弃并计数）
  51 
  52         :param adapter_key: 事件来源适配器连接标识（onebot/qq_official/…），
  53             用于多平台接入身份命名空间（multi_platform.namespace 开启时生效）
  54         """
  55         # 身份键要当 dict key 和 DB 参数用：畸形/攻击型输入（dict/list 等）必须
  56         # 在入队前收敛，否则 _aggregate_registrations 会以 unhashable type 抛错，
  57         # 让整批统计写库（含正常用户的数据）一起失败。
  58         if not isinstance(user_id, (int, str)):
  59             logger.debug("忽略非法 user_id 类型: %s", type(user_id).__name__)
  60             return
  61         # 超大整数（如攻击者构造 2**200）虽是合法 int，但超出 SQL BIGINT 范围，
  62         # executemany 会抛 OverflowError，使同一批次里所有正常用户的注册一起失败。
  63         # 与上面的类型收敛同理：坏身份键必须在入队前挡掉，不能带进批量写库。
  64         if isinstance(user_id, int) and not (-(1 << 63) <= user_id <= (1 << 63) - 1):
  65             logger.debug("忽略超出 BIGINT 范围的 user_id（%d 位）", len(str(abs(user_id))))
  66             return
  67         if group_id is not None and not isinstance(group_id, (int, str)):
  68             group_id = None
  69         if isinstance(group_id, int) and not (-(1 << 63) <= group_id <= (1 << 63) - 1):
  70             logger.debug("忽略超出 BIGINT 范围的 group_id")
  71             group_id = None
  72         try:
  73             self._reg_queue.put_nowait(
  74                 (user_id, dict(sender), message_type, group_id, adapter_key))
  75         except asyncio.QueueFull:
  76             self._dropped += 1
  77             # 原来只在第 1/1001/2001… 条才告警：突发高峰丢掉的前 999 个
  78             # 新用户/新群完全静默，事后无从查起。改为首次立即报 + 时间节流。
  79             now = time.monotonic()
  80             if self._dropped == 1 or now - self._last_drop_warn >= 60.0:
  81                 self._last_drop_warn = now
  82                 logger.warning(
  83                     f"用户注册队列已满，累计丢弃 {self._dropped} 条注册请求（消息量过大；"
  84                     "对应新用户/新群不会出现在数据库中）")
  85 
  86     async def _run(self):
  87         """后台循环：周期性 flush。
  88 
  89         各落库目标（命中计数 / users / groups / group_members）互不依赖，
  90         各自封装为闭包并行提交到 DB 线程池（MySQL 下真实并行；SQLite 内核
  91         单写者仍串行落盘，但省去三次往返的串行等待），任一失败不影响其余。
  92         """
  93         while True:
  94             try:
  95                 await asyncio.sleep(self.flush_interval)
  96                 fns = self._drain_to_fns()
  97                 if not fns:
  98                     continue
  99                 results = await asyncio.gather(
 100                     *[self._run_in_db_thread(fn) for fn in fns],
 101                     return_exceptions=True)
 102                 for r in results:
 103                     if isinstance(r, Exception):
 104                         logger.error(f"统计批量写库异常: {r}")
 105             except asyncio.CancelledError:
 106                 raise
 107             except Exception as e:
 108                 logger.error(f"统计批量写库异常: {e}")
 109                 await asyncio.sleep(1)
 110 
 111     async def _run_in_db_thread(self, func):
 112         """在数据库专用线程池中执行（与默认线程池隔离，DB 阻塞不影响消息处理）"""
 113         ex = getattr(self.framework, '_db_executor', None)
 114         if ex is not None:
 115             return await asyncio.get_running_loop().run_in_executor(ex, func)
 116         return await asyncio.to_thread(func)
 117 
 118     def _drain_to_fns(self) -> list:
 119         """把内存聚合数据排空为一组互不依赖的落库闭包（供 DB 线程池并行执行）"""
 120         fns = []
 121         hits = self._cmd_hits
 122         self._cmd_hits = {}
 123         kwhits = self._kw_hits
 124         self._kw_hits = {}
 125         if hits or kwhits:
 126             fns.append(lambda: self._flush_hits(hits, kwhits))
 127         items = []
 128         while True:
 129             try:
 130                 items.append(self._reg_queue.get_nowait())
```
