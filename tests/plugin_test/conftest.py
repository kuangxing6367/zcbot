"""plugin_test · 插件测试公共夹具。

给开发者 / AI 写插件测试用的底座：只依赖 ``framework.plugin`` 的装饰器登记表和
一个<b>录制型 ctx</b>，**不启动 Framework、不连数据库、不占端口、不发网络请求**，
所以一条用例通常毫秒级跑完，随手跑几百条也不心疼。

为什么不直接起 Framework：插件测试绝大多数只想验两件事——「插件声明了什么」和
「handler 拿到事件后返回什么」。起真框架会连带 SQLite 迁移、适配器注册、端口绑定，
慢且脆，一个无关的迁移失败就把插件测试拖红。

三个 fixture：

    rec_ctx       录制型 ctx。插件调 ctx.command(...) 之类，它只记账不执行。
    make_plugin   把一段插件源码写成临时模块并导入，测完清掉 sys.modules 与待登记项。
    msg_event     构造一条最小消息事件，作为 handler 的 event 参数。

跑法::

    py -3 -m pytest tests/plugin_test -q

注意：``tests/plugin_test`` 会被 ``pytest tests`` 一并收集，所以在 ``plugin_test``
下写的用例必须是**稳定通过**的；不确定的验尸性代码放 ``tests/backups`` 一类的
非收集目录。
"""
import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework import plugin as plugin_api


class RecordingCtx:
    """只记录、不执行任何注册动作的 ctx。

    每个登记方法在真实 ctx 上都会落库 / 建表 / 挂路由，测试里既不需要也不该发生。
    这里用 ``__getattr__`` 兜底而不是把 ``command / on / hook / task / api ...``
    逐个写死：ctx 以后新增登记方法（比如又加一种 extension），夹具不用跟着改，
    否则插件测试会因为夹具过时集体假红，而那种红最难判断。
    """

    def __init__(self, name='plugin_test'):
        self.name = name
        self.calls = []          # [(method, args, kwargs), ...] 按调用顺序

    def __getattr__(self, item):
        # 下划线开头一律当作真属性缺失：避免 pytest / copy / pickle 探测时被吞掉
        if item.startswith('_'):
            raise AttributeError(item)

        def record(*args, **kwargs):
            self.calls.append((item, args, kwargs))

        return record

    # ── 断言辅助 ────────────────────────────────────────────
    def of(self, method):
        """取某个登记方法的全部调用：``[(args, kwargs), ...]``。"""
        return [(args, kwargs) for name, args, kwargs in self.calls if name == method]

    def handlers(self, method):
        """取某个登记方法登记的处理函数，保持登记顺序。"""
        out = []
        for name, args, kwargs in self.calls:
            if name != method:
                continue
            handler = kwargs.get('handler')
            if handler is None and args:
                handler = args[0]
            if handler is not None:
                out.append(handler)
        return out

    def methods(self):
        """调用过的登记方法名（去重前，保序）。"""
        return [name for name, _, _ in self.calls]


@pytest.fixture
def rec_ctx():
    """一条用例一个干净 ctx，不跨用例串账。"""
    return RecordingCtx()


@pytest.fixture
def make_plugin(tmp_path):
    """把插件源码写成临时模块并导入。

    用法::

        mod = make_plugin("from framework.plugin import command\\n...")
        mod.register(ctx)

    模块名默认 ``plugin_test_demo_plugin``，多次调用时靠 ``name`` 区分——
    ``flush()`` 是按模块名匹配登记项的，重名会让两个插件的登记混到一起。
    """
    created = []

    def _make(source, name='demo_plugin'):
        module_name = 'plugin_test_' + name
        path = tmp_path / (module_name + '.py')
        path.write_text(source, encoding='utf-8')
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module     # 先注册再执行，装饰器才拿得到 __module__
        created.append(module_name)
        spec.loader.exec_module(module)
        return module

    yield _make

    # 测完必须清两处：sys.modules 留着会让下一个用例 import 到旧模块；
    # 待登记表留着会在下一次 flush 时把旧登记重放一遍（同一命令注册两次）。
    for module_name in created:
        sys.modules.pop(module_name, None)
        plugin_api.clear_module(module_name)


@pytest.fixture
def msg_event():
    """构造一条最小消息事件。

    字段取真实事件里插件最常碰的那些；``reply()`` 只做记录，方便断言「插件回了什么」。
    """

    def _make(text='', *, group_id=None, user_id=10001, message_id=1, segments=None):
        replies = []
        event = types.SimpleNamespace(
            text=text,
            message=text,
            raw_message=text,
            segments=list(segments or []),
            group_id=group_id,
            user_id=user_id,
            sender_id=user_id,
            message_id=message_id,
            is_group=group_id is not None,
            replies=replies,
            reply=lambda *a, **k: replies.append((a, k)),
        )
        return event

    return _make
