"""回归测试：群消息的群名必须能落到 groups_info。

OneBot 11 的群消息事件 sender 里只有 nickname/card/role，没有 group_name
（那是 get_group_info 的字段）。此前 _aggregate_registrations 直接读
sender['group_name']，于是 groups_info.group_name 恒为空，群组管理页面
「群名称」整列都是空白。现在改由 dispatch 层按需向适配器查询并缓存。

覆盖：
- 适配器返回 {"data": {"group_name": ...}} 能正确取到
- 适配器返回扁平结构也能取到
- 同一群第二次不再调用适配器（走缓存）
- 查询抛异常时返回空串，绝不向上抛（不能影响消息分发）
"""
import asyncio

from framework.core.dispatch import FrameworkDispatchMixin


class _FakeAdapter:
    def __init__(self, payload=None, boom=False):
        self.payload = payload
        self.boom = boom
        self.calls = 0

    async def acall(self, action, bot=None, **params):
        self.calls += 1
        assert action == 'get_group_info', action
        if self.boom:
            raise RuntimeError('boom')
        return self.payload


class _FakeServices:
    def __init__(self, adapter):
        self._adapter = adapter

    def adapter_for_source(self, src):
        return self._adapter

    def get(self, k, default=None):
        return default


def _make(payload=None, boom=False):
    inst = FrameworkDispatchMixin()
    adapter = _FakeAdapter(payload, boom)
    inst.services = _FakeServices(adapter)
    return inst, adapter


def test_group_name_from_wrapped_data():
    inst, _ = _make({'status': 'ok', 'data': {'group_id': 111, 'group_name': '测试群'}})
    name = asyncio.run(inst._resolve_group_name('default', 111, 'onebot'))
    assert name == '测试群', name


def test_group_name_from_flat_payload():
    inst, _ = _make({'group_id': 111, 'group_name': '扁平群'})
    name = asyncio.run(inst._resolve_group_name('default', 111, 'onebot'))
    assert name == '扁平群', name


def test_group_name_is_cached():
    inst, adapter = _make({'data': {'group_name': '缓存群'}})
    a = asyncio.run(inst._resolve_group_name('default', 222, 'onebot'))
    b = asyncio.run(inst._resolve_group_name('default', 222, 'onebot'))
    assert a == b == '缓存群'
    assert adapter.calls == 1, f"第二次应命中缓存，实际调用 {adapter.calls} 次"


def test_group_name_failure_is_silent():
    inst, _ = _make(boom=True)
    name = asyncio.run(inst._resolve_group_name('default', 333, 'onebot'))
    assert name == '', "查询失败必须返回空串，不能向上抛"


def test_group_name_none_group_id():
    inst, adapter = _make({'data': {'group_name': 'x'}})
    assert asyncio.run(inst._resolve_group_name('default', None, 'onebot')) == ''
    assert adapter.calls == 0, "没有 group_id 时不该发查询"
