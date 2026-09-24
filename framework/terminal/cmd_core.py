# -*- coding: utf-8 -*-
"""
终端命令：核心信息（help / status / plugins / log / clear / exit）
"""
import asyncio
import os

from .command import terminal_commands


def register(fw):
    """注册本组终端命令"""
    def cmd_help(args):
        """显示帮助"""
        print(terminal_commands.help_text())

    def cmd_status(args):
        """查看框架状态"""
        try:
            import psutil
            proc = psutil.Process()
            mem = proc.memory_info().rss / 1024 / 1024
            uptime = fw._format_uptime() if hasattr(fw, '_format_uptime') else "N/A"

            bots = []
            try:
                adapter = fw.services.get('protocol_adapter')
                if adapter is None:
                    adapter = getattr(fw, 'protocol_adapter', None)
                if adapter is not None and hasattr(adapter, 'get_connected_bots'):
                    bots = adapter.get_connected_bots()
                else:
                    ws_server = fw.services.get('ws_server')
                    if ws_server and hasattr(ws_server, 'get_connected_bots'):
                        bots = ws_server.get_connected_bots()
            except Exception:
                pass

            # 统计信息
            try:
                user_count = fw.db.query_one("SELECT COUNT(*) as cnt FROM users")['cnt']
                group_count = fw.db.query_one("SELECT COUNT(*) as cnt FROM groups_info WHERE is_active=1")['cnt']
                cmd_count = fw.db.query_one("SELECT COUNT(*) as cnt FROM commands")['cnt']
            except Exception:
                user_count = group_count = cmd_count = 0

            print("=" * 50)
            print("ZCBOT 框架状态")
            print("=" * 50)
            print(f"  版本: {open('VERSION').read().strip() if os.path.exists('VERSION') else '未知'}")
            print(f"  运行时间: {uptime}")
            print(f"  进程内存: {mem:.1f} MB")
            print(f"  已连接客户端: {len(bots)} 个")
            if bots:
                for b in bots:
                    print(f"    - {b}")
            print(f"  已加载插件: {len(fw.plugin_loader.get_loaded_plugins())} 个")
            print(f"  注册命令: {cmd_count} 条")
            print(f"  用户数: {user_count}")
            print(f"  群数: {group_count}")
            print("=" * 50)
        except Exception as e:
            print(f"获取状态失败: {e}")

    def cmd_plugins(args):
        """列出已加载插件"""
        plugins = fw.plugin_loader.get_loaded_plugins()
        print(f"已加载插件 ({len(plugins)} 个):")
        print("-" * 50)
        for name, info in plugins.items():
            meta = info.get('meta', {})
            version = meta.get('version', '?')
            desc = meta.get('desc', '')
            source = "官方" if name.startswith('core:') else "用户"
            print(f"  {name} v{version} [{source}]")
            if desc:
                print(f"    {desc}")
        print("-" * 50)

    def cmd_log(args):
        """查看日志: log [行数]"""
        try:
            lines = int(args.strip()) if args.strip() else 30
            log_file = fw.config.get('log', {}).get('file', 'data/logs/zcbot.log')
            if os.path.exists(log_file):
                with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                    all_lines = f.readlines()
                    recent = all_lines[-lines:]
                    print(f"最近 {len(recent)} 行日志:")
                    print("-" * 60)
                    for line in recent:
                        print(line.rstrip())
                    print("-" * 60)
            else:
                print("日志文件不存在")
        except Exception as e:
            print(f"读取日志失败: {e}")

    def cmd_clear(args):
        """清屏"""
        os.system('cls' if os.name == 'nt' else 'clear')

    def cmd_exit(args):
        """退出框架"""
        print("正在停止框架...")
        loop = fw.loop
        if loop and loop.is_running():
            async def _stop():
                await fw.stop()
                os._exit(0)
            asyncio.run_coroutine_threadsafe(_stop(), loop)
        else:
            asyncio.run(fw.stop())
            os._exit(0)



    # ---- 注册 ----
    terminal_commands.register("help", cmd_help, "显示帮助", ["h", "?"])
    terminal_commands.register("status", cmd_status, "查看框架状态", ["st"], target="both")
    terminal_commands.register("plugins", cmd_plugins, "列出已加载插件", ["pl"], target="both")
    terminal_commands.register("log", cmd_log, "查看日志: log [行数]")
    terminal_commands.register("clear", cmd_clear, "清屏", ["cls"])
    terminal_commands.register("exit", cmd_exit, "退出框架", ["quit", "q"])

