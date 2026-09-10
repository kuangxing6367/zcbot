"""
终端交互模块
支持从控制台输入命令直接操作框架，如 status、plugins、reload 等
"""
import asyncio
import logging
import os
import sys
import threading

logger = logging.getLogger('zcbot')


def _installed_core_plugins() -> set:
    """扫描 core_plugins/ 目录得到已安装官方插件名（替代硬编码名单）"""
    plugins_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'core_plugins')
    try:
        return {
            n for n in os.listdir(plugins_dir)
            if not n.startswith('_') and os.path.isfile(os.path.join(plugins_dir, n, 'main.py'))
        }
    except Exception:
        return set()


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
                    ).result(timeout=30)
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
            print(f"  版本: {open('VERSION').read().strip() if __import__('os').path.exists('VERSION') else '未知'}")
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
    
    def cmd_enable(args):
        """启用插件: enable <插件名>"""
        plugin_name = args.strip()
        if not plugin_name:
            print("用法: enable <插件名>")
            print("示例: enable onebot_adapter")
            return
        
        # 检查是否是核心插件
        core_plugins = _installed_core_plugins()
        if plugin_name in core_plugins:
            # 更新配置
            import yaml
            config_path = fw.config_path
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f) or {}
                if 'core_plugins' not in config:
                    config['core_plugins'] = {}
                config['core_plugins'][plugin_name] = True
                with open(config_path, 'w', encoding='utf-8') as f:
                    yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
                print(f"已启用核心插件 [{plugin_name}]，重启后生效")
            except Exception as e:
                print(f"启用失败: {e}")
        else:
            # 用户插件
            try:
                fw.plugin_loader.enable_plugin(plugin_name)
                print(f"已启用插件 [{plugin_name}]")
            except Exception as e:
                print(f"启用失败: {e}")
    
    def cmd_disable(args):
        """禁用插件: disable <插件名>"""
        plugin_name = args.strip()
        if not plugin_name:
            print("用法: disable <插件名>")
            print("示例: disable onebot_adapter")
            return
        
        # 检查是否是核心插件
        core_plugins = _installed_core_plugins()
        if plugin_name in core_plugins:
            # 更新配置
            import yaml
            config_path = fw.config_path
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f) or {}
                if 'core_plugins' not in config:
                    config['core_plugins'] = {}
                config['core_plugins'][plugin_name] = False
                with open(config_path, 'w', encoding='utf-8') as f:
                    yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
                print(f"已禁用核心插件 [{plugin_name}]，重启后生效")
            except Exception as e:
                print(f"禁用失败: {e}")
        else:
            # 用户插件
            try:
                fw.plugin_loader.disable_plugin(plugin_name)
                print(f"已禁用插件 [{plugin_name}]")
            except Exception as e:
                print(f"禁用失败: {e}")
    
    def cmd_config(args):
        """查看/修改配置: config [key] [value]"""
        parts = args.split(maxsplit=1)
        if not parts:
            # 显示所有配置
            print("当前配置:")
            print("-" * 50)
            import yaml
            with open(fw.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            for section, values in config.items():
                if isinstance(values, dict):
                    print(f"  {section}:")
                    for k, v in values.items():
                        print(f"    {k}: {v}")
                else:
                    print(f"  {section}: {values}")
            print("-" * 50)
            return
        
        key = parts[0]
        if len(parts) == 1:
            # 查看单个配置
            import yaml
            with open(fw.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            # 支持点号分隔的路径
            keys = key.split('.')
            value = config
            for k in keys:
                if isinstance(value, dict):
                    value = value.get(k)
                else:
                    value = None
                    break
            if value is not None:
                print(f"{key} = {value}")
            else:
                print(f"配置项 {key} 不存在")
        else:
            # 修改配置
            value = parts[1]
            import yaml
            config_path = fw.config_path
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f) or {}
                # 支持点号分隔的路径
                keys = key.split('.')
                target = config
                for k in keys[:-1]:
                    if k not in target:
                        target[k] = {}
                    target = target[k]
                # 尝试转换类型
                if value.lower() == 'true':
                    value = True
                elif value.lower() == 'false':
                    value = False
                else:
                    try:
                        value = int(value)
                    except ValueError:
                        try:
                            value = float(value)
                        except ValueError:
                            pass
                target[keys[-1]] = value
                with open(config_path, 'w', encoding='utf-8') as f:
                    yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
                print(f"已设置 {key} = {value}")
            except Exception as e:
                print(f"设置失败: {e}")
    
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
            
            # 通过当前接入端的中立 send_text 发送（协议翻译在适配器内）
            adapter = fw.services.get('protocol_adapter')
            api_caller = fw.services.get('api_caller')
            if adapter is None and api_caller is None:
                print("错误: 未加载任何协议接入端")
                return

            async def _send():
                if adapter is not None:
                    await adapter.send_text(message, group_id=group_id, user_id=user_id)
                else:
                    act = 'send_group_msg' if group_id else 'send_private_msg'
                    await api_caller.acall(act, group_id=group_id, user_id=user_id, message=message)
                if group_id:
                    print(f"已发送到群 {group_id}: {message}")
                else:
                    print(f"已发送给用户 {user_id}: {message}")
            
            asyncio.ensure_future(_send())
            
        except ValueError:
            print("错误: user_id/group_id 必须是数字")
        except Exception as e:
            print(f"发送失败: {e}")
    
    def cmd_recv(args):
        """模拟接收消息: recv <user_id> <消息内容> 或 recv g:<group_id> <user_id> <消息>"""
        try:
            parts = args.split()
            if len(parts) < 2:
                print("用法: recv <user_id> <消息内容>")
                print("      recv g:<group_id> <user_id> <消息>")
                print("示例: recv 123456 /help")
                print("      recv g:654321 123456 大家好")
                return
            
            if parts[0].startswith('g:'):
                # 群消息
                group_id = int(parts[0][2:])
                user_id = int(parts[1])
                message = ' '.join(parts[2:])
                message_type = 'group'
            else:
                # 私聊消息
                group_id = None
                user_id = int(parts[0])
                message = ' '.join(parts[1:])
                message_type = 'private'
            
            # 构造模拟事件
            mock_event = {
                'post_type': 'message',
                'message_type': message_type,
                'sub_type': 'friend' if message_type == 'private' else 'normal',
                'user_id': user_id,
                'group_id': group_id,
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
            
            asyncio.ensure_future(fw.dispatch_event(mock_event))
            print(f"已模拟接收消息: {message_type} user={user_id}, msg={message}")
            
        except ValueError:
            print("错误: user_id/group_id 必须是数字")
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
        """查看用户列表: users [数量]"""
        try:
            limit = int(args.strip()) if args.strip() else 20
            rows = fw.db.query(f"SELECT user_id, nickname, last_active_at FROM users ORDER BY last_active_at DESC LIMIT {limit}")
            print(f"最近活跃用户 (前{limit}):")
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
    
    def cmd_ban(args):
        """禁言/封禁: ban <user_id> [分钟] 或 ban g:<group_id> <user_id> [分钟]"""
        try:
            parts = args.split()
            if not parts:
                print("用法: ban <user_id> [分钟]")
                print("      ban g:<group_id> <user_id> [分钟]")
                print("示例: ban 123456 60 (禁言1小时)")
                print("      ban g:654321 123456 10 (群内禁言10分钟)")
                return
            
            if parts[0].startswith('g:'):
                group_id = int(parts[0][2:])
                user_id = int(parts[1])
                duration = int(parts[2]) * 60 if len(parts) > 2 else 600  # 默认10分钟
            else:
                group_id = None
                user_id = int(parts[0])
                duration = int(parts[1]) * 60 if len(parts) > 1 else 600
            
            api_caller = fw.services.get('api_caller')
            if api_caller is None:
                print("错误: 未加载任何协议接入端")
                return
            
            async def _ban():
                if group_id:
                    await api_caller.acall('set_group_ban', group_id=group_id, user_id=user_id, duration=duration)
                    print(f"已禁言用户 {user_id} {duration//60} 分钟")
                else:
                    # 私聊封禁（标记到数据库）
                    fw.db.execute("UPDATE users SET is_banned=1 WHERE user_id=%s", (user_id,))
                    print(f"已封禁用户 {user_id}")
            
            asyncio.ensure_future(_ban())
            
        except ValueError:
            print("错误: 参数格式错误")
        except Exception as e:
            print(f"操作失败: {e}")
    
    def cmd_unban(args):
        """解封/解禁: unban <user_id> 或 unban g:<group_id> <user_id>"""
        try:
            parts = args.split()
            if not parts:
                print("用法: unban <user_id>")
                print("      unban g:<group_id> <user_id>")
                return
            
            if parts[0].startswith('g:'):
                group_id = int(parts[0][2:])
                user_id = int(parts[1])
            else:
                group_id = None
                user_id = int(parts[0])
            
            api_caller = fw.services.get('api_caller')
            if api_caller is None:
                print("错误: 未加载任何协议接入端")
                return
            
            async def _unban():
                if group_id:
                    await api_caller.acall('set_group_ban', group_id=group_id, user_id=user_id, duration=0)
                    print(f"已解除用户 {user_id} 的禁言")
                else:
                    fw.db.execute("UPDATE users SET is_banned=0 WHERE user_id=%s", (user_id,))
                    print(f"已解封用户 {user_id}")
            
            asyncio.ensure_future(_unban())
            
        except ValueError:
            print("错误: 参数格式错误")
        except Exception as e:
            print(f"操作失败: {e}")
    
    def cmd_kick(args):
        """踢出群成员: kick <group_id> <user_id>"""
        try:
            parts = args.split()
            if len(parts) < 2:
                print("用法: kick <group_id> <user_id>")
                print("示例: kick 654321 123456")
                return
            
            group_id = int(parts[0])
            user_id = int(parts[1])
            
            api_caller = fw.services.get('api_caller')
            if api_caller is None:
                print("错误: 未加载任何协议接入端")
                return
            
            async def _kick():
                await api_caller.acall('set_group_kick', group_id=group_id, user_id=user_id)
                print(f"已踢出用户 {user_id}")
            
            asyncio.ensure_future(_kick())
            
        except ValueError:
            print("错误: group_id/user_id 必须是数字")
        except Exception as e:
            print(f"操作失败: {e}")
    
    def cmd_broadcast(args):
        """广播消息: broadcast <消息>"""
        if not args.strip():
            print("用法: broadcast <消息>")
            print("示例: broadcast 系统维护通知")
            return
        
        message = args.strip()
        adapter = fw.services.get('protocol_adapter')
        api_caller = fw.services.get('api_caller')
        if adapter is None and api_caller is None:
            print("错误: 未加载任何协议接入端")
            return
        
        try:
            rows = fw.db.query("SELECT group_id FROM groups_info WHERE is_active=1")
            group_ids = [row['group_id'] for row in rows]
            
            async def _broadcast():
                success = 0
                for gid in group_ids:
                    try:
                        if adapter is not None:
                            await adapter.send_text(message, group_id=gid)
                        else:
                            await api_caller.acall('send_group_msg', group_id=gid, message=message)
                        success += 1
                    except Exception:
                        pass
                print(f"广播完成: 成功 {success}/{len(group_ids)} 个群")
            
            asyncio.ensure_future(_broadcast())
            
        except Exception as e:
            print(f"广播失败: {e}")
    
    def cmd_tasks(args):
        """查看定时任务"""
        try:
            scheduler = fw.services.get('scheduler')
            if scheduler is None:
                print("错误: 调度器未加载")
                return
            
            jobs = scheduler.get_jobs()
            print(f"定时任务 ({len(jobs)} 个):")
            print("-" * 60)
            for job in jobs:
                print(f"  {job.id}")
                print(f"    下次运行: {job.next_run_time}")
                print(f"    触发器: {job.trigger}")
            print("-" * 60)
        except Exception as e:
            print(f"查询失败: {e}")
    
    def cmd_log(args):
        """查看日志: log [行数]"""
        try:
            lines = int(args.strip()) if args.strip() else 30
            log_file = fw.config.get('log', {}).get('file', 'data/logs/zcbot.log')
            if __import__('os').path.exists(log_file):
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
        import os
        os.system('cls' if os.name == 'nt' else 'clear')
    
    def cmd_exit(args):
        """退出框架"""
        print("正在停止框架...")
        import os
        loop = fw.loop
        if loop and loop.is_running():
            async def _stop():
                await fw.stop()
                os._exit(0)
            asyncio.run_coroutine_threadsafe(_stop(), loop)
        else:
            asyncio.run(fw.stop())
            os._exit(0)

    def cmd_update(args):
        """更新框架: update [版本号]"""
        import requests, zipfile, tempfile, shutil, os
        repo = 'kuangxing6367/zcbot'
        branch = 'main'
        target = args.strip() or ''

        # 读取本地版本
        try:
            local_ver = open('VERSION', 'r', encoding='utf-8').read().strip()
        except Exception:
            local_ver = '0.0.0'

        # 确定目标版本
        if target:
            tag = f'v{target}' if not target.startswith('v') else target
            zip_url = f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"
            print(f"正在下载框架更新（{tag}）...")
        else:
            # 取最新 Release
            try:
                r = requests.get(f"https://api.github.com/repos/{repo}/releases?per_page=5", timeout=15)
                releases = r.json() if r.status_code == 200 else []
                best = None
                for rel in releases:
                    t = rel.get('tag_name', '')
                    if best is None or t > best:
                        best = t
                if best:
                    tag = best
                    zip_url = f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"
                    print(f"正在下载框架更新（最新 Release {tag}）...")
                else:
                    tag = ''
                    zip_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
                    print("仓库无 Release，下载 main 分支最新代码...")
            except Exception:
                tag = ''
                zip_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
                print("获取版本信息失败，下载 main 分支最新代码...")

        # 下载
        try:
            resp = requests.get(zip_url, timeout=60)
            if resp.status_code != 200:
                print(f"下载失败: HTTP {resp.status_code}")
                return
        except Exception as e:
            print(f"下载失败: {e}")
            return

        # 解压并覆盖
        tmp_zip = tempfile.mktemp(suffix='.zip')
        try:
            with open(tmp_zip, 'wb') as f:
                f.write(resp.content)
            tmp_dir = tempfile.mkdtemp(prefix='zcbot_upd_')
            try:
                with zipfile.ZipFile(tmp_zip, 'r') as zf:
                    zf.extractall(tmp_dir)
                entries = [e for e in os.listdir(tmp_dir) if os.path.isdir(os.path.join(tmp_dir, e))]
                src_root = os.path.join(tmp_dir, entries[0]) if entries[0] else tmp_dir

                root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                include = {'framework', 'core_plugins', 'web', 'webui', 'sql', 'main.py', 'requirements.txt', 'VERSION', 'CHANGELOG.md', 'README.md', 'start.sh'}
                updated = []
                for name in os.listdir(src_root):
                    if name not in include:
                        continue
                    src = os.path.join(src_root, name)
                    dst = os.path.join(root, name)
                    if os.path.isdir(src):
                        if os.path.isdir(dst):
                            shutil.rmtree(dst, ignore_errors=True)
                        shutil.copytree(src, dst)
                    elif os.path.isfile(src):
                        os.makedirs(os.path.dirname(dst), exist_ok=True) if os.path.dirname(dst) else None
                        shutil.copy2(src, dst)
                    updated.append(name)

                print(f"更新完成！共更新 {len(updated)} 项: {', '.join(updated)}")
                print("请重启框架生效: python main.py")
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            print(f"更新失败: {e}")
        finally:
            try:
                os.unlink(tmp_zip)
            except Exception:
                pass

    # 注册内置命令
    terminal_commands.register("help", cmd_help, "显示帮助", ["h", "?"])
    terminal_commands.register("status", cmd_status, "查看框架状态", ["st"])
    terminal_commands.register("plugins", cmd_plugins, "列出已加载插件", ["pl"])
    terminal_commands.register("enable", cmd_enable, "启用插件: enable <插件名>")
    terminal_commands.register("disable", cmd_disable, "禁用插件: disable <插件名>")
    terminal_commands.register("config", cmd_config, "查看/修改配置: config [key] [value]")
    terminal_commands.register("send", cmd_send, "发送消息: send <user_id> <消息>")
    terminal_commands.register("recv", cmd_recv, "模拟接收消息: recv <user_id> <消息>")
    terminal_commands.register("reload", cmd_reload, "重载插件: reload [插件名]")
    terminal_commands.register("users", cmd_users, "查看用户列表")
    terminal_commands.register("groups", cmd_groups, "查看群列表")
    terminal_commands.register("ban", cmd_ban, "禁言/封禁: ban <user_id> [分钟]")
    terminal_commands.register("unban", cmd_unban, "解封/解禁: unban <user_id>")
    terminal_commands.register("kick", cmd_kick, "踢出群成员: kick <group_id> <user_id>")
    terminal_commands.register("broadcast", cmd_broadcast, "广播消息: broadcast <消息>")
    terminal_commands.register("tasks", cmd_tasks, "查看定时任务")
    terminal_commands.register("log", cmd_log, "查看日志: log [行数]")
    terminal_commands.register("clear", cmd_clear, "清屏", ["cls"])
    terminal_commands.register("exit", cmd_exit, "退出框架", ["quit", "q"])
    terminal_commands.register("update", cmd_update, "更新框架: update [版本号]")
