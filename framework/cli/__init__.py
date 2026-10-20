# -*- coding: utf-8 -*-
"""ZCBOT CLI —— 脚手架命令行工具。

命令树为框架自带的命令行脚手架（``create / init / run / generate /
plugin / adapter / driver / self / clean / config``），作用于 ZCBOT 自身的
项目 / 插件 / 适配器 / 驱动器 / 实例概念。

用法::

    zb --help
    zb create project
    zb create plugin <name>
    zb init
    zb run [--reload]
    zb generate
    zb plugin list
    zb adapter list
    zb self list
    zb clean
    zb config list

全局选项：
    -w, --working-dir PATH   工作目录（默认当前目录）
    -p, --python-path PATH   Python 解释器路径
    --help, -h               显示帮助并退出
"""
import argparse
import os
import sys

from . import commands
from .utils import PROG, read_version

# 这些一级命令会被 main.py 的入口分发接管（python main.py <cmd> 等价 zb <cmd>）
CLI_GROUPS = ("create", "init", "run", "generate", "plugin",
              "adapter", "driver", "self", "clean", "config")


def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="ZCBOT 脚手架命令行工具",
    )
    parser.add_argument(
        "-w", "--working-dir", default=".",
        help="工作目录（默认当前目录）",
    )
    parser.add_argument(
        "-p", "--python-path", default=sys.executable,
        help="Python 解释器路径（默认自动探测）",
    )
    parser.add_argument(
        "-v", "--version", action="version",
        version="%(prog)s " + read_version(),
    )

    sub = parser.add_subparsers(dest="group", metavar="COMMAND")

    # ---- create ----
    p_create = sub.add_parser("create", help="创建项目 / 插件 / 适配器")
    cs = p_create.add_subparsers(dest="action")

    p_proj = cs.add_parser("project", help="创建 ZCBOT 项目")
    p_proj.add_argument("-o", "--output-dir", default=None,
                        help="项目输出目录（默认当前目录）")
    p_proj.add_argument("-n", "--name", default=None, help="项目名称")
    p_proj.add_argument("-t", "--template", default="bootstrap",
                        choices=["bootstrap", "simple"],
                        help="项目模板（bootstrap=含示例插件；simple=仅骨架）")
    p_proj.add_argument("pip_args", nargs="*",
                        help="传递给依赖安装的额外 pip 参数")

    p_plug = cs.add_parser("plugin", help="创建 ZCBOT 插件")
    p_plug.add_argument("name", help="插件 id（全小写下划线）")

    p_adapt = cs.add_parser("adapter", help="创建 ZCBOT 适配器")
    p_adapt.add_argument("name", help="适配器 id（全小写下划线）")

    # ---- init ----
    p_init = sub.add_parser("init", help="在当前目录初始化 ZCBOT 项目")
    p_init.add_argument("-n", "--name", default=None, help="项目名称")

    # ---- run ----
    p_run = sub.add_parser("run", help="运行机器人")
    p_run.add_argument("-f", "--file", default="main.py", help="入口文件路径")
    p_run.add_argument("-r", "--reload", action="store_true",
                       help="文件变更时热重载")
    p_run.add_argument("--reload-dirs", default=None,
                       help="额外监听目录（逗号分隔）")
    p_run.add_argument("--reload-delay", type=float, default=0.5,
                       help="重载延迟（秒）")

    # ---- generate ----
    p_gen = sub.add_parser("generate", help="生成启动脚本")
    p_gen.add_argument("-f", "--file", default="main.py", help="输出文件路径")

    # ---- plugin ----
    p_plugin = sub.add_parser("plugin", help="管理插件")
    ps = p_plugin.add_subparsers(dest="action")
    ps.add_parser("list", help="列出插件")
    ps.add_parser("search", help="在集市中搜索插件")
    ps.add_parser("store", help="打开插件集市")
    p_install = ps.add_parser("install", help="启用 / 安装插件")
    p_install.add_argument("id", help="插件 id")
    p_uninstall = ps.add_parser("uninstall", help="禁用 / 卸载插件")
    p_uninstall.add_argument("id", help="插件 id")

    # ---- adapter ----
    p_adapter = sub.add_parser("adapter", help="管理适配器")
    aps = p_adapter.add_subparsers(dest="action")
    aps.add_parser("list", help="列出适配器")
    aps.add_parser("search", help="在集市中搜索适配器")
    a_install = aps.add_parser("install", help="启用适配器")
    a_install.add_argument("id", help="适配器 id")
    a_uninstall = aps.add_parser("uninstall", help="禁用适配器")
    a_uninstall.add_argument("id", help="适配器 id")

    # ---- driver ----
    p_driver = sub.add_parser("driver", help="管理驱动器")
    dps = p_driver.add_subparsers(dest="action")
    dps.add_parser("list", help="列出驱动器")
    dps.add_parser("search", help="在集市中搜索驱动器")
    d_install = dps.add_parser("install", help="安装驱动器")
    d_install.add_argument("id", help="驱动器 id")
    d_uninstall = dps.add_parser("uninstall", help="卸载驱动器")
    d_uninstall.add_argument("id", help="驱动器 id")

    # ---- self ----
    p_self = sub.add_parser("self", help="管理 ZCBOT 实例")
    sps = p_self.add_subparsers(dest="action")
    sps.add_parser("list", help="列出已安装的 ZCBOT")
    s_install = sps.add_parser("install", help="安装 ZCBOT")
    s_install.add_argument("spec", nargs="?", default="zcbot", help="包规格")
    s_uninstall = sps.add_parser("uninstall", help="卸载 ZCBOT")
    sps.add_parser("upgrade", help="升级 ZCBOT")

    # ---- clean ----
    p_clean = sub.add_parser("clean", help="清理项目")
    p_clean.add_argument("--py", action=argparse.BooleanOptionalAction, default=True,
                         help="清理 Python 缓存文件")
    p_clean.add_argument("--npm", action=argparse.BooleanOptionalAction, default=False,
                         help="清理 npm 缓存文件")

    # ---- config ----
    p_config = sub.add_parser("config", help="查看 / 修改配置")
    cfgs = p_config.add_subparsers(dest="action")
    cfgs.add_parser("list", help="显示配置摘要")
    cfg_get = cfgs.add_parser("get", help="查看某一项")
    cfg_get.add_argument("key", help="配置键")
    cfg_set = cfgs.add_parser("set", help="修改某一项")
    cfg_set.add_argument("key", help="配置键")
    cfg_set.add_argument("value", nargs="+", help="配置值")

    return parser


def main(argv=None):
    """CLI 入口；返回进程退出码。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.group:
        parser.print_help()
        return 0
    working_dir = os.path.abspath(args.working_dir)
    handler = getattr(commands, "do_" + args.group, None)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args, working_dir) or 0
