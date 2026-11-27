# 第 2 轮侦察材料：framework/database/db_conn.py 第 1~130 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
   1 """Database 连接管理 mixin（自 database/db.py 剥离）
   2 
   3 初始化 / 连接获取 / 断线重连 / 线程连接关闭 / 池状态。
   4 依赖 Database.__init__ 建立的配置与线程本地存储。
   5 """
   6 import logging
   7 import os
   8 import sqlite3
   9 import threading
  10 import time
  11 from contextlib import suppress
  12 
  13 from .sqlite_compat import ensure_registered
  14 
  15 # MySQL 连接断开类错误码（触发自动重连）——定义在此避免 db↔db_conn 循环
  16 _MYSQL_RECONNECT_ERRORS = {2006, 2013, 2055, 1927, 1040}
  17 # 连接断开类错误关键字（用于兜底判断）
  18 _MYSQL_RECONNECT_KEYWORDS = (
  19     'server has gone away',
  20     'lost connection',
  21     'connection is closed',
  22     'broken pipe',
  23     'connection reset by peer',
  24 )
  25 
  26 logger = logging.getLogger('zcbot')
  27 
  28 
  29 def _safe_close_conn(conn) -> None:
  30     """尽力关闭 SQLite 连接。GC / 解释器退出阶段没有可上报的调用方，只记 debug。"""
  31     try:
  32         conn.close()
  33     except Exception as _exc:  # 已关闭或连接已损坏
  34         # finalizer 可能在解释器退出时跑，此时 logging 已拆掉，不能再往外抛
  35         with suppress(Exception):
  36             logger.debug('[db] 关闭 SQLite 连接失败: %s', _exc)
  37 
  38 
  39 def _close_conns_at_collect(conns, lock) -> None:
  40     """weakref.finalize 回调：Database 被回收时关掉它名下所有 SQLite 连接。"""
  41     with lock:
  42         pending = list(conns)
  43         conns.clear()
  44     for _ident, conn in pending:
  45         _safe_close_conn(conn)
  46 
  47 
  48 class DatabaseConnMixin:
  49     """连接池 / 断线重连 / 生命周期"""
  50 
  51 
  52     def _init_sqlite(self):
  53         """初始化 SQLite"""
  54         db_path = self.config.get('path', 'data/zcbot.db')
  55         # 相对路径必须解析到可写状态目录，不能依赖进程 cwd。
  56         # 日志路径早就 join 了项目根，唯独库路径漏了：systemd / cron 不设
  57         # WorkingDirectory 时，root 会把库建到 /data/zcbot.db（与 get_home()
  58         # 的数据树分叉），非 root 则 makedirs('/data') 失败后静默降级 JSON 模拟引擎。
  59         if not os.path.isabs(db_path):
  60             try:
  61                 from framework.config import get_home
  62                 db_path = os.path.normpath(os.path.join(get_home(), db_path))
  63             except Exception as _exc:
  64                 # 极端情况（配置模块不可导入）退回代码根，仍优于 cwd
  65                 logger.debug('[_init_sqlite] 捕获异常后按降级路径继续: %s', _exc)
  66                 db_path = os.path.normpath(os.path.join(
  67                     os.path.dirname(os.path.dirname(os.path.dirname(
  68                         os.path.abspath(__file__)))), db_path))
  69         # 确保目录存在
  70         db_dir = os.path.dirname(db_path)
  71         if db_dir and not os.path.exists(db_dir):
  72             os.makedirs(db_dir, exist_ok=True)
  73         self._db_path = db_path
  74         logger.info(f"SQLite 数据库已初始化: {db_path}")
  75 
  76     def _normalize_mysql_params(self) -> dict:
  77         """校验并规范化 MySQL 连接参数。
  78 
  79         pymysql 对 dict/list 类型的参数不会报「哪个键写错了」，而是在内部
  80         拼接 SQL 时抛 ``unsupported operand type(s) for +: 'dict' and 'bytes'``，
  81         完全指不到配置项。典型成因是把参数多嵌套了一层：
  82 
  83             database:
  84               type: mysql
  85               mysql:            # ← 多出来的这一层
  86                 host: 127.0.0.1
  87 
  88         这里在建池前把类型问题变成一句能直接照着改的错误。
  89         """
  90         cfg = self.config
  91         expected = {
  92             'host': '127.0.0.1',
  93             'port': 3306,
  94             'user': 'root',
  95             'password': '',
  96             'database': 'zcbot',
  97             'charset': 'utf8mb4',
  98         }
  99         bad = []
 100         for key in expected:
 101             val = cfg.get(key, expected[key])
 102             if isinstance(val, (dict, list, tuple, set)):
 103                 bad.append(
 104                     f"database.{key} 应为字符串/数字，实际是 {type(val).__name__}"
 105                     f"（{val!r}）"
 106                 )
 107         if bad:
 108             msg = (
 109                 "MySQL 连接参数类型错误，请检查 config.yaml 的 database 段："
 110                 + "；".join(bad)
 111                 + "。常见原因是把 mysql 参数又嵌套了一层（如 database.mysql.host），"
 112                   "或 database.database 被写成了对象——所有键应与 database.type 平级。"
 113             )
 114             logger.error(msg)
 115             raise ValueError(msg)
 116 
 117         def _s(key):
 118             val = cfg.get(key, expected[key])
 119             return '' if val is None else str(val)
 120 
 121         try:
 122             port = int(cfg.get('port', 3306) or 3306)
 123         except (TypeError, ValueError):
 124             msg = (f"database.port 必须是整数，实际是 "
 125                    f"{cfg.get('port')!r}，请检查 config.yaml")
 126             logger.error(msg)
 127             raise ValueError(msg) from None
 128 
 129         return {
 130             'host': _s('host') or '127.0.0.1',
```
