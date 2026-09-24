# -*- coding: utf-8 -*-
"""权限审计与维护
audit / list_audit / cleanup_expired / context_from_event

自 framework/perm/ 剥离；核心解析仍在 perm.py，本模块通过
`from framework.perm import ...` / `from framework.perm.admin import ...` 单向依赖。
"""
import logging

logger = logging.getLogger('zcbot')

from framework.perm import _now, _parse_ts, invalidate_all


# ═══════════════════════════════════════════════════════════
# 审计
# ═══════════════════════════════════════════════════════════

def audit(db, operator, action, target_type=None, target=None,
          node=None, value=None, context=None, detail=None):
    """写一条权限变更审计（失败不影响主流程）"""
    try:
        ctx_str = ''
        if context:
            ctx_str = ';'.join(f'{k}={v}' for k, v in context.items() if v)
        db.execute(
            "INSERT INTO perm_audit "
            "(operator, action, target_type, target, node, value, context, detail, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (str(operator or 'system'), action, target_type, str(target or ''),
             node, None if value is None else (1 if value else 0),
             ctx_str or None, detail, str(int(_now())))
        )
    except Exception as e:
        logger.warning(f"权限审计写入失败: {e}")


def list_audit(db, target_type=None, target=None, limit=100) -> list:
    try:
        if target_type and target:
            return db.query("SELECT * FROM perm_audit WHERE target_type=%s AND target=%s "
                            "ORDER BY id DESC LIMIT %s", (target_type, str(target), int(limit)))
        return db.query("SELECT * FROM perm_audit ORDER BY id DESC LIMIT %s", (int(limit),))
    except Exception as e:
        logger.warning(f"权限审计读取失败: {e}")
        return []


# ═══════════════════════════════════════════════════════════
# 维护
# ═══════════════════════════════════════════════════════════

def cleanup_expired(db) -> int:
    """清理已过期的节点（供定时调度调用），返回清理条数"""
    now_str = str(int(_now()))
    removed = 0
    try:
        for table in ('perm_user_nodes', 'perm_group_nodes'):
            rows = db.query(
                f"SELECT id, expire_at FROM {table} "
                f"WHERE expire_at IS NOT NULL AND expire_at != ''")
            for r in rows:
                exp = _parse_ts(r.get('expire_at'))
                if exp is not None and exp <= _now():
                    db.execute(f"DELETE FROM {table} WHERE id = %s", (r['id'],))
                    removed += 1
        if removed:
            invalidate_all()
            logger.info(f"权限: 清理过期节点 {removed} 条")
    except Exception as e:
        logger.warning(f"权限: 清理过期节点失败 {e}")
    return removed


def context_from_event(ev) -> dict:
    """从 Event 对象构造上下文（供 Event.has_perm 使用）"""
    ctx = {}
    if getattr(ev, 'bot_name', None):
        ctx['bot'] = str(ev.bot_name)
    if getattr(ev, 'group_id', 0):
        ctx['group'] = str(ev.group_id)
    if getattr(ev, 'message_type', ''):
        ctx['msgtype'] = str(ev.message_type)
    return ctx
