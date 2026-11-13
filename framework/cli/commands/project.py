# -*- coding: utf-8 -*-
"""``zb create`` / ``zb init`` —— 项目与插件 / 适配器脚手架。"""
import os

from ..utils import PROG

PLUGIN_TEMPLATE = '''# -*- coding: utf-8 -*-
"""{desc}"""
from framework.plugin import command, on_message

__plugin_meta__ = {{
    "name": "{name}",
    "version": "0.1.0",
    "author": "your-name",
    "desc": "{desc}",
    "priority": 50,
}}

_ctx = None


def register(ctx):
    """插件入口：框架加载时调用，注入 ctx。"""
    global _ctx
    _ctx = ctx
    ctx.log("插件已加载: {id}")


@command("/hello")
def hello(event, match):
    """示例命令：/hello"""
    _ctx.send_msg(
        message="hello, %s" % (event.sender.get("nickname") or event.user_id),
        user_id=event.user_id,
        group_id=event.group_id,
    )


@on_message
def on_message(event):
    """订阅所有消息（示例：计数，可删）。"""
    # PluginContext 没有 set_config（那是 --iipc py_sdk 的 API）。
    # 示例只做进程内计数，需要持久化请用 _ctx.get_data_dir() 自行落盘。
    on_message.count = getattr(on_message, "count", 0) + 1
    _ctx.log("收到消息 #%d" % on_message.count)
'''

ADAPTER_TEMPLATE = '''# -*- coding: utf-8 -*-
"""{desc}（适配器脚手架）

这是 core_plugins 下的一个适配器骨架。在这里建立与上游协议的连接，
把收到的消息转成框架事件，并通过 ``_ctx.send_msg`` 下发回复。
网络循环建议放在独立线程 / 协程里，避免阻塞框架主线程。
"""
from framework.plugin import command

__plugin_meta__ = {{
    "name": "{name}",
    "version": "0.1.0",
    "author": "your-name",
    "desc": "{desc}",
    "priority": 50,
}}

_ctx = None


def register(ctx):
    global _ctx
    _ctx = ctx
    ctx.log("适配器已加载: {id}（TODO: 建立协议连接）")


@command("/{id} status")
def status(event, match):
    """适配器状态自检（示例）。"""
    _ctx.send_msg(
        message="[{id}] 适配器在线（连接逻辑待实现）",
        user_id=event.user_id,
        group_id=event.group_id,
    )
'''

PROJECT_CONFIG = '''# ZCBOT 项目配置（由 `{prog} create project` 生成）
language: zh

database:
  type: sqlite
  path: data/zcbot.db

onebot:
  listen_port: 6830          # 反向 WS 监听端口，与接入端配置保持一致
  access_token: ''

web:
  port: 8080
  # ui_enabled: false        # 纯 API 模式：只提供 /api，不启动 WebUI 前端

plugin:
  dat_dir: data/plugins_dat  # 插件数据目录（顶层 plugins_dat 键不会被读取）
'''

PROJECT_REQUIREMENTS = '''# ZCBOT 项目依赖（核心）
websockets>=12.0
apscheduler>=3.10.0
pyyaml>=6.0
bcrypt>=4.0.0
requests>=2.28.0
psutil>=5.9.0
'''

BOT_LAUNCHER = '''# -*- coding: utf-8 -*-
"""项目入口（由 `{prog} create project` 生成）。

确保 zcbot 框架在 PYTHONPATH 中：
    export ZCBOT_HOME=/path/to/zcbot
    export PYTHONPATH=$ZCBOT_HOME:$PYTHONPATH
然后运行：python bot.py
"""
import os
import sys

_home = os.environ.get("ZCBOT_HOME")
if _home:
    sys.path.insert(0, _home)

from main import main

if __name__ == "__main__":
    main()
'''

README_TPL = '''# {name}

ZCBOT 项目（由 `{prog} create project` 生成）。

## 运行

    python bot.py

或在本目录下使用脚手架：

    {prog} run
    {prog} plugin list
    {prog} config list
'''


def _prompt(text, default=""):
    import sys
    if not sys.stdin.isatty():
        # 非交互环境（管道 / CI）：不要阻塞在 input()，直接回退默认值
        return default
    try:
        val = input(text).strip()
    except (EOFError, KeyboardInterrupt):
        val = ""
    return val or default


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _slug(name):
    return name.strip().replace(" ", "_").lower()


def do_create(args, working_dir):
    """创建 project / plugin / adapter。"""
    if args.action == "plugin":
        return _create_plugin(args, working_dir)
    if args.action == "adapter":
        return _create_adapter(args, working_dir)
    if args.action in (None, "project"):
        return _create_project(args, working_dir)
    print("未知 create 子命令: %s" % args.action)
    return 2


def _create_project(args, working_dir):
    name = args.name or _prompt("项目名称: ", "awesome-bot")
    if not name:
        print("错误：项目名不能为空")
        return 2
    slug = _slug(name)
    base = os.path.abspath(args.output_dir or working_dir)
    proj = os.path.join(base, slug)
    if os.path.exists(proj):
        print("错误：目录已存在: %s" % proj)
        return 1
    os.makedirs(proj, exist_ok=True)

    _write(os.path.join(proj, "config.yaml"),
           PROJECT_CONFIG.format(prog=PROG))
    _write(os.path.join(proj, "requirements.txt"), PROJECT_REQUIREMENTS)
    _write(os.path.join(proj, "bot.py"), BOT_LAUNCHER.format(prog=PROG))
    _write(os.path.join(proj, "README.md"), README_TPL.format(name=name, prog=PROG))

    plug_dir = os.path.join(proj, "plugins", "hello_world")
    os.makedirs(plug_dir, exist_ok=True)
    _write(os.path.join(plug_dir, "main.py"),
           PLUGIN_TEMPLATE.format(id="hello_world", name="Hello World",
                                   desc="示例插件：/hello 命令 + 消息计数"))

    print("已创建项目: %s" % proj)
    print("  - config.yaml")
    print("  - plugins/hello_world/main.py")
    print("  - bot.py / requirements.txt / README.md")
    print("")
    print("下一步：")
    print("  cd %s" % slug)
    print("  pip install -r requirements.txt")
    print("  %s run" % PROG)
    return 0


def _create_plugin(args, working_dir):
    name = _slug(args.name or "")
    if not name:
        print("错误：插件名不能为空（全小写下划线）")
        return 2
    if not re_fullmatch(name):
        print("错误：插件 id 只能含 [a-z0-9_]: %s" % name)
        return 2
    target = os.path.join(_state_paths(working_dir)[1], name, "main.py")
    if os.path.exists(target):
        print("错误：插件已存在: %s" % target)
        return 1
    pretty = name.replace("_", " ").title()
    _write(target, PLUGIN_TEMPLATE.format(
        id=name, name=pretty, desc="%s 插件" % pretty))
    print("已创建插件: %s" % target)
    print("  register(ctx) 已注入全局 _ctx；@command / @on_message 可直接用")
    print("  启动后访问: %s plugin list" % PROG)
    return 0


def _create_adapter(args, working_dir):
    name = _slug(args.name or "")
    if not name:
        print("错误：适配器名不能为空（全小写下划线）")
        return 2
    if not re_fullmatch(name):
        print("错误：适配器 id 只能含 [a-z0-9_]: %s" % name)
        return 2
    target = os.path.join(working_dir, "core_plugins", name, "main.py")
    if os.path.exists(target):
        print("错误：适配器已存在: %s" % target)
        return 1
    pretty = name.replace("_", " ").title()
    _write(target, ADAPTER_TEMPLATE.format(
        id=name, name=pretty, desc="%s 适配器" % pretty))
    # 默认在 core_plugins.yaml 里登记为禁用；清单路径与运行时同一套解析
    from ..utils import set_core_plugin_enabled
    try:
        from framework.paths import core_plugins_yaml_path
        yaml_path = core_plugins_yaml_path()
    except Exception:
        yaml_path = os.path.join(working_dir, "core_plugins.yaml")
    set_core_plugin_enabled(yaml_path, name, False)
    print("已创建适配器: %s（默认 disabled，改 core_plugins.yaml 启用）" % target)
    return 0


def _state_paths(working_dir):
    """配置与用户插件目录：与内核运行时用同一套解析（framework.paths）。

    原实现把两者都写死成 ``working_dir/config.yaml`` 与 ``working_dir/plugins``。
    在 pip 安装态，运行时读的是 ``<home>/data``（home 默认 ``~/.zcbot``），
    于是 ``zb init`` 在 cwd 造出一份运行时根本不读的**旧布局**配置，下次启动
    被内核迁移识别成「新旧两处并存」，用户莫名其妙被要求人工确认信哪份。
    """
    cfg = os.path.join(working_dir, "config.yaml")
    plugins = os.path.join(working_dir, "plugins")
    try:
        from framework import paths
        cfg = paths.resolve_config_path()
        plugins = paths.user_plugins_dir()
    except Exception:
        pass
    return cfg, plugins


def do_init(args, working_dir):
    """在状态目录初始化 ZCBOT 项目（config.yaml + 用户插件目录）。"""
    name = args.name or os.path.basename(working_dir)
    cfg, plug_root = _state_paths(working_dir)
    if not os.path.isfile(cfg):
        _write(cfg, PROJECT_CONFIG.format(prog=PROG))
        print("已生成 config.yaml: %s" % cfg)
    else:
        print("config.yaml 已存在，跳过: %s" % cfg)
    os.makedirs(plug_root, exist_ok=True)
    print("已就绪：%s" % os.path.dirname(cfg))
    print("  运行: %s run" % PROG)
    return 0


def re_fullmatch(name):
    import re
    return re.fullmatch(r"[a-z][a-z0-9_]*", name) is not None
