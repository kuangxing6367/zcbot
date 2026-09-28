# -*- coding: utf-8 -*-
"""
调试模式 SQL 模拟引擎（低性能模式 / 本地模拟 SQL）

定位：用户在 config 中显式配置 database.type: debug，即可在完全不需要
SQLite/MySQL 的本机环境模拟 SQL 语义运行框架 —— 查询返回符合语义的数据
（含过滤、聚合、排序、分页），写入真实落盘到 JSON 行集文件。

能力边界（对齐内核与插件实际使用的单表 SQL 形态，见 tests 摸底）：
    SELECT   * / 列 / COUNT(*) / DISTINCT col / DATE(col) AS d /
             WHERE(= <> != > < >= <= AND OR IN NOT IN IS NULL) /
             参数(%s、?) / 字面量 / GROUP BY / ORDER BY / LIMIT / OFFSET
    INSERT[IGNORE] INTO t (col,...) VALUES (v,...)[, (v,...)]
    UPDATE t SET a=.., b=NOW(), c=c+1, d=d-1 WHERE ..
    DELETE FROM t [WHERE ..]（无 WHERE 全删）
    CREATE TABLE [IF NOT EXISTS] t (ddl...)（注册列 schema）
    DROP TABLE t
不支持的语法（JOIN、子查询、复杂表达式、ALTER 等）安全兜底：
    query → []、execute → 0，绝不抛异常，保证框架可启动。

行集持久化：<dir>/sql/<table>.json，每表一文件，原子写（临时文件+rename）：
    {"schema": [{"name": "id", "type": "INTEGER"}, ...],
     "rows":   [{"id": 1, ...}, ...],
     "next_id": 2}
主键列名约定为 id（自增）；无 id 列的表不做自增。

仅开发/调试使用；生产环境请配置 database.type: sqlite / mysql。
"""
import json
import logging
import os
import re
import threading
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from .file_store import FileStore, _ensure_dir

logger = logging.getLogger('zcbot')

# ── 迷你 SQL tokenizer ──────────────────────────────────────

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>--[^\n]*|/\*.*?\*/)
  | (?P<str>'(?:[^']|'')*')
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<ident>`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)
  | (?P<param>%s|\?)
  | (?P<op><=|>=|<>|!=|=|<|>|\(|\)|,|;|\*|\+|-)
""", re.VERBOSE | re.DOTALL)

# SQL 关键字（大小写不敏感，tokenize 时归一为大写比较）
_RESERVED = {
    'SELECT', 'FROM', 'WHERE', 'GROUP', 'BY', 'ORDER', 'LIMIT', 'OFFSET',
    'INSERT', 'INTO', 'VALUES', 'IGNORE', 'UPDATE', 'SET', 'DELETE',
    'CREATE', 'TABLE', 'IF', 'NOT', 'EXISTS', 'AND', 'OR', 'IN', 'IS',
    'NULL', 'AS', 'DISTINCT', 'COUNT', 'DATE', 'NOW', 'ASC', 'DESC',
    'LIKE', 'PRIMARY', 'KEY', 'UNIQUE', 'AUTO_INCREMENT', 'COMMENT',
    'DEFAULT', 'ENGINE', 'CHARSET', 'COLLATE', 'ON', 'REFERENCES', 'DROP',
    'ALTER', 'ADD', 'COLUMN', 'MODIFY', 'CHANGE', 'RENAME', 'TO',
    'INTEGER', 'INT', 'TEXT', 'VARCHAR', 'BIGINT', 'BOOLEAN', 'TIMESTAMP',
    'DATETIME', 'DECIMAL', 'FLOAT', 'DOUBLE', 'REAL', 'BLOB', 'CHAR',
    'CONSTRAINT', 'FOREIGN', 'INDEX', 'ENUM', 'JSON', 'MEDIUMTEXT',
    'LONGTEXT', 'SMALLINT', 'TINYINT', 'UNSIGNED', 'ZEROFILL',
}


class _Token:
    __slots__ = ('kind', 'value', 'upper')

    def __init__(self, kind, value):
        self.kind = kind
        self.value = value
        self.upper = value.upper() if kind in ('ident', 'kw') else value

    def __repr__(self):
        return f"T({self.kind},{self.value!r})"


def _tokenize(sql: str):
    """把 SQL 切成 token 流（跳过空白与注释）"""
    tokens = []
    pos = 0
    while pos < len(sql):
        m = _TOKEN_RE.match(sql, pos)
        if not m:
            # 未匹配字符（如 % 取模、反引号内点号）：跳过，保守容错
            pos += 1
            continue
        pos = m.end()
        kind = m.lastgroup
        text = m.group()
        if kind in ('ws', 'comment'):
            continue
        if kind == 'ident' and text.upper() in _RESERVED:
            kind = 'kw'
        tokens.append(_Token(kind, text[1:-1] if kind == 'str' else text))
    return tokens


def _unquote_ident(tok):
    """去掉反引号包裹的标识符 token/名称字符串"""
    v = tok.value if isinstance(tok, _Token) else str(tok)
    if v.startswith('`') and v.endswith('`'):
        return v[1:-1]
    return v


class _UnsupportedSQL(Exception):
    """本引擎不支持的 SQL（上级捕获后安全返回默认值）"""


# ── 调试模式 SQL 模拟引擎 ──────────────────────────────────

class SqlSimEngine(FileStore):
    """调试模式 SQL 模拟引擎（低性能本地模拟，行集 JSON 落盘）"""

    db_type = 'debug'
    degraded = False        # 用户显式配置的调试模式，不是故障降级
    debug_mode = True       # 标志：本地 SQL 模拟（低性能模式）

    def __init__(self, config: Optional[dict] = None):
        super().__init__(config or {})
        self._sql_dir = os.path.join(self._dir, 'sql')
        try:
            _ensure_dir(os.path.join(self._sql_dir, '.keep'))
        except Exception as e:
            logger.error(f"SQL 模拟目录创建失败（{self._sql_dir}）: {e}")
        logger.info(
            f"调试模式已启用（低性能本地 SQL 模拟）: {self._sql_dir} "
            f"（行集 JSON 落盘，仅开发调试使用）"
        )

    # ── 行集表文件读写 ────────────────────────────────────

    def _tbl_path(self, table: str) -> str:
        return os.path.join(self._sql_dir, f"{table}.json")

    def _read_tbl(self, table: str) -> dict:
        try:
            if not os.path.isfile(self._tbl_path(table)):
                return {'schema': [], 'rows': [], 'next_id': 1}
            with open(self._tbl_path(table), 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {'schema': [], 'rows': [], 'next_id': 1}
            data.setdefault('schema', [])
            data.setdefault('rows', [])
            data.setdefault('next_id', 1)
            return data
        except Exception as e:
            logger.error(f"SQL 模拟表读取失败 {table}: {e}")
            return {'schema': [], 'rows': [], 'next_id': 1}

    def _write_tbl(self, table: str, data: dict):
        path = self._tbl_path(table)
        tmp = f"{path}.tmp.{os.getpid()}"
        try:
            _ensure_dir(path)
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except Exception as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            raise

    def _drop_tbl(self, table: str):
        try:
            os.remove(self._tbl_path(table))
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"删除模拟表 {table} 失败: {e}")

    # ── SQL 兼容接口（与 Database / FileStore 同构）────────

    def query(self, sql: str, params=None) -> list:
        """SELECT → list[dict]；不支持的 SQL 安全返回 []"""
        try:
            return self._exec(sql, params)
        except _UnsupportedSQL:
            logger.debug(f"SQL 模拟不支持: {sql[:120]}")
            return []
        except Exception as e:
            logger.warning(f"SQL 模拟查询失败: {e} | sql={sql[:120]}")
            return []

    def query_one(self, sql: str, params=None):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params=None) -> int:
        try:
            return self._exec(sql, params)
        except _UnsupportedSQL:
            logger.debug(f"SQL 模拟不支持: {sql[:120]}")
            return 0
        except Exception as e:
            logger.warning(f"SQL 模拟执行失败: {e} | sql={sql[:120]}")
            return 0

    def execute_many(self, sql: str, params_list: list) -> int:
        total = 0
        for params in params_list or []:
            total += self.execute(sql, params)
        return total

    def insert(self, sql: str, params=None) -> int:
        """INSERT → 返回自增 id（无 id 列返回 0）"""
        try:
            return self._exec(sql, params, want_id=True)
        except _UnsupportedSQL:
            logger.debug(f"SQL 模拟不支持: {sql[:120]}")
            return 0
        except Exception as e:
            logger.warning(f"SQL 模拟插入失败: {e} | sql={sql[:120]}")
            return 0

    def scalar(self, sql: str, params=None):
        row = self.query_one(sql, params)
        if not row:
            return None
        return next(iter(row.values()), None)

    def count(self, sql: str, params=None) -> int:
        v = self.scalar(sql, params)
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    def exists(self, sql: str, params=None) -> bool:
        return self.query_one(sql, params) is not None

    def table_exists(self, table_name: str) -> bool:
        return os.path.isfile(self._tbl_path(_unquote_ident(table_name)))

    def table_info(self, table_name: str) -> list:
        data = self._read_tbl(_unquote_ident(table_name))
        return [dict(c) for c in data.get('schema', [])]

    def table_has_column(self, table_name: str, column_name: str) -> bool:
        return any(c['name'] == column_name for c in self.table_info(table_name))

    def get_connection(self):
        raise NotImplementedError(
            "调试模式 SQL 模拟不支持原始数据库连接，请配置真实数据库"
        )

    def transaction(self, conn=None):
        """调试模式无真实事务：no-op 上下文（块内操作即时落盘）"""

        class _NoopTx:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _NoopTx()

    @property
    def pool_status(self) -> dict:
        return {
            'type': 'debug',
            'path': self._sql_dir,
            'degraded': False,
            'debug_mode': True,
            'note': '本地 SQL 模拟（低性能模式），数据以 JSON 行集落盘',
        }

    def close(self):
        logger.debug("SQL 模拟引擎 close()：行集已落盘，无需关闭")

    def __repr__(self):
        return f"<SqlSimEngine dir={self._sql_dir}>"

    # ── 参数绑定与执行分派 ────────────────────────────────

    @staticmethod
    def _bind(tokens, params):
        """按占位符出现顺序绑定参数 → 替换为 val token"""
        params = list(params or ())
        out = []
        idx = 0
        for t in tokens:
            if t.kind == 'param':
                if idx >= len(params):
                    raise _UnsupportedSQL("参数个数不足")
                out.append(_Token('val', params[idx]))
                idx += 1
            else:
                out.append(t)
        return out

    @staticmethod
    def _bind_segments(segments, params):
        """按 SQL 顺序绑定多段 token 后切回各段（保证占位符跨段顺序正确）"""
        flat = []
        for seg in segments:
            flat.extend(seg)
        bound = SqlSimEngine._bind(flat, params)
        out = []
        i = 0
        for seg in segments:
            out.append(bound[i:i + len(seg)])
            i += len(seg)
        return out

    def _exec(self, sql: str, params, want_id=False) -> Any:
        sql = sql.strip()
        if not sql:
            return 0
        head = sql[:7].upper()
        with self._lock:
            if head.startswith('SELECT'):
                return self._exec_select(sql, params)
            if head.startswith('INSERT'):
                return self._exec_insert(sql, params, want_id)
            if head.startswith('UPDATE'):
                return self._exec_update(sql, params)
            if head.startswith('DELETE'):
                return self._exec_delete(sql, params)
            if head.startswith('CREATE'):
                return self._exec_create(sql)
            if head.startswith('DROP'):
                return self._exec_drop(sql)
            if head.startswith('ALTER'):
                return 0  # 迁移用 ALTER：调试模式安全返回 0
            raise _UnsupportedSQL(f"未知语句类型: {sql[:40]}")

    # ── SELECT ────────────────────────────────────────────

    def _exec_select(self, sql: str, params):
        tokens = _tokenize(sql)
        if not tokens or tokens[0].upper != 'SELECT':
            raise _UnsupportedSQL("SELECT 解析失败")

        # 子查询拦截：SELECT 出现超过 1 次视为子查询（本引擎不支持，安全兜底）
        if sum(1 for t in tokens if t.kind == 'kw' and t.upper == 'SELECT') > 1:
            raise _UnsupportedSQL("SELECT 含子查询，本引擎不支持")

        # 定位各子句（最外层）
        n = len(tokens)
        from_i = where_i = group_i = order_i = limit_i = None
        depth = 0
        for i in range(1, n):
            k = tokens[i].upper
            if k == '(':
                depth += 1
                continue
            if k == ')':
                depth -= 1
                continue
            if depth != 0:
                continue
            if k == 'FROM' and from_i is None:
                from_i = i
            elif where_i is None and k == 'WHERE':
                where_i = i
            elif group_i is None and k == 'GROUP' and i + 1 < n and tokens[i + 1].upper == 'BY':
                group_i = i
            elif order_i is None and k == 'ORDER' and i + 1 < n and tokens[i + 1].upper == 'BY':
                order_i = i
            elif limit_i is None and k == 'LIMIT':
                limit_i = i
        if from_i is None:
            raise _UnsupportedSQL("SELECT 缺少 FROM")
        # 多表连接（JOIN / ON / USING / 逗号多表）不属于本引擎范围：安全兜底
        # （JOIN 等词不在关键字集，token 为 ident，故不按 kind 过滤）
        for t in tokens[from_i + 2:]:
            if t.upper in ('JOIN', 'INNER', 'LEFT', 'RIGHT', 'FULL', 'CROSS',
                           'NATURAL', 'ON', 'USING', 'OUTER'):
                raise _UnsupportedSQL("SELECT 含多表 JOIN，本引擎不支持")
        if tokens[from_i + 1].value == ',':
            raise _UnsupportedSQL("SELECT 逗号多表，本引擎不支持")
        table = _unquote_ident(tokens[from_i + 1])

        stop = n
        where_end = group_i if group_i is not None else (order_i if order_i is not None
                                                         else (limit_i if limit_i is not None else stop))
        # 各段 token
        col_tokens = tokens[1:from_i]
        where_tokens = tokens[(where_i + 1):where_end] if where_i is not None else []
        g_end = order_i if order_i is not None else (limit_i if limit_i is not None else stop)
        group_tokens = tokens[(group_i + 2):g_end] if group_i is not None else []
        o_end = limit_i if limit_i is not None else stop
        order_tokens = tokens[(order_i + 2):o_end] if order_i is not None else []
        limit_tokens = tokens[(limit_i + 1):stop] if limit_i is not None else []

        # WHERE/GROUP/ORDER 段含 SELECT（子查询）→ 不支持，安全兜底
        for seg in (where_tokens, group_tokens, order_tokens):
            if any(t.kind == 'kw' and t.upper == 'SELECT' for t in seg):
                raise _UnsupportedSQL("SELECT 条件段含子查询，本引擎不支持")

        # 按 SQL 顺序统一绑定参数（WHERE / GROUP / ORDER / LIMIT 段无其它参数源）
        bound_where, bound_group, bound_order, bound_limit = self._bind_segments(
            [where_tokens, group_tokens, order_tokens, limit_tokens], params)

        data = self._read_tbl(table)
        rows = list(data.get('rows', []))
        schema = data.get('schema', [])

        # WHERE 过滤
        if bound_where:
            rows = [r for r in rows if self._eval_where(bound_where, r)]

        cols = self._parse_select_cols(col_tokens)
        has_agg = any(c.get('agg') for c in cols)

        # 列存在性校验：表 schema 非空时，SELECT 显式引用的列必须存在，否则
        # 视为生产 SQLite 的 "no such column"（查询失败 → 上层安全降级），
        # 避免缺列行被投影成 {'col': None} 值行，把「列不存在」误当成
        # 「列值为 NULL」——这是插件 is_active 在 debug 新库被误判禁用的根因。
        if schema:
            known = {c['name'] for c in schema}
            missing = [c['name'] for c in cols
                       if c['name'] != '*' and not c.get('const')
                       and c['name'] not in known]
            if missing:
                raise _UnsupportedSQL(
                    f"列不存在: {missing[0]}（表 {table} "
                    f"schema: {sorted(known) if known else '空'}）")

        if has_agg and (group_tokens or len(group_tokens) == 0 and False):
            pass
        if has_agg and group_tokens:
            # 按 GROUP BY 表达式（DATE(col) 或列名）分组
            gexpr = self._parse_agg_expr(bound_group)
            groups = {}
            order_keys = []
            for r in rows:
                key = self._eval_expr(gexpr, r)
                if key not in groups:
                    groups[key] = []
                    order_keys.append(key)
                groups[key].append(r)
            result = []
            for key in order_keys:
                row = {}
                for c in cols:
                    row[c['alias']] = self._agg_value(c, groups[key], key)
                result.append(row)
            rows = result
        elif has_agg:
            # 全表/过滤后聚合：无 GROUP BY 时聚合恒返回一行（空表 COUNT=0）
            row = {}
            for c in cols:
                row[c['alias']] = self._agg_value(c, rows, None)
            rows = [row]
        elif group_tokens:
            # 有 GROUP BY 但列无聚合函数：语义不符，安全返回空
            rows = []
        else:
            # 普通投影（先 ORDER BY 原行，再 DISTINCT，最后投影）
            if bound_order:
                self._sort_rows(rows, bound_order)
            distinct_flag = bool(cols) and cols[0].get('distinct')
            if distinct_flag:
                seen = set()
                uniq = []
                for r in rows:
                    vals = tuple(str(r.get(c['name'])) for c in cols)
                    if vals in seen:
                        continue
                    seen.add(vals)
                    uniq.append(r)
                rows = uniq
            proj = []
            for r in rows:
                row2 = dict(r) if any(c['name'] == '*' for c in cols) else {}
                for c in cols:
                    if c['name'] == '*':
                        continue
                    row2[c['alias']] = (
                        c['name'] if c.get('const') else r.get(c['name']))
                proj.append(row2)
            rows = proj

        # 聚合/分组结果的排序（普通分支已在投影前排过）
        if bound_order and (has_agg or group_tokens):
            self._sort_rows(rows, bound_order)

        # LIMIT / OFFSET
        if bound_limit:
            try:
                offset = 0
                limit = None
                tok = bound_limit[0]
                if tok.kind == 'val':
                    limit = int(str(tok.value))
                else:
                    limit = int(str(tok.value))
                # 支持 "LIMIT n OFFSET m"
                j = 1
                while j + 1 < len(bound_limit):
                    if bound_limit[j].upper == 'OFFSET':
                        v = bound_limit[j + 1]
                        offset = int(str(v.value if v.kind == 'val' else v.value))
                        break
                    j += 1
                rows = rows[offset:offset + limit] if limit is not None else rows[offset:]
            except Exception:
                pass  # LIMIT 解析失败：返回未分页结果
        return rows

    @staticmethod
    def _sort_rows(rows, bound_order):
        """按 ORDER BY 段排序行（NULL 恒排最后）"""
        col_name = _unquote_ident(bound_order[0])
        reverse = False
        for t in bound_order[1:]:
            if t.upper == 'DESC':
                reverse = True
            elif t.upper == 'ASC':
                reverse = False
        rows.sort(key=lambda r: (r.get(col_name) is None,
                                 str(r.get(col_name))), reverse=reverse)

    def _parse_select_cols(self, tokens):
        """解析 SELECT 列段 → [{'name','alias','agg','distinct'}]"""
        if not tokens:
            return []
        groups = []
        cur = []
        depth = 0
        for t in tokens:
            if t.upper == '(':
                depth += 1
            elif t.upper == ')':
                depth -= 1
            if t.value == ',' and depth == 0:
                groups.append(cur)
                cur = []
            else:
                cur.append(t)
        if cur:
            groups.append(cur)

        cols = []
        for g in groups:
            if not g:
                continue
            # AS alias
            alias = None
            for i, t in enumerate(g):
                if t.upper == 'AS' and i + 1 < len(g):
                    alias = _unquote_ident(g[i + 1])
                    g = g[:i]
                    break
            if not g:
                continue
            if len(g) == 1 and g[0].value == '*':
                cols.append({'name': '*', 'alias': '*', 'agg': None, 'distinct': False})
                continue
            # COUNT(...) / DATE(...) 聚合表达式
            if g[0].kind in ('kw', 'ident') and g[0].upper in ('COUNT', 'DATE') and \
                    len(g) >= 3 and g[1].upper == '(':
                fn = g[0].upper.lower()
                inner = _unquote_ident(g[2]) if len(g) >= 3 else '*'
                cols.append({'name': inner, 'alias': alias or fn,
                             'agg': fn, 'distinct': False})
                continue
            if g[0].upper == 'DISTINCT' and len(g) >= 2:
                name = _unquote_ident(g[1])
                cols.append({'name': name, 'alias': alias or name,
                             'agg': None, 'distinct': True})
                continue
            name = _unquote_ident(g[0])
            is_const = g[0].kind in ('num', 'str', 'val')
            cols.append({'name': name, 'alias': alias or name,
                         'agg': None, 'distinct': False,
                         'const': is_const})
        return cols

    @staticmethod
    def _parse_agg_expr(tokens):
        """GROUP BY 表达式：DATE(col) → ('date', col)，否则列名"""
        if not tokens:
            raise _UnsupportedSQL("GROUP BY 为空")
        if tokens[0].upper == 'DATE' and len(tokens) >= 3 and tokens[1].upper == '(':
            return ('date', _unquote_ident(tokens[2]))
        return ('col', _unquote_ident(tokens[0]))

    @staticmethod
    def _eval_expr(expr, row):
        kind = expr[0]
        if kind == 'date':
            v = row.get(expr[1])
            if v is None:
                return None
            if isinstance(v, datetime):
                return v.strftime('%Y-%m-%d')
            if isinstance(v, date):
                return v.strftime('%Y-%m-%d')
            s = str(v)
            return s[:10] if len(s) >= 10 else s
        return row.get(expr[1])

    @staticmethod
    def _agg_value(col, rows, group_key):
        agg = col['agg']
        if agg == 'count':
            return len(rows)
        if agg == 'date':
            return group_key
        return None if agg is None else len(rows)

    # ── WHERE 条件求值 ────────────────────────────────────

    def _eval_where(self, tokens, row) -> bool:
        """递归下降求值：OR → AND → NOT → 比较/IN/IS NULL/LIKE"""
        depth = 0
        or_idx = None
        for i, t in enumerate(tokens):
            if t.upper == '(':
                depth += 1
            elif t.upper == ')':
                depth -= 1
            elif depth == 0 and t.upper == 'OR':
                or_idx = i
                break
        if or_idx is not None:
            return (self._eval_where(tokens[:or_idx], row) or
                    self._eval_where(tokens[or_idx + 1:], row))

        and_parts = []
        cur = []
        depth = 0
        for t in tokens:
            if t.upper == '(':
                depth += 1
            elif t.upper == ')':
                depth -= 1
            if depth == 0 and t.upper == 'AND':
                and_parts.append(cur)
                cur = []
            else:
                cur.append(t)
        if cur:
            and_parts.append(cur)
        if len(and_parts) > 1:
            return all(self._eval_where(p, row) for p in and_parts)

        p = tokens
        # 剥离外层括号
        while len(p) >= 2 and p[0].upper == '(' and p[-1].upper == ')':
            depth = 0
            matched = True
            for i, t in enumerate(p):
                if t.upper == '(':
                    depth += 1
                elif t.upper == ')':
                    depth -= 1
                    if depth == 0 and i != len(p) - 1:
                        matched = False
                        break
            if matched and depth == 0:
                p = p[1:-1]
            else:
                break
        if not p:
            return True

        if p[0].upper == 'NOT':
            return not self._eval_where(p[1:], row)

        # IS NULL / IS NOT NULL
        if len(p) >= 2 and p[1].upper == 'IS':
            name = _unquote_ident(p[0])
            v = row.get(name)
            if len(p) >= 3 and p[2].upper == 'NOT':
                return v is not None
            return v is None

        # [NOT] IN (v1, v2, ...)
        for i, t in enumerate(p):
            if t.upper == 'IN' and i + 1 < len(p) and p[i + 1].upper == '(':
                neg = i > 0 and p[i - 1].upper == 'NOT'
                name = _unquote_ident(p[i - 2] if neg else p[i - 1])
                v = row.get(name)
                vals = []
                k = i + 2
                while k < len(p) and p[k].upper != ')':
                    if p[k].kind not in (',',):
                        vals.append(self._tok_value(p[k]))
                    k += 1
                matched = v in vals
                return (not matched) if neg else matched

        # LIKE 'pattern'
        for i, t in enumerate(p):
            if t.upper == 'LIKE' and i >= 1 and i + 1 < len(p):
                name = _unquote_ident(p[i - 1])
                v = '' if row.get(name) is None else str(row.get(name))
                pattern = str(self._tok_value(p[i + 1]))
                rx = '^' + re.escape(pattern).replace('%', '.*').replace('_', '.') + '$'
                return re.match(rx, v) is not None

        # 简单比较：col op value
        if len(p) >= 3:
            lv = row.get(_unquote_ident(p[0]))
            rv = self._tok_value(p[2])
            return self._cmp(lv, rv, p[1].value)
        raise _UnsupportedSQL(f"WHERE 无法解析: {[t.value for t in tokens]}")

    @staticmethod
    def _tok_value(t):
        if t.kind == 'val':
            return t.value
        if t.kind == 'str':
            return t.value.replace("''", "'")
        if t.kind == 'num':
            try:
                return int(t.value)
            except ValueError:
                return float(t.value)
        if t.kind == 'kw' and t.upper == 'NULL':
            return None
        return t.value

    @staticmethod
    def _eq(a, b):
        try:
            if isinstance(a, (int, float)) or isinstance(b, (int, float)):
                return float(a) == float(b)
        except (TypeError, ValueError):
            pass
        return str(a) == str(b)

    @classmethod
    def _cmp(cls, lv, rv, op):
        if op in ('=', '=='):
            if lv is None or rv is None:
                return lv is rv
            return cls._eq(lv, rv)
        if op in ('!=', '<>'):
            if lv is None or rv is None:
                return lv is not rv
            return not cls._eq(lv, rv)
        if lv is None or rv is None:
            return False
        if op == '>':
            try:
                return float(lv) > float(rv)
            except (TypeError, ValueError):
                return str(lv) > str(rv)
        if op == '<':
            try:
                return float(lv) < float(rv)
            except (TypeError, ValueError):
                return str(lv) < str(rv)
        if op == '>=':
            try:
                return float(lv) >= float(rv)
            except (TypeError, ValueError):
                return str(lv) >= str(rv)
        if op == '<=':
            try:
                return float(lv) <= float(rv)
            except (TypeError, ValueError):
                return str(lv) <= str(rv)
        raise _UnsupportedSQL(f"未知运算符 {op}")

    # ── INSERT ────────────────────────────────────────────

    def _exec_insert(self, sql: str, params, want_id):
        tokens = _tokenize(sql)
        i = 1
        ignore = False
        if i < len(tokens) and tokens[i].upper == 'IGNORE':
            ignore = True
            i += 1
        if i >= len(tokens) or tokens[i].upper != 'INTO':
            raise _UnsupportedSQL("INSERT 缺少 INTO")
        i += 1
        if i >= len(tokens):
            raise _UnsupportedSQL("INSERT 缺表名")
        table = _unquote_ident(tokens[i])
        i += 1
        # 列段（可省略）
        cols = []
        if i < len(tokens) and tokens[i].upper == '(':
            depth = 0
            names = []
            k = i
            while k < len(tokens):
                t = tokens[k]
                if t.upper == '(':
                    depth += 1
                elif t.upper == ')':
                    depth -= 1
                    if depth == 0:
                        break
                if depth == 1 and t.kind in ('ident', 'kw') and t.value not in (',', '(', ')'):
                    names.append(_unquote_ident(t))
                k += 1
            cols = names
            i = k + 1
        if i >= len(tokens) or tokens[i].upper != 'VALUES':
            raise _UnsupportedSQL("INSERT 缺 VALUES（INSERT...SELECT 不支持）")
        i += 1
        # VALUES 行组（深度感知：NOW() 等函数调用不提前截断）
        rows = []
        while i < len(tokens):
            if tokens[i].upper == '(':
                depth = 0
                k = i + 1
                vals = []
                while k < len(tokens):
                    t = tokens[k]
                    if t.upper == '(':
                        depth += 1
                    elif t.upper == ')':
                        if depth == 0:
                            break
                        depth -= 1
                    if t.value == ',' and depth == 0:
                        k += 1
                        continue
                    vals.append(t)
                    k += 1
                # 合并函数调用（NOW() → 当前时间）
                merged = []
                j = 0
                while j < len(vals):
                    if vals[j].upper == 'NOW' and j + 2 < len(vals) and \
                            vals[j + 1].upper == '(' and vals[j + 2].upper == ')':
                        merged.append(_Token(
                            'val',
                            datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
                        j += 3
                    else:
                        merged.append(vals[j])
                        j += 1
                rows.append(merged)
                i = k + 1
                if i < len(tokens) and tokens[i].value == ',':
                    i += 1
                    continue
                break
            i += 1
        if not rows:
            raise _UnsupportedSQL("INSERT VALUES 为空")

        # 全部行参数整体顺序绑定
        flat = [t for r in rows for t in r]
        bound_flat = self._bind(flat, params)
        bound_rows = []
        ptr = 0
        for r in rows:
            bound_rows.append(bound_flat[ptr:ptr + len(r)])
            ptr += len(r)

        data = self._read_tbl(table)
        schema = data['schema']
        if not schema and cols:
            schema.extend({'name': c, 'type': 'TEXT'} for c in cols)
        has_id_col = any(c['name'] == 'id' for c in schema)
        inserted = 0
        last_id = 0
        for vals in bound_rows:
            if cols:
                row = {}
                for name, t in zip(cols, vals):
                    row[name] = self._tok_value(t)
            else:
                names = [c['name'] for c in schema]
                row = {}
                for name, t in zip(names, vals):
                    row[name] = self._tok_value(t)
            # 缺列补列默认值（CREATE 声明了 DEFAULT 时落默认值，否则 None；
            # id 除外，id 走自增）。对齐真实 SQLite 语义，避免 is_active 等
            # 带 DEFAULT 1 的列被 NULL 误判。
            for c in schema:
                if row.get(c['name']) is None:
                    row[c['name']] = c.get('default')
            # 自增 id
            if has_id_col and row.get('id') is None:
                row['id'] = data.get('next_id', 1)
            # INSERT IGNORE：整列全等视为重复则跳过
            if ignore:
                dup = any(all(str(e.get(c)) == str(row.get(c)) for c in cols)
                          for e in data['rows']) if cols else any(e == row for e in data['rows'])
                if dup:
                    continue
            data['rows'].append(row)
            inserted += 1
            if has_id_col:
                last_id = row['id']
                data['next_id'] = max(int(data.get('next_id', 1)), int(last_id) + 1)
            # schema 补齐新列
            for c in cols:
                if not any(s['name'] == c for s in schema):
                    schema.append({'name': c, 'type': 'TEXT'})
        self._write_tbl(table, data)
        if want_id:
            return int(last_id or 0)
        return inserted

    # ── UPDATE ────────────────────────────────────────────

    def _exec_update(self, sql: str, params):
        tokens = _tokenize(sql)
        if len(tokens) < 3:
            raise _UnsupportedSQL("UPDATE 语法过短")
        table = _unquote_ident(tokens[1])
        i = 2
        if i >= len(tokens) or tokens[i].upper != 'SET':
            raise _UnsupportedSQL("UPDATE 缺 SET")
        i += 1
        # 切 SET / WHERE 段
        set_tokens = []
        where_tokens = []
        depth = 0
        in_where = False
        while i < len(tokens):
            t = tokens[i]
            if t.upper == '(':
                depth += 1
            elif t.upper == ')':
                depth -= 1
            if not in_where and depth == 0 and t.upper == 'WHERE':
                in_where = True
                i += 1
                continue
            (where_tokens if in_where else set_tokens).append(t)
            i += 1

        # SET/WHERE 段含 SELECT（子查询）→ 不支持，安全兜底
        for seg in (set_tokens, where_tokens):
            if any(t.kind == 'kw' and t.upper == 'SELECT' for t in seg):
                raise _UnsupportedSQL("UPDATE 含子查询，本引擎不支持")

        bound_set, bound_where = self._bind_segments([set_tokens, where_tokens], params)

        # 解析 SET 赋值（最外层逗号分隔）
        assigns = []
        cur = []
        depth = 0
        for t in bound_set:
            if t.upper == '(':
                depth += 1
            elif t.upper == ')':
                depth -= 1
            if t.value == ',' and depth == 0:
                assigns.append(cur)
                cur = []
            else:
                cur.append(t)
        if cur:
            assigns.append(cur)
        parsed = []
        for a in assigns:
            if len(a) < 3 or a[1].value != '=':
                raise _UnsupportedSQL(f"SET 无法解析: {[t.value for t in a]}")
            parsed.append((_unquote_ident(a[0]), a[2:]))

        data = self._read_tbl(table)
        affected = 0
        new_rows = []
        for r in data['rows']:
            if bound_where and not self._eval_where(bound_where, r):
                new_rows.append(r)
                continue
            row2 = dict(r)
            for name, expr in parsed:
                row2[name] = self._eval_set_expr(expr, row2)
            new_rows.append(row2)
            affected += 1
        data['rows'] = new_rows
        self._write_tbl(table, data)
        return affected

    @staticmethod
    def _eval_set_expr(tokens, row):
        """SET 右侧：值 / 字面量 / NOW() / col / col+N / col-N"""
        if not tokens:
            return None
        if len(tokens) == 1:
            t = tokens[0]
            if t.kind == 'val':
                return t.value
            if t.kind in ('str', 'num'):
                return SqlSimEngine._tok_value(t)
            if t.kind in ('ident', 'kw'):
                if t.upper == 'NOW':
                    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                return row.get(_unquote_ident(t))
            return t.value
        if tokens[0].upper == 'NOW' and len(tokens) >= 2 and tokens[1].upper == '(':
            return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if len(tokens) == 3 and tokens[1].value in ('+', '-'):
            base = row.get(_unquote_ident(tokens[0]))
            try:
                delta = float(SqlSimEngine._tok_value(tokens[2]))
                result = float(base) + (delta if tokens[1].value == '+' else -delta)
                if isinstance(base, int) and result.is_integer():
                    return int(result)
                return result
            except (TypeError, ValueError):
                return base
        raise _UnsupportedSQL(f"SET 表达式不支持: {[t.value for t in tokens]}")

    # ── DELETE ────────────────────────────────────────────

    def _exec_delete(self, sql: str, params):
        tokens = _tokenize(sql)
        if len(tokens) < 3 or tokens[1].upper != 'FROM':
            raise _UnsupportedSQL("DELETE 语法过短")
        table = _unquote_ident(tokens[2])
        i = 3
        where_tokens = tokens[i + 1:] if i < len(tokens) and tokens[i].upper == 'WHERE' else []
        # WHERE 段含 SELECT（子查询）→ 不支持，安全兜底
        if any(t.kind == 'kw' and t.upper == 'SELECT' for t in where_tokens):
            raise _UnsupportedSQL("DELETE 含子查询，本引擎不支持")
        bound_where = self._bind(where_tokens, params)
        data = self._read_tbl(table)
        before = len(data['rows'])
        if bound_where:
            data['rows'] = [r for r in data['rows']
                            if not self._eval_where(bound_where, r)]
        else:
            data['rows'] = []
        self._write_tbl(table, data)
        return before - len(data['rows'])

    # ── DDL ───────────────────────────────────────────────

    def _exec_create(self, sql: str):
        tokens = _tokenize(sql)
        if len(tokens) < 2 or tokens[0].upper != 'CREATE' or tokens[1].upper != 'TABLE':
            raise _UnsupportedSQL("CREATE 仅支持 CREATE TABLE")
        i = 2
        while i < len(tokens) and tokens[i].upper in ('IF', 'NOT', 'EXISTS'):
            i += 1
        if i >= len(tokens):
            raise _UnsupportedSQL("CREATE 缺表名")
        table = _unquote_ident(tokens[i])
        i += 1
        cols = []
        if i < len(tokens) and tokens[i].upper == '(':
            depth = 1  # '(' 已被跳过，深度从 1 起（嵌套括号才递增）
            k = i + 1
            seg = []
            segs = []
            while k < len(tokens):
                t = tokens[k]
                if t.upper == '(':
                    depth += 1
                elif t.upper == ')':
                    depth -= 1
                    if depth == 0:
                        segs.append(seg)
                        break
                if t.value == ',' and depth == 1:
                    segs.append(seg)
                    seg = []
                    k += 1
                    continue
                seg.append(t)
                k += 1
            _TYPES = {'INTEGER', 'INT', 'BIGINT', 'TINYINT', 'SMALLINT', 'FLOAT',
                      'DOUBLE', 'REAL', 'DECIMAL', 'TEXT', 'VARCHAR', 'CHAR',
                      'BOOLEAN', 'TIMESTAMP', 'DATETIME', 'DATE', 'BLOB', 'JSON',
                      'MEDIUMTEXT', 'LONGTEXT', 'ENUM'}
            for s in segs:
                if not s:
                    continue
                first = s[0]
                if first.kind in ('ident', 'kw') and first.upper not in (
                        'PRIMARY', 'KEY', 'UNIQUE', 'CONSTRAINT', 'FOREIGN',
                        'INDEX', 'CHECK'):
                    name = _unquote_ident(first)
                    ctype = 'TEXT'
                    if len(s) >= 2 and s[1].kind in ('ident', 'kw') and s[1].upper in _TYPES:
                        ctype = s[1].upper
                    # DEFAULT 字面量（对齐真实 SQLite：INSERT 缺列时落默认值而非 NULL）
                    default = None
                    for j, t in enumerate(s):
                        if t.upper == 'DEFAULT' and j + 1 < len(s):
                            default = self._tok_value(s[j + 1])
                            break
                    col = {'name': name, 'type': ctype}
                    if default is not None:
                        col['default'] = default
                    cols.append(col)
        data = self._read_tbl(table)
        existing = {c['name'] for c in data['schema']}
        for c in cols:
            if c['name'] not in existing:
                data['schema'].append(c)
        self._write_tbl(table, data)
        return 0

    def _exec_drop(self, sql: str):
        tokens = _tokenize(sql)
        if len(tokens) < 3 or tokens[1].upper != 'TABLE':
            return 0
        i = 2
        while i < len(tokens) and tokens[i].upper in ('IF', 'NOT', 'EXISTS'):
            i += 1
        if i < len(tokens):
            self._drop_tbl(_unquote_ident(tokens[i]))
        return 0