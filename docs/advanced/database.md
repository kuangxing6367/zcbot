# 数据库

ZCBOT 支持 SQLite（默认，零配置）与 MySQL，上层使用同一套接口，
插件基本不需要感知当前是哪种数据库。

- 数据库封装：`framework/db.py` 的 `Database` 类；
- 启动自动建表/迁移：`framework/init_db.py` 的 `auto_init_database(db)`；
- 配置见 [配置系统](../guide/configuration.md#数据库)。

## 自动初始化

框架启动时根据 `config.yaml → database` 建立连接并自动建表：

- SQLite：按 `database.path`（默认 `data/zcbot.db`）创建文件库；
- MySQL：自动探测版本与字符集，适配 DDL 后建库建表。

插件不要自己去改框架表结构；自己的业务表用 `ctx.create_table()` 创建。

## 插件建表：统一写 MySQL 风格

```python
def register(ctx):
    ctx.create_table("""
        CREATE TABLE IF NOT EXISTS my_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            score INTEGER DEFAULT 0,
            memo TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
```

框架自动完成方言适配，插件无需判断数据库类型：

- SQLite：`AUTO_INCREMENT → AUTOINCREMENT`、`ENUM → TEXT`、移除不支持的内联 INDEX 等；
- MySQL：`AUTOINCREMENT → AUTO_INCREMENT`，对长文本列上的索引自动改成
  前缀索引 `col(191)`，规避 MySQL 的 1170/1064 错误。

建议在 `register(ctx)` 里建表（幂等，`IF NOT EXISTS`），每次注册都会确保表存在。

## 查询接口

### 同步

```python
rows = ctx.db_query("SELECT * FROM users WHERE group_id=%s", (group_id,))     # list[dict]
row  = ctx.db_query_one("SELECT * FROM users WHERE user_id=%s", (user_id,))  # dict / None
n    = ctx.db_execute("UPDATE users SET score=score+%s WHERE id=%s", (1, id_))  # 受影响行数
new_id = ctx.db_insert("INSERT INTO logs (msg) VALUES (%s)", ("hello",))     # 自增 ID
ctx.db_execute_many("INSERT INTO t (v) VALUES (%s)", [(1,), (2,)])           # 批量
```

### 异步（async handler 推荐）

异步方法在**数据库专用线程池**执行，DB 繁忙也不会卡住消息事件循环：

```python
rows = await ctx.db_query_async(sql, params)
row  = await ctx.db_query_one_async(sql, params)
n    = await ctx.db_execute_async(sql, params)
new_id = await ctx.db_insert_async(sql, params)
await ctx.db_execute_many_async(sql, params_list)
```

查询结果统一为 `dict`（单条）或 `list[dict]`（多条），列名即键。

## 占位符：统一用 `%s`

无论 SQLite 还是 MySQL，**插件 SQL 一律写 `%s` 占位符**，框架在 SQLite 下
自动转换成 `?`。不要拼接字符串，避免 SQL 注入与方言问题：

```python
# ✅ 参数化
ctx.db_execute("UPDATE t SET v=%s WHERE id=%s", (v, id_))
# ❌ 字符串拼接
ctx.db_execute(f"UPDATE t SET v={v} WHERE id={id_}")
```

## 事务

多条必须一起成功/失败的写操作，用 `db_connection()` 取连接手动提交/回滚。
连接池模式下 `close()` 是归还连接，不是断开：

```python
conn = ctx.db_connection()
try:
    cur = conn.cursor()
    cur.execute("UPDATE account SET balance=balance-%s WHERE id=%s", (100, from_id))
    cur.execute("UPDATE account SET balance=balance+%s WHERE id=%s", (100, to_id))
    conn.commit()
except Exception:
    conn.rollback()
    raise
finally:
    cur.close()
    conn.close()
```

## 连接池状态

```python
ctx.db_pool_status     # dict：连接池占用/空闲等状态，便于排障
```

## SQLite vs MySQL 对比

| 特性 | SQLite | MySQL |
|------|--------|-------|
| 配置 | 零配置，单文件 | 需 host/port/user/password/database |
| 并发 | 单写多读，适合轻量场景 | 支持高并发 |
| 占位符 | 插件写 `%s`，运行时转 `?` | 原生 `%s` |
| 自增主键 | `INTEGER PRIMARY KEY AUTOINCREMENT` | `INT ... AUTO_INCREMENT PRIMARY KEY` |
| 适合规模 | 个人/小群 | 多群、高并发、多进程部署 |

## 字段元信息系统

框架为业务表维护字段描述等元信息，配套提供（通过 ctx/框架能力）：

- 描述字段、更新字段描述、列出全表字段描述；
- 群级动态列等高级能力。

插件自定义表只要遵循统一建表入口，即可被这些能力识别。

## 实践建议

1. 表名加插件前缀（如 `sign_records`），避免不同插件撞表；
2. `async def` handler 里一律用 `*_async` 接口；
3. 建表 DDL 保持幂等（`IF NOT EXISTS`），在 `register(ctx)` 调用；
4. 高频写库考虑批量接口 `db_execute_many`；
5. 用户可改的配置走 `_conf_schema.json` + `ctx.get_config`，不要自己建配置表。
