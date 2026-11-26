"""被动回复的「引对人 / 不撞名额」回归。

背景（真实线上表现）：一个群只缓存一条 msg_id，A 说完 B 再说就把 A 顶掉，
机器人回 A 时引用的是 B 的消息；同一个 msg_id 还被反复消耗被动回复名额
（官方：群聊一条消息最多回 5 次、单聊 4 次，用光即发送失败）。

这里锁四件事：
1. 群内多人交错发言，按 user_id 回谁就引用谁；
2. 连续多条回复轮换不同的 msg_id，不吊死在同一条上；
3. msg_seq 按 msg_id 递增，名额用光后退回主动消息而不是硬塞重复 seq；
4. ctx.send_msg / asend_msg 直接支持 reply_msg_id 与 active，
   插件不必再掉到 ctx.aapi 那一层。
"""
import asyncio
import time

from core_plugins.qq_official.main import QQOfficialAdapter
from framework.ctx.messaging import PluginMessagingMixin


def _adapter():
    return QQOfficialAdapter(None, app_id='appid', app_secret='secret')


def _seed(a, conv, msg_id, kind='group', author='', age=0.0):
    a._pending_reply.setdefault(conv, []).append(
        (msg_id, time.time() - age, kind, author))


# ── 引用对象 ─────────────────────────────────────────────────


def test_group_picks_message_of_the_user_being_replied_to():
    """A、B 先后发言，回 A 时必须引 A 的消息（而不是会话里最新的那条）"""
    a = _adapter()
    _seed(a, 'G1', 'MA', author='UA', age=2)
    _seed(a, 'G1', 'MB', author='UB', age=1)

    assert a._reply_msg_id('G1', 'UA') == 'MA'
    assert a._reply_msg_id('G1', 'UB') == 'MB'


def test_group_replies_rotate_instead_of_reusing_one_msg_id():
    """不带 user_id 时先回最新的，再回次新的——不反复消耗同一条的名额"""
    a = _adapter()
    _seed(a, 'G1', 'MA', author='UA', age=3)
    _seed(a, 'G1', 'MB', author='UB', age=2)

    first = a._reply_msg_id('G1')
    assert first == 'MB'
    a._reply_seq[first] = 1          # 模拟这一条已被发送消耗
    assert a._reply_msg_id('G1') == 'MA'


def test_unknown_author_falls_back_to_conversation():
    """传入的 user_id 在该会话没有待回复消息时，退回会话内最新一条"""
    a = _adapter()
    _seed(a, 'G1', 'MA', author='UA')
    assert a._reply_msg_id('G1', 'UOTHER') == 'MA'


def test_no_pending_returns_empty():
    a = _adapter()
    assert a._reply_msg_id('G1') == ''
    assert a._reply_msg_id(None) == ''


# ── msg_seq 与名额 ───────────────────────────────────────────


def test_msg_seq_increments_per_msg_id_and_stops_at_cap():
    """群聊同一条消息最多回 5 次：seq 1..5 之后不再挂 msg_id"""
    async def run():
        a = _adapter()
        seqs = []
        for _ in range(6):
            body, err = await a._build_body('G1', 'hi', 'M5', is_group=True)
            assert not err
            seqs.append((body.get('msg_id'), body.get('msg_seq')))
        return seqs

    seqs = asyncio.run(run())
    assert seqs[:5] == [('M5', 1), ('M5', 2), ('M5', 3), ('M5', 4), ('M5', 5)]
    # 第 6 次：名额已尽，退回主动消息（不带 msg_id / msg_seq）
    assert seqs[5] == (None, None)


def test_c2c_cap_is_four_and_counted_separately():
    """单聊一条消息最多回 4 次（与群聊各自计数）"""
    async def run():
        a = _adapter()
        out = []
        for _ in range(5):
            body, _err = await a._build_body('U1', 'hi', 'M9', is_group=False)
            out.append(body.get('msg_seq'))
        return out

    assert asyncio.run(run()) == [1, 2, 3, 4, None]


def test_exhausted_msg_id_is_not_picked_again():
    """名额用光的消息不再被自动选中"""
    a = _adapter()
    _seed(a, 'G1', 'MOLD', author='UA')
    a._reply_seq['MOLD'] = 5
    assert a._reply_msg_id('G1') == ''


def test_expired_entries_are_dropped_with_their_seq():
    a = _adapter()
    _seed(a, 'G1', 'MOLD', author='UA', age=400)
    a._reply_seq['MOLD'] = 2
    assert a._reply_msg_id('G1') == ''
    assert 'G1' not in a._pending_reply


def test_pending_is_bounded():
    """连发不停也不能把缓存撑爆"""
    a = _adapter()
    for i in range(50):
        a._remember_pending('G1', f'M{i}', 'group', 'UA')
    assert len(a._pending_reply['G1']) <= a._PENDING_PER_CONV
    for i in range(600):
        a._remember_pending(f'G{i}', f'MX{i}', 'group')
    assert len(a._pending_reply) <= a._PENDING_MAX_CONV


def test_same_message_twice_is_not_duplicated():
    """Resume 重放同一条消息，不该在待回复列表里占两格"""
    a = _adapter()
    a._remember_pending('G1', 'MA', 'group')
    a._remember_pending('G1', 'MA', 'group')
    assert [e[0] for e in a._pending_reply['G1']] == ['MA']


# ── 互动回调走 event_id ──────────────────────────────────────


def test_interaction_reply_uses_event_id_field():
    """按钮回调的被动回复必须放 event_id；塞 msg_id 平台会判消息不存在"""
    async def run():
        a = _adapter()
        a._remember_pending('G1', 'I1', 'group', is_interaction=True)
        body, _err = await a._build_body('G1', 'hi', 'I1', is_group=True)
        return body

    body = asyncio.run(run())
    assert body.get('event_id') == 'I1'
    assert 'msg_id' not in body and 'msg_seq' not in body


# ── ctx 层快捷参数 ───────────────────────────────────────────


class _Ctx(PluginMessagingMixin):
    def __init__(self, act):
        self._act = act
        self._plugin_name = 'test'

    @property
    def actions(self):
        return self._act


class _DynamicApi:
    """吞 **params 的动作面（协议适配器走 call_api）"""

    def __init__(self):
        self.calls = []

    def send_msg(self, **params):
        self.calls.append(params)
        return {'status': 'ok', 'retcode': 0}

    async def acall(self, action, bot=None, **params):
        return self.send_msg(**params)


class _FixedApi:
    """OneBot11 那种固定签名：多传关键字会 TypeError"""

    def __init__(self):
        self.calls = []

    def send_msg(self, user_id=None, group_id=None, message=None,
                 auto_escape=False, bot=None):
        self.calls.append({'user_id': user_id, 'group_id': group_id,
                           'message': message, 'auto_escape': auto_escape,
                           'bot': bot})
        return {'status': 'ok', 'retcode': 0}

    async def acall(self, action, bot=None, **params):
        return self.send_msg(**params)


def test_ctx_passes_reply_msg_id_to_dynamic_action_surface():
    api = _DynamicApi()
    asyncio.run(_Ctx(api).asend_msg(group_id='G1', message='hi',
                                    reply_msg_id='M1'))
    assert api.calls[0]['reply_msg_id'] == 'M1'
    assert api.calls[0]['message'] == 'hi'


def test_ctx_translates_reply_msg_id_to_reply_segment_for_onebot():
    """固定签名动作面：改成标准 reply 段，不往里塞它不认识的参数"""
    api = _FixedApi()
    asyncio.run(_Ctx(api).asend_msg(group_id=123, message='hi',
                                    reply_msg_id='M1'))
    call = api.calls[0]
    assert 'reply_msg_id' not in call
    assert call['message'][0] == {'type': 'reply', 'data': {'id': 'M1'}}
    assert call['message'][1]['type'] == 'text'


def test_ctx_active_skips_reply_id():
    api = _DynamicApi()
    asyncio.run(_Ctx(api).asend_msg(group_id='G1', message='hi',
                                    reply_msg_id='M1', active=True))
    assert api.calls[0].get('active') is True
    assert 'reply_msg_id' not in api.calls[0]


def test_ctx_sync_send_supports_reply_msg_id():
    api = _DynamicApi()
    _Ctx(api).send_msg(group_id='G1', message='hi', reply_msg_id='M2')
    assert api.calls[0]['reply_msg_id'] == 'M2'
