# -*- coding: utf-8 -*-
"""数据库表快照导出工具（sqlite 生产路径）

把 sqlite 库的每个业务表导出为 data/db/sql/{table}.json——与调试模式
（framework/database/sql_sim.py）的落盘格式**完全同构**：

    {"schema": [{"name": "id", "type": "INTEGER"}, ...],
     "rows":   [{"col": value, ...}, ...],
     "next_id": 自增游标（无 id 列时为 1）}

用途：
  - 生产 sqlite 数据可视化 / 备份 / 排查（JSON 可读，Y 盘参考部署即此形态）；
  - 与 debug 模式（database.type: debug）共享同一套 data/db/sql 视图，
    避免"生产 .db 二进制看不到、调试 JSON 又一套"的割裂感。

用法（仓库根执行）：
  python tools/export_db_snapshot.py                 # 导出到 data/db/sql/
  python tools/export_db_snapshot.py --db data/zcbot.db
  python tools/export_db_snapshot.py --dir /tmp/snap --list   # 仅列出业务表
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _list_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def _snapshot_table(conn, table):
    """生成与 sql_sim 同构的 {schema, rows, next_id}"""
    # schema：仅 name/type（与 SqlSimEngine 落盘字段一致）
    cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    schema = [{'name': c[1], 'type': c[2]} for c in cols]
    # rows：全列读取，datetime/bytes 等转 JSON 可序列化形式
    col_names = [c[1] for c in cols]
    rows = []
    if col_names:
        sel = ', '.join(f'"{c}"' for c in col_names)
        for rec in conn.execute(f'SELECT {sel} FROM "{table}"'):
            row = {}
            for name, val in zip(col_names, rec):
                if isinstance(val, (bytes, bytearray)):
                    val = val.hex()
                row[name] = val
            rows.append(row)
    # next_id：优先 sqlite_sequence，回退 MAX(id)
    next_id = 1
    if any(c['name'] == 'id' for c in schema):
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
        if seq is not None and seq[0] is not None:
            next_id = seq[0] + 1
        else:
            mx = conn.execute(
                f'SELECT MAX("id") FROM "{table}"').fetchone()[0]
            next_id = int(mx) + 1 if mx is not None else 1
    return {'schema': schema, 'rows': rows, 'next_id': next_id}


def main():
    ap = argparse.ArgumentParser(description='sqlite 表快照导出（data/db/sql 格式）')
    ap.add_argument('--db', default=os.path.join(_REPO_ROOT, 'data/zcbot.db'),
                    help='sqlite 库路径（默认 data/zcbot.db）')
    ap.add_argument('--dir',
                    default=os.path.join(_REPO_ROOT, 'data/db/sql'),
                    help='输出目录（默认 data/db/sql）')
    ap.add_argument('--list', action='store_true', help='仅列出业务表，不导出')
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        print(f"[dbdump] 库不存在: {args.db}")
        return 2
    conn = sqlite3.connect(args.db)
    conn.row_factory = None
    tables = _list_tables(conn)
    if args.list:
        print('\n'.join(f"{t}" for t in tables))
        conn.close()
        return 0

    os.makedirs(args.dir, exist_ok=True)
    total_rows = 0
    for table in tables:
        data = _snapshot_table(conn, table)
        tmp = os.path.join(args.dir, f'{table}.json.tmp')
        dst = os.path.join(args.dir, f'{table}.json')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True,
                      default=str)
        os.replace(tmp, dst)
        total_rows += len(data['rows'])
        print(f"  {table}: {len(data['rows'])} 行, "
              f"{len(data['schema'])} 列, next_id={data['next_id']}")
    conn.close()
    print(f"[dbdump] 已导出 {len(tables)} 张表 -> {args.dir}（共 {total_rows} 行）")
    return 0


if __name__ == '__main__':
    sys.exit(main())