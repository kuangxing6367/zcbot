# -*- coding: utf-8 -*-
"""
消息契约测试：规范消息段 / 接入端契约 / 事件补齐

这批用例守的是"跨协议一致"这条底线：
- 语音在 OneBot 叫 record、在 Telegram 叫 voice、在 QQ 官方是独立 file_type，
  但插件侧只能看到 `voice`
- Telegram/Discord 的附件必须变成消息段，不能只剩一串文本
- 接入端契约自检要能点名不合规的实现
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.messaging import contract as C  # noqa: E402
from framework.messaging import segments as S  # noqa: E402
from framework.messaging.event import Event  # noqa: E402
from framework.messaging.protocol import ProtocolAdapter  # noqa: E402

_PASS = []
_FAIL = []


def check(name, cond, extra=''):
    if cond:
        _PASS.append(name)
    else:
        _FAIL.append(f"{name} {extra}")
    return cond


# ─────────────────────────────────────────────────────
# 1. 规范消息段
# ─────────────────────────────────────────────────────

def test_alias_record_to_voice():
    """OneBot 的 record / 各家 audio 统一归位成 voice"""
    segs = S.normalize_message([{'type': 'record', 'data': {'file': 'a.silk'}}])
    check('record→voice', segs[0]['type'] == 'voice', segs)
    segs2 = S.normalize_message([{'type': 'audio', 'data': {'file_id': 'x'}}])
    check('audio→voice', segs2[0]['type'] == 'voice', segs2)


def test_alias_image_variants():
    for src in ('img', 'pic', 'photo', 'image'):
        segs = S.normalize_message([{'type': src, 'data': {'file': 'x'}}])
        check(f'{src}→image', segs[0]['type'] == 'image', segs)


def test_cq_string_parsed():
    """CQ 码字符串也能进规范形态（历史字符串格式不能丢）"""
    segs = S.normalize_message('你好[CQ:at,qq=123][CQ:image,file=a.jpg]尾')
    types = [s['type'] for s in segs]
    check('CQ解析', types == ['text', 'at', 'image', 'text'], types)
    check('CQ文本', S.segments_to_text(segs) == '你好[@123]尾',
          S.segments_to_text(segs))


def test_plain_string():
    segs = S.normalize_message('就是一句话')
    check('纯文本', segs == [{'type': 'text', 'data': {'text': '就是一句话'}}], segs)


def test_media_only_flag():
    segs = S.normalize_message([{'type': 'image', 'data': {'file': 'a.jpg'}}])
    check('纯图片=media_only', S.is_media_only(segs))
    check('纯图片文本为空', S.segments_to_text(segs) == '',
          repr(S.segments_to_text(segs)))
    segs2 = S.normalize_message([{'type': 'image', 'data': {'file': 'a.jpg'}},
                                 {'type': 'text', 'data': {'text': '看'}}])
    check('图+文≠media_only', not S.is_media_only(segs2))


def test_media_ref_across_protocols():
    """各协议把文件放不同字段，media_ref 取值要统一"""
    cases = [
        ({'type': 'image', 'data': {'file': 'http://x/a.png'}}, 'http://x/a.png'),
        ({'type': 'image', 'data': {'file_id': 'TG123'}}, 'TG123'),
        ({'type': 'voice', 'data': {'url': 'https://y/a.ogg'}}, 'https://y/a.ogg'),
        ({'type': 'file', 'data': {'file_info': 'QQFI'}}, 'QQFI'),
        ({'type': 'image', 'data': {'base64': 'AAAA'}}, 'base64://AAAA'),
    ]
    for seg, want in cases:
        got = S.media_ref(seg)
        check(f'media_ref {seg["type"]}', got == want, f'{got!r} != {want!r}')


def test_pick_and_first():
    segs = S.normalize_message([
        {'type': 'text', 'data': {'text': 'hi'}},
        {'type': 'record', 'data': {'file': 'a.silk', 'duration': 3}},
    ])
    check('pick voice', len(S.pick(segs, 'voice')) == 1)
    check('first voice', S.first(segs, 'voice') == {'file': 'a.silk', 'duration': 3},
          S.first(segs, 'voice'))
    check('data_list', S.data_list(segs, 'voice') == [{'file': 'a.silk', 'duration': 3}])


def test_media_ref_accepts_data_or_segment():
    """media_ref 同时接受整个消息段或裸 data 字典（ev.first_voice 返回的就是 data）"""
    check('裸 data 字典', S.media_ref({'file': 'http://x/a.png'}) == 'http://x/a.png')
    check('裸 data file_id', S.media_ref({'file_id': 'TG123'}) == 'TG123')
    check('整段', S.media_ref({'type': 'image', 'data': {'file': 'a.png'}}) == 'a.png')
    check('data 非字典不炸', S.media_ref({'type': 'text', 'data': 'oops'}) == '')


# ─────────────────────────────────────────────────────
# 2. 接入端：Telegram
# ─────────────────────────────────────────────────────

def _load_adapter_module(plugin_dir, mod_name):
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'core_plugins', plugin_dir, 'main.py')
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_telegram_segments():
    tg = _load_adapter_module('telegram', '_tg_for_test')
    msg = {
        'message_id': 42,
        'text': '听听这个',
        'voice': {'file_id': 'V1', 'duration': 5, 'file_size': 900,
                  'mime_type': 'audio/ogg'},
        'reply_to_message': {'message_id': 41},
    }
    segs = tg.build_segments(msg)
    types = [s['type'] for s in segs]
    check('tg voice 成段', 'voice' in types, types)
    check('tg reply 成段', 'reply' in types, types)
    check('tg 语音可读取',
          S.first(segs, 'voice').get('file_id') == 'V1', S.first(segs, 'voice'))
    check('tg 时长保留', S.first(segs, 'voice').get('duration') == 5)


def test_telegram_photo_and_document():
    tg = _load_adapter_module('telegram', '_tg_for_test')
    msg = {
        'caption': '图',
        'photo': [{'file_id': 'S', 'width': 90}, {'file_id': 'L', 'width': 800}],
        'document': {'file_id': 'D1', 'file_name': 'a.pdf',
                     'mime_type': 'application/pdf'},
    }
    segs = tg.build_segments(msg)
    check('tg photo 取最大', S.first(segs, 'image').get('file_id') == 'L',
          S.first(segs, 'image'))
    check('tg document→file', bool(S.pick(segs, 'file')))
    check('tg 文件名保留', S.first(segs, 'file').get('name') == 'a.pdf')


def test_telegram_event_carries_segments():
    tg = _load_adapter_module('telegram', '_tg_for_test')
    update = {'update_id': 1, 'message': {
        'message_id': 7, 'chat': {'id': -100, 'type': 'supergroup'},
        'from': {'id': 55, 'username': 'u'},
        'voice': {'file_id': 'V9', 'duration': 2},
    }}
    ev = tg.normalize_event(update, 'tgbot')
    check('tg 事件带 segments', isinstance(ev.get('segments'), list) and ev['segments'],
          ev.get('segments'))
    check('tg 语音不是文本残留', '[voice:' not in (ev.get('message') or ''),
          ev.get('message'))


def test_telegram_to_native():
    tg = _load_adapter_module('telegram', '_tg_for_test')
    adapter = tg.TelegramAdapter.__new__(tg.TelegramAdapter)
    ops = adapter.to_native(S.normalize_message([
        {'type': 'text', 'data': {'text': '看图'}},
        {'type': 'image', 'data': {'file': 'http://x/a.png'}},
    ]))
    check('tg 出站 sendPhoto', ops and ops[0]['method'] == 'sendPhoto', ops)
    check('tg 出站 caption', ops[0]['caption'] == '看图', ops)
    only_text = adapter.to_native(S.normalize_message('纯文本'))
    check('tg 出站纯文本', only_text[0]['method'] == 'sendMessage', only_text)


def test_telegram_capabilities():
    tg = _load_adapter_module('telegram', '_tg_for_test')
    adapter = tg.TelegramAdapter.__new__(tg.TelegramAdapter)
    caps = C.as_capabilities(adapter.capabilities())
    check('tg 能收语音', caps.supports(C.CAP_VOICE, C.IN), caps)
    check('tg 能发图片', caps.supports(C.CAP_IMAGE, C.OUT), caps)
    check('tg 声明了出站能力', bool(caps.outbound))


# ─────────────────────────────────────────────────────
# 3. 接入端：Discord
# ─────────────────────────────────────────────────────

def test_discord_attachment_segments():
    dc = _load_adapter_module('discord', '_dc_for_test')
    d = {
        'id': '99', 'content': '看这个',
        'attachments': [
            {'url': 'https://cdn/a.png', 'content_type': 'image/png',
             'filename': 'a.png', 'size': 12},
            {'url': 'https://cdn/b.mp4', 'content_type': 'video/mp4',
             'filename': 'b.mp4'},
            {'url': 'https://cdn/c.zip', 'filename': 'c.zip'},
        ],
        'message_reference': {'message_id': '98'},
    }
    segs = dc.build_segments(d)
    types = [s['type'] for s in segs]
    check('dc 图片成段', 'image' in types, types)
    check('dc 视频成段', 'video' in types, types)
    check('dc 无类型按扩展名', 'file' in types, types)
    check('dc 回复成段', 'reply' in types, types)


def test_discord_event_segments():
    dc = _load_adapter_module('discord', '_dc_for_test')
    d = {'id': '1', 'content': 'hi', 'channel_id': 'C', 'guild_id': 'G',
         'author': {'id': 'U', 'username': 'u'},
         'attachments': [{'url': 'https://cdn/a.png',
                          'content_type': 'image/png'}]}
    ev = dc.normalize_event(d, 'dcbot', '')
    check('dc 事件带 segments', bool(ev.get('segments')), ev.get('segments'))
    check('dc 文本不再塞URL', ev['message'] == 'hi', ev['message'])


def test_discord_to_native_remote_degrades():
    dc = _load_adapter_module('discord', '_dc_for_test')
    adapter = dc.DiscordAdapter.__new__(dc.DiscordAdapter)
    native = adapter.to_native(S.normalize_message([
        {'type': 'text', 'data': {'text': '图'}},
        {'type': 'image', 'data': {'url': 'https://cdn/a.png'}},
    ]))
    # Discord 不能拿远程 URL 当附件 → 退化进正文
    check('dc 远程进正文', 'https://cdn/a.png' in (native.get('content') or ''),
          native)
    check('dc 无待上传附件', native.get('files') == [], native)


# ─────────────────────────────────────────────────────
# 4. 接入端：QQ 官方
# ─────────────────────────────────────────────────────

def test_qq_official_attachments_to_segments():
    qq = _load_adapter_module('qq_official', '_qq_for_test')
    adapter = qq.QQOfficialAdapter.__new__(qq.QQOfficialAdapter)
    segs = adapter.normalize_incoming({
        'content': '听',
        'attachments': [{'content_type': 'audio/silk', 'url': 'https://q/a.silk',
                         'duration': 4}],
    })
    check('qq 语音成段', bool(S.pick(segs, 'voice')), segs)
    check('qq 时长保留', S.first(segs, 'voice').get('duration') == 4)


def test_qq_official_msg_type_selection():
    """QQ 官方 msg_type 互斥：有媒体→7，有按钮→2，否则 0"""
    qq = _load_adapter_module('qq_official', '_qq_for_test')
    adapter = qq.QQOfficialAdapter.__new__(qq.QQOfficialAdapter)
    plain = adapter.to_native(S.normalize_message('你好'))
    check('qq 纯文本 msg_type=0', plain['msg_type'] == 0, plain)
    kb = adapter.to_native([
        {'type': 'text', 'data': {'text': '选'}},
        {'type': 'keyboard', 'data': {'rows': [[{'text': 'A'}]]}},
    ])
    check('qq 有按钮 msg_type=2', kb['msg_type'] == 2, kb)
    media = adapter.to_native(S.normalize_message([
        {'type': 'image', 'data': {'file': 'http://x/a.png'}}]))
    check('qq 有图 msg_type=7', media['msg_type'] == 7, media)


# ─────────────────────────────────────────────────────
# 5. 事件补齐 + Event 属性
# ─────────────────────────────────────────────────────

def test_event_segments_canonical():
    ev = Event({'message': [{'type': 'record', 'data': {'file': 'a.silk'}}],
                'message_type': 'group', 'user_id': 1, 'group_id': 2},
               'bot')
    check('ev.has_voice', ev.has_voice)
    check('ev.voices', ev.voices == [{'file': 'a.silk'}], ev.voices)
    check('ev.first_voice', ev.first_voice == {'file': 'a.silk'})


def test_event_reply_id_string():
    """QQ 官方消息 ID 是字符串，不能因为 int() 失败就返回 None"""
    ev = Event({'message': [{'type': 'reply', 'data': {'id': 'abc-123'}}],
                'message_type': 'group', 'user_id': 1}, 'bot')
    check('字符串 reply_id', ev.reply_id == 'abc-123', ev.reply_id)
    ev2 = Event({'message': [{'type': 'reply', 'data': {'id': '77'}}],
                 'message_type': 'group', 'user_id': 1}, 'bot')
    check('数字 reply_id 仍为 int', ev2.reply_id == 77, ev2.reply_id)


def test_event_media_only():
    ev = Event({'message': [{'type': 'image', 'data': {'file': 'a.jpg'}}],
                'message_type': 'private', 'user_id': 1}, 'bot')
    check('ev.is_media_only', ev.is_media_only)
    check('ev.media', len(ev.media) == 1)
    check('ev.media_url', ev.media_url(ev.media[0]) == 'a.jpg')


def test_normalize_event_fills_gaps():
    """接入端只给最少字段，框架补齐成标准形状"""
    raw = {'message': 'hi', 'user_id': 9}
    out = C.normalize_event(raw, None, 'myadapter')
    check('补齐 type', out['type'] == 'message', out.get('type'))
    check('补齐 message_type', out['message_type'] == 'private', out)
    check('补齐 sender', isinstance(out.get('sender'), dict) and out['sender']['user_id'] == 9)
    check('补齐 segments', bool(out['segments']), out.get('segments'))
    check('补齐 adapter', out.get('adapter') == 'myadapter')
    check('不覆盖已有字段', out['user_id'] == 9)


def test_normalize_event_notice_alias():
    """
    规范名只能"附加"不能"改写"：
    既有插件订阅的是协议原名（notice.group_increase），改写会让它们集体失效。
    """
    out = C.normalize_event({'type': 'notice', 'notice_type': 'group_increase',
                             'user_id': 1, 'group_id': 2, 'bot_name': 'b'},
                            None, 'x')
    check('notice 原名保留', out['notice_type'] == 'group_increase', out['notice_type'])
    check('notice 规范名附加',
          out['notice_type_canonical'] == 'group_member_increase', out)
    out2 = C.normalize_event({'type': 'notice', 'notice_type': 'recall',
                              'user_id': 1, 'bot_name': 'b'}, None, 'x')
    check('recall 规范名附加',
          out2['notice_type_canonical'] == 'message_recall', out2)
    # 未知类型不丢：至少要有 canonical 与原名一致的兜底
    out3 = C.normalize_event({'type': 'notice', 'notice_type': 'zzz',
                              'bot_name': 'b'}, None, 'x')
    check('未知 notice 兜底', out3['notice_type_canonical'] == 'zzz', out3)


def test_normalize_event_idempotent():
    once = C.normalize_event({'type': 'message', 'message': 'hi',
                              'user_id': 1, 'bot_name': 'b'}, None, 'x')
    twice = C.normalize_event(dict(once), None, 'x')
    check('幂等', once == twice, (once, twice))


# ─────────────────────────────────────────────────────
# 6. 契约自检
# ─────────────────────────────────────────────────────

class _GoodAdapter(ProtocolAdapter):
    adapter_id = 'good'

    async def handle_event(self, raw_event, bot_name):
        return None

    async def call_api(self, action, bot=None, **params):
        return {'status': 'ok'}

    def get_connected_bots(self):
        return ['good']

    def start(self):
        pass

    async def stop(self):
        pass

    def capabilities(self):
        return C.Capabilities(inbound=[C.CAP_TEXT, C.CAP_VOICE],
                              outbound=[C.CAP_TEXT, C.CAP_IMAGE])


class _BadAdapter(ProtocolAdapter):
    """故意不合规：归一化返回 dict、能力空、连接描述缺 id"""

    adapter_id = 'bad'

    async def handle_event(self, raw_event, bot_name):
        return None

    async def call_api(self, action, bot=None, **params):
        return {'status': 'ok'}

    def get_connected_bots(self):
        return []

    def start(self):
        pass

    async def stop(self):
        pass

    def normalize_incoming(self, raw_message):
        return {'type': 'text'}          # 应为 list

    def capabilities(self):
        return C.Capabilities()          # 没声明任何出站能力

    def get_connection_info(self):
        return {'name': 'x'}             # 缺 id


def test_validate_good_adapter():
    issues = C.validate_adapter(_GoodAdapter())
    check('合规接入端无问题', issues == [], issues)


def test_validate_bad_adapter():
    issues = C.validate_adapter(_BadAdapter())
    text = ' | '.join(issues)
    check('点名归一化形状', any('normalize_incoming' in i for i in issues), text)
    check('点名能力缺失', any('capabilities' in i for i in issues), text)
    check('点名连接描述', any('get_connection_info' in i for i in issues), text)


def test_default_translators():
    """最小接入端：基类兜底也要能跑（入站归一、出站透传）"""
    ad = _GoodAdapter()
    check('默认入站归一', ad.normalize_incoming('hi') ==
          [{'type': 'text', 'data': {'text': 'hi'}}], ad.normalize_incoming('hi'))
    segs = [{'type': 'text', 'data': {'text': 'x'}}]
    check('默认出站透传', ad.to_native(segs) == segs)
    check('默认只有文本能力', ad.supports(C.CAP_TEXT) and not ad.supports(C.CAP_VOICE))


def test_contract_report():
    rep = C.contract_report(_GoodAdapter())
    check('报告含能力', rep['capabilities']['outbound'] == ['text', 'image'], rep)
    check('报告含 id', rep['id'] == 'good', rep)
    check('报告无问题', rep['issues'] == [], rep)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in tests:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            _FAIL.append(f"{fn.__name__} 抛异常: {e}\n{traceback.format_exc()}")
    for name in _PASS:
        print(f"  PASS  {name}")
    for name in _FAIL:
        print(f"  FAIL  {name}")
    print(f"\n通过 {len(_PASS)} / {len(_PASS) + len(_FAIL)}")
    return 1 if _FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
