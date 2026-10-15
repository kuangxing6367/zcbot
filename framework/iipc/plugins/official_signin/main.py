"""
官方示例插件（逐字照抄 docs/writing-plugins.md「完整的签到插件」一节）

本文件不修改官方示例的一行代码，用于验证 IIPC SDK 对官方文档承诺能力的 1:1 覆盖：
  ctx.create_table / ctx.command(alias=) / ctx.task(cron) / ctx.on(event)
  ctx.db_query_one(%s) / ctx.db_execute(%s) / ctx.send_msg
且 handler 内直接使用裸 ctx（依赖框架把 ctx 注入插件模块全局的既有行为）。
"""
# plugins/sign_in/main.py
import time, random

__plugin_meta__ = {
    "name": "每日签到",
    "version": "1.0.0",
    "author": "你的名字",
    "desc": "每日签到领积分，连续签到有奖励",
    "priority": 50,
}

def register(ctx):
    # 建表（插件会被反复热加载，必须 IF NOT EXISTS）
    ctx.create_table("""
        CREATE TABLE IF NOT EXISTS sign_in_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            day TEXT,
            score INTEGER DEFAULT 0
        )
    """)
    ctx.command("/签到", handle_sign, alias="/sign", description="每日签到")
    ctx.command("/我的积分", handle_score, description="查看我的积分")
    ctx.task("0 0 * * *", reset_daily, description="每日清理过期标记")  # 由 scheduler 插件提供（默认关闭，需启用）
    ctx.on("notice.group_increase", on_new_member)                     # 新成员入群

def handle_sign(event, match):
    today = time.strftime("%Y-%m-%d")
    signed = ctx.db_query_one(
        "SELECT id FROM sign_in_records WHERE user_id=%s AND day=%s",
        (event.user_id, today))
    if signed:
        ctx.send_msg(user_id=event.user_id,
                     group_id=event.group_id if event.is_group else None,
                     message="你今天已经签到过了")
        return
    score = random.randint(1, 10)
    ctx.db_execute(
        "INSERT INTO sign_in_records (user_id, day, score) VALUES (%s,%s,%s)",
        (event.user_id, today, score))
    ctx.send_msg(user_id=event.user_id,
                 group_id=event.group_id if event.is_group else None,
                 message=f"签到成功！获得 {score} 积分")

def handle_score(event, match):
    row = ctx.db_query_one(
        "SELECT COALESCE(SUM(score),0) AS total FROM sign_in_records WHERE user_id=%s",
        (event.user_id,))
    total = row["total"] if row else 0
    ctx.send_msg(user_id=event.user_id,
                 group_id=event.group_id if event.is_group else None,
                 message=f"你当前积分：{total}")

def reset_daily():
    # 定时任务：无 event 参数
    pass

def on_new_member(payload):
    # 事件订阅：payload 是 dict
    user_id = payload.get("user_id")
    if user_id:
        ctx.db_execute(
            "INSERT INTO sign_in_records (user_id, day, score) VALUES (%s,'welcome',100)",
            (user_id,))
