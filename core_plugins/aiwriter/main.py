# -*- coding: utf-8 -*-
"""终端 AI 智能体（官方插件，process=core）。

把 AI 智能体做成 ZCBOT 的**终端界面 + 独立 CLI**：给它一个任务，它自己读工作区、
写/改文件、跑命令，把过程与结果实时打印出来；也可以进入多轮交互会话连续对话。

- 定位：**专门给 ZCBOT 写插件的智能体**（Python，`plugins/<名>/main.py`），
  也能做常规编码与运维；系统提示里会自动注入仓库的插件开发要点。
- 工具集：read / list / glob / grep / webfetch / websearch / db_query / plugin_list /
  write / edit / remember / plugin_new / shell / python / db_execute / plugin_action，
  并带能力级权限闸门（readonly / workspace / full）。
- 系统提示与上下文压缩：分节式系统提示（含工作区 / 长期记忆 / 插件要点）+ 超阈值会话压缩。

自带最小 LLM 客户端（OpenAI 兼容流式），不依赖 llm_core。整条链路与 llm_core 解耦，
换模型厂商只改配置。

模块拆分：
  llm.py      OpenAI 兼容流式客户端（provider 层）
  prompt.py   系统提示词（harness / communication / codebase）
  tools.py    工作区工具集（智能体的「手」）+ 权限
  agent.py    工具调用闭环（流式事件）
  session.py  会话存储与上下文压缩
"""
import json
import logging
import os
import sys
import time

# 本插件的包名（_PKG）：包导入时用真实包名；框架以顶层模块加载时，给本模块
# 补上 __path__/__package__ 使其成为包，再用相对导入——与框架给用户插件建
# 合成包同一机制，避免 agent/llm/tools/session/prompt 这些通用名与其它插件的
# 同名模块（如 plugins/llm_core/agent.py）在 sys.modules 里撞车。
_PKG = None
try:  # 包导入（测试 / 包内引用）
    from .llm import LLMError, stream_chat
    from .tools import ToolRegistry
    from .agent import Agent
    from .session import SessionStore
    from . import prompt as prompt_mod
    _PKG = __package__
except ImportError:  # 兜底：加载器未把本模块建成包（老加载路径）时，自行补包属性
    import importlib
    _self = sys.modules.get(__name__)
    _d = os.path.dirname(os.path.abspath(__file__))
    if _self is not None and not getattr(_self, '__path__', None):
        _self.__path__ = [_d]
    # 清掉上一次加载（热重载）遗留的子模块，避免命中旧代码
    for _k in [k for k in list(sys.modules) if k.startswith(__name__ + '.')]:
        sys.modules.pop(_k, None)
    _PKG = __name__
    # 用 importlib 显式按包名导入子模块（不用 from .x 语法，避免 __package__ 与
    # __spec__.parent 不一致的 DeprecationWarning）
    _llm = importlib.import_module(__name__ + '.llm')
    LLMError, stream_chat = _llm.LLMError, _llm.stream_chat
    ToolRegistry = importlib.import_module(__name__ + '.tools').ToolRegistry
    Agent = importlib.import_module(__name__ + '.agent').Agent
    SessionStore = importlib.import_module(__name__ + '.session').SessionStore
    prompt_mod = importlib.import_module(__name__ + '.prompt')

__plugin_meta__ = {
    "name": "终端 AI 智能体",
    "version": "2.0.0",
    "author": "ZCBOT",
    "desc": "终端 AI 智能体（ZCBOT 插件开发）：读写工作区 / 跑命令 / 写插件",
    "priority": 50,
    "official": True,
    "process": "core",
}

DEFAULTS = {
    "enabled": False,
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "temperature": 0.7,
    "max_rounds": 12,
    "timeout": 120,
    "mode": "workspace",          # readonly | workspace | full
    "allow_exec": False,          # full 模式下是否开放 shell
    "workspace": "",              # 工作区根目录；留空 = 框架根目录（直接在项目里干活）
    "keep_turns": 20,             # 会话保留轮数
    "max_history_chars": 24000,   # 超过则触发上下文压缩
    "session_hard_cap": 400,      # 会话历史硬上限（条，超出丢最旧；0 = 按 keep_turns×20 推导）
    "persist_session": True,      # 跨重启保留会话（关掉则每次启动都是新会话）
    "thinking": False,            # 是否请求深度思考（模型/网关需支持）
    "thinking_style": "auto",     # 注入方式：auto / anthropic / flag / openai
    "show_thinking": True,        # 界面是否显示思考过程（默认折叠为一行摘要）
    "show_tools": True,           # 界面是否显示工具调用
    "tool_detail": False,         # 是否展开工具参数 / 完整结果（防刷屏默认关）
    "max_output": 8000,           # 单次工具输出截断
    "shell_timeout": 60,
    "session_key": "terminal",
    "agent": "build",             # 当前人格（agents 里的 name）
    "agents": [],                 # 人格列表 [{name, desc, prompt, model?, mode?}]
}

# 内置默认人格（agents 为空时使用）
_DEFAULT_AGENT = {"name": "build", "desc": "ZCBOT 插件开发", "prompt": ""}

_CTX = None
_FW = None
_CFG = dict(DEFAULTS)
_TOOLS = None
_STORE = None

# 项目根（用于插件脚手架落点 plugins/<名>）
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 可在设置界面编辑并写回 core_plugins.yaml 的键
_EDITABLE_KEYS = ('base_url', 'api_key', 'model', 'temperature', 'mode',
                  'allow_exec', 'max_rounds', 'workspace', 'agent', 'agents',
                  'keep_turns', 'max_history_chars', 'session_hard_cap',                   'persist_session', 'thinking', 'show_thinking', 'show_tools', 'tool_detail',
                  'max_output', 'shell_timeout', 'python_timeout', 'net_timeout')


# ── 配置 ─────────────────────────────────────────────────────────────
def _load_cfg(fw):
    cfg = dict(DEFAULTS)

    def _merge(block):
        # core_plugins 段的值可能是布尔开关（enabled），只在确为配置字典时合并
        if isinstance(block, dict):
            cfg.update(block)

    _merge(fw.config.get("aiwriter"))
    _merge((fw.config.get("core_plugins", {}) or {}).get("aiwriter"))
    if not cfg.get("api_key"):
        cfg["api_key"] = os.environ.get("AIWRITER_API_KEY", "")
    return cfg


def _data_dir():
    return _CTX.get_data_dir() if _CTX is not None else os.getcwd()


def _workspace_dir(cfg=None):
    """工作区（智能体的读写边界）。

    缺省 = **框架根目录**——直接在当前项目里干活；要限制范围
    就把 `aiwriter.workspace` 指到别的目录。
    """
    cfg = cfg if cfg is not None else _CFG
    ws = (cfg.get("workspace") or "").strip()
    if ws:
        return os.path.abspath(os.path.expanduser(ws))
    return _ROOT


def _temp_dir():
    """临时文件目录（data/tmp）——中间产物丢这里，别污染工作区。"""
    d = os.path.join(_ROOT, "data", "tmp")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _system_prompt():
    """当前系统提示（含工作区 / 临时目录 / 长期记忆 / 人格 / 插件开发要点）。"""
    return prompt_mod.build([s["name"] for s in _TOOLS.specs()],
                            _active_agent().get("prompt", ""),
                            workspace=_workspace_dir(), temp_dir=_temp_dir(),
                            memory=_load_memory(),
                            plugin_brief=_load_plugin_brief())


# ── 插件开发要点（取自仓库文档，注入系统提示）─────────────────────────
_PLUGIN_BRIEF = None


def _load_plugin_brief():
    """读仓库里的插件开发要点（docs/llm-plugins.md + LLM.md），进程内缓存。"""
    global _PLUGIN_BRIEF
    if _PLUGIN_BRIEF is not None:
        return _PLUGIN_BRIEF
    parts = []
    for rel in ('docs/llm-plugins.md', 'LLM.md'):
        try:
            p = os.path.join(_ROOT, *rel.split('/'))
            if os.path.isfile(p):
                with open(p, encoding='utf-8', errors='replace') as f:
                    parts.append('# 来自仓库 %s\n%s' % (rel, f.read()))
        except Exception:
            continue
    _PLUGIN_BRIEF = '\n\n'.join(parts)
    return _PLUGIN_BRIEF


# ── 长期记忆（跨会话）────────────────────────────────────────────────
def _memory_path():
    return os.path.join(_data_dir(), "memory.md")


def _load_memory():
    try:
        p = _memory_path()
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                return f.read()
    except Exception:
        pass
    return ""


def _append_memory(text):
    text = (text or "").strip()
    if not text:
        return False, "内容为空"
    try:
        p = _memory_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write("- %s\n" % text.replace("\n", " "))
        return True, "已记入长期记忆（memory.md）"
    except Exception as e:  # noqa: BLE001
        return False, "写入失败：%s" % e


def _clear_memory():
    try:
        os.remove(_memory_path())
        return True, "已清空长期记忆"
    except FileNotFoundError:
        return True, "长期记忆本来就是空的"
    except Exception as e:  # noqa: BLE001
        return False, "清空失败：%s" % e


# ── 历史归档（跨会话）────────────────────────────────────────────────
def _history_path():
    return os.path.join(_data_dir(), "history.jsonl")


def _archive_turn(user_text, assistant_text):
    """把一轮对话追加到历史归档（jsonl），供 /history 回看。"""
    try:
        p = _history_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), "user": user_text,
                                "assistant": assistant_text}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read_history(limit=20):
    """读最近 limit 条历史（新 → 旧）。"""
    p = _history_path()
    if not os.path.isfile(p):
        return []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max(1, int(limit)):]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return list(reversed(out))
    except Exception:
        return []


def _session_key():
    return _CFG.get("session_key", "terminal")


# ── 会话管理（多会话：列举 / 新建 / 进入 / 重命名 / 配置 / 删除）────────
def _sessions():
    """全部会话（按最近更新倒序）。"""
    return _STORE.sessions() if _STORE else []


def _effective_cfg():
    """当前会话生效配置：全局配置 + 会话级覆盖（模型 / 模式）。"""
    cfg = dict(_CFG)
    if _STORE:
        m = _STORE.meta(_session_key())
        if m.get('model'):
            cfg['model'] = m['model']
        if m.get('mode') in ('readonly', 'workspace', 'full'):
            cfg['mode'] = m['mode']
            cfg['allow_exec'] = (m['mode'] == 'full')
    return cfg


def _new_session(name=None):
    """新建会话并切过去。返回 (ok, key|msg)。"""
    key = _STORE.new_key((name or '').strip() or '会话')
    _STORE.ensure(key, name=(name or '').strip() or key)
    _STORE.save()
    _set_session(key)
    return True, key


def _set_session(key):
    """切换到指定会话（不存在则创建）。"""
    if not _STORE.exists(key):
        _STORE.ensure(key, name=key)
    _CFG['session_key'] = key
    _STORE.save()
    _persist_cfg(_FW, _CFG)
    return True, key


def _delete_session(key):
    if not _STORE.exists(key):
        return False, '没有这个会话：%s' % key
    was_active = (key == _session_key())
    _STORE.delete(key)
    if was_active:
        rest = _STORE.sessions()
        nxt = rest[0]['key'] if rest else _STORE.new_key('会话')
        _CFG['session_key'] = nxt
        _STORE.ensure(nxt, name=nxt)
        _persist_cfg(_FW, _CFG)
    _STORE.save()
    return True, '已删除会话：%s%s' % (key, '（已切到 %s）' % _session_key() if was_active else '')


def _rename_session(key, new_name):
    if not _STORE.exists(key):
        return False, '没有这个会话：%s' % key
    _STORE.rename(key, new_name)
    _STORE.save()
    return True, '已重命名：%s → %s' % (key, new_name)


def _configure_session(key, model=None, mode=None):
    if not _STORE.exists(key):
        return False, '没有这个会话：%s' % key
    _STORE.set_meta(key, model=model, mode=mode)
    _STORE.save()
    return True, '已更新会话配置：%s' % key


# ── 人格（agent）─────────────────────────────────────────────────────
def _agents():
    """全部人格（配置为空时给内置默认人格）。"""
    raw = _CFG.get("agents")
    out = []
    if isinstance(raw, list):
        for a in raw:
            if isinstance(a, dict) and a.get("name"):
                out.append({"name": str(a["name"]),
                            "desc": str(a.get("desc", "")),
                            "prompt": str(a.get("prompt", "")),
                            "model": str(a.get("model", "") or ""),
                            "mode": str(a.get("mode", "") or "")})
    if not out:
        out = [dict(_DEFAULT_AGENT)]
    return out


def _active_agent():
    """当前人格（名字不存在时回落到第一个）。"""
    name = _CFG.get("agent") or _DEFAULT_AGENT["name"]
    for a in _agents():
        if a["name"] == name:
            return a
    return _agents()[0]


def _save_agents(agents, active=None):
    _CFG["agents"] = agents
    if active:
        _CFG["agent"] = active
    elif _CFG.get("agent") not in [a["name"] for a in agents]:
        _CFG["agent"] = agents[0]["name"] if agents else _DEFAULT_AGENT["name"]
    if _FW is not None:
        _persist_cfg(_FW, _CFG)


def _set_agent(name):
    names = [a["name"] for a in _agents()]
    if name not in names:
        return False, "没有这个人格：%s（可用：%s）" % (name, ", ".join(names))
    _CFG["agent"] = name
    if _FW is not None:
        _persist_cfg(_FW, _CFG)
    return True, "已切换人格：%s" % name


def _add_agent(name, prompt=""):
    name = (name or "").strip()
    if not name:
        return False, "用法：/agent new <名字>"
    agents = _agents()
    if any(a["name"] == name for a in agents):
        return False, "人格已存在：%s" % name
    agents.append({"name": name, "desc": "", "prompt": prompt, "model": "", "mode": ""})
    _save_agents(agents, active=name)
    return True, "已新建人格：%s（用 /agent prompt %s <文本> 设置它的提示）" % (name, name)


def _del_agent(name):
    agents = [a for a in _agents() if a["name"] != name]
    if len(agents) == len(_agents()):
        return False, "没有这个人格：%s" % name
    if not agents:
        agents = [dict(_DEFAULT_AGENT)]
    _save_agents(agents)
    return True, "已删除人格：%s" % name


def _set_agent_prompt(name, text):
    agents = _agents()
    for a in agents:
        if a["name"] == name:
            a["prompt"] = text
            _save_agents(agents)
            return True, "已更新人格 %s 的提示" % name
    return False, "没有这个人格：%s" % name


def _rebuild_tools(cfg=None):
    """按当前配置重建工具集（mode / allow_exec / workspace 变更后调用）。"""
    global _TOOLS
    cfg = cfg if cfg is not None else _CFG
    workspace = _workspace_dir(cfg)
    os.makedirs(workspace, exist_ok=True)
    _temp_dir()
    _TOOLS = ToolRegistry(
        workspace, mode=cfg.get("mode", "workspace"),
        allow_exec=cfg.get("allow_exec", False),
        max_output=cfg.get("max_output", 8000),
        shell_timeout=cfg.get("shell_timeout", 60),
        python_timeout=cfg.get("python_timeout", 60),
        net_timeout=cfg.get("net_timeout", 20),
        host=_HostBridge(_FW))
    return _TOOLS


class _StandaloneDb:
    """独立 CLI 用的**只读**数据库连接。

    直接按 config.yaml 的 database 段连库（SQLite 以 `mode=ro` 打开）。
    只读是有意为之：bot 可能正在运行并持有写锁，只读既安全又不会与它争锁。
    写操作（db_execute）在独立模式下明确拒绝，并在消息里给出指引。
    """

    def __init__(self, dbcfg):
        self._cfg = dbcfg or {}
        self.readonly = True
        self.path = None
        self._con = None
        typ = str(self._cfg.get('type') or 'sqlite').lower()
        if typ != 'sqlite':
            raise RuntimeError('独立模式目前只支持只读访问 sqlite（当前 database.type=%s）' % typ)
        raw = self._cfg.get('path') or 'data/zcbot.db'
        p = raw if os.path.isabs(raw) else os.path.join(_ROOT, raw)
        if not os.path.isfile(p):
            raise RuntimeError('数据库文件不存在：%s' % p)
        import sqlite3
        uri = 'file:%s?mode=ro' % p.replace('\\', '/')
        self._con = sqlite3.connect(uri, uri=True, timeout=5)
        self._con.row_factory = sqlite3.Row
        self.path = p

    def query(self, sql, params=None):
        cur = self._con.execute(sql, tuple(params) if params else ())
        return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql, params=None):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=None):
        raise RuntimeError('独立模式是只读连接，不能写库；请在运行中的 bot 终端里执行')


class _HostBridge:
    """把框架侧能力（数据库 / 插件管理）暴露给工具集。

    - **bot 内**（真实框架）：复用框架 db / 插件加载器，读写与插件管理都可用。
    - **独立 CLI**（`python main.py code`）：用只读连接查库（db_query 可用、
      db_execute 拒绝并提示）；插件管理给指引（避免动到运行中实例的状态）。
    """

    def __init__(self, fw):
        self.fw = fw
        self.standalone = isinstance(fw, _StubFramework)

    def _db(self):
        db = getattr(self.fw, "db", None)
        if db is None:
            raise RuntimeError("数据库不可用：%s"
                               % (getattr(self.fw, "db_error", "") or "未初始化"))
        return db

    def db_query(self, sql):
        return self._db().query(sql)

    def db_execute(self, sql):
        return self._db().execute(sql)

    def db_path(self):
        return getattr(self.fw, "db_path", None) or getattr(getattr(self.fw, 'db', None),
                                                           'path', None)

    def plugins(self):
        try:
            loaded = self.fw.plugin_loader.get_loaded_plugins()
            if loaded:
                return sorted(loaded.keys())
        except Exception:
            pass
        try:
            # 独立模式：直接读 core_plugins.yaml 的启用列表（真实可用）
            if self.standalone:
                import yaml
                from framework.config import CORE_PLUGINS_YAML
                if os.path.isfile(CORE_PLUGINS_YAML):
                    with open(CORE_PLUGINS_YAML, encoding='utf-8') as f:
                        data = yaml.safe_load(f) or {}
                    on = [k for k, v in data.items()
                          if isinstance(v, dict) and v.get('enabled')]
                    if on:
                        return sorted(on)
        except Exception:
            pass
        try:
            from framework.terminal.helper import installed_core_plugins
            return sorted(installed_core_plugins())
        except Exception:
            return []

    def plugin_action(self, action, name):
        if self.standalone:
            return ("独立模式下不代管插件（避免与运行中的 bot 冲突）。"
                    "请在 bot 终端执行 `%s %s`，或改 core_plugins.yaml 后重载。" % (action, name))
        # 复用终端命令（已处理官方/用户插件差异与配置写回）
        import contextlib
        import io
        from framework.terminal import terminal_commands
        handler = terminal_commands.get(action)
        if handler is None:
            return "不支持的动作：%s" % action
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            handler(name)
        return buf.getvalue().strip() or "已执行 %s %s" % (action, name)

    # 长期记忆（供 remember 工具）
    def memory_add(self, text):
        return _append_memory(text)[1]

    def memory_read(self):
        return _load_memory()

    # 插件脚手架（供 plugin_new 工具）
    def plugin_new(self, name):
        return _scaffold_plugin(name)


def _rebuild_store(cfg=None):
    """按当前配置重建会话存储（keep_turns / 压缩阈值 / 硬上限变更后调用）。"""
    global _STORE
    cfg = cfg if cfg is not None else _CFG
    _STORE = SessionStore(
        os.path.join(_data_dir(), "sessions.json"),
        keep_turns=cfg.get("keep_turns", 20),
        max_chars=cfg.get("max_history_chars", 24000),
        hard_cap=cfg.get("session_hard_cap"))
    return _STORE


def _persist_cfg(fw, cfg):
    """把可编辑键写回 core_plugins.yaml（官方插件配置的唯一权威）并同步内存。"""
    editable = {k: cfg[k] for k in _EDITABLE_KEYS if k in cfg}
    try:
        import yaml
        from framework.config import CORE_PLUGINS_YAML
        path = CORE_PLUGINS_YAML
        data = {}
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            data = {}
        cps = data.get("core_plugins")
        if not isinstance(cps, dict):
            cps = {}
        blk = cps.get("aiwriter")
        if not isinstance(blk, dict):
            blk = {}
        blk.update(editable)
        cps["aiwriter"] = blk
        data["core_plugins"] = cps
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    except Exception:
        pass
    if isinstance(fw.config.get("aiwriter"), dict):
        fw.config["aiwriter"].update(editable)
    else:
        fw.config["aiwriter"] = dict(editable)


def _list_plugins(fw):
    """已加载插件名（失败则退回扫描已安装官方插件）。"""
    try:
        names = sorted((fw.plugin_loader.get_loaded_plugins() or {}).keys())
        if names:
            return names
    except Exception:
        pass
    try:
        from framework.terminal.helper import installed_core_plugins
        return sorted(installed_core_plugins())
    except Exception:
        return []


_PLUGIN_SKELETON = '''# -*- coding: utf-8 -*-
"""{name} —— zcbot 用户插件（由终端 AI 智能体脚手架生成）。

把 __plugin_meta__ 改好，在 register(ctx) 里用 ctx.command 注册命令、
ctx.hook 挂扩展点、ctx.register_api 暴露 REST 路由。
"""
__plugin_meta__ = {{
    "name": "{name}",
    "version": "0.1.0",
    "author": "",
    "desc": "TODO: 一句话描述",
    "priority": 50,
}}


def register(ctx):
    def handle(event, match):
        # TODO: 实现你的逻辑；回复示例：
        # import asyncio
        # asyncio.create_task(ctx.asend_msg(
        #     user_id=event.user_id,
        #     group_id=event.group_id if event.is_group else None,
        #     message="hello from {name}"))
        pass

    ctx.command("/{cmd}", handle, description="TODO: 命令说明")
    ctx.log("{name} 已加载")


def unregister():
    pass
'''

_PLUGIN_README = '''# {name}

zcbot 用户插件（脚手架生成）。

## 元信息

- 优先级：50
- 命令：`/{cmd}`

## 用法

```
/{cmd} <参数>
```

把本目录放进项目的 `plugins/` 下，在后台点「重载」或重启框架即可加载。
'''


def _scaffold_plugin(name):
    """在工作区/项目 plugins 目录生成插件骨架。返回 (ok, 消息)。"""
    import re
    name = (name or "").strip()
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
        return False, "插件名非法（仅字母/数字/下划线，且不以数字开头）：%r" % name
    base = os.path.join(_ROOT, "plugins", name)
    if os.path.exists(os.path.join(base, "main.py")):
        return False, "插件已存在：plugins/%s/main.py" % name
    try:
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "main.py"), "w", encoding="utf-8") as f:
            f.write(_PLUGIN_SKELETON.format(name=name, cmd=name))
        with open(os.path.join(base, "README.md"), "w", encoding="utf-8") as f:
            f.write(_PLUGIN_README.format(name=name, cmd=name))
    except Exception as e:
        return False, "生成失败：%s" % e
    return True, "已生成插件骨架：plugins/%s/（main.py + README.md）" % name



# ── 输出（对非 UTF-8 控制台做兜底，避免符号导致 UnicodeEncodeError）──
def _out(text, end="\n"):
    try:
        print(text, end=end, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(str(text).encode(enc, "replace").decode(enc))
        sys.stdout.write(end)
        sys.stdout.flush()


# ── 会话压缩 ─────────────────────────────────────────────────────────
def _summarize(older):
    convo = "\n".join("%s: %s" % (m.get("role"), m.get("content")) for m in older)
    msgs = [
        {"role": "system",
         "content": "你是对话摘要器。用中文把下面的对话压缩成要点，保留关键结论、"
                    "涉及的文件路径与未决事项，尽量简短。"},
        {"role": "user", "content": convo[:20000]},
    ]
    parts = []
    for kind, val in stream_chat(dict(_CFG, max_rounds=1), msgs, []):
        if kind == "text":
            parts.append(val)
    return "".join(parts)


def _maybe_compact(key):
    if _STORE.needs_compaction(key):
        _out("\033[2m[*] 会话较长，正在压缩较早上下文…\033[0m")
        _STORE.compact(key, _summarize)
        _STORE.save()


# ── 终端命令（bot 运行中）─────────────────────────────────────────────
def _load_tui():
    """返回 tui 模块（Textual 全屏界面）。缺 textual 时给出安装提示。"""
    import importlib
    try:
        return importlib.import_module(_PKG + '.tui')
    except ImportError as e:
        raise RuntimeError("code 界面需要 textual：pip install textual（%s）" % e)


def _cmd_code(args):
    """打开 AI 智能体界面: code

    界面在**独立进程**里跑（Textual 全屏接管终端），界面与后端分离——
    也彻底避开在 bot 线程里跑全屏 TUI 的坑。
    经远程通道（调试控制台 / HTTP）调用时直接拒绝：那里没有 TTY，起不来。
    """
    import subprocess
    try:
        from framework.terminal.context import is_remote_session
        if is_remote_session():
            _out("[code] 这是远程文本通道（调试控制台 / HTTP），没有 TTY，无法承载全屏界面。\n"
                 "       本地终端直接跑：python main.py code\n"
                 "       只想在文本通道里跟智能体对话：python main.py -a --agent")
            return
    except Exception:
        pass
    main_py = os.path.join(_ROOT, "main.py")
    if not os.path.isfile(main_py):
        _out("[code] 找不到入口：%s" % main_py)
        return
    try:
        rc = subprocess.call([sys.executable, main_py, "code"], cwd=_ROOT)
    except KeyboardInterrupt:
        return
    except Exception as e:  # noqa: BLE001
        _out("[code] 启动界面失败：%s" % e)
        return
    if rc:
        _out("[code] 界面退出码 %s" % rc)


def _register_terminal():
    from framework.terminal import terminal_commands
    terminal_commands.register(
        "code", _cmd_code,
        "AI 智能体界面（全屏 TUI）: code",
        aliases=["oc"], target="core")


# ── 独立 CLI（命令行直接启动，不启动 bot）─────────────────────────────
class _StubPluginLoader:
    def __init__(self, plugins_dat_dir):
        self.plugins_dat_dir = plugins_dat_dir
        self._loaded_plugins = {}


class _StubFramework:
    """独立 CLI 用的最小框架面：config / plugin_loader / 只读 db / _running。"""

    def __init__(self, config, config_path=None):
        self.config = config
        self.config_path = config_path
        self._running = True
        self.plugin_loader = _StubPluginLoader(
            os.path.join(_ROOT, "data", "plugins_dat"))
        # 只读数据库：独立模式下也能查库（写操作会被拒并给出指引）
        self.db = None
        self.db_error = ''
        self.db_path = None
        try:
            self.db = _StandaloneDb((config or {}).get('database'))
            self.db_path = self.db.path
        except Exception as e:  # noqa: BLE001
            self.db_error = str(e)

    def stop(self):
        self._running = False


class _StubCtx:
    def __init__(self, framework):
        self._framework = framework

    def get_data_dir(self):
        d = os.path.join(self._framework.plugin_loader.plugins_dat_dir, "core_aiwriter")
        os.makedirs(d, exist_ok=True)
        return d

    def log(self, msg, level="info"):
        logging.getLogger("zcbot").info(msg)


def bootstrap(config_path=None):
    """独立启动：加载配置、初始化插件状态（不启动 bot）。返回 (framework, module)。"""
    from framework.config import load_config
    cfg = load_config(config_path)
    fw = _StubFramework(cfg, config_path)
    register(_StubCtx(fw))
    return fw, sys.modules[__name__]


def run_cli(config_path=None):
    """CLI 入口：命令行直接打开 AI 智能体界面。返回退出码。"""
    try:
        tui = _load_tui()
    except Exception as e:  # noqa: BLE001
        _out("[code] 界面不可用：%s" % e)
        return 1
    try:
        _fw, plugin = bootstrap(config_path)
    except Exception as e:  # noqa: BLE001
        _out("[code] 初始化失败：%s" % e)
        return 1
    try:
        tui.run(plugin)
    except RuntimeError as e:
        _out("[code] %s" % e)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


class _AgentService:
    """把智能体暴露为框架服务：`svc.run(text, on_event=None) -> 最终文本`（同步阻塞）。

    供调试控制台等「无 TTY 的文本通道」调用——那样也能跟智能体对话，
    而不必起全屏界面。
    """

    def run(self, text, on_event=None):
        key = _session_key()
        try:
            _maybe_compact(key)
        except Exception:
            pass
        tools = _TOOLS
        system = _system_prompt()
        messages = _STORE.build_messages(key, system, text)
        agent = Agent(_effective_cfg(), tools, on_event=on_event)
        final = agent.run(messages)
        _STORE.append_turn(key, text, final)
        _STORE.save()
        _archive_turn(text, final)
        return final


# ── 注册 / 注销（bot 内）───────────────────────────────────────────────
def register(ctx):
    global _CTX, _FW, _CFG, _TOOLS, _STORE
    _CTX = ctx
    _FW = ctx._framework
    _CFG = _load_cfg(_FW)

    _rebuild_tools(_CFG)
    _rebuild_store(_CFG)
    # 确保当前会话存在，并写入默认名
    _STORE.ensure(_session_key(), name=_session_key())
    # 关掉持久化 → 每次启动都是全新会话（历史归档与长期记忆不受影响）
    if not _CFG.get("persist_session", True):
        _STORE.clear(_session_key())
    _STORE.save()

    _register_terminal()
    # 暴露智能体服务（调试控制台等文本通道可用 `svc.run(text)` 对话）
    try:
        _FW.services.register("aiwriter", _AgentService())
    except Exception:
        pass
    ctx.log("终端 AI 智能体已注册：model=%s mode=%s workspace=%s（终端输入 code 打开界面）"
            % (_CFG.get("model"), _CFG.get("mode"), _TOOLS.root))


def unregister():
    global _CTX, _FW, _TOOLS, _STORE
    try:
        from framework.terminal import terminal_commands
        terminal_commands.remove("code")
    except Exception:
        pass
    try:
        _FW.services.remove("aiwriter")
    except Exception:
        pass
    _CTX = None
    _FW = None
    _TOOLS = None
    _STORE = None
