# -*- coding: utf-8 -*-
"""
llm_core 载荷测试（全离线，不联网、不需要真实模型）

按概念的先后顺序测：

    函数 schema 推导 → 参数校验 → 执行语义 → 提供商总线 → 会话窗口
    → 工具调用闭环 → 插件注册 → 多模态内容构造

跑的是 zip 里释放出来的那份代码，等价于线上实际运行的内容。

运行：python tests/test_llm_core.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import types
import zipfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_ZIP = os.path.join(_REPO_ROOT, 'core_plugins', 'llm_load', 'llm_core.zip')
_PKG = 'plugin_llm_core'


# ── 载荷装载（模拟框架给用户插件建的合成包） ──────────────────

def _load_payload():
    """把 zip 释放到临时目录，按 plugin_llm_core 合成包方式导入"""
    tmp = tempfile.mkdtemp()
    with zipfile.ZipFile(_ZIP, 'r') as zf:
        zf.extractall(tmp)
    root = os.path.join(tmp, 'llm_core')
    pkg = types.ModuleType(_PKG)
    pkg.__package__ = _PKG
    pkg.__path__ = [root]
    sys.modules[_PKG] = pkg
    mods = {}
    for name in ('tools', 'providers', 'history', 'agent', 'media', 'mcp'):
        mods[name] = __import__(f'{_PKG}.{name}', fromlist=['*'])
    mods['main'] = __import__(f'{_PKG}.main', fromlist=['*'])
    return mods, tmp


_MODS = None
_TMP = None


def mods():
    global _MODS, _TMP
    if _MODS is None:
        _MODS, _TMP = _load_payload()
    return _MODS


# ── 1. 函数契约 ───────────────────────────────────────────────

def test_schema_derivation():
    """类型注解 → JSON Schema，带默认值的参数不算必填，event 被跳过"""
    m = mods()
    schema_from_callable = m['tools'].schema_from_callable

    def add_score(uid: int, score: int = 1, memo: str = ''):
        """给用户加积分。"""

    schema = schema_from_callable(add_score)
    props = schema['properties']
    assert props['uid']['type'] == 'integer', f"uid 类型推导错误: {props['uid']}"
    assert props['score']['type'] == 'integer', f"score 类型推导错误: {props['score']}"
    assert props['memo']['type'] == 'string', f"memo 类型推导错误: {props['memo']}"
    assert schema['required'] == ['uid'], f"必填项应为 ['uid']: {schema['required']}"

    # event 是框架注入的，不该出现在给模型看的 schema 里
    def kick(user_id: int, event=None):
        """踢人。"""

    assert set(schema_from_callable(kick)['properties']) == {'user_id'}, \
        "event 不该进入参数表"
    return "注解推导类型/必填正确，event 被排除"


def test_validate_errors():
    """缺参、未知参数、类型不符都要被拦下来并说清楚"""
    m = mods()
    tools_mod = m['tools']
    FunctionTool, ToolRegistry = tools_mod.FunctionTool, tools_mod.ToolRegistry

    tool = FunctionTool(
        name='set_price', description='设置价格',
        parameters={'type': 'object',
                    'properties': {'sku': {'type': 'string'},
                                   'price': {'type': 'number'}},
                    'required': ['sku', 'price']},
        handler=lambda sku, price: f"{sku}={price}")

    for bad, why in (
        ({}, 'missing'),
        ({'sku': 'a'}, 'missing2'),
        ({'sku': 'a', 'price': 1, 'zzz': 3}, 'unknown'),
        ({'sku': 'a', 'price': '免费'}, 'type'),
    ):
        try:
            ToolRegistry.validate(tool, bad)
        except tools_mod.ToolCallError as e:
            assert str(e), f"{why} 的错误信息是空的"
        else:
            raise AssertionError(f"{why} 这组参数没有被拦住: {bad}")

    ToolRegistry.validate(tool, {'sku': 'a', 'price': 9.9})  # 合法的不该抛
    return "缺参/未知参数/类型错误均被拦截"


def test_execute_semantics():
    """返回值语义：字符串回灌、None 不回灌、异常变成可回灌的错误文本"""
    m = mods()
    tools_mod = m['tools']
    registry = tools_mod.ToolRegistry('test')

    seen = {}

    @tools_mod.llm_function(description="记账")
    def note(amount: int):
        """记一笔账。"""
        seen['amount'] = amount
        return f"记好了 {amount} 元"

    @tools_mod.llm_function(description="发图片", enabled=True)
    def send_pic(url: str):
        """发一张图片。"""
        seen['url'] = url
        return None          # 约定：返回 None = 不进上下文

    def boom(text: str):
        """会炸的工具。"""
        raise RuntimeError("磁盘满了")

    def boom(text: str):
        """会炸的工具。"""
        raise RuntimeError("磁盘满了")

    boom_schema = {'type': 'object',
                   'properties': {'text': {'type': 'string'}},
                   'required': ['text']}
    for fn in (note, send_pic):
        registry.register(getattr(fn, '__llm_tool__'))

    ok = asyncio.run(registry.aexecute(registry.get('note'), {'amount': 5}))
    assert ok == "记好了 5 元", f"返回值不对: {ok}"
    assert seen['amount'] == 5

    empty = asyncio.run(registry.aexecute(registry.get('send_pic'), {'url': 'x'}))
    assert empty is None, f"None 应当原样返回: {empty!r}"

    # 工具炸了不能连带炸会话，要变成一句给模型看的话
    error_tool = tools_mod.FunctionTool(
        name='boom', description='会炸', parameters=boom_schema, handler=boom)
    registry.register(error_tool)
    err = asyncio.run(registry.aexecute(registry.get('boom'), {'text': 'x'}))
    assert err and 'boom' in err and '磁盘满了' in err, f"异常未转成可回灌文本: {err}"

    # 停用的工具不出现在给模型的声明里
    registry.set_enabled('note', False)
    assert 'note' not in [t['function']['name']
                          for t in registry.to_openai_tools()], "停用工具仍被导出"
    return "字符串回灌 / None 不回灌 / 异常降级为文本"


# ── 2. 提供商总线 ─────────────────────────────────────────────

def test_provider_registry():
    """从配置批量构造多个提供商，默认项与按模型名解析都要对"""
    m = mods()
    providers_mod = m['providers']
    reg = providers_mod.ProviderRegistry.build([
        {'id': 'deepseek', 'type': 'openai', 'base_url': 'https://api.deepseek.com/v1',
         'api_key': 'sk-x', 'model': 'deepseek-chat', 'default': True},
        {'id': 'local', 'type': 'openai', 'base_url': 'http://127.0.0.1:11434/v1',
         'api_key': 'x', 'model': 'qwen2.5', 'supports_tools': False},
    ])
    assert len(reg.list_providers()) == 2, "应构造出两个提供商"
    assert reg.default_id == 'deepseek', f"默认提供商不对: {reg.default_id}"
    assert reg.get().model == 'deepseek-chat'
    assert reg.resolve('qwen2.5') is not None, "按模型名解析失败"
    assert reg.get('local').supports_tools is False, "supports_tools 覆盖未生效"
    assert reg.unregister('local') and len(reg.list_providers()) == 1
    return "多提供商构造 / 默认 / 按模型解析正常"


def test_openai_provider_tool_parsing():
    """伪造一次 HTTP 响应，验证 tool_calls 能被正确拆出来"""
    m = mods()
    providers_mod = m['providers']

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            call = {'id': 'call_1', 'type': 'function',
                    'function': {'name': 'note', 'arguments': '{"amount": 3}'}}
            return {
                'model': 'fake-1',
                'choices': [{'message': {'content': None, 'tool_calls': [call]}}],
            }

    class _FakeRequests:
        last = None

        @staticmethod
        def post(url, headers=None, json=None, timeout=None, proxies=None):
            _FakeRequests.last = {'url': url, 'json': json, 'headers': headers}
            return _Resp()

    real = sys.modules.get('requests')
    sys.modules['requests'] = _FakeRequests
    try:
        p = providers_mod.OpenAICompatProvider(
            id='fake', base_url='https://example.com/v1', api_key='k',
            model='fake-1')
        # 不支持 tools 的提供商不该带上 tools 字段（老模型会直接报错）
        reply = p.chat([{'role': 'user', 'content': 'hi'}], tools=[])
        sent = _FakeRequests.last['json']
        assert 'tools' not in sent, "没工具时不该下发 tools 字段"
        assert reply.has_tool_calls and reply.tool_calls[0].name == 'note'
        assert reply.tool_calls[0].arguments == {'amount': 3}
        assert sent['messages'][0]['content'] == 'hi'
    finally:
        if real is not None:
            sys.modules['requests'] = real
        else:
            sys.modules.pop('requests', None)
    return "OpenAI 兼容层正确解析 tool_calls"


# ── 3. 会话窗口 ───────────────────────────────────────────────

def test_conversation_trim():
    """超出轮数上限时按轮砍掉最老的对话，且不留下孤儿工具消息"""
    m = mods()
    history_mod = m['history']
    conv = history_mod.Conversation('k', system_prompt='sys', max_turns=3)
    for i in range(6):
        conv.append({'role': 'user', 'content': f'q{i}'})
        conv.append({'role': 'assistant', 'content': f'a{i}'})
    assert conv.turns <= 3, f"轮数没有被限制住: {conv.turns}"

    # 孤儿 tool 消息要在导出时被清掉
    conv2 = history_mod.Conversation('k2', system_prompt='sys')
    conv2.append({'role': 'assistant', 'content': None,
                  'tool_calls': [{'id': 'c1', 'type': 'function',
                                  'function': {'name': 'note', 'arguments': '{}'}}]})
    conv2.append({'role': 'user', 'content': 'hi'})
    exported = conv2.as_request()
    assert not any(x.get('role') == 'assistant' and x.get('tool_calls')
                   for x in exported), "孤儿 tool_calls 没有被清理"
    assert exported[0]['role'] == 'system', "system 必须在最前"
    return "轮数裁剪与孤儿工具消息清理正常"


# ── 4. 工具调用闭环 ───────────────────────────────────────────

def _fake_provider_script(m, script, supports_tools=True):
    """造一个按脚本吐回复的假提供商"""

    class Fake(m['providers'].Provider):
        id = 'fake'
        model = 'fake-1'
        supports_tools = True

        def __init__(self):
            super().__init__(model='fake-1')
            self.script = list(script)
            self.calls = []

        def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024):
            self.calls.append({'messages': list(messages), 'tools': tools})
            if not self.script:
                return m['providers'].ProviderReply(content='收口回答')
            return self.script.pop(0)

    return Fake()


def test_tool_loop():
    """模型调工具 → 我们执行 → 结果回灌 → 模型给最终答案"""
    m = mods()
    tools_mod, providers_mod, agent_mod, history_mod = (
        m['tools'], m['providers'], m['agent'], m['history'])

    registry = tools_mod.ToolRegistry('test')

    @tools_mod.llm_function(description="把两段文本拼起来")
    def concat(a: str, b: str):
        """拼接两段文本。"""
        return a + b

    registry.register(getattr(concat, '__llm_tool__'))

    provider = _fake_provider_script(m, [
        providers_mod.ProviderReply(tool_calls=[
            providers_mod.ToolCall(id='c1', name='concat',
                                   arguments={'a': '你好', 'b': '世界'})]),
        providers_mod.ProviderReply(content='结果是你你好世界'),
    ])

    agent = agent_mod.ToolLoopAgent(registry, provider=provider, max_turns=4)
    conv = history_mod.Conversation('k', system_prompt='sys')
    conv.append({'role': 'user', 'content': '把你好和世界拼起来'})

    result = asyncio.run(agent.arun(conv))
    assert result.ok, f"流程报错: {result.error}"
    assert result.text == '结果是你你好世界', f"最终回答不对: {result.text}"
    assert len(result.tool_calls) == 1 and result.tool_calls[0]['name'] == 'concat'

    history = conv.messages
    assert any(msg.get('role') == 'tool' and msg.get('name') == 'concat'
               for msg in history), "工具结果没有进历史"
    assert any(msg.get('role') == 'assistant' and msg.get('tool_calls')
               for msg in history), "assistant 的工具调用没进历史"
    # 第二轮请求里必须带上工具结果，否则模型不知道发生了什么
    assert provider.calls[1]['messages'][-1]['role'] == 'tool', \
        "第二轮请求没有携带工具结果"
    return "工具调用闭环走通，中间状态正确入历史"


def test_loop_forced_convergence():
    """模型死循环调工具时，必须在到达轮数上限后收口给结论"""
    m = mods()
    tools_mod, providers_mod, agent_mod, history_mod = (
        m['tools'], m['providers'], m['agent'], m['history'])

    registry = tools_mod.ToolRegistry('test')

    @tools_mod.llm_function(description="反复调也没用的工具")
    def ping(text: str):
        """反复调用的工具。"""
        return 'pong'

    registry.register(getattr(ping, '__llm_tool__'))

    always_call = providers_mod.ProviderReply(tool_calls=[
        providers_mod.ToolCall(id='c', name='ping', arguments={'text': 'x'})])
    provider = _fake_provider_script(m, [always_call] * 8)

    agent = agent_mod.ToolLoopAgent(registry, provider=provider, max_turns=3)
    conv = history_mod.Conversation('k')
    conv.append({'role': 'user', 'content': '多说几句'})

    result = asyncio.run(agent.arun(conv))
    assert result.ok, f"收口过程报错: {result.error}"
    assert result.text, "超出轮数后没有给出任何结论"
    assert len(result.tool_calls) <= 4, f"调了太多轮: {len(result.tool_calls)}"
    if provider.calls:
        assert provider.calls[-1]['tools'] is None, "收口请求应当不带工具"
    return f"超轮数自动收口（执行 {len(result.tool_calls)} 次工具）"


# ── 5. 插件注册与端到端调用 ───────────────────────────────────

class _StubServices:
    def __init__(self):
        self._d = {}

    def register(self, name, svc):
        self._d[name] = svc

    def remove(self, name):
        self._d.pop(name, None)

    def get(self, name, default=None):
        return self._d.get(name, default)


class _StubFramework:
    def __init__(self):
        self.services = _StubServices()
        self.config = {}


class _StubCtx:
    """够用的 ctx 替身：把 register 里用到的调用全记录下来"""

    def __init__(self, config=None):
        self._framework = _StubFramework()
        self._current_bot = 'stub'
        self.commands = []
        self.raw_handlers = []
        self.event_handlers = []
        self.web_pages = []
        self.apis = []
        self.config = config or {}
        self.msgs = []

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def command(self, pattern, handler, **kw):
        self.commands.append((pattern, handler, kw))

    def on_raw_message(self, handler):
        self.raw_handlers.append(handler)

    def on(self, name, handler):
        self.event_handlers.append((name, handler))

    def webui(self, title, entry='index.html', icon=None, order=50, sidebar=False):
        self.web_pages.append({'title': title, 'sidebar': sidebar})

    def register_api(self, path, handler, methods=None, auth=True, description=None):
        self.apis.append(path)

    def has_perm(self, user_id, node, context=None, role=None):
        return role in ('super', 'admin', 'owner')

    def log(self, msg, level='info'):
        pass

    async def asend_msg(self, **kw):
        self.msgs.append(kw)

    def send_msg(self, **kw):
        self.msgs.append(kw)


def test_register_and_service():
    """register 能跑通：服务注册、命令登记、内置工具入库"""
    m = mods()
    main_mod = m['main']
    ctx = _StubCtx()
    main_mod.register(ctx)

    svc = ctx._framework.services.get('llm_core')
    assert svc is not None, "服务没有注册上"
    names = [t.name for t in svc.list_tools()]
    assert 'get_current_time' in names and 'calculator' in names, \
        f"内置工具缺失: {names}"
    patterns = [c[0] for c in ctx.commands]
    assert '/llm' in patterns, f"对话命令没注册: {patterns}"
    assert '/llmreset' in patterns and '/llmtools' in patterns, f"命令不全: {patterns}"
    assert ctx.raw_handlers, "没有注册原始消息处理器（图片就抓不到了）"
    assert ctx.web_pages and ctx.web_pages[0]['sidebar'] is True, \
        "WebUI 面板页未注册"

    # 别的插件用法：装饰器注册 + 立刻可用
    @svc.tool(description="把文本变大写")
    def upper(text: str):
        """把文本变大写。"""
        return (text or '').upper()

    assert 'upper' in [t.name for t in svc.list_tools()], "装饰器注册失败"
    assert svc.unregister_tool('upper'), "注销失败"
    assert 'upper' not in [t.name for t in svc.list_tools()]
    return f"register 正常：{len(patterns)} 条命令 / {len(names)} 个内置工具"


def test_end_to_end_chat():
    """端到端：服务面发起一次对话，含工具调用，验证结构化配置解析"""
    m = mods()
    main_mod = m['main']
    ctx = _StubCtx({
        'providers': [{'id': 'fake', 'type': 'openai',
                       'base_url': 'https://example.com/v1',
                       'api_key': 'k', 'model': 'fake-1', 'default': True}],
    })
    main_mod.register(ctx)
    svc = ctx._framework.services.get('llm_core')
    # 配的是假地址，换成脚本驱动的假 provider
    provider = _fake_provider_script(m, [
        m['providers'].ProviderReply(tool_calls=[
            m['providers'].ToolCall(id='c1', name='calculator',
                                    arguments={'expression': '(12+8)*3'})]),
        m['providers'].ProviderReply(content='(12+8)*3 = 60'),
    ])
    svc.unregister_provider('fake')
    svc.register_provider(provider, default=True)

    class _Event:
        user_id = 10001
        group_id = 20002
        is_group = True
        role = 'member'

    result = asyncio.run(svc.achat('算一下 (12+8)*3', event=_Event()))
    assert result.ok, f"端到端对话报错: {result.error}"
    assert result.text == '(12+8)*3 = 60', f"最终回答不对: {result.text}"
    assert result.tool_calls and result.tool_calls[0]['name'] == 'calculator'
    assert '60' in result.tool_calls[0]['result'], \
        f"工具结果不对: {result.tool_calls[0]['result']}"
    conv = svc.conversation(group_id=20002, user_id=10001)
    assert conv is not None and conv.turns >= 1, "会话历史没有留下"
    return "端到端对话 + 工具调用成功"


def test_handle_chat_no_provider():
    """没配任何提供商时 /llm 回一句可读提示，而不是抛异常静默"""
    import re
    m = mods()
    main_mod = m['main']
    ctx = _StubCtx()          # 空配置 → providers 为空
    main_mod.register(ctx)

    class _Event:
        user_id = 10001
        group_id = 20002
        is_group = True
        role = 'member'

    match = re.match(r'/llm\s+(.*)', '/llm 你好', re.S)
    asyncio.run(main_mod.handle_chat(_Event(), match))

    assert ctx.msgs, "没有可用提供商时 handle_chat 没有回复任何消息（静默失败）"
    sent = ctx.msgs[-1].get('message', '')
    assert 'LLM 未就绪' in sent, f"提示不对: {sent}"
    assert '没有可用的模型提供商' in sent, f"应引导去配置提供商: {sent}"
    return "无可用提供商回可读提示，不静默"


# ── 6. 多模态内容构造 ─────────────────────────────────────────

def test_panel_api_payload():
    """面板接口能序列化出结构正确的 JSON（挂在裸 Flask app 上测，绕开框架鉴权）"""
    try:
        from flask import Flask
    except ImportError:
        return "未安装 Flask，跳过"
    m = mods()
    main_mod = m['main']
    ctx = _StubCtx()
    main_mod.register(ctx)

    app = Flask(__name__)
    app.add_url_rule('/api/llm_core/overview', 'overview',
                     main_mod._view_overview, methods=['GET'])
    app.add_url_rule('/api/llm_core/sessions/clear', 'clear',
                     main_mod._view_clear_sessions, methods=['POST'])
    client = app.test_client()

    r = client.get('/api/llm_core/overview')
    assert r.status_code == 200, f"接口返回 {r.status_code}"
    body = r.get_json()
    assert body['code'] == 0, f"业务码不对: {body}"
    data = body['data']
    assert data['version'], "没有版本号"
    assert isinstance(data['providers'], list), "providers 应是数组"
    assert isinstance(data['tools'], list) and data['tools'], "tools 应有内容"
    first = data['tools'][0]
    assert {'name', 'source', 'enabled'} <= set(first), f"工具字段不全: {first}"
    assert 'sessions' in data and 'estimated_tokens' in data['sessions']

    r2 = client.post('/api/llm_core/sessions/clear')
    assert r2.get_json()['code'] == 0, "清空会话接口失败"
    return "面板 overview / clear 接口返回结构正确"


def test_media_content():
    """图片只在 provider 声明支持视觉时才拼进内容块"""
    m = mods()
    media_mod = m['media']

    raw = {'message': [{'type': 'text', 'data': {'text': '看看这个'}},
                       {'type': 'image', 'data': {'url': 'https://x/y.png'}}],
           'user_id': 1, 'group_id': None}
    images = media_mod.extract_images(raw)
    assert len(images) == 1 and images[0]['kind'] == 'url', f"图片提取失败: {images}"

    text_only = media_mod.build_user_content('看看', images, vision_supported=False)
    assert isinstance(text_only, str), "不支持视觉时应回落到纯文本"

    multi = media_mod.build_user_content('看看', images, vision_supported=True)
    assert isinstance(multi, list), "支持视觉时应产出内容块数组"
    assert any(p.get('type') == 'image_url' for p in multi), "内容块里没有图片"
    assert multi[0]['type'] == 'text', "文本块应在最前"
    return "图片提取与内容块构造正确"


def test_mcp_failure_isolated():
    """MCP server 拉不起来时不能影响整体（只在配置为坏的时候验证降级）"""
    m = mods()
    mcp_mod, tools_mod = m['mcp'], m['tools']
    registry = tools_mod.ToolRegistry('test')
    summary = mcp_mod.bind_mcp_tools(registry, [
        {'name': 'broken', 'transport': 'stdio', 'command': '__not_exist_cmd__'},
        {'name': 'disabled', 'enabled': False, 'transport': 'stdio',
         'command': '__not_exist_cmd__'},
    ])
    assert summary.get('broken') == 0, f"坏 server 应当返回 0: {summary}"
    assert 'disabled' not in summary, "禁用的 server 不该被拉起"
    assert registry.list_tools() == [], "失败时不该留下工具"
    return "MCP 接入失败被隔离，不影响其它部分"


def test_session_persistence():
    """会话落盘：重启（重建 Store）后历史可恢复；clear_all 连文件一起清"""
    m = mods()
    hist = m['history']
    d = os.path.join(_TMP, 'sessions_test')
    key = hist.session_key('bot1', 123, 456)

    store = hist.ConversationStore(persist=hist.SessionPersist(d))
    conv = store.get(key)
    conv.append({'role': 'user', 'content': '记住这句话'})
    conv.append({'role': 'assistant', 'content': '好的'})
    assert len(conv.messages) == 2

    # 模拟重启：全新 Store + 同一目录
    store2 = hist.ConversationStore(persist=hist.SessionPersist(d))
    conv2 = store2.get(key)
    assert [x['content'] for x in conv2.messages] == ['记住这句话', '好的']
    assert conv2.turns == 1

    # clear_all 要把磁盘文件也清掉
    assert store2.clear_all() >= 1
    fresh = hist.ConversationStore(persist=hist.SessionPersist(d))
    assert fresh.get(key).messages == []
    return "会话重启后恢复，clear_all 连文件清理"


def test_trigger_policy():
    """自由对话触发：群白名单硬门槛 → 唤醒词必回且剥前缀 → 概率兜底"""
    m = mods()
    dec = m['main']._should_free_reply

    # 群白名单：不在名单的群直接拒；私聊不受限
    assert dec('你好', 111, '', [222], 100) == (False, '你好')
    assert dec('你好', 222, '', [222], 100)[0] is True
    assert dec('你好', None, '', [222], 100)[0] is True

    # 唤醒词：前缀命中剥词；中间命中不去词；必回（概率 0 也回）
    assert dec('小雪 今天天气', 111, '小雪', [], 0) == (True, '今天天气')
    hit, txt = dec('请问小雪在吗', 111, '小雪', [], 0)
    assert hit and txt == '请问小雪在吗'
    # 群白名单仍在唤醒词之前：白名单外喊唤醒词也不理
    assert dec('小雪 你好', 333, '小雪', [222], 100)[0] is False

    # 概率：0% 必不回，100% 全接
    assert dec('普通消息', 111, '', [], 0)[0] is False
    assert dec('普通消息', 111, '', [], 100)[0] is True
    # 空消息
    assert dec('  ', 111, '', [], 100) == (False, '')
    return "触发策略三层判定符合预期"


def test_split_text_sentences():
    """分段：优先句子边界，永不超限，超长单句硬切，内容不丢"""
    m = mods()
    split = m['main']._split_text

    # 短文本不分段
    assert split('你好', 100) == ['你好']

    # 句边界优先：两句拼一起没超限就不拆
    assert split('今天天气不错。明天也行。', 100) == ['今天天气不错。明天也行。']

    # 超限在句边界拆，各段不超限
    parts = split('第一句话比较长一点。第二句话也很长。', 12)
    assert len(parts) >= 2 and all(len(p) <= 12 for p in parts)
    assert ''.join(parts) == '第一句话比较长一点。第二句话也很长。'

    # 无标点长文硬切，不丢字符
    runon = '啊' * 30
    parts = split(runon, 10)
    assert parts == [runon[i:i+10] for i in range(0, 30, 10)]

    # 默认 size 走配置路径（ctx=None 时取默认 1200）
    assert split('x' * 1300) == ['x' * 1200, 'x' * 100]
    return "分段按句边界且内容无损"


def test_chat_gates():
    """对话闸门：任一拒绝即拒绝，闸门异常不拦截，unregister 清空"""
    m = mods()
    main_mod = m['main']
    gates = main_mod._CHAT_GATES
    gates.clear()
    try:
        gates.append(lambda uid: '你被拉黑了')
        assert main_mod._check_chat_gates(123) == '你被拉黑了'
        gates.clear()
        gates.append(lambda uid: None)   # 放行
        assert main_mod._check_chat_gates(123) is None
        gates.append(lambda uid: 1 / 0)  # 故障闸门不拦截
        assert main_mod._check_chat_gates(123) is None
        svc_gate = main_mod.LLMCoreService.register_chat_gate
        assert callable(svc_gate)
    finally:
        gates.clear()
    assert main_mod._check_chat_gates(123) is None
    return "对话闸门拦截/放行/容错符合预期"


def test_persona_override():
    """会话级人格：persona 优先于全局 system_prompt，落盘可恢复"""
    m = mods()
    hist = m['history']
    d = os.path.join(_TMP, 'persona_test')
    key = hist.session_key('bot1', 111, 222)

    store = hist.ConversationStore(system_prompt='默认人格', persist=hist.SessionPersist(d))
    conv = store.get(key)
    assert conv.effective_prompt == '默认人格'
    conv.persona = '猫娘模式'
    conv.append({'role': 'user', 'content': 'hi'})
    assert conv.effective_prompt == '猫娘模式'
    req = conv.as_request()
    assert req[0] == {'role': 'system', 'content': '猫娘模式'}

    # 模拟重启：persona 从磁盘恢复
    store2 = hist.ConversationStore(system_prompt='默认人格', persist=hist.SessionPersist(d))
    conv2 = store2.get(key)
    assert conv2.persona == '猫娘模式'
    conv2.persona = ''
    assert conv2.effective_prompt == '默认人格'
    return "人格覆盖/恢复默认/落盘恢复符合预期"


def cleanup():
    if _TMP and os.path.isdir(_TMP):
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == '__main__':
    cases = [
        test_schema_derivation,
        test_validate_errors,
        test_execute_semantics,
        test_provider_registry,
        test_openai_provider_tool_parsing,
        test_conversation_trim,
        test_tool_loop,
        test_loop_forced_convergence,
        test_register_and_service,
        test_end_to_end_chat,
        test_handle_chat_no_provider,
        test_panel_api_payload,
        test_media_content,
        test_mcp_failure_isolated,
        test_session_persistence,
        test_trigger_policy,
        test_split_text_sentences,
        test_chat_gates,
        test_persona_override,
    ]
    failed = 0
    try:
        for fn in cases:
            try:
                note = fn()
                print(f"PASS  {fn.__name__}: {note or ''}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL  {fn.__name__}: {e}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    finally:
        cleanup()
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    sys.exit(1 if failed else 0)
