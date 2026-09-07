"""
权限系统（LuckPerms 风格）
====================================

在框架原有「单一 role 字符串」身份轴之外，平行提供一套完整的权限节点模型。

核心概念（对齐 Minecraft LuckPerms v5）：

- **节点 node**：`plugin.action.sub` 形式的权限字符串，三态（授予 / 显式否决 / 未定义）
- **组 group**：一组节点的集合，带 weight（权重决定优先级与 primary group）
- **继承**：组通过 `group.xxx` 节点继承另一个组 —— 继承与权限统一用节点表达
- **上下文 context**：节点可限定只在特定环境生效
  - `group=<群号>` / `bot=<OneBot实例名>` / `msgtype=group|private`
  - 上下文为 NULL 表示全局生效
- **临时**：节点可带 `expire_at`（unix 时间戳），过期自动失效
- **否决**：value=0 的同名节点优先于 value=1

内置角色组（不入库，运行时虚拟注入）
------------------------------------
框架原有的 super / owner / admin / member 四层身份，被映射为一条内置继承链：

    __member(w0) ← __admin(w20) ← __owner(w30) ← __super(w100)

每个内置组自带 `zcbot.role.{member|admin|owner|super}` 节点，因此：

    require_level='admin'  ≡  检查节点 zcbot.role.admin

语义与 `Event.is_admin` 完全一致，但 `Event.role` 本身一个字都不用改。

优先级规则（从高到低）
----------------------
1. 用户直接节点
2. 所属组的节点，按组 weight 降序（weight 相同按组名字典序，保证确定性）
3. 同一来源内按节点精确度：精确 `a.b.c` > 段级通配 `a.b.*` > 全局 `*`
4. 同一精确度下，value=0（否决）优先于 value=1

第一条能给出结论的来源即为最终结果，不再往下找。

上下文限制
----------
当前实现每条节点只能绑定**一个**上下文维度（数据库为 context_key / context_val 两列）。
需要组合条件（如「群 123 且私聊」）时暂不支持，属已知取舍：换取索引可用 + 管理界面
可以用下拉框而不是自由文本输入。确有需要时再加 context_extra 列扩展。
"""
import logging
import time

logger = logging.getLogger('zcbot')

# ── 缓存 ────────────────────────────────────────────────────
# 解析结果缓存：(user_id, 上下文本地键, role) -> (PermissionSet, ts)
_PERM_CACHE_TTL = 60.0
_PERM_CACHE_MAX = 5000
_perm_cache = {}
_perm_cache_checks = 0

# 组快照缓存（全表读取，避免每个用户解析都重复拉组数据）
_SNAPSHOT_TTL = 10.0
_snapshot_cache = {'ts': 0.0, 'groups': {}, 'nodes': {}}

# ── 内置角色组 ──────────────────────────────────────────────
# inherits 顺着链表即得到全部祖先节点，因此 super 自动拥有 owner/admin/member 的一切
BUILTIN_GROUPS = {
    '__member': {'weight': 0,   'display_name': '成员',   'inherits': [],           'node': 'zcbot.role.member'},
    '__admin':  {'weight': 20,  'display_name': '群管理', 'inherits': ['__member'], 'node': 'zcbot.role.admin'},
    '__owner':  {'weight': 30,  'display_name': '群主',   'inherits': ['__admin'],  'node': 'zcbot.role.owner'},
    '__super':  {'weight': 100, 'display_name': '超级管理员', 'inherits': ['__owner'], 'node': 'zcbot.role.super'},
}

# Event.role -> 内置组名。blacklist 由 router 在权限判定之前拦截，
# 这里退化为 member，保证语义与「黑名单不参与权限计算」一致
ROLE_TO_GROUP = {
    'member': '__member',
    'admin': '__admin',
    'owner': '__owner',
    'super': '__super',
    'blacklist': '__member',
}

CONTEXT_KEYS = ('group', 'bot', 'msgtype')


# ═══════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════

def normalize_node(node) -> str:
    """节点规范化：去空白 + 转小写（与 LuckPerms 一致，权限节点大小写不敏感）"""
    return (str(node or '')).strip().lower()


def _now() -> float:
    return time.time()


def _parse_ts(val):
    """解析 expire_at（VARCHAR 存的 unix 时间戳字符串），失败/空返回 None"""
    if val is None or val == '':
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _alive(row, now: float) -> bool:
    """行是否未过期"""
    exp = _parse_ts(row.get('expire_at'))
    return exp is None or exp > now


def _ctx_match(row, context: dict) -> bool:
    """行的上下文是否与当前环境匹配（上下文为空=全局，永远匹配）"""
    key = row.get('context_key')
    val = row.get('context_val')
    if not key or val is None or val == '':
        return True
    if not context:
        return False
    return str(context.get(key, '')).lower() == str(val).lower()


def _ctx_signature(context: dict) -> str:
    """上下文指纹，用于缓存键（只取受支持的维度，顺序固定）"""
    if not context:
        return ''
    return '|'.join(f'{k}={context.get(k, "")}' for k in CONTEXT_KEYS if context.get(k))


def _bool_val(row) -> bool:
    v = row.get('value')
    return False if v in (0, '0', False) else True


# ═══════════════════════════════════════════════════════════
# 解析结果
# ═══════════════════════════════════════════════════════════

class PermissionSet:
    """一次权限解析的结果（不可变快照）

    :ivar user_id: QQ 号
    :ivar context: 解析时使用的上下文字典
    :ivar groups:  生效组名列表，按 weight 降序
    :ivar nodes:   合并后的节点表 {node: True/False}
    """

    # _sources：按优先级排列的多个节点表（用户直节点 → 各组按 weight 降序）
    # 必须在类定义时就声明，事后追加 __slots__ 不会生成描述符
    __slots__ = ('user_id', 'context', 'groups', 'nodes', '_meta', '_sources')

    def __init__(self, user_id, context, groups, nodes, meta, sources=None):
        self.user_id = user_id
        self.context = context or {}
        self.groups = groups
        self.nodes = nodes
        self._meta = meta or {}
        self._sources = sources or []

    # ---- 查询 ----

    def check(self, node: str):
        """三态查询：True=授予 / False=显式否决 / None=未定义"""
        n = normalize_node(node)
        if not n:
            return None
        # 节点表已按来源顺序合并：先写入的优先级高，命中即返回
        for src in self._sources:
            hit = _resolve_in_source(src, n)
            if hit is not None:
                return hit
        return None

    def has(self, node: str) -> bool:
        """二态查询：未定义按拒绝处理（与 LuckPerms 默认行为一致）"""
        return self.check(node) is True

    def has_any(self, *nodes) -> bool:
        return any(self.has(n) for n in nodes)

    def has_all(self, *nodes) -> bool:
        return all(self.has(n) for n in nodes)

    # ---- 组信息 ----

    @property
    def primary_group(self) -> str:
        """权重最高且非内置的组；没有则回退 default"""
        for g in self.groups:
            if not g.startswith('__'):
                return g
        return 'default'

    def in_group(self, name: str) -> bool:
        """是否在指定组内（groups 已包含继承展开的结果，无需二次展开）"""
        return normalize_node(name) in self.groups

    @property
    def prefix(self) -> str:
        return self._meta.get('prefix') or ''

    @property
    def suffix(self) -> str:
        return self._meta.get('suffix') or ''

    def to_dict(self) -> dict:
        return {
            'user_id': self.user_id,
            'context': self.context,
            'groups': list(self.groups),
            'primary_group': self.primary_group,
            'nodes': {k: v for k, v in self.nodes.items()},
        }

    def __repr__(self):
        return f'<PermissionSet user={self.user_id} groups={self.groups} nodes={len(self.nodes)}>'


def _resolve_in_source(nodes, node: str):
    """在单一来源内按精确度解析节点

    精确度：精确(0) > 段级通配(1..n，前缀越长越精确) > 全局 `*`(999)
    同一精确度下 value=0（否决）优先
    """
    cands = []
    if node in nodes:
        cands.append((0, nodes[node]))
    parts = node.split('.')
    for i in range(len(parts) - 1, 0, -1):
        wildcard = '.'.join(parts[:i]) + '.*'
        if wildcard in nodes:
            cands.append((len(parts) - i, nodes[wildcard]))
    if '*' in nodes:
        cands.append((999, nodes['*']))
    if not cands:
        return None
    best = min(s for s, _ in cands)
    vals = [v for s, v in cands if s == best]
    return False if False in vals else True


# ═══════════════════════════════════════════════════════════
# 缓存
# ═══════════════════════════════════════════════════════════

def invalidate_user(user_id=None):
    """清除用户权限缓存（Web 端改动后调用，None=全部）"""
    if user_id is None:
        _perm_cache.clear()
    else:
        for k in [k for k in _perm_cache if k[0] == user_id]:
            _perm_cache.pop(k, None)


def invalidate_groups():
    """清除组快照缓存（改动组/组节点后调用）"""
    _snapshot_cache['ts'] = 0.0


def invalidate_all():
    invalidate_user()
    invalidate_groups()


def _lazy_cleanup(now: float):
    """惰性上限清理：每 256 次访问检查一次，防止长期运行内存无限增长"""
    global _perm_cache_checks
    _perm_cache_checks += 1
    if _perm_cache_checks % 256:
        return
    if len(_perm_cache) > _PERM_CACHE_MAX:
        for k in [k for k, v in _perm_cache.items() if now - v[1] > _PERM_CACHE_TTL]:
            _perm_cache.pop(k, None)


# ═══════════════════════════════════════════════════════════
# 组快照
# ═══════════════════════════════════════════════════════════

def _load_snapshot(db) -> dict:
    """加载全部组定义与组节点（表小，整体缓存 10s，避免按组逐条查库）"""
    now = _now()
    if _snapshot_cache['ts'] and now - _snapshot_cache['ts'] <= _SNAPSHOT_TTL:
        return _snapshot_cache

    groups = {}
    try:
        for r in db.query("SELECT name, display_name, weight, prefix, suffix, is_default "
                          "FROM perm_groups"):
            groups[r['name']] = {
                'name': r['name'],
                'display_name': r.get('display_name') or r['name'],
                'weight': int(r.get('weight') or 0),
                'prefix': r.get('prefix') or '',
                'suffix': r.get('suffix') or '',
                'is_default': 1 if r.get('is_default') else 0,
                'builtin': False,
            }
    except Exception as e:
        logger.debug(f"权限: 读取 perm_groups 失败 {e}")

    nodes = {}
    try:
        for r in db.query("SELECT group_name, node, value, context_key, context_val, expire_at "
                          "FROM perm_group_nodes"):
            nodes.setdefault(r['group_name'], []).append(r)
    except Exception as e:
        logger.debug(f"权限: 读取 perm_group_nodes 失败 {e}")

    _snapshot_cache['ts'] = now
    _snapshot_cache['groups'] = groups
    _snapshot_cache['nodes'] = nodes
    return _snapshot_cache


def _builtin_group_meta(name: str) -> dict:
    b = BUILTIN_GROUPS[name]
    return {
        'name': name,
        'display_name': b['display_name'],
        'weight': b['weight'],
        'prefix': '',
        'suffix': '',
        'is_default': 0,
        'builtin': True,
    }


# ═══════════════════════════════════════════════════════════
# 解析主流程
# ═══════════════════════════════════════════════════════════

def resolve(db, user_id, context=None, role=None, use_cache=True) -> PermissionSet:
    """解析用户在指定上下文下的完整权限

    :param db: 数据库实例
    :param user_id: QQ 号
    :param context: {'group': '123456', 'bot': 'main', 'msgtype': 'group'}，None 表示无上下文
    :param role: 框架身份（Event.role），用于注入内置角色组
    :return: PermissionSet
    """
    context = context or {}
    sig = _ctx_signature(context)
    key = (user_id, sig, role or '')
    now = _now()

    if use_cache:
        hit = _perm_cache.get(key)
        if hit and now - hit[1] <= _PERM_CACHE_TTL:
            return hit[0]
        _lazy_cleanup(now)

    snap = _load_snapshot(db)
    groups_meta = dict(snap['groups'])
    groups_nodes = snap['nodes']

    # ── 1. 用户直接节点 + 组归属 ──
    direct = {}
    joined, excluded = [], set()
    try:
        rows = db.query("SELECT node, value, context_key, context_val, expire_at "
                        "FROM perm_user_nodes WHERE user_id = %s", (user_id,))
    except Exception as e:
        logger.debug(f"权限: 读取用户节点失败 {e}")
        rows = []

    for r in rows:
        if not _alive(r, now) or not _ctx_match(r, context):
            continue
        node = normalize_node(r.get('node'))
        if not node:
            continue
        if node.startswith('group.'):
            gname = node[6:]
            if _bool_val(r):
                joined.append(gname)
            else:
                excluded.add(gname)
        else:
            direct[node] = _bool_val(r)

    # ── 2. 默认组 + 内置角色组 ──
    for name, meta in groups_meta.items():
        if meta['is_default'] and name not in excluded:
            joined.append(name)

    builtin = ROLE_TO_GROUP.get(role or '')
    if builtin:
        joined.append(builtin)

    # ── 3. 沿 group.xxx 展开继承（BFS，环检测）──
    all_groups = _expand_inheritance(joined, excluded, groups_nodes, now, context)

    # ── 4. 按 weight 降序排序（同名按字典序，保证确定性）──
    def _weight_of(g):
        if g in BUILTIN_GROUPS:
            return BUILTIN_GROUPS[g]['weight']
        return (groups_meta.get(g) or {}).get('weight', 0)

    all_groups.sort(key=lambda g: (-_weight_of(g), g))

    # ── 5. 收集节点来源：用户直节点优先，其后各组按 weight 降序 ──
    sources = [direct] if direct else []
    merged = {}
    for g in all_groups:
        src = {}
        for r in groups_nodes.get(g, []):
            if not _alive(r, now) or not _ctx_match(r, context):
                continue
            node = normalize_node(r.get('node'))
            if not node or node.startswith('group.'):
                continue
            src[node] = _bool_val(r)
        # 内置组的身份节点
        bnode = BUILTIN_GROUPS.get(g, {}).get('node')
        if bnode:
            src[bnode] = True
        if src:
            sources.append(src)
        for k, v in src.items():
            merged.setdefault(k, v)

    # ── 6. 元信息（前缀/后缀取 primary group）──
    meta = {}
    for g in all_groups:
        if g.startswith('__'):
            continue
        m = groups_meta.get(g)
        if m:
            meta = {'prefix': m['prefix'], 'suffix': m['suffix']}
            break

    pset = PermissionSet(user_id, context, all_groups, merged, meta, sources)
    _perm_cache[key] = (pset, now)
    return pset


def _expand_inheritance(joined, excluded, groups_nodes, now, context):
    """BFS 展开 group.xxx 继承链，带环检测；被显式否决的组及其子节点不生效"""
    result, seen, queue = [], set(), list(joined)
    while queue:
        g = queue.pop(0)
        if g in seen or g in excluded:
            continue
        seen.add(g)
        result.append(g)
        for r in groups_nodes.get(g, []):
            if not _alive(r, now) or not _ctx_match(r, context):
                continue
            node = normalize_node(r.get('node'))
            if not node or not node.startswith('group.') or not _bool_val(r):
                continue
            parent = node[6:]
            if parent not in seen:
                queue.append(parent)
    # 内置组的静态继承（__admin ⊃ __member 等）
    added = True
    while added:
        added = False
        for g in list(result):
            for parent in BUILTIN_GROUPS.get(g, {}).get('inherits', []):
                if parent not in result:
                    result.append(parent)
                    added = True
    return result


# ═══════════════════════════════════════════════════════════
# 便捷 API
# ═══════════════════════════════════════════════════════════

def has_perm(db, user_id, node, context=None, role=None) -> bool:
    """单点权限判断（未定义按拒绝）"""
    return resolve(db, user_id, context, role).has(node)


def check_perm(db, user_id, node, context=None, role=None):
    """三态权限判断：True / False / None"""
    return resolve(db, user_id, context, role).check(node)


def user_groups(db, user_id, context=None, role=None) -> list:
    """用户生效组（含继承展开，按 weight 降序）"""
    return list(resolve(db, user_id, context, role).groups)


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
