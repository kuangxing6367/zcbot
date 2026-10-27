# -*- coding: utf-8 -*-
"""多平台接入：适配器注册表（drive 表）与身份命名空间工具。

背景：zcbot 默认只允许单一平台视图——所有适配器共享同一套 user/group 身份。
「多平台接入」概念要求：
  1. 数据库有一张 drive 表，记录每个适配器（按连接标识 adapter_key）的稳定 uuid；
  2. 首次加载/注册适配器时自动登记该 uuid（持久化复用，重启不变）；
  3. 配置开关 multi_platform.namespace 打开后，user/group 身份按
     ``<adapter_uuid>@<id>`` 形式命名空间隔离（即用户概念中的 user-<uuid> / group-<uuid>），
     不同适配器下同一 QQ 号被视为不同身份；关闭时退化为裸号（旧行为不变）。

本模块只负责「适配器 ↔ uuid 映射」与「身份键改写」，不改动任何现有写库语义。
"""
import logging
import threading
import uuid as _uuid

logger = logging.getLogger('zcbot')


class AdapterRegistry:
    """适配器实例注册表（对应数据库 drive 表）。

    维护 ``adapter_key``（连接标识，如 onebot / qq_official / telegram …）
    与稳定 ``uuid`` 的映射；首次 ensure 时落库，之后全程走内存缓存，
    热路径（路由/写库）零 DB 查询。
    """

    def __init__(self, db, framework=None):
        self.db = db
        self.framework = framework
        self._cache = {}                 # adapter_key -> uuid
        self._lock = threading.Lock()

    # ── 自动登记（首次加载/注册适配器时调用）──

    def ensure(self, adapter_key: str, kind: str = '', self_id=None) -> str:
        """确保 adapter_key 在 drive 表中有稳定 uuid，返回该 uuid（内存缓存）。

        已登记则直接返回并轻量刷新 last_seen；未登记则生成 uuid4 落库。
        任何异常都降级返回 ''（不阻断适配器启动）。
        """
        if not adapter_key:
            return ''
        with self._lock:
            cached = self._cache.get(adapter_key)
            if cached:
                self._touch(adapter_key, kind, self_id)
                return cached
        try:
            row = self.db.query_one(
                "SELECT uuid FROM drive WHERE adapter_key=%s", (adapter_key,))
            if row and row.get('uuid'):
                uuid_val = row['uuid']
            else:
                uuid_val = _uuid.uuid4().hex
                self.db.execute(
                    "INSERT INTO drive (adapter_key, uuid, kind, self_id, "
                    "created_at, last_seen, enabled) "
                    "VALUES (%s,%s,%s,%s,NOW(),NOW(),1) "
                    "ON DUPLICATE KEY UPDATE kind=VALUES(kind), "
                    "self_id=VALUES(self_id), last_seen=NOW()",
                    (adapter_key, uuid_val, kind or '',
                     '' if self_id is None else str(self_id)))
            with self._lock:
                self._cache[adapter_key] = uuid_val
            return uuid_val
        except Exception as e:
            logger.error(f"适配器注册表 ensure 失败 [{adapter_key}]: {e}")
            return ''

    def _touch(self, adapter_key, kind, self_id):
        """已缓存路径：仅当 self_id 变化时写库刷新（不阻塞返回路径）。"""
        if self_id is None:
            return
        try:
            self.db.execute(
                "UPDATE drive SET last_seen=NOW(), self_id=%s "
                "WHERE adapter_key=%s",
                (str(self_id), adapter_key))
        except Exception:
            pass

    def uuid_for(self, adapter_key: str) -> str:
        """查内存缓存中的 uuid（无则返回 ''）。"""
        with self._lock:
            return self._cache.get(adapter_key) or ''

    def refresh(self):
        """从数据库重载缓存（启动后/适配器动态变更后调用）。"""
        try:
            rows = self.db.query("SELECT adapter_key, uuid FROM drive")
            with self._lock:
                self._cache = {r['adapter_key']: r['uuid'] for r in rows}
        except Exception as e:
            logger.warning(f"适配器注册表刷新失败: {e}")

    def all(self) -> list:
        """返回全部 drive 行（面板/调试用）。"""
        try:
            return self.db.query(
                "SELECT adapter_key, uuid, kind, self_id, created_at, "
                "last_seen, enabled FROM drive ORDER BY adapter_key")
        except Exception as e:
            logger.error(f"查询适配器注册表失败: {e}")
            return []


def namespace_identity(registry, adapter_key, enabled, raw_id):
    """把裸身份改写为命名空间键（enabled=False 或无法解析时返回原值）。

    形态：``<adapter_uuid>@<raw_id>``（即用户概念中的 user-<uuid> / group-<uuid>）。
    raw_id 为 0/空 时直接返回，避免生成 ``<uuid>@0`` 这类无意义键。
    """
    if not enabled or not adapter_key or raw_id in (None, '', 0):
        return raw_id
    if registry is None:
        return raw_id
    uuid_val = registry.uuid_for(adapter_key)
    if not uuid_val:
        return raw_id
    return f"{uuid_val}@{raw_id}"
