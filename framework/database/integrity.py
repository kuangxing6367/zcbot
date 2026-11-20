"""库结构完整性标记：区分「首次安装」与「运行期表被删」。

为什么要有标记
--------------
`CREATE TABLE IF NOT EXISTS` 无法区分这两种情况：

1. 新装：核心表本来就不存在，应该建表并写入种子数据；
2. 运行期被删：库里原本有数据，表是后来被 DROP / 回滚 / 误操作弄丢的，
   此时再建表等于**静默重建**，还可能把默认管理员账号密码一并种回去。

标记落在数据库文件之外，所以表被删不会连带丢失。只有标记存在而核心表
缺失时，才判定为运行期丢表；此时默认只告警，不补建。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger('zcbot')

MARKER_SUFFIX = '.initstate'
RESET_SUFFIX = '.initstate.reset'
STATE_SUBDIR = '.db_initstate'


def _sqlite_path(database) -> str:
    path = getattr(database, '_db_path', None)
    if not path:
        cfg = getattr(database, 'config', None) or {}
        path = cfg.get('path', 'data/zcbot.db')
    if not os.path.isabs(path):
        from framework.paths import home_dir

        path = os.path.join(home_dir(), path)
    return str(path)


def marker_path(database) -> str:
    """标记文件路径。SQLite 放库文件旁；MySQL 按连接信息哈希落到状态目录。"""
    db_type = getattr(database, 'db_type', 'sqlite')
    if db_type == 'sqlite':
        return _sqlite_path(database) + MARKER_SUFFIX
    cfg = getattr(database, 'config', None) or {}
    key = '|'.join(
        str(cfg.get(k, ''))
        for k in ('type', 'host', 'port', 'database')
    )
    digest = hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]
    from framework.paths import state_dir

    return os.path.join(state_dir(), STATE_SUBDIR, f'{digest}{MARKER_SUFFIX}')


def read_marker(database) -> dict[str, Any] | None:
    path = marker_path(database)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_initialized(database) -> bool:
    """这个库此前是否已经完成过一次初始化。

    判定不能只看标记文件：生产把库恢复到新机器 / 新目录时 ``.initstate``
    不会跟着走，只认标记就会把「有数据的老库」误判成首次安装，进而重建
    缺失的 admin_users 并把默认口令 admin123 种回去——那等于给任何能删表
    的人一个后门账号。因此标记缺失时再看库内证据。

    证据查询本身失败（库不可用）时保守返回 True：宁可判成「可能已初始化、
    不写默认账号」，也不能在坏库上写入已知口令。
    """
    if reset_requested(database):
        # 人工重置优先：用户明确要求按首次安装重来，证据判定必须让位。
        return False
    if read_marker(database) is not None:
        return True
    evidence = has_data_evidence(database)
    if evidence is True:
        # 纯查询，不在这里写标记：本函数也被直接建表/迁移的路径调用，
        # 顺手落盘会改变那些路径的行为（例如把结构重建当成「运行期丢表」）。
        # 标记补写由 auto_init_database 在确认后显式完成。
        return True
    if evidence is None:
        # 证据查不出来（库不可用）：保守判为已初始化，不在坏库上写默认账号。
        return True
    return False


def _current_tables(database) -> list[str]:
    """当前库里实际存在的表名（补写标记时用，避免把 tables 写成空表）。"""
    batch = getattr(database, 'all_table_names', None)
    try:
        if callable(batch):
            return sorted({str(t).lower() for t in batch()})
        return sorted(
            t for t in _known_names(database) if database.table_exists(t))
    except Exception as e:
        logger.debug('[integrity] 枚举当前表失败: %s', e)
        return []


# 判定「库曾经被用过」的种子表：框架完成初始化必然写入这些行。
# 刻意不含 users / plugins 这类业务表——用户手工建一个只有 users 表的库
# （迁移测试、老库升级）不能被误判成「已初始化」，否则结构重建会被拦住。
_EVIDENCE_TABLES = ('admin_users', 'perm_groups', 'system_config',
                    'schema_migrations')


def has_data_evidence(database):
    """库内是否已有「用过」的痕迹：True=有，False=确实空，None=查不出来。

    两条独立证据，任一成立即可：
      1. **结构证据**：核心表存在过半。首次安装的空库一张表都没有；从备份
         恢复的库哪怕业务数据为空，表结构一定在。
      2. **行证据**：种子表 / 业务表里存在记录。

    只看行证据不够：一个刚建好还没跑过业务的新库，plugins/users 都是空表，
    一旦管理员表恰好在缺表列表里，就会被误判成首次安装。
    """
    unknown = False
    # 结构证据必须看「现在的」表，不能看启动时的元数据快照：运行期 DROP 掉的
    # 表如果还留在快照里，会把一个空库硬判成「已初始化」。
    refresh = getattr(database, 'refresh_schema_snapshot', None)
    if callable(refresh):
        try:
            refresh()
        except Exception as e:
            logger.debug('[integrity] 刷新表结构快照失败: %s', e)
    try:
        from .init_db import _REQUIRED_TABLES

        present = 0
        for table in _REQUIRED_TABLES:
            try:
                if database.table_exists(table):
                    present += 1
            except Exception:
                unknown = True
        if present >= max(1, len(_REQUIRED_TABLES) // 2):
            return True
    except Exception as e:
        logger.debug('[integrity] 核心表结构证据检查失败: %s', e)
        unknown = True

    for table in _EVIDENCE_TABLES:
        try:
            if not database.table_exists(table):
                continue
            value = database.scalar(f'SELECT COUNT(*) FROM {table}')
        except Exception as e:
            logger.debug('[integrity] 证据表 %s 查询失败: %s', table, e)
            unknown = True
            continue
        try:
            if int(value or 0) > 0:
                return True
        except (TypeError, ValueError):
            unknown = True
    return None if unknown else False


def mark_initialized(database, tables: list[str] | None = None,
                     evidence: str | None = None) -> bool:
    """写入/刷新初始化标记。写不进去不阻断启动，但要让调用方知道。

    ``tables`` 为 None 时自动取当前实际存在的表：只写空列表会让
    ``may_create()`` 把每张表都当成「升级新增」而放行重建。
    """
    if tables is None:
        tables = _current_tables(database)
    payload = {
        'initialized_at': time.time(),
        'db_type': getattr(database, 'db_type', 'sqlite'),
        'tables': [str(t).lower() for t in (tables or [])],
    }
    if evidence:
        payload['evidence'] = str(evidence)
    ok = _write(database, payload)
    if ok:
        # 重新初始化完成，撤掉显式重置标记，否则下次启动又会被判成首次安装。
        _clear_reset(database)
    return ok


def clear_marker(database) -> bool:
    """人工重置：删掉标记后，下次启动按「首次安装」处理。

    只删 ``.initstate`` 不够：库里的表结构还在，``is_initialized()`` 会立刻
    用库内证据把它重新判成「已初始化」，用户的重置意图被吃掉。所以这里额外
    落一个 ``.initstate.reset`` 显式重置标记，优先级高于证据判定。
    """
    path = marker_path(database)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning('删除初始化标记失败: %s', e)
        return False
    try:
        with open(path + RESET_SUFFIX, 'w', encoding='utf-8') as f:
            f.write(str(int(time.time())))
        return True
    except OSError as e:
        logger.warning('写入重置标记失败: %s', e)
        return False


def reset_requested(database) -> bool:
    """是否存在显式「重新初始化」请求。"""
    return os.path.exists(marker_path(database) + RESET_SUFFIX)


def _clear_reset(database) -> None:
    try:
        os.unlink(marker_path(database) + RESET_SUFFIX)
    except OSError:
        pass


def may_create(database, table: str) -> bool:
    """这张表现在允许创建吗？

    - 没有标记 → 首次安装，允许；
    - 有标记但该表从未记录过 → 这是升级新增的表，允许；
    - 有标记且该表被记录过 → 运行期被删，拒绝（只告警）。
    """
    marker = read_marker(database)
    if marker is None:
        return True
    recorded = {str(t).lower() for t in marker.get('tables', []) or []}
    return table.lower() not in recorded


def record_tables(database) -> None:
    """把当前实际存在的表并入标记，供下次判定「是否曾经存在」。"""
    marker = read_marker(database)
    if marker is None:
        return
    recorded = {str(t).lower() for t in marker.get('tables', []) or []}
    batch = getattr(database, 'all_table_names', None)
    try:
        if callable(batch):
            recorded |= {str(t).lower() for t in batch()}
        else:
            recorded |= {
                t for t in _known_names(database) if database.table_exists(t)
            }
    except Exception:
        return
    marker['tables'] = sorted(recorded)
    _write(database, marker)


def _known_names(database) -> list[str]:
    """兜底：没有批量接口时，用核心表 + 扩展表清单逐表探测。"""
    names = []
    try:
        from .init_db import _EXTENSION_TABLES, _REQUIRED_TABLES

        names = list(_REQUIRED_TABLES) + list(_EXTENSION_TABLES)
    except Exception:
        pass
    return names


def _write(database, payload: dict[str, Any]) -> bool:
    path = marker_path(database)
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + '.part'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


__all__ = [
    'MARKER_SUFFIX',
    'clear_marker',
    'has_data_evidence',
    'is_initialized',
    'mark_initialized',
    'marker_path',
    'may_create',
    'read_marker',
    'record_tables',
    'reset_requested',
]
