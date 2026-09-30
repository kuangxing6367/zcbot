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

内存行集缓冲：行集常驻内存（读写不再逐次全量解析/重写 JSON 文件），
写入先进缓冲、由后台线程按 database.debug_flush_ms（默认 1000，0=逐次
同步落盘）合并落盘，close()/进程退出兜底全量落盘。注意：缓冲模式下
进程被强杀（非正常退出）最多丢最近一个刷盘间隔的写入；外部进程/手工
改动 JSON 文件按 mtime 检测自动重载（脏表以内存为准）。

仅开发/调试使用；生产环境请配置 database.type: sqlite / mysql。
"""
import atexit
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger('zcbot')


def _ensure_dir(path: str):
    """确保目录存在（不存在则创建）"""
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)

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

class SqlSimEngine:
    """调试模式 SQL 模拟引擎（低性能本地模拟，行集 JSON 落盘）"""

    db_type = 'debug'
    degraded = False        # 用户显式配置的调试模式，不是故障降级
    debug_mode = True       # 标志：本地 SQL 模拟（低性能模式）

    def __init__(self, config: Optional[dict] = None):
        # 自包含初始化（不依赖外部基类）：仅取目录与锁，
        # 不再打印「降级为文件存储」的误导警告（debug 是显式调试模式，非故障降级）
        config = config or {}
        base = config.get('path', 'data/zcbot.db')
        default_dir = os.path.join(os.path.dirname(base) or '.', 'db')
        self._dir = config.get('fallback_dir') or default_dir
        self._lock = threading.RLock()
        self._db_path = self._dir
        try:
            _ensure_dir(os.path.join(self._dir, '.keep'))
        except Exception as e:
            logger.error(f"调试模式存储目录创建失败（{self._dir}）: {e}")
        self._sql_dir = os.path.join(self._dir, 'sql')
        try:
            _ensure_dir(os.path.join(self._sql_dir, '.keep'))
        except Exception as e:
            logger.error(f"SQL 模拟目录创建失败（{self._sql_dir}）: {e}")
        # ── 内存行集缓冲 ──
        # 表数据缓存：table -> {'schema','rows','next_id'}（脏表不因 mtime 校验
        # 被逐出；外部改文件按 mtime 失效重读）；_dirty 为待落盘表集合
        self._cache: Dict[str, dict] = {}
        self._mtimes: Dict[str, Optional[float]] = {}
        self._dirty: set = set()
        # debug_flush_ms：缓冲落盘间隔（毫秒）。>0 → 写入先进内存，后台线程
        # 按该间隔合并落盘（close/atexit 兜底）；0 → 每次写同步立即落盘（旧行为）
        self._flush_interval = max(0.0, float(config.get('debug_flush_ms', 1000))) / 1000.0
        self._closed = False
        self._flusher = None
        if self._flush_interval > 0:
            self._flusher = threading.Thread(
                target=self._flush_loop, name='sqlsim-flush', daemon=True)
            self._flusher.start()
            atexit.register(self.close)
        logger.info(
            f"调试模式已启用（低性能本地 SQL 模拟）: {self._sql_dir} "
            f"（行集 JSON 落盘，内存缓冲 {int(self._flush_interval * 1000)}ms 合并刷盘，"
            f"仅开发调试使用）"
        )

    # ── 行集表文件读写 ────────────────────────────────────

    def _tbl_path(self, table: str) -> str:
        return os.path.join(self._sql_dir, f"{table}.json")

    def _read_tbl(self, table: str) -> dict:
        """读表行集：优先内存缓冲；未命中读盘并装入（缺表返回空集不缓存）"""
        with self._lock:
            cached = self._cache.get(table)
            if cached is not None:
                if table in self._dirty:
                    return cached  # 有未落盘写入：以内存为准
                try:
                    mt = os.path.getmtime(self._tbl_path(table))
                except OSError:
                    mt = None
                if mt is not None and mt == self._mtimes.get(table):
                    return cached  # 文件未被外部改动：直接命中缓冲
                self._cache.pop(table, None)  # 外部改/删文件：丢弃缓冲重读
            path = self._tbl_path(table)
            if not os.path.isfile(path):
                return {'schema': [], 'rows': [], 'next_id': 1}
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception as e:
                logger.error(f"SQL 模拟表读取失败 {table}: {e}")
                return {'schema': [], 'rows': [], 'next_id': 1}
            if not isinstance(data, dict):
                return {'schema': [], 'rows': [], 'next_id': 1}
            data.setdefault('schema', [])
            data.setdefault('rows', [])
            data.setdefault('next_id', 1)
            try:
                self._mtimes[table] = os.path.getmtime(path)
            except OSError:
                self._mtimes[table] = None
            self._cache[table] = data
            return data

    def _write_tbl(self, table: str, data: dict):
        """写表行集：更新内存缓冲；落盘按缓冲策略（后台合并 / 同步立即）"""
        with self._lock:
            self._cache[table] = data
            if self._flush_interval > 0 and not self._closed:
                self._dirty.add(table)
                return
        # 同步模式 / 已关闭：立即落盘，失败上抛（对齐旧行为：execute 捕获后返回 0）
        if not self._flush_table(table, data):
            raise OSError(f"SQL 模拟表落盘失败: {table}")

    def _flush_table(self, table: str, data: dict) -> bool:
        """单表快照落盘（原子写；全程持锁防与 close/后台刷盘并发写坏 tmp）。
        失败不抛：留脏待重试，返回 False（同步写路径据之上抛）。"""
        path = self._tbl_path(table)
        tmp = f"{path}.tmp.{os.getpid()}"
        with self._lock:
            payload = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)
            self._dirty.discard(table)
            try:
                _ensure_dir(path)
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(payload)
                os.replace(tmp, path)
                try:
                    self._mtimes[table] = os.path.getmtime(path)
                except OSError:
                    self._mtimes[table] = None
                return True
            except Exception as e:
                self._dirty.add(table)  # 失败留脏，下轮/下次写重试
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass
                logger.error(f"SQL 模拟表落盘失败 {table}: {e}")
                return False

    def flush(self):
        """把所有脏表缓冲立即落盘（后台线程周期调用；close/测试可显式调用）"""
        with self._lock:
            pending = [(t, self._cache[t]) for t in self._dirty if t in self._cache]
        for t, data in pending:
            self._flush_table(t, data)

    def _flush_loop(self):
        """后台刷盘线程：按 debug_flush_ms 间隔把脏表合并落盘"""
        while not self._closed:
            time.sleep(self._flush_interval)
            if self._closed:
                break
            try:
                self.flush()
            except Exception as e:  # flush 内部已兜底，此处防御线程死亡
                logger.error(f"SQL 模拟缓冲刷盘异常: {e}")

    def _drop_tbl(self, table: str):
        with self._lock:
            self._cache.pop(table, None)
            self._mtimes.pop(table, None)
            self._dirty.discard(table)
        try:
            os.remove(self._tbl_path(table))
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"删除模拟表 {table} 失败: {e}")

    # ── SQL 兼容接口（与 Database 同构）────────────────

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
        t = _unquote_ident(table_name)
        with self._lock:
            if t in self._cache:
                return True  # 缓冲中已建未落盘的表也算存在
        return os.path.isfile(self._tbl_path(t))

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
            'note': '本地 SQL 模拟（低性能模式），行集 JSON 落盘'
                    f"（内存缓冲 {int(self._flush_interval * 1000)}ms 合并刷盘）",
            'buffered_flush_ms': int(self._flush_interval * 1000),
            'dirty_tables': len(self._dirty),
        }

    def close(self):
        """关停：脏表缓冲全部落盘（幂等；atexit 兜底调用）"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = [(t, self._cache[t]) for t in self._dirty if t in self._cache]
        for t, data in pending:
            self._flush_table(t, data)
        logger.debug("SQL 模拟引擎 close()：缓冲行集已全部落盘")

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

        # WHERE 过滤（编译一次闭包逐行求值；不可编译形态回退逐行解析）
        if bound_where:
            pred = self._compile_where(bound_where)
            if pred is not None:
                rows = [r for r in rows if pred(r)]
            else:
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
                first = int(str(bound_limit[0].value))
                if len(bound_limit) >= 3 and bound_limit[1].value == ',':
                    # MySQL 变体 "LIMIT offset, count"
                    offset = first
                    limit = int(str(bound_limit[2].value))
                else:
                    limit = first
                    j = 1
                    while j + 1 < len(bound_limit):
                        if bound_limit[j].upper == 'OFFSET':
                            offset = int(str(bound_limit[j + 1].value))
                            break
                        j += 1
                rows = rows[offset:offset + limit] if limit is not None else rows[offset:]
            except Exception:
                pass  # LIMIT 解析失败：返回未分页结果
        return rows

    @staticmethod
    def _sort_rows(rows, bound_order):
        """按 ORDER BY 段排序行（多列；数值列按数值序、其余按字符串序；
        NULL 恒排最后，与 ASC/DESC 方向无关）"""
        # 解析 "col [ASC|DESC]" 逗号列表（多列；DATE() 等表达式退化为首 token）
        keys = []  # [(col, desc)]
        i = 0
        n = len(bound_order)
        while i < n:
            t = bound_order[i]
            if t.value == ',':
                i += 1
                continue
            col = _unquote_ident(t)
            i += 1
            desc = False
            while i < n and bound_order[i].value != ',':
                u = bound_order[i].upper
                if u == 'DESC':
                    desc = True
                elif u == 'ASC':
                    desc = False
                i += 1
            keys.append((col, desc))

        def norm(v):
            # 数值桶 (0, x) / 字符串桶 (1, s)：同桶内可比较，跨桶数值在前
            if isinstance(v, bool):
                return (0, int(v))
            if isinstance(v, (int, float)):
                return (0, v)
            s = str(v)
            try:
                return (0, int(s))
            except ValueError:
                try:
                    return (0, float(s))
                except ValueError:
                    return (1, s)

        def make_key(col, desc):
            def k(r):
                v = r.get(col)
                if v is None:
                    # NULL 殿后：ASC 取最大键、DESC（reverse）取最小键
                    return (1, (0, 0.0)) if not desc else (0, (0, 0.0))
                return (0, norm(v)) if not desc else (1, norm(v))
            return k

        # 多列排序：Python 排序稳定，从最后一列往前逐列排
        for col, desc in reversed(keys):
            rows.sort(key=make_key(col, desc), reverse=desc)

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

    def _compile_where(self, tokens):
        """WHERE → 闭包谓词（编译一次、逐行求值，热路径省去逐行重复解析）。

        形态与 _eval_where 完全同构（OR → AND → 括号 → NOT → IS NULL → IN →
        LIKE → 简单比较）；无法编译的形态返回 None，调用方回退 _eval_where。
        """
        # 顶层 OR
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
            left = self._compile_where(tokens[:or_idx])
            right = self._compile_where(tokens[or_idx + 1:])
            if left is None or right is None:
                return None
            return lambda r: left(r) or right(r)

        # 顶层 AND
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
            preds = [self._compile_where(p) for p in and_parts]
            if any(p is None for p in preds):
                return None
            return lambda r: all(p(r) for p in preds)

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
            return lambda r: True

        if p[0].upper == 'NOT':
            inner = self._compile_where(p[1:])
            if inner is None:
                return None
            return lambda r: not inner(r)

        # IS NULL / IS NOT NULL
        if len(p) >= 2 and p[1].upper == 'IS':
            name = _unquote_ident(p[0])
            if len(p) >= 3 and p[2].upper == 'NOT':
                return lambda r, n=name: r.get(n) is not None
            return lambda r, n=name: r.get(n) is None

        # [NOT] IN (v1, v2, ...)
        for i, t in enumerate(p):
            if t.upper == 'IN' and i + 1 < len(p) and p[i + 1].upper == '(':
                neg = i > 0 and p[i - 1].upper == 'NOT'
                name = _unquote_ident(p[i - 2] if neg else p[i - 1])
                vals = []
                k = i + 2
                while k < len(p) and p[k].upper != ')':
                    if p[k].kind not in (',',):
                        vals.append(self._tok_value(p[k]))
                    k += 1
                return lambda r, n=name, vals=vals, neg=neg: \
                    (r.get(n) in vals) != neg

        # LIKE 'pattern'（正则编译一次）
        for i, t in enumerate(p):
            if t.upper == 'LIKE' and i >= 1 and i + 1 < len(p):
                name = _unquote_ident(p[i - 1])
                pattern = str(self._tok_value(p[i + 1]))
                rx = re.compile(
                    '^' + re.escape(pattern).replace('%', '.*').replace('_', '.') + '$')
                return lambda r, n=name, rx=rx: \
                    rx.match('' if r.get(n) is None else str(r.get(n))) is not None

        # 简单比较：col op value（右值编译期取定，与 _tok_value 语义一致）
        if len(p) >= 3:
            name = _unquote_ident(p[0])
            op = p[1].value
            rv = self._tok_value(p[2])
            if op in ('=', '=='):
                def pred(r, n=name, rv=rv):
                    lv = r.get(n)
                    if lv is None or rv is None:
                        return lv is rv
                    return SqlSimEngine._eq(lv, rv)
                return pred
            if op in ('!=', '<>'):
                def pred(r, n=name, rv=rv):
                    lv = r.get(n)
                    if lv is None or rv is None:
                        return lv is not rv
                    return not SqlSimEngine._eq(lv, rv)
                return pred
            if op in ('>', '<', '>=', '<='):
                return lambda r, n=name, op=op, rv=rv: \
                    SqlSimEngine._cmp(r.get(n), rv, op)
        return None  # 无法编译：回退逐行解析

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

        # VALUES 组之后的尾部子句：ON DUPLICATE KEY UPDATE（含占位符需一并按序绑定）
        rest = tokens[i:]
        ru = [t.upper for t in rest]
        odku_raw = []
        for k in range(max(0, len(rest) - 3)):
            if ru[k:k + 4] == ['ON', 'DUPLICATE', 'KEY', 'UPDATE']:
                odku_raw = rest[k + 4:]
                break

        # 全部行参数 + ODKU 参数整体顺序绑定
        flat = [t for r in rows for t in r]
        bound_flat, bound_odku = self._bind_segments([flat, odku_raw], params)
        bound_rows = []
        ptr = 0
        for r in rows:
            bound_rows.append(bound_flat[ptr:ptr + len(r)])
            ptr += len(r)

        # 解析 ODKU 赋值：col = expr（expr 可含 VALUES(col)/IF(...)/NOW()）。
        # 解析失败退化为普通 INSERT（不追加失败/O(1) 语义丢失，但绝不影响插入本身）。
        parsed_odku = []
        if bound_odku:
            try:
                assigns, cur, depth = [], [], 0
                for t in bound_odku:
                    if t.value == '(':
                        depth += 1
                    elif t.value == ')':
                        depth -= 1
                    if t.value == ',' and depth == 0:
                        if cur:
                            assigns.append(cur)
                        cur = []
                    else:
                        cur.append(t)
                if cur:
                    assigns.append(cur)
                for a in assigns:
                    if len(a) < 3 or a[1].value != '=':
                        raise _UnsupportedSQL(
                            f"ODKU 无法解析: {[t.value for t in a]}")
                    parsed_odku.append((_unquote_ident(a[0]), a[2:]))
            except _UnsupportedSQL:
                parsed_odku = []

        data = self._read_tbl(table)
        schema = data['schema']
        if not schema and cols:
            schema.extend({'name': c, 'type': 'TEXT'} for c in cols)
        has_id_col = any(c['name'] == 'id' for c in schema)
        inserted = 0
        last_id = 0
        # INSERT IGNORE：去重键集合（每条语句建一次 O(N)，逐行 O(1) 查——
        # 原实现每行全表扫比对，大表批量写为 O(N²)）
        existing = None
        if ignore:
            if cols:
                key_cols = cols
                existing = {tuple(str(e.get(c)) for c in key_cols)
                            for e in data['rows']}
            else:
                existing = {tuple(sorted((k, str(v)) for k, v in e.items()))
                            for e in data['rows']}

        # ODKU 冲突键索引：按唯一键/主键把已有行建 map，逐行 O(1) 命中
        uk_list = (data.get('unique_keys') or []) if parsed_odku else []
        conflict_maps = []
        for uk in uk_list:
            m = {}
            for r in data['rows']:
                m.setdefault(tuple(str(r.get(c)) for c in uk), r)
            conflict_maps.append((uk, m))

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
            # ON DUPLICATE KEY UPDATE：唯一键命中已有行 → 就地更新，不追加新行
            if conflict_maps:
                hit = None
                for uk, m in conflict_maps:
                    if any(row.get(c) is None for c in uk):
                        continue
                    hit = m.get(tuple(str(row.get(c)) for c in uk))
                    if hit is not None:
                        break
                if hit is not None:
                    for name, expr in parsed_odku:
                        sub = SqlSimEngine._sub_values(expr, row)
                        try:
                            hit[name] = self._eval_odku_rhs(sub, hit)
                        except _UnsupportedSQL:
                            pass  # 单列表达式不支持：保留原值，不因此丢整行
                    if has_id_col:
                        last_id = hit.get('id', last_id)
                    continue
            # INSERT IGNORE：整列全等视为重复则跳过
            if ignore:
                k = (tuple(str(row.get(c)) for c in cols) if cols else
                     tuple(sorted((k2, str(v)) for k2, v in row.items())))
                if k in existing:
                    continue
                existing.add(k)
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
        pred = None
        if bound_where:
            pred = self._compile_where(bound_where)
        affected = 0
        new_rows = []
        for r in data['rows']:
            if bound_where:
                hit = pred(r) if pred is not None else \
                    self._eval_where(bound_where, r)
                if not hit:
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
    def _paren_cols(seg):
        """从约束/索引 token 段中取第一个括号组内的列名列表（PRIMARY/UNIQUE KEY）"""
        i, n = 0, len(seg)
        while i < n and seg[i].value != '(':
            i += 1
        if i >= n:
            return []
        cols, i = [], i + 1
        while i < n:
            t = seg[i]
            if t.value == ')':
                break
            if t.kind in ('ident', 'kw'):
                cols.append(_unquote_ident(t))
            i += 1
        return cols

    @staticmethod
    def _sub_values(tokens, new_row):
        """把 ODKU 表达式中的 VALUES(col) 预替换为待插入行的对应值 token"""
        out, i, n = [], 0, len(tokens)
        while i < n:
            t = tokens[i]
            if (t.upper == 'VALUES' and i + 3 < n
                    and tokens[i + 1].value == '(' and tokens[i + 3].value == ')'):
                out.append(_Token('val', new_row.get(_unquote_ident(tokens[i + 2]))))
                i += 4
                continue
            out.append(t)
            i += 1
        return out

    @staticmethod
    def _split_top_commas(tokens):
        """按最外层逗号切段（括号内逗号不切）"""
        segs, cur, depth = [], [], 0
        for t in tokens:
            if t.value == '(':
                depth += 1
            elif t.value == ')':
                depth -= 1
            if t.value == ',' and depth == 0:
                segs.append(cur)
                cur = []
            else:
                cur.append(t)
        segs.append(cur)
        return segs

    def _eval_odku_rhs(self, tokens, row):
        """ODKU 右值：IF(cond, a, b) / 其余委托 _eval_set_expr（已替换 VALUES()）"""
        if not tokens:
            return None
        if (len(tokens) >= 2 and tokens[0].upper == 'IF' and tokens[1].value == '('):
            inner = tokens[2:]
            if inner and inner[-1].value == ')':
                inner = inner[:-1]
            args = self._split_top_commas(inner)
            if len(args) != 3:
                raise _UnsupportedSQL("IF 参数数不支持")
            if self._eval_odku_cond(args[0], row):
                return self._eval_odku_rhs(args[1], row)
            return self._eval_odku_rhs(args[2], row)
        return self._eval_set_expr(tokens, row)

    def _eval_odku_cond(self, tokens, row):
        """IF 条件：单个顶层比较运算 a OP b"""
        for i, t in enumerate(tokens):
            if t.kind == 'op' and t.value in ('=', '!=', '<>', '<', '>', '<=', '>='):
                lv = self._eval_odku_rhs(tokens[:i], row)
                rv = self._eval_odku_rhs(tokens[i + 1:], row)
                return self._cmp(lv, rv, t.value)
        raise _UnsupportedSQL("IF 条件不支持")

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
            pred = self._compile_where(bound_where)
            if pred is not None:
                data['rows'] = [r for r in data['rows'] if not pred(r)]
            else:
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
            ukeys = []
            for s in segs:
                if not s:
                    continue
                first = s[0]
                fu = first.upper
                if fu in ('PRIMARY', 'UNIQUE', 'CONSTRAINT'):
                    # 表级约束：PRIMARY KEY(col..) / [CONSTRAINT n] UNIQUE [KEY](col..)
                    pk = SqlSimEngine._paren_cols(s)
                    if pk:
                        ukeys.append(pk)
                    continue
                if fu in ('KEY', 'INDEX', 'FOREIGN', 'CHECK'):
                    continue
                if first.kind in ('ident', 'kw'):
                    name = _unquote_ident(first)
                    ctype = 'TEXT'
                    if len(s) >= 2 and s[1].kind in ('ident', 'kw') and s[1].upper in _TYPES:
                        ctype = s[1].upper
                    # DEFAULT 字面量（对齐真实 SQLite：INSERT 缺列时落默认值而非 NULL）；
                    # CURRENT_TIMESTAMP 等函数式默认不落地，避免调试模式写入 'CURRENT_TIMESTAMP' 字面串
                    default = None
                    for j, t in enumerate(s):
                        if t.upper == 'DEFAULT' and j + 1 < len(s):
                            dt = s[j + 1]
                            if (dt.kind in ('num', 'str', 'val')
                                    or (dt.kind in ('ident', 'kw')
                                        and dt.upper in ('NULL', 'TRUE', 'FALSE'))):
                                default = self._tok_value(dt)
                            break
                    col = {'name': name, 'type': ctype}
                    if default is not None:
                        col['default'] = default
                    cols.append(col)
                    # 列级 PRIMARY KEY / UNIQUE（单列冲突键）
                    if any(t.upper in ('PRIMARY', 'UNIQUE') for t in s):
                        ukeys.append([name])
        data = self._read_tbl(table)
        existing = {c['name'] for c in data['schema']}
        for c in cols:
            if c['name'] not in existing:
                data['schema'].append(c)
        # 唯一键/主键集合：ON DUPLICATE KEY UPDATE 靠它定位冲突行；幂等合并去重
        uk_existing = [list(x) for x in (data.get('unique_keys') or [])]
        for k in ukeys:
            if list(k) not in uk_existing:
                uk_existing.append(list(k))
        if uk_existing:
            data['unique_keys'] = uk_existing
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