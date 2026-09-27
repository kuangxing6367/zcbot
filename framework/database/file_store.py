# -*- coding: utf-8 -*-
"""
降级文件存储（"最垃计划"）：数据库存储不可用时的最低限度可用方案。

语义：
- 真实数据库（SQLite/MySQL）初始化失败或未配置时，框架不再因此无法启动，
  而是降级为 data/db/ 目录下的 JSON / YAML 文件存储。
- 与 framework.database.db.Database 公开接口同构：
  SQL 类接口（query/query_one/execute/execute_many/insert/scalar/count/
  exists/table_exists/table_info/table_has_column）一律返回"空/假/0"安全默认值，
  不抛异常 —— 内核所有已用 try/except 包裹的 DB 调用点自然降级。
- 提供真正的文档存取能力（put/get/delete/list 等），数据以 JSON（默认）
  或 YAML 文件形式落盘，每个"表"一个文件，原子写入（临时文件 + rename）。
- get_connection / transaction 等强依赖真实数据库的高级能力不支持，
  调用时抛 NotImplementedError（明确降级语义，不静默伪造成功）。

仅作为数据库不可用时的兜底，不支持 SQL 解析与多行事务；
需要完整能力请配置 database.type: sqlite / mysql。
"""
import logging
import os
import threading

logger = logging.getLogger('zcbot')


# 检测 YAML 可用性（pyyaml 在 requirements 中，但允许缺失回退纯 JSON）
try:
    import yaml as _yaml
    _YAML_OK = True
except Exception:  # pragma: no cover - 环境缺 pyyaml 时回退
    _yaml = None
    _YAML_OK = False


def _ensure_dir(path: str):
    """确保目录存在（不存在则创建）"""
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)


class FileStore:
    """
    文件存储降级后端。

    目录规则：config['path'] 所在目录下建 'db' 子目录（如默认
    data/zcbot.db → data/db/）；config['fallback_dir'] 可显式覆盖。
    """

    db_type = 'file'          # 与 Database.db_type 对齐（'sqlite'/'mysql'/'file'）
    degraded = True           # 标志：当前为降级存储

    # ── 生命周期 ──────────────────────────────────────────────

    def __init__(self, config: dict = None):
        config = config or {}
        base = config.get('path', 'data/zcbot.db')
        default_dir = os.path.join(os.path.dirname(base) or '.', 'db')
        self._dir = config.get('fallback_dir') or default_dir
        self._lock = threading.RLock()
        self._db_path = self._dir  # 兼容 remote_tx 等偶发读取
        try:
            _ensure_dir(os.path.join(self._dir, '.keep'))
        except Exception as e:
            # 目录都建不出来：记录但保持可启动，写入时再抛具体错误
            logger.error(f"文件存储目录创建失败（{self._dir}）: {e}")
        logger.warning(
            f"数据库存储不可用，已降级为文件存储（最垃计划）: {self._dir} "
            f"（JSON/YAML 文件读写，仅最低限度可用）"
        )

    # ── 文档存储 API（真正的"硬解 json/yaml"）──────────────

    def _table_path(self, table: str, fmt: str = 'json') -> str:
        """计算表文件路径；fmt 仅为兼容 YAML 调用保留，表格式由写入时决定"""
        return os.path.join(self._dir, f"{table}.{fmt}")

    def _read_table(self, table: str):
        """读取整表（dict），文件不存在或损坏返回 {}。JSON 优先、YAML 兜底。"""
        jpath = self._table_path(table, 'json')
        ypath = self._table_path(table, 'yaml')
        data = {}
        try:
            if os.path.isfile(jpath):
                with open(jpath, 'r', encoding='utf-8') as f:
                    data = _load_json(f.read())
            elif os.path.isfile(ypath) and _YAML_OK:
                with open(ypath, 'r', encoding='utf-8') as f:
                    data = _load_yaml(f.read())
        except Exception as e:
            logger.error(f"文件存储读取表 {table} 失败: {e}")
            # 保留损坏现场：把损坏表文件改名为 .corrupt 留证，
            # 避免每次读取反复报错，也便于人工排查/恢复
            for p in (jpath, ypath):
                if os.path.isfile(p):
                    try:
                        corrupt = f"{p}.corrupt"
                        if not os.path.exists(corrupt):
                            os.replace(p, corrupt)
                            logger.warning(f"已保留损坏表文件 → {corrupt}")
                    except Exception as ce:
                        logger.warning(f"保留损坏表文件 {p} 失败: {ce}")
            data = {}
        return data if isinstance(data, dict) else {}

    def _write_table(self, table: str, data: dict, fmt: str = 'json',
                     comment: str = None):
        """原子写整表：临时文件 + os.replace，避免写一半损坏"""
        if not isinstance(data, dict):
            raise ValueError("文件存储仅支持 dict 类型数据")
        fmt = (fmt or 'json').lower()
        if fmt not in ('json', 'yaml'):
            raise ValueError(f"不支持的文件格式: {fmt}")
        path = self._table_path(table, fmt)
        tmp = f"{path}.tmp.{os.getpid()}"
        try:
            _ensure_dir(path)
            if fmt == 'yaml':
                if not _YAML_OK:
                    raise RuntimeError("pyyaml 不可用，无法写入 yaml 文件（可用 json）")
                content = _dump_yaml(data, comment)
            else:
                content = _dump_json(data, comment)
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(content)
            os.replace(tmp, path)
        except Exception as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            raise
        return path

    def put(self, table: str, key, value, fmt: str = 'json'):
        """写入一条记录 key -> value（按表聚合到一个文件）"""
        with self._lock:
            data = self._read_table(table)
            data[str(key)] = value
            return self._write_table(table, data, fmt)

    def get(self, table: str, key, default=None):
        """读取一条记录；不存在返回 default"""
        with self._lock:
            data = self._read_table(table)
            return data.get(str(key), default)

    def delete(self, table: str, key) -> bool:
        """删除一条记录，返回是否实际删除"""
        with self._lock:
            data = self._read_table(table)
            k = str(key)
            if k not in data:
                return False
            del data[k]
            self._write_table(table, data)
            return True

    def list(self, table: str) -> dict:
        """列出整表记录的浅拷贝 dict"""
        with self._lock:
            return dict(self._read_table(table))

    def keys(self, table: str) -> list:
        """列出表中全部 key"""
        return list(self.list(table).keys())

    def clear(self, table: str):
        """清空整表（删除表文件）"""
        with self._lock:
            jpath = self._table_path(table, 'json')
            ypath = self._table_path(table, 'yaml')
            for p in (jpath, ypath):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception as e:
                    logger.warning(f"清空表 {table} 失败: {e}")

    # 语义化别名（表 = 集合）
    def save_doc(self, table: str, key, value, fmt: str = 'json'):
        return self.put(table, key, value, fmt)

    def load_doc(self, table: str, key, default=None):
        return self.get(table, key, default)

    def delete_doc(self, table: str, key) -> bool:
        return self.delete(table, key)

    def list_docs(self, table: str) -> dict:
        return self.list(table)

    # ── SQL 兼容安全默认（不抛异常）────────────────────────

    def query(self, sql: str, params=None) -> list:
        """查询多条 → 降级返回空列表"""
        self._sql_degraded('query', sql)
        return []

    def query_one(self, sql: str, params=None):
        """查询单条 → 降级返回 None"""
        self._sql_degraded('query_one', sql)
        return None

    def execute(self, sql: str, params=None) -> int:
        """执行写 → 降级返回 0（未执行）"""
        self._sql_degraded('execute', sql)
        return 0

    def execute_many(self, sql: str, params_list: list) -> int:
        """批量写 → 降级返回 0"""
        self._sql_degraded('execute_many', sql)
        return 0

    def insert(self, sql: str, params=None) -> int:
        """插入返回自增 ID → 降级返回 0"""
        self._sql_degraded('insert', sql)
        return 0

    def scalar(self, sql: str, params=None):
        """单行单列 → 降级返回 None"""
        self._sql_degraded('scalar', sql)
        return None

    def count(self, sql: str, params=None) -> int:
        """COUNT → 降级返回 0"""
        self._sql_degraded('count', sql)
        return 0

    def exists(self, sql: str, params=None) -> bool:
        """存在性 → 降级返回 False"""
        self._sql_degraded('exists', sql)
        return False

    def table_exists(self, table_name: str) -> bool:
        """表存在性 → 降级返回 False（表示无 SQL 表）"""
        self._sql_degraded('table_exists', table_name)
        return False

    def table_info(self, table_name: str) -> list:
        """表结构 → 降级返回空列表"""
        self._sql_degraded('table_info', table_name)
        return []

    def table_has_column(self, table_name: str, column_name: str) -> bool:
        """列存在性 → 降级返回 False"""
        self._sql_degraded('table_has_column', f"{table_name}.{column_name}")
        return False

    def _sql_degraded(self, op: str, target):
        """SQL 调用降级提示（debug 级，避免刷屏）"""
        logger.debug(f"文件存储降级：忽略 SQL {op}({target})")

    # ── 连接/事务（不支持，明确报错）──────────────────────

    def get_connection(self):
        raise NotImplementedError(
            "文件存储降级模式不支持原始数据库连接（get_connection），"
            "请配置真实数据库（database.type: sqlite/mysql）"
        )

    def transaction(self, conn=None):
        raise NotImplementedError(
            "文件存储降级模式不支持 SQL 事务，请配置真实数据库"
        )

    # ── 状态与关闭 ──────────────────────────────────────────

    @property
    def pool_status(self) -> dict:
        """与 Database.pool_status 对齐：返回降级存储状态"""
        return {
            'type': 'file',
            'path': self._dir,
            'degraded': True,
            'note': '数据库不可用，已降级为 JSON/YAML 文件存储',
        }

    def close(self):
        """无需关闭资源；保留接口兼容"""
        logger.debug("文件存储 close()：降级存储无需关闭")

    def __repr__(self):
        return f"<FileStore dir={self._dir}>"


# ── JSON/YAML 序列化小工具（逐表独立文件）────────────────

def _dump_json(data: dict, comment: str = None) -> str:
    import json
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    if comment:
        return f"// {comment}\n{text}\n"
    return f"{text}\n"


def _load_json(text: str) -> dict:
    import json
    # 容忍首行 // 注释（本模块写入时可能带）
    lines = text.split('\n')
    while lines and lines[0].startswith('//'):
        lines.pop(0)
    return json.loads('\n'.join(lines))


def _dump_yaml(data: dict, comment: str = None) -> str:
    text = _yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    if comment:
        return f"# {comment}\n{text}"
    return text


def _load_yaml(text: str) -> dict:
    return _yaml.safe_load(text) or {}