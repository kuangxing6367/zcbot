# -*- coding: utf-8 -*-
"""建表 SQL 目录（作为包分发，确保 pip 安装后 sql/init.sql 随包落地）。

framework/database/init_db.py 的 _find_sql_file 会从 framework/database 向上回溯，
在项目根（或 framework 父目录）下查找 sql/init.sql；把本目录做成包可让它随 wheel
安装到 site-packages 根，从而被正确定位。
"""
