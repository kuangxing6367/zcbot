# -*- coding: utf-8 -*-
"""
终端命令：用户 / 群 / 定时任务查询（users / groups / tasks）
"""

from .command import terminal_commands


def register(fw):
    """注册本组终端命令"""
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



    # ---- 注册 ----
    terminal_commands.register("users", cmd_users, "查看用户列表")
    terminal_commands.register("groups", cmd_groups, "查看群列表")
    terminal_commands.register("tasks", cmd_tasks, "查看定时任务", target="host")

