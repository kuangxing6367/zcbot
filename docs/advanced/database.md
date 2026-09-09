# 数据库

ZCBOT 支持 SQLite（默认）和 MySQL，提供统一查询接口。

## 自动建表

框架启动时自动建表（`core_plugins/db_manager/tables.py`）：

```python
from framework.db_pool import get_connection
from framework.db_pool import get_db_dialect

# 建表 DDL
CTX_TABLE = """
CREATE TABLE IF NOT EXISTS ctx ...
"""

# 根据方言自动适配
def create_tables():
    dialect = get_db_dialect()
    with get_connection() as conn:
        cursor = conn.cursor()
        for table_sql in ALL_TABLES:
            cursor.execute(table_sql)
        conn.commit()
```

## 插件建表

使用 `ctx.create_table()` 自动适配方言：

```python
def register(ctx):
    ctx.create_table("""
        CREATE TABLE IF NOT EXISTS my_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            score INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
```

## 查询接口

### 同步

```python
rows = ctx.db_query("SELECT * FROM users WHERE group_id = %s", (group_id,))
row = ctx.db_query_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
affected = ctx.db_execute("UPDATE users SET score = %s WHERE user_id = %s", (100, user_id))
new_id = ctx.db_insert("INSERT INTO logs (msg) VALUES (%s)", ("hello",))
```

### 异步

```python
rows = await ctx.db_query_async(sql, params)
row = await ctx.db_query_one_async(sql, params)
affected = await ctx.db_execute_async(sql, params)
```

## 事务

```python
with ctx.db_connection() as conn:
    cursor = conn.cursor()
    cursor.execute("UPDATE accounts SET balance = balance - %s WHERE id = %s", (100, from_id))
    cursor.execute("UPDATE accounts SET balance = balance + %s WHERE id = %s", (100, to_id))
    conn.commit()
```

## SQLite vs MySQL

| 特性 | SQLite | MySQL |
|------|--------|-------|
| 配置 | 零配置 | 需配置连接信息 |
| 并发 | 单写多读 | 支持高并发 |
| 事务 | 自动 | 需手动提交 |
| 占位符 | `?` | `%s` |
| 自增主键 | `INTEGER PRIMARY KEY AUTOINCREMENT` | `INT AUTO_INCREMENT PRIMARY KEY` |

框架自动将 `?` 转换为 `%s`，插件代码无需区分。

## 字段缓存系统

框架为每个表维护 `column_name, extra_info, data_type, description` 字段，支持：
- `ctx.describe(group_id, field)` — 查询字段描述
- `ctx.update_description(group_id, field, desc)` — 更新描述
- `ctx.drop_group_column(group_id, field)` — 删除字段
- `ctx.describe_all()` — 获取全表字段描述
