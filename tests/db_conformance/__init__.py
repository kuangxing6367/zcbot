"""方言一致性测试套件（backend-agnostic conformance suite）。

结构参照 SQLAlchemy 的多后端测试体系：
  - backends.py     后端注册与 provisioning（建库/清库）
  - requirements.py 后端能力声明，测试据此 skip 而不是散落 if db_type == ...
  - conftest.py     --db 选项、后端 fixture、数据隔离
  - test_*.py       同一套断言跑遍所有后端

用法：
    # 只测 SQLite（默认，零配置）
    pytest tests/db_conformance -q

    # SQLite + MySQL 一起跑（同一套用例在两边各跑一遍）
    ZCBOT_MYSQL_HOST=127.0.0.1 ZCBOT_MYSQL_PORT=13306 \\
    ZCBOT_MYSQL_USER=zcbot_p ZCBOT_MYSQL_PASSWORD=*** \\
    ZCBOT_MYSQL_DATABASE=zcbot_probe \\
    pytest tests/db_conformance -q --db sqlite --db mysql

设计要点：
  用例断言的是「同一份源码 SQL 在两种后端上产生相同结果」，而不是「某后端能跑」。
  这正是框架方言层的契约：源码按 MySQL 风格写，SQLite 侧靠翻译对齐。
"""
