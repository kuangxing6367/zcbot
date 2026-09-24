# -*- coding: utf-8 -*-
"""权限 Tracks 升降级
轨道 CRUD + promote/demote

自 framework/perm/ 剥离；核心解析仍在 perm.py，本模块通过
`from framework.perm import ...` / `from framework.perm.admin import ...` 单向依赖。
"""
import logging

logger = logging.getLogger('zcbot')

from framework.perm import _now, normalize_node, invalidate_groups, invalidate_user
from framework.perm.admin import audit
from framework.perm.groups import (
    add_user_group, list_user_nodes, remove_user_group,
)


# ═══════════════════════════════════════════════════════════
# Tracks 升降级
# ═══════════════════════════════════════════════════════════

def list_tracks(db) -> list:
    try:
        return db.query("SELECT * FROM perm_tracks ORDER BY name")
    except Exception:
        return []


def get_track(db, name) -> dict:
    try:
        return db.query_one("SELECT * FROM perm_tracks WHERE name = %s", (name,))
    except Exception:
        return None


def save_track(db, name, groups_order, display_name=None, operator='system'):
    groups_order = ','.join(g.strip() for g in str(groups_order).split(',') if g.strip())
    if not groups_order:
        raise ValueError("轨道至少需要一个组")
    if get_track(db, name):
        db.execute("UPDATE perm_tracks SET groups_order = %s, display_name = %s WHERE name = %s",
                   (groups_order, display_name or name, name))
    else:
        db.execute(
            "INSERT INTO perm_tracks (name, display_name, groups_order, created_at) "
            "VALUES (%s, %s, %s, %s)",
            (name, display_name or name, groups_order, str(int(_now()))))
    audit(db, operator, 'savetrack', 'track', name, detail=groups_order)
    invalidate_groups()
    invalidate_user()
    return True


def delete_track(db, name, operator='system'):
    db.execute("DELETE FROM perm_tracks WHERE name = %s", (name,))
    audit(db, operator, 'deletetrack', 'track', name)
    return True


def _track_step(db, user_id, track_name, direction, ctx_key=None, ctx_val=None,
                operator='system'):
    """direction: 1=promote(向右) / -1=demote(向左)"""
    track = get_track(db, track_name)
    if not track:
        raise ValueError(f"轨道 {track_name} 不存在")
    ladder = [g.strip() for g in (track.get('groups_order') or '').split(',') if g.strip()]
    if not ladder:
        raise ValueError(f"轨道 {track_name} 为空")

    current = [normalize_node(r.get('node'))[6:]
               for r in list_user_nodes(db, user_id)
               if normalize_node(r.get('node')).startswith('group.')]
    idx = -1
    for i, g in enumerate(ladder):
        if g in current:
            idx = i
            break
    target = idx + direction
    if idx < 0:
        target = 0 if direction > 0 else -1
    if target < 0 or target >= len(ladder):
        raise ValueError("已在轨道末端，无法继续" + ("晋升" if direction > 0 else "降级"))

    if idx >= 0:
        remove_user_group(db, user_id, ladder[idx], ctx_key, ctx_val, operator)
    add_user_group(db, user_id, ladder[target], ctx_key, ctx_val, None, operator)
    audit(db, operator, 'promote' if direction > 0 else 'demote', 'user', user_id,
          f"group.{ladder[target]}", detail=f"track={track_name} {ladder[idx] if idx >= 0 else '(none)'} → {ladder[target]}")
    invalidate_user(user_id)
    return {'from': ladder[idx] if idx >= 0 else None, 'to': ladder[target]}


def promote(db, user_id, track_name, ctx_key=None, ctx_val=None, operator='system'):
    return _track_step(db, user_id, track_name, 1, ctx_key, ctx_val, operator)


def demote(db, user_id, track_name, ctx_key=None, ctx_val=None, operator='system'):
    return _track_step(db, user_id, track_name, -1, ctx_key, ctx_val, operator)
