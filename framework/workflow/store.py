"""工作流定义与 QQ 绑定的数据库存取。

表里只存 JSON 文本与绑定关系，不做复杂关系建模：工作流数量有限，每次事件
读取走内存快照，配置变更时由管理面主动 invalidate。数据库层只负责持久化与
冷启动加载，不在消息热路径上查询。
"""
import json
import logging
import threading
import time

logger = logging.getLogger('zcbot')

_SNAPSHOT_TTL = 5.0


class WorkflowStore:
    """工作流存储 + TTL 快照。

    快照超时后下一次读取重新查库；管理面写完立即调用 ``invalidate()``，
    让新配置当场生效，不必等 TTL。
    """

    def __init__(self, db):
        self.db = db
        self._lock = threading.RLock()
        self._snapshot = None
        self._snapshot_at = 0.0
        self._generation = 0

    def invalidate(self):
        with self._lock:
            self._snapshot = None
            self._snapshot_at = 0.0
            self._generation += 1

    # ── 快照 ──────────────────────────────────────────────
    def _load(self):
        try:
            rows = self.db.query(
                'SELECT id, name, nodes, enabled, is_default FROM workflows '
                'ORDER BY id ASC')
            binds = self.db.query(
                'SELECT user_id, workflow_id FROM workflow_bindings WHERE enabled=1')
        except Exception as e:
            logger.warning('加载工作流快照失败（按无工作流处理）: %s', e)
            return {'workflows': {}, 'by_user': {}, 'default': None}

        workflows = {}
        default = None
        for row in rows:
            if not row.get('enabled'):
                continue
            try:
                nodes = json.loads(row.get('nodes') or '[]')
            except (TypeError, ValueError) as e:
                logger.warning('工作流 [%s] 节点 JSON 解析失败，已跳过: %s',
                               row.get('name'), e)
                continue
            item = {
                'id': row.get('id'),
                'name': row.get('name'),
                'nodes': nodes if isinstance(nodes, list) else [],
            }
            workflows[int(row['id'])] = item
            if row.get('is_default') and default is None:
                default = item

        by_user = {}
        for row in binds:
            uid = str(row.get('user_id') or '').strip()
            wid = row.get('workflow_id')
            if uid and wid in workflows:
                by_user[uid] = workflows[wid]
        return {'workflows': workflows, 'by_user': by_user, 'default': default}

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            if self._snapshot is not None and now - self._snapshot_at < _SNAPSHOT_TTL:
                return self._snapshot
            snap = self._load()
            self._snapshot = snap
            self._snapshot_at = now
            return snap

    def for_user(self, user_id):
        snap = self.snapshot()
        uid = str(user_id or '').strip()
        return snap['by_user'].get(uid) or snap['default']

    # ── 管理面 CRUD ───────────────────────────────────────
    def list(self):
        try:
            rows = self.db.query(
                'SELECT id, name, description, nodes, enabled, is_default, '
                'created_at, updated_at FROM workflows ORDER BY id ASC')
        except Exception as e:
            logger.error('列出工作流失败: %s', e)
            return []
        out = []
        for row in rows:
            try:
                nodes = json.loads(row.get('nodes') or '[]')
            except (TypeError, ValueError):
                nodes = []
            out.append({**row, 'nodes': nodes})
        return out

    def get(self, wid):
        rows = self.db.query(
            'SELECT id, name, description, nodes, enabled, is_default, '
            'created_at, updated_at FROM workflows WHERE id=%s', (wid,))
        if not rows:
            return None
        row = rows[0]
        try:
            row['nodes'] = json.loads(row.get('nodes') or '[]')
        except (TypeError, ValueError):
            row['nodes'] = []
        return row

    def create(self, name, nodes, description='', enabled=True, is_default=False):
        now = self._now()
        payload = json.dumps(nodes, ensure_ascii=False)
        self.db.execute(
            'INSERT INTO workflows (name, description, nodes, enabled, is_default, '
            'created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',
            (name, description, payload, int(bool(enabled)), int(bool(is_default)),
             now, now))
        self.invalidate()
        rows = self.db.query('SELECT id FROM workflows WHERE name=%s', (name,))
        return rows[0]['id'] if rows else None

    def update(self, wid, name, nodes, description='', enabled=True, is_default=False):
        payload = json.dumps(nodes, ensure_ascii=False)
        self.db.execute(
            'UPDATE workflows SET name=%s, description=%s, nodes=%s, enabled=%s, '
            'is_default=%s, updated_at=%s WHERE id=%s',
            (name, description, payload, int(bool(enabled)), int(bool(is_default)),
             self._now(), wid))
        self.invalidate()

    def delete(self, wid):
        self.db.execute('DELETE FROM workflow_bindings WHERE workflow_id=%s', (wid,))
        self.db.execute('DELETE FROM workflows WHERE id=%s', (wid,))
        self.invalidate()

    def bindings(self, wid=None):
        if wid is None:
            rows = self.db.query(
                'SELECT id, user_id, workflow_id, note, created_at '
                'FROM workflow_bindings ORDER BY id ASC')
        else:
            rows = self.db.query(
                'SELECT id, user_id, workflow_id, note, created_at '
                'FROM workflow_bindings WHERE workflow_id=%s ORDER BY id ASC', (wid,))
        return rows

    def set_bindings(self, wid, user_ids):
        """整组替换某条工作流的 QQ 绑定，避免逐条比对。"""
        self.db.execute('DELETE FROM workflow_bindings WHERE workflow_id=%s', (wid,))
        seen = set()
        for value in user_ids:
            uid = str(value).strip()
            if not uid or uid in seen:
                continue
            seen.add(uid)
            self.db.execute(
                'INSERT INTO workflow_bindings (user_id, workflow_id, enabled) '
                'VALUES (%s,%s,1)', (uid, wid))
        self.invalidate()

    @staticmethod
    def _now():
        import datetime
        return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
