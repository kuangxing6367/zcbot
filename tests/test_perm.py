# -*- coding: utf-8 -*-
"""权限引擎自测（临时脚本，验证后删除）"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.db import Database, _auto_create_tables
from framework import perm

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       f'_perm_test_{int(time.time() * 1000)}.db')

db = Database({'type': 'sqlite', 'path': DB_PATH})
_auto_create_tables(db)

ok = fail = 0


def chk(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  PASS  {label}  -> {got!r}")
    else:
        fail += 1
        print(f"  FAIL  {label}  -> got {got!r}, want {want!r}")


print("\n== 建表 ==")
chk("perm_groups 存在", db.table_exists('perm_groups'), True)
chk("perm_user_nodes 存在", db.table_exists('perm_user_nodes'), True)
chk("perm_group_nodes 存在", db.table_exists('perm_group_nodes'), True)
chk("perm_tracks 存在", db.table_exists('perm_tracks'), True)
chk("perm_audit 存在", db.table_exists('perm_audit'), True)
chk("默认组已播种", bool(perm.get_group(db, 'default')), True)

print("\n== 组与继承 ==")
perm.create_group(db, 'vip', 'VIP', weight=10)
perm.create_group(db, 'moderator', '管理', weight=30)
perm.set_group_node(db, 'moderator', 'group.vip')          # moderator 继承 vip
perm.set_group_node(db, 'vip', 'chat.color')
perm.set_group_node(db, 'vip', 'chat.image')
perm.set_group_node(db, 'moderator', 'chat.ban')
perm.set_group_node(db, 'moderator', 'chat.kick', value=False)   # 显式否决
perm.set_group_node(db, 'vip', 'chat.*', value=False)            # vip 层通配否决

UID = 10001
perm.add_user_group(db, UID, 'moderator')

ps = perm.resolve(db, UID, {}, 'member', use_cache=False)
chk("继承展开 moderator ⊃ vip", ('moderator' in ps.groups and 'vip' in ps.groups), True)
chk("默认组自动加入", 'default' in ps.groups, True)
chk("primary_group = weight 最高的 moderator", ps.primary_group, 'moderator')
chk("moderator 自身节点", ps.has('chat.ban'), True)
chk("继承得到 vip 节点 chat.color", ps.has('chat.color'), True)
chk("显式否决优先 chat.kick", ps.has('chat.kick'), False)
chk("未定义节点返回 None", ps.check('nothing.here'), None)
chk("未定义节点 has() 为 False", ps.has('nothing.here'), False)

print("\n== 组间优先级 ==")
ps2 = perm.resolve(db, UID, {}, 'member', use_cache=False)
chk("组按 weight 降序", ps2.groups.index('moderator') < ps2.groups.index('vip'), True)

# 高权重组的通配否决 应覆盖 低权重组的精确授予
perm.create_group(db, 'restricted', '受限', weight=90)
perm.set_group_node(db, 'restricted', 'chat.*', value=False)
perm.add_user_group(db, 40004, 'moderator')
chk("受限前 chat.ban 放行",
    perm.resolve(db, 40004, {}, 'member', use_cache=False).has('chat.ban'), True)
perm.add_user_group(db, 40004, 'restricted')
chk("加入高权重受限组后 chat.ban 被否决",
    perm.resolve(db, 40004, {}, 'member', use_cache=False).has('chat.ban'), False)
chk("同组内精确授予可绕过自身通配否决（vip 的 chat.color）",
    perm.resolve(db, UID, {}, 'member', use_cache=False).has('chat.color'), True)

print("\n== 内置角色组 ==")
for role, node in (('member', 'zcbot.role.member'), ('admin', 'zcbot.role.admin'),
                   ('owner', 'zcbot.role.owner'), ('super', 'zcbot.role.super')):
    p = perm.resolve(db, UID, {}, role, use_cache=False)
    chk(f"role={role} 持有 {node}", p.has(node), True)

p_super = perm.resolve(db, UID, {}, 'super', use_cache=False)
chk("super 继承得到 zcbot.role.admin", p_super.has('zcbot.role.admin'), True)
chk("super 继承得到 zcbot.role.member", p_super.has('zcbot.role.member'), True)
p_admin = perm.resolve(db, UID, {}, 'admin', use_cache=False)
chk("admin 不持有 zcbot.role.owner", p_admin.has('zcbot.role.owner'), False)

print("\n== 上下文 ==")
perm.set_group_node(db, 'vip', 'chat.sticker', ctx_key='group', ctx_val='555')
perm.set_group_node(db, 'moderator', 'chat.mute', ctx_key='group', ctx_val='555')
perm.set_group_node(db, 'moderator', 'chat.notice', ctx_key='msgtype', ctx_val='private')

p555 = perm.resolve(db, UID, {'group': '555', 'msgtype': 'group'}, 'member', use_cache=False)
chk("群 555 生效 chat.sticker", p555.has('chat.sticker'), True)
p666 = perm.resolve(db, UID, {'group': '666', 'msgtype': 'group'}, 'member', use_cache=False)
chk("群 666 不生效 chat.sticker", p666.has('chat.sticker'), False)
p_pri = perm.resolve(db, UID, {'msgtype': 'private'}, 'member', use_cache=False)
chk("私聊生效 chat.notice", p_pri.has('chat.notice'), True)
chk("私聊不生效群 555 的 chat.mute", p_pri.has('chat.mute'), False)

print("\n== 用户直节点优先级 ==")
perm.set_user_node(db, UID, 'chat.ban', value=False)
p = perm.resolve(db, UID, {'group': '555'}, 'member', use_cache=False)
chk("用户直节点否决覆盖组的授予", p.has('chat.ban'), False)
perm.unset_user_node(db, UID, 'chat.ban')

print("\n== 临时权限 ==")
perm.set_user_node(db, UID, 'chat.temp', value=True, expire_at=time.time() + 5)
chk("未过期 -> 放行", perm.resolve(db, UID, {}, 'member', use_cache=False).has('chat.temp'), True)
perm.set_user_node(db, UID, 'chat.temp', value=True, expire_at=time.time() - 1)
chk("已过期 -> 拒绝", perm.resolve(db, UID, {}, 'member', use_cache=False).has('chat.temp'), False)
perm.unset_user_node(db, UID, 'chat.temp')

print("\n== Tracks ==")
perm.create_group(db, 'lv1', '一级', weight=1)
perm.create_group(db, 'lv2', '二级', weight=2)
perm.create_group(db, 'lv3', '三级', weight=3)
perm.save_track(db, 'rank', 'lv1,lv2,lv3')
r1 = perm.promote(db, 20002, 'rank')
chk("首次 promote -> lv1", r1['to'], 'lv1')
r2 = perm.promote(db, 20002, 'rank')
chk("再次 promote -> lv2", r2['to'], 'lv2')
chk("promote 后脱离 lv1",
    'group.lv1' not in [perm.normalize_node(x['node']) for x in perm.list_user_nodes(db, 20002)], True)
r3 = perm.demote(db, 20002, 'rank')
chk("demote -> lv1", r3['to'], 'lv1')
try:
    perm.demote(db, 20002, 'rank')
    chk("末端继续 demote 应报错", 'no-error', 'ValueError')
except ValueError:
    chk("末端继续 demote 抛 ValueError", True, True)

print("\n== 通配符 ==")
perm.set_group_node(db, 'vip', 'chat.*', value=True)
perm.unset_group_node(db, 'vip', 'chat.*')
perm.set_group_node(db, 'lv1', 'plugin.*')
p = perm.resolve(db, 20002, {}, 'member', use_cache=False)
chk("段级通配 plugin.anything", p.has('plugin.anything.deep'), True)
chk("通配不越界 chat.x", p.has('chat.x'), False)

print("\n== 缓存与失效 ==")
a = perm.resolve(db, UID, {}, 'member')
b = perm.resolve(db, UID, {}, 'member')
chk("缓存命中同一对象", a is b, True)
perm.invalidate_user(UID)
c = perm.resolve(db, UID, {}, 'member')
chk("失效后重新解析", c is not a, True)

print("\n== 审计 ==")
logs = perm.list_audit(db, 'user', UID, limit=5)
chk("审计有记录", len(logs) > 0, True)

print("\n== 过期清理 ==")
perm.set_user_node(db, 30003, 'tmp.node', value=True, expire_at=time.time() - 10)
n = perm.cleanup_expired(db)
chk("清理掉 1 条过期记录", n >= 1, True)

print(f"\n{'=' * 46}\n通过 {ok} / 失败 {fail}\n{'=' * 46}")
db.close()
# 清理本次临时库（含 SQLite 的 -wal/-shm 旁车文件），避免每次跑测试都残留一个 .db
for _suf in ('', '-wal', '-shm'):
    _p = DB_PATH + _suf
    try:
        if os.path.exists(_p):
            os.remove(_p)
    except OSError:
        pass
sys.exit(1 if fail else 0)
