# -*- coding: utf-8 -*-
"""多平台接入（multi-platform access）逻辑验证。

覆盖：
  1. AdapterRegistry.ensure 自动登记 + uuid 稳定 + 内存缓存 + 落库持久化
  2. namespace_identity 命名空间键改写（开关 / adapter_key 缺失 / 空 id 退化）
  3. stats_writer 用户/群/成员自动注册在 namespace 开启时改写身份键
  4. dispatch 群成员同步在 namespace 开启时改写 group_members 落库键
  5. Event.role 按命名空间键查 users/group_members，与写库一致
  6. Framework.adapter_plugin_allowed 双向绑定判定（白名单 + 插件声明）

运行：python tests/test_multi_platform.py
（本文件为脚本式：直接 python 运行，不依赖 pytest 收集）
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.database.db import Database
from framework.messaging.adapter_registry import AdapterRegistry, namespace_identity
from framework.core.stats_writer import AsyncStatsWriter
from framework.core.dispatch import FrameworkDispatchMixin
from framework.messaging.event import Event

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


def make_db():
    tmp = tempfile.mktemp(suffix=".db")
    db = Database({"type": "sqlite", "path": tmp})
    for sql in [
        "CREATE TABLE drive (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "adapter_key VARCHAR(64) NOT NULL, uuid VARCHAR(64) NOT NULL, "
        "kind VARCHAR(64) DEFAULT '', self_id VARCHAR(64) DEFAULT '', "
        "created_at VARCHAR(32) DEFAULT NULL, last_seen VARCHAR(32) DEFAULT NULL, "
        "enabled INTEGER DEFAULT 1, UNIQUE(adapter_key))",
        "CREATE TABLE users (user_id VARCHAR(64) NOT NULL PRIMARY KEY, "
        "nickname VARCHAR(100) DEFAULT NULL, is_blacklist INTEGER DEFAULT 0, "
        "role VARCHAR(20) DEFAULT '', first_seen_at VARCHAR(32), last_active_at VARCHAR(32))",
        "CREATE TABLE groups_info (group_id VARCHAR(64) NOT NULL PRIMARY KEY, "
        "group_name VARCHAR(200) DEFAULT NULL, is_active INTEGER DEFAULT 1, join_at VARCHAR(32))",
        "CREATE TABLE group_members (group_id VARCHAR(64) NOT NULL, user_id VARCHAR(64) NOT NULL, "
        "card VARCHAR(100), role VARCHAR(20) DEFAULT 'member', title VARCHAR(100), "
        "message_count INTEGER DEFAULT 0, last_active_at VARCHAR(32), "
        "PRIMARY KEY(group_id, user_id))",
    ]:
        db.execute(sql)
    return db


class StubFramework(FrameworkDispatchMixin):
    """最小可运行的 Framework 替身：仅提供多平台接入所需字段/方法。"""

    def __init__(self, db, ns_enabled, config=None, plugins=None):
        self.db = db
        self._ns = ns_enabled
        self.config = config or {}
        self.adapter_registry = AdapterRegistry(db, self)
        self._member_sync_queue = asyncio.Queue()
        self.plugin_loader = _StubLoader(plugins or {})

    def multi_platform_namespace(self):
        return self._ns

    def adapter_plugin_allowed(self, plugin_name, adapter_key):
        """复用真实实现（见模块级 adapter_plugin_allowed）。"""
        return adapter_plugin_allowed(self, plugin_name, adapter_key)

    # adapter_plugin_allowed 直接复用真实实现（若可导入），否则用等价副本
    if False:  # 占位，避免静态分析误报；真实方法在下方动态绑定
        pass


class _StubModule:
    """模拟插件模块：真实插件用模块级 __plugin_meta__ 声明元信息。"""

    def __init__(self, meta):
        self.__plugin_meta__ = meta


class _StubLoader:
    def __init__(self, plugins):
        self._plugins = plugins  # {name: {'meta': {...}}}

    def get_loaded_plugins(self):
        return self._plugins

    def get_plugin_module(self, plugin_name):
        info = self._plugins.get(plugin_name)
        return _StubModule(info.get('meta') or {}) if info else None


# 尝试复用真实 Framework.adapter_plugin_allowed，否则用等价实现
try:
    from framework.core.base import Framework
    _real_allowed = Framework.adapter_plugin_allowed
except Exception as _e:  # 导入失败（依赖过重）→ 等价副本
    print(f"  [INFO] 无法导入 framework.core.base（{_e}），使用等价副本验证 adapter_plugin_allowed")
    _real_allowed = None


def adapter_plugin_allowed(fw, plugin_name, adapter_key):
    if _real_allowed is not None:
        return _real_allowed(fw, plugin_name, adapter_key)
    # 等价副本（与 framework/core/base.py 保持一致）
    try:
        whitelist = (fw.config.get(adapter_key) or {}).get('plugins') or []
    except Exception:
        whitelist = []
    if whitelist and plugin_name not in whitelist:
        return False
    try:
        module = fw.plugin_loader.get_plugin_module(plugin_name)
        meta = getattr(module, '__plugin_meta__', None) or {}
        padapters = meta.get('adapters') or []
    except Exception:
        padapters = []
    if padapters and adapter_key not in padapters:
        return False
    return True


def main():
    print("== 多平台接入验证 ==")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # ── 1. AdapterRegistry 自动登记 + uuid 稳定 + 缓存 ──
    print("[1] AdapterRegistry 自动登记")
    db = make_db()
    reg = AdapterRegistry(db, None)
    key = "qq_official"
    u1 = reg.ensure(key, kind="QQOfficialAdapter", self_id="10001")
    check("ensure 返回非空 uuid", bool(u1), repr(u1))
    u2 = reg.ensure(key, kind="QQOfficialAdapter", self_id="10001")
    check("同一 adapter_key 返回的 uuid 稳定", u1 == u2, f"{u1} != {u2}")
    row = db.query_one("SELECT uuid FROM drive WHERE adapter_key=%s", (key,))
    check("uuid 已落库", row and row["uuid"] == u1, repr(row))
    check("uuid_for 命中缓存", reg.uuid_for(key) == u1)
    # 新实例（新缓存）从库恢复
    reg2 = AdapterRegistry(db, None)
    check("重建注册表能从库恢复 uuid", reg2.ensure(key) == u1)
    # 不同 adapter_key 不同 uuid
    u3 = reg.ensure("onebot", kind="OneBotAdapter", self_id=12345)
    check("不同 adapter_key 生成不同 uuid", u1 != u3 and bool(u3))

    # ── 2. namespace_identity 改写规则 ──
    print("[2] namespace_identity 键改写")
    check("关闭时返回裸号", namespace_identity(reg, key, False, 12345) == 12345)
    check("adapter_key 空时返回裸号", namespace_identity(reg, "", True, 12345) == 12345)
    check("raw_id 为 0 时返回 0", namespace_identity(reg, key, True, 0) == 0)
    check("raw_id 空串时返回空串", namespace_identity(reg, key, True, "") == "")
    check("开启且合法时返回 <uuid>@<id>",
          namespace_identity(reg, key, True, 12345) == f"{u1}@12345")

    # ── 3. stats_writer 自动注册改写身份键 ──
    print("[3] stats_writer 身份键命名空间")
    fw_off = StubFramework(db, ns_enabled=False)
    writer_off = AsyncStatsWriter(fw_off)
    fw_on = StubFramework(db, ns_enabled=True)
    fw_on.adapter_registry.ensure(key)   # 预热缓存（生产由启动期 _on_adapter_registered 完成）
    writer_on = AsyncStatsWriter(fw_on)

    # 构造 items 同 register_user 的入队单元： (user, sender, msg_type, group, adapter_key)
    items = [(12345, {"nickname": "张三", "card": "卡", "group_name": "群A",
                      "role": "member", "title": ""}, "group", 999, key)]

    u_off, g_off, m_off = writer_off._aggregate_registrations(items)
    u_on, g_on, m_on = writer_on._aggregate_registrations(items)
    check("关闭时 user 键为裸号", list(u_off.keys()) == [12345], repr(u_off))
    check("开启时 user 键为 <uuid>@<id>", list(u_on.keys()) == [f"{u1}@12345"], repr(u_on))
    check("关闭时 group 键为裸号", list(g_off.keys()) == [999], repr(g_off))
    check("开启时 group 键为 <uuid>@<id>", list(g_on.keys()) == [f"{u1}@999"], repr(g_on))
    mg_key_off = list(m_off.keys())[0]
    mg_key_on = list(m_on.keys())[0]
    check("关闭时 member 键 (group,user) 裸号", mg_key_off == (999, 12345), repr(mg_key_off))
    check("开启时 member 键 (group,user) 命名空间",
          mg_key_on == (f"{u1}@999", f"{u1}@12345"), repr(mg_key_on))

    # 真实写库：开启 namespace 时落库键应与 role 查询键一致
    fw_on.adapter_registry.ensure(key)
    writer_on._write_users(u_on)
    writer_on._write_groups(g_on)
    writer_on._write_members(m_on)
    urow = db.query_one("SELECT user_id FROM users WHERE user_id=%s", (f"{u1}@12345",))
    check("开启时 users 表确为命名空间键", bool(urow), repr(urow))
    mrow = db.query_one(
        "SELECT group_id,user_id FROM group_members WHERE group_id=%s AND user_id=%s",
        (f"{u1}@999", f"{u1}@12345"))
    check("开启时 group_members 表确为命名空间键", bool(mrow), repr(mrow))

    # ── 4. dispatch 群成员同步改写落库键 ──
    print("[4] dispatch 群成员同步命名空间")
    fw_d_on = StubFramework(db, ns_enabled=True)
    fw_d_on.adapter_registry.ensure(key)  # 预热缓存
    # 模拟一条 join 入队
    async def _enqueue():
        fw_d_on._enqueue_member_sync("join",
            {"group_id": 999, "user_id": 12345, "adapter": key})
    asyncio.get_event_loop().run_until_complete(_enqueue())
    fw_d_on._flush_member_sync()
    mrow2 = db.query_one(
        "SELECT group_id,user_id FROM group_members WHERE group_id=%s AND user_id=%s",
        (f"{u1}@999", f"{u1}@12345"))
    check("群成员同步按命名空间落库", bool(mrow2), repr(mrow2))
    # 关闭时裸号
    fw_d_off = StubFramework(db, ns_enabled=False)
    async def _enqueue2():
        fw_d_off._enqueue_member_sync("join",
            {"group_id": 888, "user_id": 777, "adapter": "onebot"})
    asyncio.get_event_loop().run_until_complete(_enqueue2())
    fw_d_off._flush_member_sync()
    mrow3 = db.query_one(
        "SELECT group_id,user_id FROM group_members WHERE group_id=%s AND user_id=%s",
        (888, 777))
    check("群成员同步关闭时裸号落库", bool(mrow3), repr(mrow3))

    # ── 5. Event.role 按命名空间键查权限 ──
    print("[5] Event.role 命名空间隔离")
    # 写入：qq_official 下 7777 是 super；onebot 下 7777 是普通
    # （用 7777 避开 [3] 已写库的 12345，避免主键冲突）
    db.execute("INSERT INTO users (user_id, nickname, role) VALUES (%s,%s,%s)",
               (f"{u1}@7777", "A", "super"))
    reg.ensure("onebot", kind="OneBotAdapter", self_id=12345)
    u_one = reg.uuid_for("onebot")
    db.execute("INSERT INTO users (user_id, nickname, role) VALUES (%s,%s,%s)",
               (f"{u_one}@7777", "B", "member"))
    # namespace 开启；预热注册表缓存（生产由启动期完成）
    fw_role = StubFramework(db, ns_enabled=True)
    fw_role.adapter_registry.ensure("qq_official")
    fw_role.adapter_registry.ensure("onebot")
    ev_qq = Event({"type": "message", "message_type": "private", "user_id": 7777,
                   "adapter": "qq_official", "message": "x"}, "qq_official")
    ev_qq._framework = fw_role
    ev_one = Event({"type": "message", "message_type": "private", "user_id": 7777,
                    "adapter": "onebot", "message": "x"}, "onebot")
    ev_one._framework = fw_role
    check("qq_official 下 7777 为 super", ev_qq.role == "super", ev_qq.role)
    check("onebot 下 7777 为 member（隔离）", ev_one.role == "member", ev_one.role)
    # namespace 关闭：裸号 7777 不在 users（只存了命名空间键）→ 兜底 member
    fw_role_off = StubFramework(db, ns_enabled=False)
    ev_off = Event({"type": "message", "message_type": "private", "user_id": 7777,
                    "adapter": "qq_official", "message": "x"}, "qq_official")
    ev_off._framework = fw_role_off
    check("关闭时裸号查无记录兜底 member", ev_off.role == "member", ev_off.role)

    # ── 6. adapter_plugin_allowed 双向绑定 ──
    print("[6] adapter_plugin_allowed 双向绑定")
    cfg = {
        "qq_official": {"plugins": ["hello", "guardian"]},
    }
    plugins = {
        "hello": {"meta": {}},                       # 无 adapters 声明
        "guardian": {"meta": {"adapters": ["qq_official"]}},
        "broad": {"meta": {"adapters": ["onebot"]}},
        "free": {"meta": {}},
    }
    fw_bind = StubFramework(db, ns_enabled=False, config=cfg, plugins=plugins)
    # qq_official 白名单含 hello → 放行；不含 broad → 拒绝
    check("白名单命中放行(hello)", adapter_plugin_allowed(fw_bind, "hello", "qq_official") is True)
    check("白名单未含拒绝(broad)", adapter_plugin_allowed(fw_bind, "broad", "qq_official") is False)
    # 白名单未配置适配器的消息源（onebot）→ 白名单空，放行
    check("无白名单源放行(free,onebot)", adapter_plugin_allowed(fw_bind, "free", "onebot") is True)
    # guardian 声明仅 qq_official → 在 qq_official 放行，在 onebot 拒绝
    check("插件声明匹配源放行(guardian,qq_official)",
          adapter_plugin_allowed(fw_bind, "guardian", "qq_official") is True)
    check("插件声明不匹配源拒绝(guardian,onebot)",
          adapter_plugin_allowed(fw_bind, "guardian", "onebot") is False)
    # 双方约束：broad 声明 onebot，且在 qq_official 白名单外 → 拒绝（任一不满足）
    check("插件声明不匹配即拒绝(broad,qq_official)",
          adapter_plugin_allowed(fw_bind, "broad", "qq_official") is False)

    # ── 7. 消息广播按适配器过滤订阅者（ctx.on('message') 通道）──
    print("[7] 消息广播订阅者过滤")
    from framework.messaging.event_bus import EventBus
    from framework.messaging.router import MessageRouter

    bus = EventBus()
    calls = []
    bus.subscribe('message', 'hello', lambda p: calls.append('hello'))
    bus.subscribe('message', 'guardian', lambda p: calls.append('guardian'))
    bus.subscribe('message', 'broad', lambda p: calls.append('broad'))

    fw_bus = StubFramework(db, ns_enabled=False, config=cfg, plugins=plugins)
    fw_bus.event_bus = bus
    router = MessageRouter(fw_bus)

    async def _broadcast(adapter_key):
        calls.clear()
        ev_b = Event({"type": "message", "message_type": "group", "user_id": 1,
                      "group_id": 2, "adapter": adapter_key, "message": "hi"}, adapter_key)
        ev_b._framework = fw_bus
        await router._broadcast_message_event(ev_b, "hi")
        return list(calls)

    got_qq = loop.run_until_complete(_broadcast('qq_official'))
    # qq_official：白名单 [hello, guardian] → broad 被拒
    check("qq_official 广播只到白名单插件", got_qq == ['hello', 'guardian'], got_qq)
    got_one = loop.run_until_complete(_broadcast('onebot'))
    # onebot：无白名单；guardian 声明仅 qq_official → 被拒；broad 声明 onebot → 放行
    check("onebot 广播按插件声明过滤（guardian 被拒）",
          got_one == ['hello', 'broad'], got_one)

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
