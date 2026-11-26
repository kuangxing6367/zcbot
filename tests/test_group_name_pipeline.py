"""集成测试：群里来一条消息，群名要一路传到 groups_info 的写入参数。

分两段验证同一条链路：
1) dispatch 层：事件 sender 没有 group_name 时，向适配器查一次并回填到
   sender（同时写回 event，路由层也能看到）；
2) 统计写入层：_aggregate_registrations 把 sender['group_name'] 收进
   groups 映射，最终作为 groups_info.group_name 的写入值。

背景：OneBot 11 群消息事件的 sender 只有 nickname/card/role，
group_name 属于 get_group_info 的返回字段。缺了这段，群组管理里
群名整列永远为空。
"""
import asyncio
from unittest.mock import AsyncMock

from framework.core.dispatch import FrameworkDispatchMixin
from framework.core.stats_writer import AsyncStatsWriter
from framework.hooks import HookRegistry


class _Adapter:
    def __init__(self, name):
        self.name = name
        self.calls = 0

    async def acall(self, action, bot=None, **params):
        self.calls += 1
        return {'status': 'ok', 'data': {'group_id': params.get('group_id'),
                                         'group_name': self.name}}


class _Services:
    def __init__(self, adapter):
        self._a = adapter

    def adapter_for_source(self, src):
        return self._a

    def get(self, k, default=None):
        return default


class _Stats:
    def __init__(self):
        self.calls = []

    def register_user(self, user_id, sender, message_type, group_id, adapter_key=None):
        self.calls.append({'user_id': user_id, 'sender': dict(sender),
                           'message_type': message_type,
                           'group_id': group_id, 'adapter_key': adapter_key})


class _Router:
    def __init__(self):
        self.seen = None

    async def route(self, event, bot_name='default'):
        self.seen = event


def _build(group_name_in_sender=None):
    adapter = _Adapter('真实群名')
    inst = FrameworkDispatchMixin()
    inst.config = {'log': {'log_raw_message': False}}
    inst.services = _Services(adapter)
    inst.stats_writer = _Stats()
    inst.router = _Router()
    inst.hooks = HookRegistry(inst)
    inst._dispatch_raw_message_handlers = AsyncMock(return_value=False)

    sender = {'user_id': 10001, 'nickname': '小明', 'card': '', 'role': 'member'}
    if group_name_in_sender is not None:
        sender['group_name'] = group_name_in_sender
    event = {
        'type': 'message', 'post_type': 'message', 'message_type': 'group',
        'user_id': 10001, 'group_id': 555, 'bot_name': 'default',
        'message': [{'type': 'text', 'data': {'text': 'hi'}}],
        'raw_message': 'hi', 'sender': sender,
    }
    return inst, adapter, event


def test_dispatch_backfills_group_name_when_missing():
    inst, adapter, event = _build()

    asyncio.run(inst._process_event(event))

    assert adapter.calls == 1, "缺群名时应查询一次 get_group_info"
    assert inst.stats_writer.calls, "消息应进入自动注册队列"
    rec = inst.stats_writer.calls[0]
    assert rec['sender'].get('group_name') == '真实群名', rec['sender']
    assert event['sender'].get('group_name') == '真实群名', \
        "群名要写回 event，路由层同样需要读它"
    assert inst.router.seen is event


def test_dispatch_keeps_existing_group_name():
    inst, adapter, event = _build(group_name_in_sender='事件自带群名')

    asyncio.run(inst._process_event(event))

    assert adapter.calls == 0, "事件已带群名就不该再查接口"
    assert inst.stats_writer.calls[0]['sender']['group_name'] == '事件自带群名'


def test_dispatch_group_name_failure_does_not_break_dispatch():
    class Boom:
        async def acall(self, *a, **k):
            raise RuntimeError('接口挂了')

    inst, _, event = _build()
    inst.services = _Services(Boom())

    asyncio.run(inst._process_event(event))

    assert inst.stats_writer.calls, "查群名失败不能影响消息继续处理"
    assert inst.stats_writer.calls[0]['sender'].get('group_name') in (None, '')


def test_aggregate_carries_group_name_into_write_params():
    w = AsyncStatsWriter.__new__(AsyncStatsWriter)          # 不启动线程与连接池
    w._namespace_cache = {}

    def _ns(gid, key):
        return str(gid)

    w._namespace_id = _ns
    items = [(10001, {'group_name': '真实群名'}, 'group', 555, 'onebot')]
    _users, got_groups, _members = w._aggregate_registrations(items)

    assert got_groups == {'555': '真实群名'}, got_groups
