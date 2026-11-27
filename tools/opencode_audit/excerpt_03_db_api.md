# 第 3 轮侦察材料：framework/database/db.py 第 1~130 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
   1 
   2 #                    _ooOoo_
   3 #                   o8888888o
   4 #                   88" . "88
   5 #                   (| -_- |)
   6 #                    O\ = /O
   7 #                ____/`---'\____
   8 #              .   ' \\| |// `.
   9 #               / \\||| : |||// \
  10 #             / _||||| -:- |||||- \
  11 #               | | \\\ - /// | |
  12 #             | \_| ''\---/'' | |
  13 #              \ .-\__ `-` ___/-. /
  14 #           ___`. .' /--.--\ `. . __
  15 #        ."" '< `.___\_<|>_/___.' >'"".
  16 #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
  17 #         \ \ `-. \_ __\ /__ _/ .-` / /
  18 # ======`-.____`-.___\_____/___.-`____.-'======
  19 #                    `=---='
  20 #
  21 # .............................................
  22 #          佛祖保佑             永无BUG
  23 
  24 """
  25 数据库操作模块
  26 支持 SQLite（默认）和 MySQL（可选），自动适配。
  27 
  28 设计原则：
  29 - 默认使用 SQLite —— 零配置开箱即用，但仅适合小环境与开发环境（单写多读）；
  30   多群、高并发、多进程等大环境请配置 database.type: mysql（见 docs/进阶/数据库.md）
  31 - 自动处理 %s → ? 占位符转换（插件无需修改 SQL 方言）
  32 - 自动处理 DDL 语法差异（ENGINE=、COMMENT、AUTO_INCREMENT 等）
  33 - 自动处理 NOW() → datetime 参数转换
  34 
  35 模块拆分（史山剥离）：
  36 - dialect.py  SQL 方言翻译（纯函数）
  37 - schema.py   自动建表 + 运行时迁移
  38 - db.py       连接管理（Database）+ 查询/执行/事务 + 全局单例
  39 """
  40 import logging
  41 import os
  42 import re
  43 import threading
  44 import time
  45 import weakref
  46 from contextlib import contextmanager
  47 from threading import local
  48 
  49 from framework.paths import home_dir
  50 
  51 from .dialect import (
  52     _replace_now,
  53     _translate_sql_for_mysql,
  54     _translate_sql_for_sqlite,
  55 )
  56 from .schema import _auto_create_tables
  57 
  58 logger = logging.getLogger('zcbot')
  59 
  60 # MySQL 连接断开类错误码（触发自动重连）——定义在 db_conn，此处 re-export 兼容旧导入
  61 from framework.database.db_conn import (
  62     _MYSQL_RECONNECT_ERRORS,
  63     _MYSQL_RECONNECT_KEYWORDS,
  64 )
  65 
  66 # ── 数据库引擎 ──────────────────────────────────────────────────────
  67 from .db_conn import DatabaseConnMixin, _close_conns_at_collect
  68 from .storage import create_storage
  69 
  70 
  71 def _is_readonly_error(err) -> bool:
  72     """判定是否为 SQLITE_READONLY 类错误（只读挂载 / 文件只读属性 / 写入被拒）。
  73 
  74     SQLite 与 MySQL 的措辞不同，统一按关键字判定：
  75       - SQLite: "attempt to write a readonly database"
  76       - MySQL : "The MySQL server is running with the --read-only option"
  77     这类错误与"SQL 写错、唯一约束冲突"有本质区别——它意味着**本次运行的写入
  78     不会落盘**，必须让运维看见，而不是让它混在普通异常里。
  79     """
  80     s = str(err).lower()
  81     return 'readonly' in s or 'read-only' in s
  82 
  83 
  84 def _emergency_recover(err, db_path, source='db'):
  85     """明确的损坏异常走备份 + 官方恢复 + 故意崩溃退出；其余交回调用方。
  86 
  87     不能在查询/写入失败后悄悄换成降级库：那会让运维以为写入仍落在主库上。
  88     恢复后写一份崩溃日志（计入原生崩溃守卫）再退出；守护脚本拉起时若计数
  89     到了上限，框架直接进安全模式假启动——进程活着但不加载插件、不写库，
  90     直到人工核对数据并清掉崩溃计数。
  91     """
  92     try:
  93         from . import recovery
  94         if not recovery.is_corruption(err):
  95             return False
  96     except Exception as import_err:
  97         logger.error('应急恢复模块不可用（%s: %s）',
  98                      type(import_err).__name__, import_err)
  99         return False
 100 
 101     path = str(db_path or '')
 102     if path and not os.path.isabs(path):
 103         path = os.path.join(home_dir(), path)
 104     logger.error('检测到数据库损坏（%s: %s，来源 %s），进入应急恢复流程',
 105                  type(err).__name__, err, source)
 106 
 107     report = None
 108     try:
 109         report = recovery.run_recovery(path)
 110     except Exception as rec_exc:
 111         logger.error('应急恢复流程自身失败（%s: %s）',
 112                      type(rec_exc).__name__, rec_exc)
 113         report = {'status': 'recovery-error', 'ok': False,
 114                   'db_path': path, 'note': '%s: %s'
 115                   % (type(rec_exc).__name__, rec_exc)}
 116     try:
 117         recovery.perform_exit(report)
 118     except Exception as exit_exc:
 119         # 正常情况下 perform_exit 用 os._exit 不会返回；到这里说明是测试替身
 120         # 或退出被拦截，不能吞掉原异常，调用方仍要看到最初的损坏。
 121         logger.error('退出动作失败（%s: %s）', type(exit_exc).__name__, exit_exc)
 122     return True
 123 
 124 
 125 class Database(DatabaseConnMixin):
 126     """
 127     数据库连接管理器
 128     支持 SQLite（默认，小环境/开发环境）和 MySQL（可选，大环境）
 129     """
 130 
```
