# -*- coding: utf-8 -*-
"""终端 AI 智能体界面（Textual 实现）。

界面用**成熟的 TUI 框架**（Textual，MIT）承载——对话流、输入框、
弹窗、键位、resize、鼠标全部交给框架，agent 在工作线程里跑、事件回灌到界面。

不再手搓 ANSI 重绘与按键解析：那套在 Windows 控制台（cmd.exe / conhost）上
极易卡死——整屏重绘、鼠标上报刷屏、转义序列解析、行缓冲/回显模式……这些都是
TUI 框架已经解决好的问题。

依赖：textual（见同目录 requirements.txt）。
"""
import os
import sys

try:  # 包导入 / 框架 loader 顶层模块导入双兼容
    from . import prompt as prompt_mod
    from .agent import Agent
    from .llm import LLMError
except ImportError:
    import importlib
    import importlib.util
    _PKG = '_zcbot_aiwriter'
    if _PKG not in sys.modules:
        _d = os.path.dirname(os.path.abspath(__file__))
        _spec = importlib.util.spec_from_file_location(
            _PKG, os.path.join(_d, '__init__.py'), submodule_search_locations=[_d])
        _m = importlib.util.module_from_spec(_spec)
        sys.modules[_PKG] = _m
        _spec.loader.exec_module(_m)
    prompt_mod = importlib.import_module(_PKG + '.prompt')
    Agent = importlib.import_module(_PKG + '.agent').Agent
    LLMError = importlib.import_module(_PKG + '.llm').LLMError

from rich.text import Text  # textual 依赖自带 rich

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Footer, Input, Label, ListItem, ListView, RichLog, Static, TextArea)

_SETTINGS_FIELDS = [
    ('agent', '当前人格', 'str'),
    ('model', '模型', 'str'),
    ('temperature', '温度', 'float'),
    ('max_rounds', '最大轮数（工具调用）', 'int'),
    ('mode', '模式(readonly/workspace/full)', 'str'),
    ('allow_exec', '允许执行(true/false)', 'bool'),
    ('workspace', '工作区目录（空=框架根目录）', 'str'),
    ('keep_turns', '压缩保留轮数', 'int'),
    ('max_history_chars', '压缩阈值(字符)', 'int'),
    ('session_hard_cap', '历史硬上限(条)', 'int'),
    ('persist_session', '跨启动保留会话(true/false)', 'bool'),
    ('thinking', '请求深度思考(true/false)', 'bool'),
    ('show_thinking', '显示思考过程(true/false)', 'bool'),
    ('show_tools', '显示工具调用(true/false)', 'bool'),
    ('tool_detail', '展开工具细节/思考全文(true/false)', 'bool'),
    ('max_output', '工具输出截断(字符)', 'int'),
    ('shell_timeout', 'shell 超时(秒)', 'int'),
    ('python_timeout', 'python 超时(秒)', 'int'),
    ('net_timeout', '网络超时(秒)', 'int'),
    ('base_url', '接口地址', 'str'),
    ('api_key', 'API Key', 'str'),
]

_MODE_DESC = {
    'readonly': '只读 —— 读文件 / 搜索 / 抓网页 / 只读查库',
    'workspace': '可写 —— 上面全部 + 新建 / 修改文件',
    'full': '可执行 —— 上面全部 + shell / python / 写库 / 插件管理',
}

_HELP = (
    '我是 ZCBOT 的插件开发智能体：描述你要的插件/需求，我读代码、写插件、跑命令。\n'
    '模式（Tab 循环切换）：\n'
    '  readonly  只读 —— read/list/glob/grep/webfetch/websearch/db_query/plugin_list\n'
    '  workspace 可写 —— + write / edit / remember / plugin_new\n'
    '  full      可执行 —— + shell / python / db_execute / plugin_action\n'
    '斜杠命令：/agent 人格 · /sessions 会话管理（进入/新建/重命名/配置/删除）· /memory 长期记忆 · '
    '/history 历史 · /settings 配置 · /model <名> · /mode <模式> · /new 清空当前会话 · '
    '/plugins 列插件 · /plugin <名> 生成插件骨架 · /help · /exit\n'
    '显示开关（防刷屏）：/tools on|off 是否显示工具调用 · /detail on|off 是否展开工具参数与思考全文 · '
    '/think on|off 是否请求深度思考（模型需支持，如 deepseek-reasoner）\n'
    '右侧栏：人格列表（Ctrl+G 聚焦、↑↓ 选择、Enter 切换）+ 当前会话与运行状态。\n'
    '键位：Tab 切模式 · Ctrl+N 会话管理 · Ctrl+S 设置 · Ctrl+T 细节 · Ctrl+G 人格 · Ctrl+C 退出'
)


class SettingsScreen(ModalScreen):
    """设置弹窗：编辑 OpenAI 接口 / Key / 模型 / 模式 / 工作区，保存即写回配置。"""

    BINDINGS = [Binding('escape', 'cancel', '取消')]

    CSS = """
    SettingsScreen { align: center middle; }
    #dlg { width: 84; height: auto; max-height: 92%; background: $surface;
           border: thick $accent; padding: 1 2; }
    #fields { height: auto; max-height: 72%; }
    #fields Label { margin-top: 1; }
    #btns { height: 3; align-horizontal: right; }
    #btns Button { margin-left: 2; }
    """

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    def compose(self) -> ComposeResult:
        cfg = self.plugin._CFG
        with Vertical(id='dlg'):
            yield Label(Text('设置（Enter 保存 · Esc 取消）', style='bold'))
            with VerticalScroll(id='fields'):
                for name, label, _typ in _SETTINGS_FIELDS:
                    val = cfg.get(name, '')
                    yield Label('%s' % label)
                    yield Input(value=str(val if val is not None else ''), id='f-' + name)
            with Horizontal(id='btns'):
                yield Button('取消', id='cancel')
                yield Button('保存', id='save', variant='primary')

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == 'save':
            self._save()
        else:
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted):
        self._save()

    def action_cancel(self):
        self.dismiss(False)

    def _save(self):
        cfg = self.plugin._CFG
        for name, _label, typ in _SETTINGS_FIELDS:
            raw = self.query_one('#f-' + name, Input).value.strip()
            try:
                if typ == 'int':
                    cfg[name] = int(raw)
                elif typ == 'float':
                    cfg[name] = float(raw)
                elif typ == 'bool':
                    cfg[name] = raw.lower() in ('1', 'true', 'yes', 'on', 'y')
                else:
                    cfg[name] = raw
            except ValueError:
                self.notify('值非法：%s = %r' % (name, raw), severity='error')
                return
        try:
            self.plugin._rebuild_tools(cfg)
            self.plugin._rebuild_store(cfg)
            self.plugin._persist_cfg(self.plugin._FW, cfg)
        except Exception as e:  # noqa: BLE001
            self.notify('保存失败：%s' % e, severity='error')
            return
        self.dismiss(True)


class SessionScreen(ModalScreen):
    """会话管理弹窗：列举旧会话，进入 / 新建 / 重命名 / 配置 / 删除。"""

    BINDINGS = [
        Binding('escape', 'cancel', '取消'),
        Binding('n', 'new_session', '新建'),
        Binding('r', 'rename', '重命名'),
        Binding('c', 'configure', '配置'),
        Binding('delete', 'delete', '删除'),
    ]

    CSS = """
    SessionScreen { align: center middle; }
    #sd { width: 96; height: auto; max-height: 90%; background: $surface;
          border: thick $accent; padding: 1 2; }
    #slist { height: auto; max-height: 60%; border: round $panel; }
    #sbtns { height: 3; align-horizontal: right; }
    #sbtns Button { margin-left: 2; }
    """

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    def compose(self) -> ComposeResult:
        with Vertical(id='sd'):
            yield Label(Text('会话（Enter 进入 · n 新建 · r 重命名 · c 配置 · Del 删除 · Esc 关闭）',
                             style='bold'))
            yield ListView(id='slist')
            with Horizontal(id='sbtns'):
                yield Button('新建', id='snew')
                yield Button('进入', id='sgo', variant='primary')
                yield Button('删除', id='sdel')

    def on_mount(self):
        self._reload()

    def _current_key(self):
        return self.plugin._session_key()

    def _reload(self, select=None):
        import datetime
        lv = self.query_one('#slist', ListView)
        lv.clear()
        cur = self._current_key()
        rows = self.plugin._sessions()
        if not rows:
            rows = [{'key': cur, 'name': cur, 'turns': 0, 'chars': 0, 'updated': 0,
                     'model': '', 'mode': ''}]
        for s in rows:
            ts = datetime.datetime.fromtimestamp(s['updated']).strftime('%m-%d %H:%M') \
                if s.get('updated') else '—'
            mark = '● ' if s['key'] == cur else '○ '
            extra = '  [%s]' % s['model'] if s.get('model') else ''
            if s.get('mode'):
                extra += ' [%s]' % s['mode']
            label = '%s%s  ·  %d 轮 / %d 字  ·  %s%s' % (
                mark, s['name'], s['turns'], s['chars'], ts, extra)
            lv.append(ListItem(Label(label)))
        if select is not None:
            for i, s in enumerate(rows):
                if s['key'] == select:
                    lv.index = i
                    break

    def _selected_key(self):
        lv = self.query_one('#slist', ListView)
        rows = self.plugin._sessions()
        if not rows:
            return self._current_key()
        idx = lv.index if lv.index is not None else 0
        idx = max(0, min(idx, len(rows) - 1))
        return rows[idx]['key']

    def on_list_view_selected(self, event):
        self._enter(self._selected_key())

    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id
        if bid == 'snew':
            self.action_new_session()
        elif bid == 'sgo':
            self._enter(self._selected_key())
        elif bid == 'sdel':
            self.action_delete()

    def _enter(self, key):
        self.plugin._set_session(key)
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)

    def action_new_session(self):
        self.plugin._new_session()
        self._reload(select=self._current_key())

    def action_rename(self):
        key = self._selected_key()
        self._prompt('重命名会话', self.plugin._STORE.meta(key).get('name', key),
                     lambda v: (self.plugin._rename_session(key, v), self._reload(select=key))[1])

    def action_configure(self):
        key = self._selected_key()
        m = self.plugin._STORE.meta(key)
        self._prompt('覆盖模型（留空=全局；格式 模型名）', m.get('model', ''),
                     lambda v1, v2: (self.plugin._configure_session(key, model=v1, mode=v2),
                                     self._reload(select=key))[1],
                     second=('覆盖模式（留空=全局；readonly/workspace/full）', m.get('mode', '')))

    def action_delete(self):
        key = self._selected_key()
        ok, msg = self.plugin._delete_session(key)
        self.notify(msg, severity='information' if ok else 'error')
        self._reload(select=self._current_key())

    def _prompt(self, title, default, callback, second=None):
        self.push_screen(_PromptScreen(title, default, callback, second))

    def on_session_changed(self, event):
        self._reload()


class _PromptScreen(ModalScreen):
    """极简输入弹窗：1 或 2 个字段。"""

    BINDINGS = [Binding('escape', 'cancel', '取消')]

    CSS = """
    _PromptScreen { align: center middle; }
    #pd { width: 80; height: auto; background: $surface;
          border: thick $accent; padding: 1 2; }
    #pd Label { margin-top: 1; }
    #pbtns { height: 3; align-horizontal: right; }
    #pbtns Button { margin-left: 2; }
    """

    def __init__(self, title, default, callback, second=None):
        super().__init__()
        self._title = title
        self._default = default
        self._cb = callback
        self._second = second

    def compose(self) -> ComposeResult:
        with Vertical(id='pd'):
            yield Label(Text(self._title + '（Enter 确认 · Esc 取消）', style='bold'))
            yield Label(self._title)
            yield Input(value=str(self._default or ''), id='p1')
            if self._second:
                yield Label(self._second[0])
                yield Input(value=str(self._second[1] or ''), id='p2')
            with Horizontal(id='pbtns'):
                yield Button('取消', id='pcancel')
                yield Button('确认', id='pok', variant='primary')

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == 'pok':
            self._submit()
        else:
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted):
        self._submit()

    def action_cancel(self):
        self.dismiss(False)

    def _submit(self):
        v1 = self.query_one('#p1', Input).value
        if self._second is not None:
            v2 = self.query_one('#p2', Input).value
            self._cb(v1, v2)
        else:
            self._cb(v1)
        self.dismiss(True)


class AgentScreen(ModalScreen):
    """人格编辑弹窗：新建 / 编辑人格（名称 · 描述 · 附加系统提示 · 覆盖模型 · 覆盖模式）。"""

    BINDINGS = [Binding('escape', 'cancel', '取消')]

    CSS = """
    AgentScreen { align: center middle; }
    #dlg { width: 92; height: auto; max-height: 94%; background: $surface;
           border: thick $accent; padding: 1 2; }
    #fields { height: auto; max-height: 74%; }
    #fields Label { margin-top: 1; }
    #f-prompt { height: 10; }
    #btns { height: 3; align-horizontal: right; }
    #btns Button { margin-left: 2; }
    """

    AGENT_FIELDS = [
        ('name', '人格名（字母/数字/下划线）', 'input'),
        ('desc', '一句话描述', 'input'),
        ('prompt', '附加系统提示（人格的灵魂，可多行）', 'area'),
        ('model', '覆盖模型（可空 = 用全局）', 'input'),
        ('mode', '覆盖模式（可空：readonly / workspace / full）', 'input'),
    ]

    def __init__(self, plugin, agent_name=None):
        super().__init__()
        self.plugin = plugin
        # 注意：Screen 自带只读的 `name` 属性，这里必须用别的名字
        self._agent_name = agent_name

    def _current(self):
        for a in self.plugin._agents():
            if a['name'] == self._agent_name:
                return a
        return {"name": self._agent_name or "", "desc": "", "prompt": "",
                "model": "", "mode": ""}

    def compose(self) -> ComposeResult:
        a = self._current()
        with Vertical(id='dlg'):
            yield Label(Text('人格（Enter 保存 · Esc 取消）', style='bold'))
            with VerticalScroll(id='fields'):
                for key, label, kind in self.AGENT_FIELDS:
                    yield Label(label)
                    if kind == 'area':
                        yield TextArea(a.get(key, '') or '', id='f-' + key)
                    else:
                        yield Input(value=str(a.get(key, '') or ''), id='f-' + key)
            with Horizontal(id='btns'):
                yield Button('删除', id='del')
                yield Button('取消', id='cancel')
                yield Button('保存', id='save', variant='primary')

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == 'save':
            self._save()
        elif event.button.id == 'del':
            self._delete()
        else:
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted):
        self._save()

    def action_cancel(self):
        self.dismiss(False)

    def _read(self):
        vals = {}
        for key, _label, kind in self.AGENT_FIELDS:
            w = self.query_one('#f-' + key)
            vals[key] = (w.text if kind == 'area' else w.value).strip()
        return vals

    def _save(self):
        import re
        vals = self._read()
        name = vals.get('name') or ''
        if not name:
            self.notify('人格名不能为空', severity='error')
            return
        if not re.match(r'^[A-Za-z0-9_\-]+$', name):
            self.notify('人格名只能用字母 / 数字 / 下划线 / 连字符', severity='error')
            return
        mode = vals.get('mode') or ''
        if mode and mode not in ('readonly', 'workspace', 'full'):
            self.notify('模式只能是 readonly / workspace / full', severity='error')
            return
        agents = self.plugin._agents()
        # 只有「未改动的默认人格」时，用新人格替换它，避免留下空壳 build
        if (len(agents) == 1 and agents[0]['name'] == self.plugin._DEFAULT_AGENT['name']
                and not agents[0].get('prompt') and name != agents[0]['name']):
            agents = []
        found = None
        for a in agents:
            if a['name'] == name:
                found = a
                break
        if found is None:
            found = {"name": name, "desc": "", "prompt": "", "model": "", "mode": ""}
            agents.append(found)
        found.update(desc=vals.get('desc', ''), prompt=vals.get('prompt', ''),
                     model=vals.get('model', ''), mode=mode)
        self.plugin._save_agents(agents, active=name)
        self.dismiss(True)

    def _delete(self):
        name = (self._read().get('name') or '').strip()
        if not name:
            self.notify('请先填人格名', severity='error')
            return
        ok, _msg = self.plugin._del_agent(name)
        if not ok:
            self.notify('没有这个人格：%s' % name, severity='error')
            return
        self.dismiss(True)


class AgentApp(App):
    """AI 智能体全屏界面。"""

    TITLE = 'ZCBOT code'
    CSS = """
    Screen { layout: vertical; }
    #top { height: 1; background: $panel; color: $text; padding: 0 1; }
    #main { height: 1fr; layout: horizontal; }
    #log { width: 1fr; padding: 0 1; }
    #side { width: 32; border-left: solid $accent; padding: 0 1; }
    .side-title { text-style: bold; color: $accent; margin-top: 1; }
    #agents { height: auto; max-height: 70%; background: transparent; }
    #agents ListItem { padding: 0; }
    #status { height: auto; color: $text-muted; }
    #prompt { height: 3; border: tall $accent; }
    """

    BINDINGS = [
        Binding('ctrl+c', 'quit', '退出', priority=True),
        Binding('tab', 'cycle_mode', '切模式', priority=True),
        Binding('ctrl+l', 'clear', '清屏'),
        Binding('ctrl+s', 'settings', '设置'),
        Binding('ctrl+g', 'focus_agents', '人格'),
        Binding('ctrl+t', 'toggle_detail', '细节'),
        Binding('ctrl+n', 'sessions', '会话'),
        # F 键在部分 Windows 控制台会被 conhost 抢（如 F3=重复上一条命令），
        # 仅作别名、不在底栏展示；可靠入口是上面的 Ctrl 组合与斜杠命令。
        Binding('f1', 'help', '帮助', show=False),
        Binding('f2', 'settings', '设置', show=False),
        Binding('f3', 'focus_agents', '人格', show=False),
        Binding('f4', 'edit_agent', '编辑人格', show=False),
    ]

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self._busy = False
        self._partial = ''
        self._reason = ''           # 本轮累积的思考过程
        self.top_text = ''          # 顶栏文本（供测试/调试读取）

    # ── 组装 ───────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Static('', id='top')
        with Horizontal(id='main'):
            yield RichLog(id='log', markup=False, highlight=False, wrap=True)
            with Vertical(id='side'):
                yield Label('人格', classes='side-title')
                yield ListView(id='agents')
                yield Label('状态', classes='side-title')
                yield Static('', id='status')
        yield Input(placeholder='描述你的需求，回车交给智能体…（/help 看命令）', id='prompt')
        yield Footer()

    def on_mount(self):
        self._refresh_top()
        self._refresh_side()
        log = self.query_one('#log', RichLog)
        cfg = self.plugin._CFG
        log.write(Text('ZCBOT code · AI 智能体', style='bold cyan'))
        log.write(Text('当前模式：%s —— %s（按 Tab 循环切换）'
                       % (cfg.get('mode', 'workspace'),
                          _MODE_DESC.get(cfg.get('mode'), '')), style='yellow'))
        log.write(Text(_HELP, style='dim'))
        if not cfg.get('api_key'):
            log.write(Text('尚未配置 api_key：按 Ctrl+S 或输入 /settings 配置。', style='bold red'))
        self._update_placeholder()
        self.query_one('#prompt', Input).focus()
        # 框架停机时自动退出界面
        self.set_interval(1.0, self._watch_framework)

    def _watch_framework(self):
        if getattr(self.plugin._FW, '_running', True) is False:
            self.exit()

    # ── 顶栏 / 侧栏 ────────────────────────────────────────────────────
    def _refresh_top(self):
        cfg = self.plugin._CFG
        busy = '  · 执行中…' if self._busy else ''
        self.top_text = 'ZCBOT code  %s · %s · %s%s' % (
            self.plugin._active_agent()['name'], cfg.get('model', '-'),
            cfg.get('mode', '-'), busy)
        self.query_one('#top', Static).update(Text(self.top_text, style='bold'))

    def _refresh_side(self):
        """右侧边栏：人格列表 + 运行状态。"""
        active = self.plugin._active_agent()['name']
        lv = self.query_one('#agents', ListView)
        lv.clear()
        items = []
        for a in self.plugin._agents():
            mark = '● ' if a['name'] == active else '○ '
            label = mark + a['name'] + (('  ' + a['desc']) if a['desc'] else '')
            item = ListItem(Label(label))
            item.agent_name = a['name']
            items.append(item)
        lv.extend(items)
        cfg = self.plugin._effective_cfg()
        ws = self.plugin._workspace_dir(cfg)
        try:
            turns = len(self.plugin._STORE.history(self.plugin._session_key())) // 2
            n_sess = len(self.plugin._sessions())
        except Exception:
            turns = n_sess = 0
        self.query_one('#status', Static).update(Text(
            '模型  %s\n模式  %s  (Tab 切换)\n工作区  %s\n会话  %s  (%d 轮 · 共 %d 个)\n状态  %s' % (
                cfg.get('model', '-'), cfg.get('mode', '-'), ws,
                self.plugin._session_key(), turns, n_sess,
                '执行中' if self._busy else '就绪'), style='dim'))

    def on_list_view_selected(self, event: ListView.Selected):
        name = getattr(event.item, 'agent_name', None)
        if name:
            self._switch_agent(name)

    # ── 输出 ───────────────────────────────────────────────────────────
    def _log(self, content):
        self.query_one('#log', RichLog).write(content)

    def _sys(self, msg):
        self._log(Text(msg, style='dim'))

    def _err(self, msg):
        self._log(Text(msg, style='bold red'))

    def _write_text(self, chunk):
        """流式文本：按行落盘（避免逐字符刷屏）。"""
        self._partial += chunk
        while '\n' in self._partial:
            line, self._partial = self._partial.split('\n', 1)
            self._log(Text(line))

    def _flush_partial(self):
        if self._partial:
            self._log(Text(self._partial))
            self._partial = ''

    def _write_reason(self, chunk):
        """累积思考过程（不逐字刷屏，结束时统一折叠成摘要或按需全文展示）。"""
        self._reason += chunk

    def _flush_reason(self):
        if not self._reason:
            return
        cfg = self.plugin._CFG
        if cfg.get('show_thinking', True):
            flat = self._reason.strip().replace('\n', ' ')
            if cfg.get('tool_detail'):
                self._log(Text('💭 思考：' + self._reason.strip(), style='dim italic'))
            else:
                preview = flat[:120] + ('…' if len(flat) > 120 else '')
                self._log(Text('💭 思考：%s（共 %d 字，Ctrl+T 展开）' % (preview, len(flat)),
                               style='dim italic'))
        self._reason = ''

    # ── 输入 ───────────────────────────────────────────────────────────
    def on_input_submitted(self, event: Input.Submitted):
        text = (event.value or '').strip()
        inp = self.query_one('#prompt', Input)
        inp.value = ''
        if not text:
            return
        if text.startswith('/'):
            self._slash(text)
            return
        if self._busy:
            self._err('上一轮还在执行中，请稍候…')
            return
        self._log(Text('> ' + text, style='bold cyan'))
        self._start(text)

    def _slash(self, text):
        parts = text[1:].split(None, 1)
        cmd = (parts[0] if parts else '').lower()
        arg = (parts[1].strip() if len(parts) > 1 else '')
        cfg = self.plugin._CFG
        if cmd in ('exit', 'quit', 'q'):
            self.exit()
        elif cmd in ('help', 'h', '?'):
            self._log(Text(_HELP))
        elif cmd in ('settings', 'config', 'set'):
            self.action_settings()
        elif cmd == 'model':
            if arg:
                cfg['model'] = arg
                self.plugin._persist_cfg(self.plugin._FW, cfg)
                self._refresh_top()
                self._sys('模型已切换：%s' % arg)
            else:
                self._sys('当前模型：%s（用法 /model <名>）' % cfg.get('model'))
        elif cmd == 'mode':
            if arg in ('readonly', 'workspace', 'full'):
                cfg['mode'] = arg
                self.plugin._rebuild_tools(cfg)
                self.plugin._persist_cfg(self.plugin._FW, cfg)
                self._refresh_top()
                self._sys('模式已切换：%s' % arg)
            else:
                self._err('用法 /mode readonly|workspace|full')
        elif cmd in ('new', 'clear', 'reset'):
            self.plugin._STORE.clear(self.plugin._session_key())
            self.plugin._STORE.save()
            self.query_one('#log', RichLog).clear()
            self._sys('已开始新会话')
        elif cmd in ('plugins', 'pluginlist'):
            for name in self.plugin._list_plugins(self.plugin._FW):
                self._sys('  · %s' % name)
        elif cmd == 'plugin':
            if not arg:
                self._err('用法 /plugin <名字>（在工作区生成插件骨架）')
            else:
                ok, msg = self.plugin._scaffold_plugin(arg)
                (self._sys if ok else self._err)(msg)
        elif cmd == 'agent':
            self._agent_cmd(arg)
        elif cmd in ('session', 'sessions'):
            self._sessions_cmd(arg)
        elif cmd == 'memory':
            self._memory_cmd(arg)
        elif cmd in ('history', 'hist'):
            self._history_cmd(arg)
        elif cmd == 'think':
            self._toggle_cmd(arg, 'thinking', '深度思考（请求层，需模型/网关支持）')
            self.plugin._rebuild_tools(self.plugin._CFG)
        elif cmd == 'tools':
            self._toggle_cmd(arg, 'show_tools', '显示工具调用')
        elif cmd == 'detail':
            self._toggle_cmd(arg, 'tool_detail', '展开工具细节/思考全文')
        else:
            self._err('未知命令：/%s（/help 查看）' % cmd)

    def _memory_cmd(self, arg):
        """/memory [add <文本> | clear]：长期记忆（跨会话注入系统提示）。"""
        p = self.plugin
        parts = arg.split(None, 1)
        sub = parts[0].lower() if parts else ''
        if sub == 'add' and len(parts) > 1:
            ok, msg = p._append_memory(parts[1])
            (self._sys if ok else self._err)(msg)
            return
        if sub in ('clear', 'reset'):
            ok, msg = p._clear_memory()
            (self._sys if ok else self._err)(msg)
            return
        self._log(Text('长期记忆（%s）' % p._memory_path(), style='bold'))
        self._log(Text(p._load_memory() or '（空）'))
        self._sys('用法：/memory · /memory add <文本> · /memory clear')

    def _history_cmd(self, arg):
        """/history [N]：回看历史归档（每轮对话都写入 history.jsonl）。"""
        try:
            n = int(arg) if arg else 10
        except ValueError:
            n = 10
        rows = self.plugin._read_history(n)
        if not rows:
            self._sys('暂无历史归档（每轮对话会写入 %s）' % self.plugin._history_path())
            return
        import datetime
        for r in rows:
            ts = datetime.datetime.fromtimestamp(r.get('ts', 0)).strftime('%m-%d %H:%M')
            u = (r.get('user') or '').replace('\n', ' ')[:70]
            a = (r.get('assistant') or '').replace('\n', ' ')[:70]
            self._log(Text('[%s] > %s' % (ts, u), style='bold cyan'))
            self._log(Text('        %s' % a, style='dim'))
        self._sys('共 %d 条（新 → 旧）' % len(rows))

    def _agent_cmd(self, arg):
        """/agent [list | <名> | new [名] | edit [名] | del <名> | prompt <名> <文本>]"""
        parts = arg.split(None, 2)
        sub = parts[0].lower() if parts else ''
        p = self.plugin
        if not arg or sub in ('list', 'ls'):
            active = p._active_agent()['name']
            for a in p._agents():
                self._sys('%s%s%s' % ('● ' if a['name'] == active else '○ ',
                                      a['name'], ('  ' + a['desc']) if a['desc'] else ''))
            self._sys('用法：/agent · 切换 <名> · new [名] 弹窗新建 · edit [名] 弹窗编辑 · '
                      'prompt <名> <文本> 快速设提示 · del <名> 删除')
        elif sub in ('new', 'edit'):
            self._open_agent_editor(parts[1].strip() if len(parts) > 1 else None)
        elif sub == 'add':
            ok, msg = p._add_agent(parts[1].strip() if len(parts) > 1 else '')
            (self._sys if ok else self._err)(msg)
            self._after_agent_change()
        elif sub in ('del', 'rm'):
            ok, msg = p._del_agent(parts[1] if len(parts) > 1 else '')
            (self._sys if ok else self._err)(msg)
            self._after_agent_change()
        elif sub in ('prompt', 'set'):
            if len(parts) < 3:
                self._err('用法：/agent prompt <名> <文本>')
            else:
                ok, msg = p._set_agent_prompt(parts[1], parts[2])
                (self._sys if ok else self._err)(msg)
                self._after_agent_change()
        else:
            self._switch_agent(arg)

    def _sessions_cmd(self, arg):
        """/sessions [list | new [名] | del <名> | rename <名> <新名> | <名>]"""
        parts = arg.split(None, 2)
        sub = parts[0] if parts else ''
        p = self.plugin
        if not arg or sub in ('list', 'ls'):
            self.action_sessions()
            return
        low = sub.lower()
        if low == 'new':
            ok, key = p._new_session(parts[1] if len(parts) > 1 else None)
            self._after_session_change('已新建并进入会话：%s' % key)
        elif low in ('del', 'rm', 'delete'):
            if len(parts) < 2:
                self._err('用法：/session del <会话名>')
                return
            ok, msg = p._delete_session(parts[1])
            (self._sys if ok else self._err)(msg)
            self._refresh_side()
        elif low == 'rename':
            if len(parts) < 3:
                self._err('用法：/session rename <会话名> <新名>')
                return
            ok, msg = p._rename_session(parts[1], parts[2])
            (self._sys if ok else self._err)(msg)
            self._refresh_side()
        elif low in ('model', 'mode'):
            if len(parts) < 3:
                self._err('用法：/session %s <会话名> <值>' % low)
                return
            kw = {low: parts[2]}
            ok, msg = p._configure_session(parts[1], **kw)
            (self._sys if ok else self._err)(msg)
        else:
            ok, key = p._set_session(arg.strip())
            self._after_session_change('已进入会话：%s' % key)

    def _after_session_change(self, msg):
        self._sys(msg)
        self._refresh_top()
        self._refresh_side()
        self._update_placeholder()
        # 回放该会话最近几轮，便于接着聊
        hist = self.plugin._STORE.history(self.plugin._session_key())
        if hist:
            self._log(Text('—— 以下为该会话最近记录 ——', style='dim'))
            for m in hist[-6:]:
                who = '>' if m['role'] == 'user' else ' '
                self._log(Text('%s %s' % (who, (m.get('content') or '')[:200]), style='dim'))

    def action_sessions(self):
        """打开会话管理弹窗（列举 / 进入 / 新建 / 重命名 / 配置 / 删除）。"""
        def _after(changed):
            if changed:
                self._after_session_change('当前会话：%s' % self.plugin._session_key())
        self.push_screen(SessionScreen(self.plugin), _after)

    def _open_agent_editor(self, name=None):
        def _after(saved):
            if saved:
                self._refresh_top()
                self._refresh_side()
                self._sys('人格已保存')
        self.push_screen(AgentScreen(self.plugin, name), _after)

    def _switch_agent(self, name):
        ok, msg = self.plugin._set_agent(name)
        if not ok:
            self._err(msg)
            return
        # 人格可自带 model / mode 覆盖
        a = self.plugin._active_agent()
        cfg = self.plugin._CFG
        if a.get('model'):
            cfg['model'] = a['model']
        if a.get('mode') in ('readonly', 'workspace', 'full'):
            cfg['mode'] = a['mode']
            self.plugin._rebuild_tools(cfg)
        self.plugin._persist_cfg(self.plugin._FW, cfg)
        self._sys('已切换人格：%s%s' % (name, ('  ' + a['desc']) if a.get('desc') else ''))
        self._after_agent_change()

    def _after_agent_change(self):
        self._refresh_top()
        self._refresh_side()
        self.query_one('#prompt', Input).focus()

    # ── 动作 ───────────────────────────────────────────────────────────
    def action_quit(self):
        self.exit()

    def action_clear(self):
        self.query_one('#log', RichLog).clear()

    def action_cycle_mode(self):
        """Tab：readonly → workspace → full 循环（并同步 allow_exec）。"""
        order = ('readonly', 'workspace', 'full')
        cfg = self.plugin._CFG
        cur = cfg.get('mode', 'workspace')
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else 'workspace'
        cfg['mode'] = nxt
        cfg['allow_exec'] = (nxt == 'full')
        self.plugin._rebuild_tools(cfg)
        self.plugin._persist_cfg(self.plugin._FW, cfg)
        names = [s['name'] for s in self.plugin._TOOLS.specs()]
        self._sys('模式 → %s：%s' % (nxt, _MODE_DESC[nxt]))
        self._sys('   已开放工具（%d）：%s' % (len(names), ', '.join(names)))
        self._refresh_top()
        self._refresh_side()
        self._update_placeholder()

    def action_help(self):
        self._log(Text(_HELP))

    def action_focus_agents(self):
        lv = self.query_one('#agents', ListView)
        if lv.children:
            lv.focus()
            self._sys('已聚焦「人格」列表：↑↓ 选择 · Enter 切换')
        else:
            self._sys('暂无其他人格（/agent new 打开编辑弹窗）')

    def action_edit_agent(self):
        """打开人格编辑弹窗（默认编辑当前人格）。"""
        self._open_agent_editor(self.plugin._active_agent()['name'])

    def action_settings(self):
        def _after(saved):
            if saved:
                self._refresh_top()
                self._refresh_side()
                self._update_placeholder()
                self._sys('设置已保存')
        self.push_screen(SettingsScreen(self.plugin), _after)

    def action_toggle_detail(self):
        """Ctrl+T：展开 / 折叠「工具参数 + 完整结果」与「思考全文」（防刷屏）。"""
        cfg = self.plugin._CFG
        cfg['tool_detail'] = not bool(cfg.get('tool_detail'))
        self.plugin._persist_cfg(self.plugin._FW, cfg)
        self._sys('细节显示：%s（工具参数/结果、思考全文）'
                  % ('展开' if cfg['tool_detail'] else '折叠'))
        self._refresh_side()

    def _toggle_cmd(self, arg, key, label):
        """通用开关：/think /tools /detail [on|off|省略则切换]。"""
        cfg = self.plugin._CFG
        v = (arg or '').strip().lower()
        if v in ('on', '1', 'true', 'yes'):
            new = True
        elif v in ('off', '0', 'false', 'no'):
            new = False
        else:
            new = not bool(cfg.get(key, False))
        cfg[key] = new
        self.plugin._persist_cfg(self.plugin._FW, cfg)
        self._sys('%s：%s' % (label, '开' if new else '关'))
        self._refresh_side()

    def _update_placeholder(self):
        """输入框占位符带上当前模式，让「Tab 切模式」一眼可见。"""
        mode = self.plugin._CFG.get('mode', 'workspace')
        try:
            self.query_one('#prompt', Input).placeholder = (
                '[%s] 描述你的需求，回车交给智能体…（Tab 切模式 · /help 命令）' % mode)
        except Exception:
            pass

    # ── 智能体（工作线程）──────────────────────────────────────────────
    def _start(self, text):
        self._busy = True
        self._refresh_top()
        self._refresh_side()
        self.run_worker(lambda: self._agent_job(text), thread=True, exclusive=True)

    def _agent_job(self, text):
        try:
            tools = self.plugin._TOOLS
            store = self.plugin._STORE
            key = self.plugin._session_key()
            try:
                self.plugin._maybe_compact(key)
            except Exception:
                pass
            system = self.plugin._system_prompt()
            messages = store.build_messages(key, system, text)
            agent = Agent(self.plugin._effective_cfg(), tools,
                          on_event=lambda k, p: self.call_from_thread(self._on_event, k, p))
            final = agent.run(messages)
            store.append_turn(key, text, final)
            store.save()
            self.plugin._archive_turn(text, final)
        except LLMError as e:
            self.call_from_thread(self._on_event, 'error', {'message': str(e)})
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._on_event, 'error', {'message': '执行异常：%s' % e})
        finally:
            self.call_from_thread(self._on_done)

    def _on_event(self, kind, payload=None):
        payload = payload or {}
        cfg = self.plugin._CFG
        if kind == 'text':
            self._flush_reason()
            self._write_text(payload)
        elif kind == 'reason':
            self._write_reason(payload)
        elif kind == 'tool_start':
            self._flush_reason()
            self._flush_partial()
            if not cfg.get('show_tools', True):
                return
            name = payload.get('name')
            if cfg.get('tool_detail'):
                self._log(Text.from_markup('[dim cyan]⚙ %s(%s)[/dim cyan]'
                                           % (name, _brief(payload.get('args')))))
            else:
                self._log(Text('⚙ %s' % name, style='dim cyan'))
        elif kind == 'tool_end':
            self._flush_reason()
            self._flush_partial()
            if not cfg.get('show_tools', True):
                return
            ok = payload.get('ok')
            if cfg.get('tool_detail'):
                self._log(Text('  %s %s' % ('✔' if ok else '✘', _brief(payload.get('result'))),
                               style='dim' if ok else 'red'))
            else:
                self._log(Text('  %s' % ('✔' if ok else '✘ 失败'), style='dim' if ok else 'red'))
        elif kind == 'error':
            self._flush_reason()
            self._flush_partial()
            self._err('[!] %s' % payload.get('message'))

    def _on_done(self):
        self._flush_reason()
        self._flush_partial()
        self._busy = False
        self._refresh_top()
        self._refresh_side()


def _brief(obj, limit=200):
    s = obj if isinstance(obj, str) else str(obj)
    s = s.replace('\n', ' ')
    return s if len(s) <= limit else s[:limit] + '…'


def run(plugin):
    """启动界面（阻塞至退出）。"""
    AgentApp(plugin).run()
