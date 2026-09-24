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

    :ivar user_id: 用户 ID
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
    :param user_id: 用户 ID
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
# 子模块懒加载 re-export（保持 from framework.perm import X 兼容）
# ═══════════════════════════════════════════════════════════
_LAZY_EXPORTS = {
    'audit': 'framework.perm.admin',
    'list_audit': 'framework.perm.admin',
    'cleanup_expired': 'framework.perm.admin',
    'context_from_event': 'framework.perm.admin',
    'list_groups': 'framework.perm.groups',
    'get_group': 'framework.perm.groups',
    'create_group': 'framework.perm.groups',
    'update_group': 'framework.perm.groups',
    'delete_group': 'framework.perm.groups',
    '_delete_node': 'framework.perm.groups',
    'set_group_node': 'framework.perm.groups',
    'unset_group_node': 'framework.perm.groups',
    'set_user_node': 'framework.perm.groups',
    'unset_user_node': 'framework.perm.groups',
    'list_user_nodes': 'framework.perm.groups',
    'add_user_group': 'framework.perm.groups',
    'remove_user_group': 'framework.perm.groups',
    'list_tracks': 'framework.perm.tracks',
    'get_track': 'framework.perm.tracks',
    'save_track': 'framework.perm.tracks',
    'delete_track': 'framework.perm.tracks',
    '_track_step': 'framework.perm.tracks',
    'promote': 'framework.perm.tracks',
    'demote': 'framework.perm.tracks',
}


def __getattr__(name):
    mod_name = _LAZY_EXPORTS.get(name)
    if mod_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    mod = importlib.import_module(mod_name)
    try:
        return getattr(mod, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
