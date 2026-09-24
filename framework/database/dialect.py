"""
SQL 方言翻译层（纯函数，无连接、无状态）

把统一书写格式的 SQL 翻译成目标方言，让插件只写一套 SQL（%s 占位符 + MySQL 方言 DDL）：
- → SQLite：清理 MySQL 专有语法（ENGINE/CHARSET/COMMENT/AUTO_INCREMENT…）、
  函数与子句改写（IF/IFNULL/ON DUPLICATE KEY/INSERT IGNORE/NOW()）、%s → ?
- → MySQL：防御性反向翻译（AUTOINCREMENT → AUTO_INCREMENT）、
  长文本列索引自动改前缀索引 col(191)

适用边界提醒：方言层只是让同一套 SQL 双跑，不代表 SQLite 能扛大环境——
SQLite 单写多读，仅适合小环境与开发环境；多群/高并发/大环境请用 MySQL
（见 docs/advanced/database.md）。
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
# 匹配 INDEX idx_name (col1, col2) / KEY idx_name (col)（普通索引；PRIMARY/UNIQUE/FULLTEXT 不处理）
_RE_MYSQL_INDEX = re.compile(
    r"^(INDEX|KEY)\s+(?:`?[A-Za-z0-9_]+`?\s+)?\(([^)]*)\)\s*$",
    re.IGNORECASE)


def _strip_mysql_ddl_syntax(sql: str) -> str:
    """
    将 MySQL DDL 语法翻译为 SQLite 兼容语法
    只做语法层面的清理，不做逻辑转换
    """
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
    sql = re.sub(
        r',?\s*\bKEY\s+\S+\s*\([^)]+\)',
        '',
        sql, flags=re.IGNORECASE
    )

    # 清理多余的逗号（在 ) 前面）
    sql = re.sub(r',\s*\)', ')', sql)

    # 清理多余空格
    sql = re.sub(r'\s+', ' ', sql).strip()

    return sql


def _convert_placeholders(sql: str) -> str:
    """将 %s 占位符转换为 ?（SQLite 用）"""
    return sql.replace('%s', '?')


def _translate_sql_for_mysql(sql: str) -> str:
    """SQLite 方言 DDL → MySQL 兼容（防御：AUTOINCREMENT → AUTO_INCREMENT；长列索引 → 前缀索引）"""
    sql = sql.replace('AUTOINCREMENT', 'AUTO_INCREMENT')
    return _mysql_prefix_indexes(sql)


def _mysql_prefix_indexes(sql: str) -> str:
    """
    MySQL DDL 兼容：被索引的列若是 TEXT 或 VARCHAR 长度 > 191，
    自动改写为前缀索引 `col`(191)，避免错误 1170（BLOB/TEXT column used in key specification）
    与 MySQL 5.7+ 的 Specified key was too long。
    仅处理 CREATE TABLE 语句；已有前缀（col(191)）的列不重复改写。
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

    def _need_prefix(col: str) -> bool:
        t = col_types.get(col)
        if not t:
            return False
        if t.startswith('TEXT') or t.startswith('LONGTEXT') or t.startswith('MEDIUMTEXT'):
            return True
        m = re.match(r'VARCHAR\((\d+)\)', t)
        return bool(m) and int(m.group(1)) > 191

    def _fix_index(part: str) -> str:
        m = _RE_MYSQL_INDEX.match(part)
        if not m:
            return part
        idx_open = part.find('(', m.end(1))
        head = part[:idx_open + 1]
        cols_text = part[idx_open + 1:part.rfind(')')]
        fixed = []
        for col in cols_text.split(','):
            col = col.strip()
            name = col.split('(')[0].strip().strip('`')
            if '(' not in col and _need_prefix(name):
                fixed.append(f"`{name}`(191)")
            else:
                fixed.append(col)
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
    """
    将 MySQL 的 IF(cond, a, b) 转换为 SQLite 兼容的 CASE WHEN cond THEN a ELSE b END
    支持嵌套括号与字符串字面量，IFNULL 单独用正则处理
    """
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
    - NOW()          → 由 execute/insert 的 _replace_now 处理
    """
    # IFNULL 参数简单，用正则即可
    sql = re.sub(
        r'\bIFNULL\s*\(\s*([^,()]+)\s*,\s*([^,()]+)\s*\)',
        r'COALESCE(\1, \2)',
        sql, flags=re.IGNORECASE
    )
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
    """
    完整翻译 SQL 供 SQLite 使用：
    1. DDL 语法清理
    2. MySQL 专有函数（IF/IFNULL）→ SQLite 兼容
    3. ON DUPLICATE KEY UPDATE → ON CONFLICT DO UPDATE
    4. INSERT IGNORE → INSERT OR IGNORE
    5. NOW() → 由调用方处理参数
    6. %s → ?
    所有需要翻译的 SQL（DDL/DML）都走这个函数，统一入口
    """
    needs_translate = _is_ddl_or_dml(sql)

    if needs_translate:
        sql = _strip_mysql_ddl_syntax(sql)

        # MySQL 专有函数 → SQLite 兼容（IF / IFNULL）
        sql = _translate_mysql_funcs(sql)

        # ON DUPLICATE KEY UPDATE → ON CONFLICT DO UPDATE SET
        if 'ON DUPLICATE KEY' in sql.upper() and 'INSERT' in sql.upper():
            sql = _on_duplicate_to_sqlite(sql)

        # INSERT IGNORE → INSERT OR IGNORE
        sql = re.sub(r'\bINSERT\s+IGNORE\b', 'INSERT OR IGNORE', sql, flags=re.IGNORECASE)

    # %s → ?（所有 SQL 都需要转）
    sql = _convert_placeholders(sql)

    return sql
