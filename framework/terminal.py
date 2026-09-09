"""
终端交互模块
支持从控制台输入命令直接操作框架，如 status、plugins、reload 等
"""
import asyncio
import logging
import sys
import threading

logger = logging.getLogger('zcbot')


class TerminalCommand:
    """终端命令注册表"""
    
    def __init__(self):
        self._commands = {}  # name -> handler
        self._aliases = {}   # alias -> name
        self._descriptions = {}  # name -> description
    
    def register(self, name: str, handler, description: str = "", aliases: list = None):
        """注册终端命令"""
        self._commands[name] = handler
        self._descriptions[name] = description
        if aliases:
            for alias in aliases:
                self._aliases[alias] = name
    
    def get(self, name: str):
        """获取命令处理器"""
        # 先查直接命令名
        if name in self._commands:
            return self._commands[name]
        # 再查别名
        real_name = self._aliases.get(name)
        if real_name and real_name in self._commands:
            return self._commands[real_name]
        return None
    
    def list_commands(self) -> dict:
        """列出所有命令"""
        result = {}
        for name, handler in self._commands.items():
            result[name] = self._descriptions.get(name, "")
        return result
    
    def help_text(self) -> str:
        """生成帮助文本"""
        lines = ["可用终端命令:"]
        lines.append("-" * 50)
        for name, handler in sorted(self._commands.items()):
            alias_str = ""
            for alias, real_name in self._aliases.items():
                if real_name == name:
                    alias_str = f" ({alias})"
                    break
            desc = self._descriptions.get(name, "")
            if not desc and hasattr(handler, '__doc__'):
                desc = handler.__doc__.strip().split('\n')[0] if handler.__doc__ else ""
            lines.append(f"  {name}{alias_str}: {desc}")
        lines.append("-" * 50)
        lines.append("用法: 命令名 参数，如: send 123456 你好")
        return "\n".join(lines)


# 全局终端命令注册表
terminal_commands = TerminalCommand()


class TerminalInput:
    """终端输入监听器"""
    
    def __init__(self, framework):
        self.framework = framework
        self._running = False
        self._thread = None
    
    def start(self):
        """启动终端监听"""
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True, name="terminal-input")
        self._thread.start()
        logger.info("终端交互已启动，输入 help 查看可用命令")
    
    def stop(self):
        """停止终端监听"""
        self._running = False
    
    def _read_loop(self):
        """读取终端输入（在单独线程中运行）"""
        while self._running:
            try:
                line = input()
                if not line.strip():
                    continue
                # 在事件循环中执行命令
                if self.framework.loop and self.framework.loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._execute_command(line.strip()),
                        self.framework.loop
                    )
            except EOFError:
                break
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"终端输入读取异常: {e}")
    
    async def _execute_command(self, line: str):
        """执行终端命令"""
        parts = line.split(maxsplit=1)
        cmd_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        
        handler = terminal_commands.get(cmd_name)
        if handler:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(args)
                else:
                    await asyncio.to_thread(handler, args)
            except Exception as e:
                logger.error(f"终端命令 [{cmd_name}] 执行失败: {e}")
        else:
            logger.warning(f"未知命令: {cmd_name}，输入 help 查看可用命令")


def register_builtins(fw):
    """注册内置终端命令"""
    
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
                ws_server = fw.services.get('ws_server')
                if ws_server and hasattr(ws_server, 'get_connected_bots'):
                    bots = ws_server.get_connected_bots()
            except Exception:
                pass
            
            print("=" * 50)
            print("ZCBOT 框架状态")
            print("=" * 50)
            print(f"  进程内存: {mem:.1f} MB")
            print(f"  已连接客户端: {len(bots)} 个")
            if bots:
                for b in bots:
                    print(f"    - {b}")
            print(f"  已加载插件: {len(fw.plugin_loader.get_loaded_plugins())} 个")
            print("=" * 50)
        except Exception as e:
            print(f"获取状态失败: {e}")
    
    def cmd_plugins(args):
        """列出已加载插件"""
        plugins = fw.plugin_loader.get_loaded_plugins()
        print(f"已加载插件 ({len(plugins)} 个):")
        print("-" * 40)
        for name, info in plugins.items():
            meta = info.get('meta', {})
            version = meta.get('version', '?')
            desc = meta.get('desc', '')
            print(f"  {name} v{version}")
            if desc:
                print(f"    {desc}")
        print("-" * 40)
    
    def cmd_send(args):
        """发送消息: send <user_id> <消息> 或 send g:<group_id> <消息>"""
        try:
            parts = args.split(maxsplit=1)
            if len(parts) < 2:
                print("用法: send <user_id> <消息> 或 send g:<group_id> <消息>")
                print("示例: send 123456 你好")
                print("      send g:654321 大家好")
                return
            
            target = parts[0]
            message = parts[1]
            
            user_id = None
            group_id = None
            
            if target.startswith('g:'):
                group_id = int(target[2:])
            elif target.startswith('p:'):
                user_id = int(target[2:])
            else:
                user_id = int(target)
            
            # 通过 api_caller 发送
            api_caller = fw.services.get('api_caller')
            if api_caller is None:
                print("错误: OneBot 适配器未加载")
                return
            
            import asyncio
            async def _send():
                if group_id:
                    await api_caller.send_group_msg(group_id=group_id, message=message)
                    print(f"已发送到群 {group_id}: {message}")
                else:
                    await api_caller.send_private_msg(user_id=user_id, message=message)
                    print(f"已发送给用户 {user_id}: {message}")
            
            asyncio.ensure_future(_send())
            
        except ValueError:
            print("错误: user_id/group_id 必须是数字")
        except Exception as e:
            print(f"发送失败: {e}")
    
    def cmd_recv(args):
        """模拟接收消息: recv <user_id> <消息内容>"""
        try:
            parts = args.split(maxsplit=1)
            if len(parts) < 2:
                print("用法: recv <user_id> <消息内容>")
                print("示例: recv 123456 /help")
                return
            
            user_id = int(parts[0])
            message = parts[1]
            
            # 构造模拟事件
            mock_event = {
                'post_type': 'message',
                'message_type': 'private',
                'sub_type': 'friend',
                'user_id': user_id,
                'message': message,
                'raw_message': message,
                'message_id': 123456789,
                'message_id_str': '123456789',
                'sender': {
                    'user_id': user_id,
                    'nickname': f'终端用户{user_id}',
                    'card': '',
                    'role': 'member',
                },
                'bot_name': 'terminal',
            }
            
            import asyncio
            asyncio.ensure_future(fw.dispatch_event(mock_event))
            print(f"已模拟接收消息: user={user_id}, msg={message}")
            
        except ValueError:
            print("错误: user_id 必须是数字")
        except Exception as e:
            print(f"模拟失败: {e}")
    
    def cmd_reload(args):
        """重载插件: reload [插件名]"""
        try:
            if args.strip():
                plugin_name = args.strip()
                success = fw.plugin_loader.reload_plugin(plugin_name)
                if success:
                    print(f"插件 [{plugin_name}] 重载成功")
                else:
                    print(f"插件 [{plugin_name}] 重载失败")
            else:
                loaded = fw.plugin_loader.reload_all()
                print(f"已重载 {len(loaded)} 个插件")
        except Exception as e:
            print(f"重载失败: {e}")
    
    def cmd_users(args):
        """查看用户列表"""
        try:
            rows = fw.db.query("SELECT user_id, nickname, last_active_at FROM users ORDER BY last_active_at DESC LIMIT 20")
            print(f"最近活跃用户 (前20):")
            print("-" * 50)
            for row in rows:
                uid = row['user_id']
                nick = row['nickname'] or str(uid)
                last = row['last_active_at']
                print(f"  {uid:<12} {nick:<15} {last}")
            print("-" * 50)
        except Exception as e:
            print(f"查询失败: {e}")
    
    def cmd_groups(args):
        """查看群列表"""
        try:
            rows = fw.db.query("SELECT group_id, group_name, is_active FROM groups_info WHERE is_active=1 ORDER BY group_id")
            print(f"活跃群列表:")
            print("-" * 50)
            for row in rows:
                gid = row['group_id']
                name = row['group_name'] or str(gid)
                print(f"  {gid:<15} {name}")
            print("-" * 50)
        except Exception as e:
            print(f"查询失败: {e}")
    
    def cmd_exit(args):
        """退出框架"""
        print("正在停止框架...")
        import asyncio
        asyncio.ensure_future(fw.stop())
    
    # 注册内置命令
    terminal_commands.register("help", cmd_help, "显示帮助", ["h", "?"])
    terminal_commands.register("status", cmd_status, "查看框架状态", ["st"])
    terminal_commands.register("plugins", cmd_plugins, "列出已加载插件", ["pl"])
    terminal_commands.register("send", cmd_send, "发送消息: send <user_id/group_id> <消息>")
    terminal_commands.register("recv", cmd_recv, "模拟接收消息: recv <user_id> <消息>")
    terminal_commands.register("reload", cmd_reload, "重载插件: reload [插件名]")
    terminal_commands.register("users", cmd_users, "查看用户列表")
    terminal_commands.register("groups", cmd_groups, "查看群列表")
    terminal_commands.register("exit", cmd_exit, "退出框架", ["quit", "q"])
