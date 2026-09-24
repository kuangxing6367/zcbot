# -*- coding: utf-8 -*-
"""
终端命令：消息收发与管理（send / recv / ban / unban / kick / broadcast）
"""
import asyncio

from .command import terminal_commands


def register(fw):
    """注册本组终端命令"""
    async def cmd_send(args):
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
            # 本函数已为 async，由事件循环直接执行，无需再套 ensure_future
            adapter = fw.services.get('protocol_adapter')
            api_caller = fw.services.get('api_caller')
            if adapter is None and api_caller is None:
                print("错误: 未加载任何协议接入端")
                return

            if adapter is not None:
                await adapter.send_text(message, group_id=group_id, user_id=user_id)
            else:
                # 无中立 send_text 的旧接入端：经 api_caller 走 send_msg 通用动作
                tgt = {'group_id': group_id} if group_id else {'user_id': user_id}
                await api_caller.acall('send_msg', **tgt, message=message)
            if group_id:
                print(f"已发送到群 {group_id}: {message}")
            else:
                print(f"已发送给用户 {user_id}: {message}")

        except ValueError:
            print("错误: user_id/group_id 必须是数字")
        except Exception as e:
            print(f"发送失败: {e}")

    async def cmd_recv(args):
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

            await fw.dispatch_event(mock_event)
            print(f"已模拟接收消息: {message_type} user={user_id}, msg={message}")

        except ValueError:
            print("错误: user_id/group_id 必须是数字")
        except Exception as e:
            print(f"模拟失败: {e}")

    async def cmd_ban(args):
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

            if group_id:
                await api_caller.acall('set_group_ban', group_id=group_id, user_id=user_id, duration=duration)
                print(f"已禁言用户 {user_id} {duration//60} 分钟")
            else:
                # 私聊封禁（标记到数据库）
                await asyncio.to_thread(fw.db.execute, "UPDATE users SET is_banned=1 WHERE user_id=%s", (user_id,))
                print(f"已封禁用户 {user_id}")

        except ValueError:
            print("错误: 参数格式错误")
        except Exception as e:
            print(f"操作失败: {e}")

    async def cmd_unban(args):
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

            if group_id:
                await api_caller.acall('set_group_ban', group_id=group_id, user_id=user_id, duration=0)
                print(f"已解除用户 {user_id} 的禁言")
            else:
                await asyncio.to_thread(fw.db.execute, "UPDATE users SET is_banned=0 WHERE user_id=%s", (user_id,))
                print(f"已解封用户 {user_id}")

        except ValueError:
            print("错误: 参数格式错误")
        except Exception as e:
            print(f"操作失败: {e}")

    async def cmd_kick(args):
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

            await api_caller.acall('set_group_kick', group_id=group_id, user_id=user_id)
            print(f"已踢出用户 {user_id}")

        except ValueError:
            print("错误: group_id/user_id 必须是数字")
        except Exception as e:
            print(f"操作失败: {e}")

    async def cmd_broadcast(args):
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
            rows = await asyncio.to_thread(fw.db.query, "SELECT group_id FROM groups_info WHERE is_active=1")
            group_ids = [row['group_id'] for row in rows]

            from framework.messaging.protocol import ProtocolAdapter
            use_adapter = (
                adapter is not None
                and type(adapter).send_text is not ProtocolAdapter.send_text
            )

            success = 0
            for gid in group_ids:
                sent = False
                if use_adapter:
                    try:
                        result = await adapter.send_text(message, group_id=gid)
                        sent = not (isinstance(result, dict)
                                    and result.get('status') in ('unsupported', 'failed'))
                    except Exception:
                        sent = False
                if not sent and api_caller is not None:
                    try:
                        await api_caller.acall('send_msg', group_id=gid, message=message)
                        sent = True
                    except Exception:
                        sent = False
                if sent:
                    success += 1
            print(f"广播完成: 成功 {success}/{len(group_ids)} 个群")

        except Exception as e:
            print(f"广播失败: {e}")



    # ---- 注册 ----
    terminal_commands.register("send", cmd_send, "发送消息: send <user_id> <消息>")
    terminal_commands.register("recv", cmd_recv, "模拟接收消息: recv <user_id> <消息>")
    terminal_commands.register("ban", cmd_ban, "禁言/封禁: ban <user_id> [分钟]")
    terminal_commands.register("unban", cmd_unban, "解封/解禁: unban <user_id>")
    terminal_commands.register("kick", cmd_kick, "踢出群成员: kick <group_id> <user_id>")
    terminal_commands.register("broadcast", cmd_broadcast, "广播消息: broadcast <消息>")

