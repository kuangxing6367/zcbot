# -*- coding: utf-8 -*-
"""权限组与节点管理
组 CRUD + 组/用户节点 upsert

自 framework/perm/ 剥离；核心解析仍在 perm.py，本模块通过
`from framework.perm import ...` / `from framework.perm.admin import ...` 单向依赖。
"""
import logging

logger = logging.getLogger('zcbot')

from framework.perm import _now, normalize_node, invalidate_groups, invalidate_user
from framework.perm.admin import audit


# ═══════════════════════════════════════════════════════════
# 组管理
# ═══════════════════════════════════════════════════════════

def list_groups(db) -> list:
    """全部权限组（含节点数），按 weight 降序"""
    try:
        rows = db.query("SELECT name, display_name, weight, prefix, suffix, is_default, created_at "
                        "FROM perm_groups")
    except Exception as e:
        logger.warning(f"权限: 读取组列表失败 {e}")
        return []
    counts = {}
    try:
        for r in db.query("SELECT group_name, COUNT(*) AS c FROM perm_group_nodes GROUP BY group_name"):
            counts[r['group_name']] = int(r['c'] or 0)
    except Exception:
        pass
    result = []
    for r in rows:
        d = dict(r)
        d['node_count'] = counts.get(r['name'], 0)
        d['builtin'] = False
        result.append(d)
    result.sort(key=lambda x: (-int(x.get('weight') or 0), x['name']))
    return result


def get_group(db, name) -> dict:
    try:
        return db.query_one("SELECT * FROM perm_groups WHERE name = %s", (name,))
    except Exception:
        return None


def create_group(db, name, display_name=None, weight=0, prefix=None, suffix=None,
                 is_default=0, operator='system'):
    name = str(name).strip()
    if not name or name.startswith('__'):
        raise ValueError("组名非法（不能为空或以 __ 开头，__ 前缀为内置组保留）")
    if get_group(db, name):
        raise ValueError(f"权限组 {name} 已存在")
    db.execute(
        "INSERT INTO perm_groups "
        "(name, display_name, weight, prefix, suffix, is_default, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (name, display_name or name, int(weight), prefix, suffix,
         1 if is_default else 0, str(int(_now())))
    )
    audit(db, operator, 'creategroup', 'group', name,
          detail=f"weight={weight} default={bool(is_default)}")
    invalidate_groups()
    invalidate_user()
    return True


def update_group(db, name, display_name=None, weight=None, prefix=None, suffix=None,
                 is_default=None, operator='system'):
    fields, args = [], []
    if display_name is not None:
        fields.append("display_name = %s")
        args.append(display_name)
    if weight is not None:
        fields.append("weight = %s")
        args.append(int(weight))
    if prefix is not None:
        fields.append("prefix = %s")
        args.append(prefix)
    if suffix is not None:
        fields.append("suffix = %s")
        args.append(suffix)
    if is_default is not None:
        fields.append("is_default = %s")
        args.append(1 if is_default else 0)
    if not fields:
        return False
    args.append(name)
    db.execute(f"UPDATE perm_groups SET {', '.join(fields)} WHERE name = %s", tuple(args))
    audit(db, operator, 'updategroup', 'group', name, detail=', '.join(
        f for f in fields))
    invalidate_groups()
    invalidate_user()
    return True


def delete_group(db, name, operator='system'):
    """删除组：同时清理该组节点、其他组对它的继承、以及用户身上对它的归属"""
    inherit_node = f"group.{name}"
    try:
        db.execute("DELETE FROM perm_group_nodes WHERE group_name = %s", (name,))
        db.execute("DELETE FROM perm_group_nodes WHERE node = %s", (inherit_node,))
        db.execute("DELETE FROM perm_user_nodes WHERE node = %s", (inherit_node,))
        db.execute("DELETE FROM perm_groups WHERE name = %s", (name,))
    except Exception as e:
        logger.warning(f"权限: 删除组 {name} 失败 {e}")
        raise
    audit(db, operator, 'deletegroup', 'group', name)
    invalidate_groups()
    invalidate_user()
    return True


# ═══════════════════════════════════════════════════════════
# 节点管理
# ═══════════════════════════════════════════════════════════

def _delete_node(db, table, key_field, key_val, node, ctx_key, ctx_val):
    """删除同（目标, 节点, 上下文）的旧记录，保证 upsert 幂等"""
    if ctx_key:
        db.execute(
            f"DELETE FROM {table} WHERE {key_field} = %s AND node = %s "
            f"AND context_key = %s AND context_val = %s",
            (key_val, node, ctx_key, ctx_val))
    else:
        db.execute(
            f"DELETE FROM {table} WHERE {key_field} = %s AND node = %s "
            f"AND (context_key IS NULL OR context_key = '')",
            (key_val, node))


def set_group_node(db, group_name, node, value=True, ctx_key=None, ctx_val=None,
                   expire_at=None, operator='system'):
    node = normalize_node(node)
    if not node:
        raise ValueError("节点名不能为空")
    ctx_key = (ctx_key or '').strip() or None
    ctx_val = None if ctx_key is None else str(ctx_val or '')
    _delete_node(db, 'perm_group_nodes', 'group_name', group_name, node, ctx_key, ctx_val)
    db.execute(
        "INSERT INTO perm_group_nodes "
        "(group_name, node, value, context_key, context_val, expire_at, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (group_name, node, 1 if value else 0, ctx_key, ctx_val,
         None if expire_at is None else str(int(expire_at)), str(int(_now())))
    )
    audit(db, operator, 'set', 'group', group_name, node, value,
          {ctx_key: ctx_val} if ctx_key else None)
    invalidate_groups()
    invalidate_user()
    return True


def unset_group_node(db, group_name, node, ctx_key=None, ctx_val=None, operator='system'):
    node = normalize_node(node)
    ctx_key = (ctx_key or '').strip() or None
    ctx_val = None if ctx_key is None else str(ctx_val or '')
    _delete_node(db, 'perm_group_nodes', 'group_name', group_name, node, ctx_key, ctx_val)
    audit(db, operator, 'unset', 'group', group_name, node, None,
          {ctx_key: ctx_val} if ctx_key else None)
    invalidate_groups()
    invalidate_user()
    return True


def set_user_node(db, user_id, node, value=True, ctx_key=None, ctx_val=None,
                  expire_at=None, operator='system'):
    node = normalize_node(node)
    if not node:
        raise ValueError("节点名不能为空")
    ctx_key = (ctx_key or '').strip() or None
    ctx_val = None if ctx_key is None else str(ctx_val or '')
    _delete_node(db, 'perm_user_nodes', 'user_id', user_id, node, ctx_key, ctx_val)
    db.execute(
        "INSERT INTO perm_user_nodes "
        "(user_id, node, value, context_key, context_val, expire_at, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (int(user_id), node, 1 if value else 0, ctx_key, ctx_val,
         None if expire_at is None else str(int(expire_at)), str(int(_now())))
    )
    audit(db, operator, 'set', 'user', user_id, node, value,
          {ctx_key: ctx_val} if ctx_key else None)
    invalidate_user(user_id)
    return True


def unset_user_node(db, user_id, node, ctx_key=None, ctx_val=None, operator='system'):
    node = normalize_node(node)
    ctx_key = (ctx_key or '').strip() or None
    ctx_val = None if ctx_key is None else str(ctx_val or '')
    _delete_node(db, 'perm_user_nodes', 'user_id', user_id, node, ctx_key, ctx_val)
    audit(db, operator, 'unset', 'user', user_id, node, None,
          {ctx_key: ctx_val} if ctx_key else None)
    invalidate_user(user_id)
    return True


def list_user_nodes(db, user_id) -> list:
    try:
        return db.query("SELECT * FROM perm_user_nodes WHERE user_id = %s ORDER BY id",
                        (int(user_id),))
    except Exception:
        return []


def add_user_group(db, user_id, group_name, ctx_key=None, ctx_val=None,
                   expire_at=None, operator='system'):
    return set_user_node(db, user_id, f"group.{group_name}", True, ctx_key, ctx_val,
                         expire_at, operator)


def remove_user_group(db, user_id, group_name, ctx_key=None, ctx_val=None, operator='system'):
    unset_user_node(db, user_id, f"group.{group_name}", ctx_key, ctx_val, operator)
    audit(db, operator, 'removegroup', 'user', user_id, f"group.{group_name}")
    invalidate_user(user_id)
    return True
