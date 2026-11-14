"""
SQL 方言翻译层（纯函数，无连接、无状态）

把统一书写格式的 SQL 翻译成目标方言，让插件只写一套 SQL（%s 占位符 + MySQL 方言 DDL）：
- → SQLite：清理 MySQL 专有语法（ENGINE/CHARSET/COMMENT/AUTO_INCREMENT…）、
  函数与子句改写（IF/IFNULL/ON DUPLICATE KEY/INSERT IGNORE/NOW()）、%s → ?
- → MySQL：防御性反向翻译（AUTOINCREMENT → AUTO_INCREMENT）、
  长文本列索引自动改前缀索引 col(191)

适用边界提醒：方言层只是让同一套 SQL 双跑，不代表 SQLite 能扛大环境——
SQLite 单写多读，仅适合小环境与开发环境；多群/高并发/大环境请用 MySQL
（见 docs/进阶/数据库.md）。
"""
import re

# ── 预编译正则（加速重复替换）─────────────────────────────────────

_RE_ENGINE = re.compile(r'\s+ENGINE\s*=\s*\S+', re.IGNORECASE)
_RE_CHARSET = re.compile(r'\s+(DEFAULT\s+)?(CHARSET|CHARACTER\s+SET)\s*=\s*\S+', re.IGNORECASE)
_RE_COLLATE = re.compile(r'\s+COLLATE\s*=\s*\S+', re.IGNORECASE)
_RE_COLLATE_INLINE = re.compile(r'\s+COLLATE\s+\S+', re.IGNORECASE)
# COMMENT 'xxx'：用非贪婪匹配引号内容，支持引号内含括号/分号等特殊字符
_RE_COMMENT = re.compile(r"\s+COMMENT\s+'[^']*'", re.IGNORECASE)
# COMMENT='xxx'（MySQL 表级/列级等号写法）
_RE_COMMENT_EQ = re.compile(r"\s+COMMENT\s*=\s*'[^']*'", re.IGNORECASE)
_RE_AUTO_INCREMENT = re.compile(r'\s*AUTO_INCREMENT\b', re.IGNORECASE)
_RE_UNSIGNED = re.compile(r'\s+UNSIGNED\b', re.IGNORECASE)
_RE_FOR_UPDATE = re.compile(r'\s+FOR\s+UPDATE\b', re.IGNORECASE)
# ON UPDATE CURRENT_TIMESTAMP（SQLite 不支持）
_RE_ON_UPDATE = re.compile(r'\s+ON\s+UPDATE\s+[^\s,)]+', re.IGNORECASE)
_RE_AFTER = re.compile(r'\s+AFTER\s+\S+', re.IGNORECASE)
_RE_ON_DUP_KEY = re.compile(
    r'\s+ON\s+DUPLICATE\s+KEY\s+UPDATE\s+(.+?)(?=\s*;|\s*$)',
    re.IGNORECASE | re.DOTALL
)
# ENUM('a','b',...)：支持嵌套引号和逗号，匹配到对应的右括号
_RE_ENUM = re.compile(r'\bENUM\s*\(([^)]*(?:\([^)]*\)[^)]*)*)\)', re.IGNORECASE)
# 匹配 INDEX idx_name (col1, col2) / KEY idx_name (col) / UNIQUE KEY uk (col)
# PRIMARY / FULLTEXT / SPATIAL 不处理（PRIMARY 是主键不走这里，全文索引语法不同）
# UNIQUE 必须一并匹配：它的列超长时同样会触发 1071，
# 且注释里旧实现把它漏掉，导致 api_tokens.uk_token(token VARCHAR(512)) 在
# MySQL 5.5/5.6 上直接建表失败。
_RE_MYSQL_INDEX = re.compile(
    r"^(?:UNIQUE\s+)?(INDEX|KEY)\s+(?:`?[A-Za-z0-9_]+`?\s+)?\(([^)]*)\)\s*$",
    re.IGNORECASE)

# 廉价预判：SQL 里是否可能出现需要翻译的 MySQL 专有函数。
# 不区分 DDL/DML，因为 SELECT 里同样会出现；命中后才跑字符级扫描。
_RE_MAYBE_MYSQL_FUNC = re.compile(
    r'\b(?:IF|IFNULL|UNIX_TIMESTAMP)\s*\(', re.IGNORECASE)


def _strip_mysql_ddl_syntax(sql: str) -> str:
    """
    将 MySQL DDL 语法翻译为 SQLite 兼容语法
    只做语法层面的清理，不做逻辑转换
    """
    # 【踩坑保佑】v1.8.5-alpha.0 方言清理误伤
    # 坑：KEY CHECK被当索引删掉，DDL静默失败  →  _strip_mysql_ddl_syntax
    #                    _ooOoo_
    #                   o8888888o
    #                   88" . "88
    #                   (| -_- |)
    #                    O\ = /O
    #                ____/`---'\____
    #              .   ' \\| |// `.
    #               / \\||| : |||// \
    #             / _||||| -:- |||||- \
    #               | | \\\ - /// | |
    #             | \_| ''\---/'' | |
    #              \ .-\__ `-` ___/-. /
    #           ___`. .' /--.--\ `. . __
    #        ."" '< `.___\_<|>_/___.' >'"".
    #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
    #         \ \ `-. \_ __\ /__ _/ .-` / /
    # ======`-.____`-.___\_____/___.-`____.-'======
    #                    `=---='
    #
    # .............................................
    #          佛祖保佑             永无BUG

    sql = _RE_ENGINE.sub('', sql)
    sql = _RE_CHARSET.sub('', sql)
    sql = _RE_COLLATE.sub('', sql)
    sql = _RE_COLLATE_INLINE.sub('', sql)
    sql = _RE_COMMENT.sub('', sql)
    sql = _RE_COMMENT_EQ.sub('', sql)
    sql = _RE_AUTO_INCREMENT.sub('', sql)
    sql = _RE_UNSIGNED.sub('', sql)
    sql = _RE_FOR_UPDATE.sub('', sql)
    sql = _RE_ON_UPDATE.sub('', sql)
    sql = _RE_AFTER.sub('', sql)

    # 数据类型转换
    sql = re.sub(r'\bBIGINT\b', 'INTEGER', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bTINYINT\s*\(\d+\)', 'INTEGER', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bTINYINT\b', 'INTEGER', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bVARCHAR\s*\(\d+\)', 'TEXT', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bDATETIME\b', 'TEXT', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bTIMESTAMP\b', 'TEXT', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bINT\s*\(\d+\)', 'INTEGER', sql, flags=re.IGNORECASE)
    sql = re.sub(r'(?<!\w)INT(?!\s*\(\d+)(?!\w)', 'INTEGER', sql, flags=re.IGNORECASE)
    # ENUM(...) → TEXT（支持嵌套括号）
    sql = _RE_ENUM.sub('TEXT', sql)

    # UNIQUE KEY uk_name (col) → UNIQUE(col)
    sql = re.sub(
        r'\bUNIQUE\s+KEY\s+\S+\s+\(([^)]+)\)',
        r'UNIQUE(\1)',
        sql, flags=re.IGNORECASE
    )
    # INDEX idx_name (col) → 删除（SQLite DDL 内不建索引）
    sql = re.sub(
        r',?\s*\bINDEX\s+\S+\s*\([^)]+\)',
        '',
        sql, flags=re.IGNORECASE
    )
    # KEY uk_name (col) → 删除
    # 注意：只匹配「KEY + 索引名 + (列)」这种 MySQL 索引定义；`PRIMARY KEY`、
    # `KEY CHECK (...)`（列级 CHECK 约束）等不能吃——否则会把
    # `id INTEGER PRIMARY KEY CHECK (id = 1)` 截成 `id INTEGER PRIMARY`（语法错误）。
    sql = re.sub(
        r',?\s*\bKEY\s+(?!(?:CHECK|CONSTRAINT|PRIMARY|FOREIGN|UNIQUE)\b)\S+\s*\([^)]+\)',
        '',
        sql, flags=re.IGNORECASE
    )

    # 清理多余的逗号（在 ) 前面）
    sql = re.sub(r',\s*\)', ')', sql)

    # 清理多余空格
    sql = re.sub(r'\s+', ' ', sql).strip()

    return sql


def _convert_placeholders(sql: str) -> str:
    """将 %s 占位符转换为 ?（SQLite 用）。

    必须只替换**字符串字面量之外**的 %s。历史实现是无脑 `sql.replace('%s','?')`，
    而 `_translate_mysql_funcs` 会把 UNIX_TIMESTAMP() 翻成 `strftime('%s','now')` ——
    其中的 '%s' 是 SQLite 的**格式符**，被一起替换后变成 `strftime('?','now')`，
    返回值是字面量 '?' 而不是 unix 秒。表现为：默认权限组的 created_at 被写成
    字符串 '?'，SQLite 松散类型照收，不报错也就一直没被发现。
    """
    # 【踩坑保佑】v1.21.0 占位符转换不认引号
    # 坑：%s被一并替换，返回值成字面量问号  →  _convert_placeholders
    #                    _ooOoo_
    #                   o8888888o
    #                   88" . "88
    #                   (| -_- |)
    #                    O\ = /O
    #                ____/`---'\____
    #              .   ' \\| |// `.
    #               / \\||| : |||// \
    #             / _||||| -:- |||||- \
    #               | | \\\ - /// | |
    #             | \_| ''\---/'' | |
    #              \ .-\__ `-` ___/-. /
    #           ___`. .' /--.--\ `. . __
    #        ."" '< `.___\_<|>_/___.' >'"".
    #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
    #         \ \ `-. \_ __\ /__ _/ .-` / /
    # ======`-.____`-.___\_____/___.-`____.-'======
    #                    `=---='
    #
    # .............................................
    #          佛祖保佑             永无BUG

    if '%s' not in sql:
        return sql
    out = []
    i, n = 0, len(sql)
    in_str = False
    quote = None
    while i < n:
        ch = sql[i]
        if in_str:
            out.append(ch)
            if ch == '\\' and i + 1 < n:        # 反斜杠转义（MySQL 风格）
                out.append(sql[i + 1])
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:   # '' 转义
                    out.append(sql[i + 1])
                    i += 2
                    continue
                in_str = False
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_str = True
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == '%' and i + 1 < n and sql[i + 1] == 's':
            out.append('?')
            i += 2
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


# 单行索引定义：INDEX/KEY name (cols) / UNIQUE KEY name (cols)
_RE_INDEX_DECL = re.compile(
    r'^\s*(?:(UNIQUE)\s+)?(?:KEY|INDEX)\s+`?([A-Za-z0-9_]+)`?\s*\(([^)]*)\)',
    re.IGNORECASE)


def extract_plain_indexes(sql_text: str) -> list:
    """从建表 SQL 中抽取**普通索引**定义，供 SQLite 建表后补建。

    为什么需要：SQLite 的 CREATE TABLE 不支持内联 `KEY idx (col)` / `INDEX idx (col)`，
    `_strip_mysql_ddl_syntax` 只能把它们整行删掉。删语法不等于不需要索引——
    不补建的话 SQLite 部署**一个二级索引都没有**，所有按非唯一列过滤的查询退化成
    全表扫描（实测 users 表 20 万行：is_blacklist 过滤 99.34ms → 建索引后 0.74ms，
    last_active_at 范围 134.49ms → 9.71ms）。

    UNIQUE 不在此列：`_strip_mysql_ddl_syntax` 已把它改写成表级 `UNIQUE(cols)`
    约束，SQLite 会自动建 sqlite_autoindex，无需重复补建。

    :return: [(table, index_name, [col, ...]), ...]，按出现顺序
    """
    # 【踩坑保佑】v1.21.0 SQLite 索引丢失
    # 坑：索引名库内全局唯一，跨表重名致静默失败  →  extract_plain_indexes
    #                    _ooOoo_
    #                   o8888888o
    #                   88" . "88
    #                   (| -_- |)
    #                    O\ = /O
    #                ____/`---'\____
    #              .   ' \\| |// `.
    #               / \\||| : |||// \
    #             / _||||| -:- |||||- \
    #               | | \\\ - /// | |
    #             | \_| ''\---/'' | |
    #              \ .-\__ `-` ___/-. /
    #           ___`. .' /--.--\ `. . __
    #        ."" '< `.___\_<|>_/___.' >'"".
    #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
    #         \ \ `-. \_ __\ /__ _/ .-` / /
    # ======`-.____`-.___\_____/___.-`____.-'======
    #                    `=---='
    #
    # .............................................
    #          佛祖保佑             永无BUG

    out = []
    cur_table = None
    for raw in sql_text.splitlines():
        line = raw.strip()
        if not line or line.startswith('--'):
            continue
        m = re.search(r'CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+`?([A-Za-z0-9_]+)`?',
                      line, re.IGNORECASE)
        if m:
            cur_table = m.group(1)
            continue
        if cur_table is None:
            continue
        if re.match(r'^\)', line):          # 表定义结束
            cur_table = None
            continue
        m2 = _RE_INDEX_DECL.match(line)
        if not m2 or m2.group(1):           # 跳过 UNIQUE（已由约束承担）
            continue
        cols = []
        for c in m2.group(3).split(','):
            c = c.strip().strip('`')
            if not c:
                continue
            cols.append(c.split('(')[0].strip().strip('`'))
        if cols:
            out.append((cur_table, m2.group(2), cols))
    return out


def build_sqlite_index_ddl(indexes: list) -> list:
    """把抽取出的索引声明转成 SQLite 的 CREATE INDEX IF NOT EXISTS 语句。

    索引名统一改写为 `{表名}_{原索引名}`。原因：init.sql 是 MySQL 方言，索引名
    只需**表内**唯一，所以 idx_active / idx_blacklist / idx_user 等在多张表重复
    出现（37 个声明只有 21 个不同名字）；而 SQLite 的索引名是**库内全局唯一**，
    直接用原名会让第一个之后的同名声明的 CREATE INDEX IF NOT EXISTS 全部变成
    空操作，实际只建出 21 个索引。加表名前缀后既唯一又稳定。
    """
    out = []
    for table, name, cols in indexes:
        col_sql = ', '.join(f'"{c}"' for c in cols)
        out.append(f'CREATE INDEX IF NOT EXISTS "{table}_{name}" ON "{table}" ({col_sql})')
    return out


def _translate_sql_for_mysql(sql: str, max_key_bytes: int = 3072) -> str:
    """SQLite 方言 DDL → MySQL 兼容（防御：AUTOINCREMENT → AUTO_INCREMENT；长列索引 → 前缀索引）"""
    # 【踩坑保佑】数据库 插件建表方言
    # 坑：规避MySQL的1170和1064错误  →  _translate_sql_for_mysql
    #                    _ooOoo_
    #                   o8888888o
    #                   88" . "88
    #                   (| -_- |)
    #                    O\ = /O
    #                ____/`---'\____
    #              .   ' \\| |// `.
    #               / \\||| : |||// \
    #             / _||||| -:- |||||- \
    #               | | \\\ - /// | |
    #             | \_| ''\---/'' | |
    #              \ .-\__ `-` ___/-. /
    #           ___`. .' /--.--\ `. . __
    #        ."" '< `.___\_<|>_/___.' >'"".
    #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
    #         \ \ `-. \_ __\ /__ _/ .-` / /
    # ======`-.____`-.___\_____/___.-`____.-'======
    #                    `=---='
    #
    # .............................................
    #          佛祖保佑             永无BUG

    sql = sql.replace('AUTOINCREMENT', 'AUTO_INCREMENT')
    return _mysql_prefix_indexes(sql, max_key_bytes=max_key_bytes)


# 各版本 InnoDB 单列/组合索引的字节上限：
#   5.5 / 5.6         767  （COMPACT 行格式，innodb_large_prefix 默认 OFF）
#   5.7 / 8.0        3072  （DYNAMIC 行格式，utf8mb4 下约 768 字符）
MYSQL_KEY_LIMIT_OLD = 767
MYSQL_KEY_LIMIT_MODERN = 3072


def _mysql_prefix_indexes(sql: str, max_key_bytes: int = MYSQL_KEY_LIMIT_MODERN) -> str:
    """
    MySQL DDL 兼容：索引列总字节数超过引擎上限时，自动把字符串列改写为前缀索引。

    两个触发场景：
      1. TEXT / LONGTEXT 列做索引 —— MySQL 报 1170（BLOB/TEXT column used in key
         specification without a key length），必须给长度；
      2. 组合索引的各列字节数之和超过 max_key_bytes —— MySQL 报 1071
         （Specified key was too long）。注意**必须按累计字节算**：单列 191 字符
         的旧判据救不了 `(target_type VARCHAR(50), target_name VARCHAR(200))`
         这种 1000 字节的组合索引。

    UNIQUE KEY 同样处理。前缀唯一索引会放宽唯一性语义（前缀相同的两行视为冲突），
    但这是 5.5/5.6 上唯一可行的方案；调用方如需精确唯一性应缩短列宽。

    :param max_key_bytes: 767 = MySQL 5.5/5.6；3072 = 5.7/8.0（默认）
    """
    if not sql.lstrip().upper().startswith('CREATE TABLE'):
        return sql

    # 定位列定义区（最外层括号）
    start = sql.find('(')
    if start < 0:
        return sql
    depth = 0
    in_str = False
    quote = None
    end = -1
    for i in range(start, len(sql)):
        c = sql[i]
        if not in_str and c in ("'", '"'):
            in_str, quote = True, c
        elif in_str:
            if c == quote:
                in_str, quote = False, None
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return sql
    body = sql[start + 1:end]

    # 解析列名 → 类型（跳过索引/约束行）
    col_types = {}
    for part in _split_top_level(body):
        m = re.match(r"^\s*`?(\w+)`?\s+(\w+(?:\([^)]*\))?)", part, re.IGNORECASE)
        if m and m.group(2).upper() not in ('INDEX', 'KEY', 'PRIMARY', 'UNIQUE',
                                            'CONSTRAINT', 'FULLTEXT', 'SPATIAL', 'CHECK'):
            col_types[m.group(1)] = m.group(2).upper()

    def _col_bytes(t: str):
        """返回 (kind, 字节数)。

        kind:
          'fixed'   —— 宽度已知，按字节累计；
          'text'    —— TEXT/BLOB 系列，必须给前缀长度（否则 1170）；
          'unknown' —— ENUM/SET/JSON/GEOMETRY 等，**不能**加前缀
                       （MySQL 对非字符串类型写前缀会报 1089），原样保留。
        """
        if not t:
            return ('unknown', 0)
        tu = t.upper()
        if tu.startswith(('TEXT', 'LONGTEXT', 'MEDIUMTEXT', 'TINYTEXT', 'BLOB')):
            return ('text', 0)
        m = re.match(r'(?:VAR)?CHAR\((\d+)\)', tu)
        if m:
            return ('fixed', int(m.group(1)) * 4)      # utf8mb4
        if tu.startswith('DECIMAL'):
            return ('fixed', 16)
        if tu.startswith(('ENUM(', 'SET(')):
            return ('fixed', 2)                        # 内部按整数存，不参与前缀
        if tu in ('INT', 'INTEGER', 'FLOAT'):
            return ('fixed', 4)
        if tu in ('BIGINT', 'DOUBLE', 'DATETIME', 'TIMESTAMP'):
            return ('fixed', 8)
        if tu in ('TINYINT', 'SMALLINT', 'MEDIUMINT'):
            return ('fixed', 2)
        if tu == 'DATE':
            return ('fixed', 3)
        return ('unknown', 0)

    def _fix_index(part: str) -> str:
        m = _RE_MYSQL_INDEX.match(part)
        if not m:
            return part
        idx_open = part.find('(', m.end(1))
        head = part[:idx_open + 1]
        cols_text = part[idx_open + 1:part.rfind(')')]
        used = 0
        fixed = []
        for col in cols_text.split(','):
            col = col.strip()
            name = col.split('(')[0].strip().strip('`')
            if '(' in col:
                # 已带前缀（col(191)）——计入预算，不再重复改写
                pm = re.search(r'\((\d+)\)', col)
                if pm:
                    used += int(pm.group(1)) * 4
                fixed.append(col)
                continue
            kind, nb = _col_bytes(col_types.get(name, ''))
            if kind == 'unknown':
                fixed.append(col)                      # 不碰
                continue
            if kind == 'text':
                room = (max_key_bytes - used) // 4
                if room > 0:
                    fixed.append(f"`{name}`({room})")
                    used += room * 4
                else:
                    fixed.append(col)
                continue
            if used + nb <= max_key_bytes:
                used += nb
                fixed.append(col)
            else:
                room = (max_key_bytes - used) // 4
                if room > 0:
                    fixed.append(f"`{name}`({room})")
                    used += room * 4
                else:
                    fixed.append(col)      # 预算已耗尽，保持原样交给 DB 报错
        return head + ', '.join(fixed) + ')'

    new_parts = [_fix_index(p) for p in _split_top_level(body)]
    new_body = ', '.join(new_parts)
    if new_body == body:
        return sql
    return sql[:start + 1] + new_body + sql[end:]


def _is_ddl_or_dml(sql: str) -> bool:
    """判断是否需要语法翻译（跳过前导注释行）DDL + INSERT/UPDATE/DELETE 都需要"""
    for line in sql.strip().splitlines():
        line = line.strip()
        if not line or line.startswith('--'):
            continue
        return line.upper().startswith((
            'CREATE', 'ALTER', 'DROP', 'INSERT', 'UPDATE', 'DELETE', 'REPLACE'
        ))
    return False


def _split_top_level(text: str) -> list:
    """
    按顶层逗号分割（忽略括号内与字符串内的逗号）
    用于解析 IF(cond, a, b) 的三个参数
    """
    parts = []
    depth = 0
    in_str = False
    quote = None
    cur = []
    for c in text:
        if not in_str and c in ("'", '"'):
            in_str = True
            quote = c
            cur.append(c)
            continue
        if in_str:
            cur.append(c)
            if c == quote:
                in_str = False
            continue
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        if c == ',' and depth == 0:
            parts.append(''.join(cur).strip())
            cur = []
            continue
        cur.append(c)
    if cur:
        parts.append(''.join(cur).strip())
    return parts


def _if_to_case(sql: str) -> str:
    """将 MySQL 的 IF(cond, a, b) 转换为 SQLite 兼容的 CASE WHEN cond THEN a ELSE b END

    支持嵌套括号与字符串字面量。嵌套 IF 需要多轮替换：单轮扫描时，外层 IF 的
    整个括号体被原样搬进 THEN 分支后指针会直接跳过，内层 IF 就留在那里没被翻译，
    SQLite 运行时仍报 `no such function: IF`。因此这里迭代到不再变化为止。
    """
    prev = None
    cur = sql
    for _ in range(8):          # 深度上限，防御病态输入
        if cur == prev:
            break
        prev = cur
        cur = _if_to_case_once(cur)
    return cur


def _if_to_case_once(sql: str) -> str:
    """单轮扫描替换（见 _if_to_case）。"""
    out = []
    i = 0
    n = len(sql)
    in_str = False
    quote = None
    while i < n:
        ch = sql[i]
        if not in_str and ch in ("'", '"'):
            in_str = True
            quote = ch
            out.append(ch)
            i += 1
            continue
        if in_str:
            out.append(ch)
            if ch == quote:
                in_str = False
            i += 1
            continue
        # 匹配单词边界后的 IF(
        if (sql[i:i + 2].upper() == 'IF'
                and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == '_'))):
            k = i + 2
            while k < n and sql[k] in ' \t\n\r':
                k += 1
            if k < n and sql[k] == '(':
                # 扫描到匹配的右括号
                j = k + 1
                depth = 1
                s_in_str = False
                s_quote = None
                while j < n and depth > 0:
                    c = sql[j]
                    if not s_in_str and c in ("'", '"'):
                        s_in_str = True
                        s_quote = c
                    elif s_in_str:
                        if c == s_quote:
                            s_in_str = False
                    elif c == '(':
                        depth += 1
                    elif c == ')':
                        depth -= 1
                    j += 1
                if depth == 0:
                    body = sql[k + 1:j - 1]
                    parts = _split_top_level(body)
                    if len(parts) == 3:
                        cond, a, b = parts
                        out.append(f"CASE WHEN {cond} THEN {a} ELSE {b} END")
                        i = j
                        continue
        out.append(ch)
        i += 1
    return ''.join(out)


def _translate_mysql_funcs(sql: str) -> str:
    """
    将 MySQL 专有函数转换为 SQLite 兼容语法：
    - IF(cond, a, b) → CASE WHEN cond THEN a ELSE b END
    - IFNULL(a, b)   → COALESCE(a, b)
    - UNIX_TIMESTAMP() → strftime('%s','now')（SQLite 无此函数，不翻译会直接报
      "no such function: UNIX_TIMESTAMP"，而这类种子/时间戳语句一旦失败就会被
      上层 try/except 吞掉，表现为"初始化成功但没有默认权限组"）
    - NOW()          → 由 execute/insert 的 _replace_now 处理
    """
    # 【踩坑保佑】v1.8.9 UNIX_TIMESTAMP() 种子失败
    # 坑：SQLite下no such function，默认权限组插入被吞  →  _translate_mysql_funcs
    #                    _ooOoo_
    #                   o8888888o
    #                   88" . "88
    #                   (| -_- |)
    #                    O\ = /O
    #                ____/`---'\____
    #              .   ' \\| |// `.
    #               / \\||| : |||// \
    #             / _||||| -:- |||||- \
    #               | | \\\ - /// | |
    #             | \_| ''\---/'' | |
    #              \ .-\__ `-` ___/-. /
    #           ___`. .' /--.--\ `. . __
    #        ."" '< `.___\_<|>_/___.' >'"".
    #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
    #         \ \ `-. \_ __\ /__ _/ .-` / /
    # ======`-.____`-.___\_____/___.-`____.-'======
    #                    `=---='
    #
    # .............................................
    #          佛祖保佑             永无BUG

    # IFNULL 参数简单，用正则即可
    sql = re.sub(
        r'\bIFNULL\s*\(\s*([^,()]+)\s*,\s*([^,()]+)\s*\)',
        r'COALESCE(\1, \2)',
        sql, flags=re.IGNORECASE
    )
    # UNIX_TIMESTAMP() / UNIX_TIMESTAMP(NOW()) → 整数秒
    sql = re.sub(
        r'\bUNIX_TIMESTAMP\s*\(\s*\)',
        "strftime('%s','now')", sql, flags=re.IGNORECASE)
    sql = re.sub(
        r'\bUNIX_TIMESTAMP\s*\(\s*NOW\s*\(\s*\)\s*\)',
        "strftime('%s','now')", sql, flags=re.IGNORECASE)
    return _if_to_case(sql)


def _on_duplicate_to_sqlite(sql: str) -> str:
    """
    将 MySQL 的 INSERT ... ON DUPLICATE KEY UPDATE 转换为
    SQLite 的 INSERT ... ON CONFLICT(...) DO UPDATE SET ...
    """
    m = _RE_ON_DUP_KEY.search(sql)
    if not m:
        return sql

    update_clause = m.group(1)
    # 将 VALUES(col) 替换为 EXCLUDED.col
    update_clause = re.sub(r'VALUES\((\w+)\)', r'EXCLUDED.\1', update_clause)
    # 替换为 SQLite 语法
    sql = _RE_ON_DUP_KEY.sub(f' ON CONFLICT DO UPDATE SET {update_clause}', sql)

    return sql


def _replace_now(sql: str, params: tuple = None) -> tuple:
    """
    将 SQL 中的 NOW() 替换为 ?，并按位置插入当前时间参数。
    SQLite 模式专用：NOW() 可能出现在语句中间（如 SET token_created_at = NOW(), ... WHERE id = %s），
    必须按占位符出现顺序与原参数交错插入，否则参数错位。
    """
    import datetime
    if 'NOW()' not in sql.upper():
        return sql, params

    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 从左到右扫描：%s → 原参数，NOW() → 时间参数，交错生成
    tokens = re.split(r'(%s|NOW\(\))', sql, flags=re.IGNORECASE)
    new_sql_parts = []
    new_params = []
    param_iter = iter(params) if params else iter(())

    for tok in tokens:
        if not tok:
            continue
        if tok == '%s':
            try:
                new_params.append(next(param_iter))
            except StopIteration:
                new_params.append(None)
            new_sql_parts.append('?')
        elif tok.upper() == 'NOW()':
            new_params.append(now_str)
            new_sql_parts.append('?')
        else:
            new_sql_parts.append(tok)

    return ''.join(new_sql_parts), tuple(new_params)


def _translate_sql_for_sqlite(sql: str) -> str:
    """完整翻译 SQL 供 SQLite 使用：
    1. DDL 语法清理
    2. MySQL 专有函数（IF/IFNULL/UNIX_TIMESTAMP）→ SQLite 兼容（含 SELECT）
    3. ON DUPLICATE KEY UPDATE → ON CONFLICT DO UPDATE
    4. INSERT IGNORE → INSERT OR IGNORE
    5. NOW() → 由调用方处理参数
    6. %s → ?
    所有需要翻译的 SQL（DDL/DML）都走这个函数，统一入口
    """
    # 【踩坑保佑】v1.21.0 SELECT 函数漏翻译
    # 坑：NOW()/IFNULL()原样发SQLite直接语法错  →  _translate_sql_for_sqlite
    #                    _ooOoo_
    #                   o8888888o
    #                   88" . "88
    #                   (| -_- |)
    #                    O\ = /O
    #                ____/`---'\____
    #              .   ' \\| |// `.
    #               / \\||| : |||// \
    #             / _||||| -:- |||||- \
    #               | | \\\ - /// | |
    #             | \_| ''\---/'' | |
    #              \ .-\__ `-` ___/-. /
    #           ___`. .' /--.--\ `. . __
    #        ."" '< `.___\_<|>_/___.' >'"".
    #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
    #         \ \ `-. \_ __\ /__ _/ .-` / /
    # ======`-.____`-.___\_____/___.-`____.-'======
    #                    `=---='
    #
    # .............................................
    #          佛祖保佑             永无BUG

    needs_translate = _is_ddl_or_dml(sql)

    if needs_translate:
        sql = _strip_mysql_ddl_syntax(sql)

    # MySQL 专有函数 → SQLite 兼容（IF / IFNULL / UNIX_TIMESTAMP）
    #
    # 这一步**不能**只在 DDL/DML 分支里做：`SELECT IF(cond, a, b)` 是查询语句，
    # `_is_ddl_or_dml` 判 False 就整段跳过，SQLite 上没有 IF() 这个函数，
    # 运行时直接 `no such function: IF`。能力暴露给插件后（插件可以写任意 SELECT），
    # 这类语句只在 MySQL 部署下能跑、SQLite 部署必挂，属于纯方言陷阱。
    # 用正则先做一次廉价的命中判断，避免给每条高频 SELECT 都跑字符级扫描。
    if _RE_MAYBE_MYSQL_FUNC.search(sql):
        sql = _translate_mysql_funcs(sql)

    if needs_translate:
        # ON DUPLICATE KEY UPDATE → ON CONFLICT DO UPDATE SET
        if 'ON DUPLICATE KEY' in sql.upper() and 'INSERT' in sql.upper():
            sql = _on_duplicate_to_sqlite(sql)

        # INSERT IGNORE → INSERT OR IGNORE
        sql = re.sub(r'\bINSERT\s+IGNORE\b', 'INSERT OR IGNORE', sql, flags=re.IGNORECASE)

    # %s → ?（所有 SQL 都需要转）
    sql = _convert_placeholders(sql)

    return sql
